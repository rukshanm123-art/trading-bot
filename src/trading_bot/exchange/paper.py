"""Paper-trading exchange simulator.

Implements the same ExchangeAdapter interface as the real adapter, with:
- real (or fixture) market prices via an injected MarketDataSource
- taker fees, bid/ask spread, bounded random slippage
- random order rejections and partial fills
- exchange filter enforcement (LOT_SIZE / NOTIONAL / status), like Binance would
- balances and open orders persisted to the database (restart-safe)
- full determinism for a given seed: randomness is derived per client order id,
  so replays and restarts do not change past outcomes

Fee convention mirrors Binance spot: BUY pays fee in base asset (you receive
qty - fee), SELL pays fee in quote asset.
"""

from __future__ import annotations

import hashlib
import logging
import random
from datetime import timedelta
from decimal import Decimal
from typing import Any

from trading_bot.config.models import PaperSimConfig
from trading_bot.core.enums import OrderState, OrderType, Side
from trading_bot.core.models import (
    AssetBalance,
    Candle,
    Fill,
    OrderRequest,
    OrderResponse,
    PriceQuote,
    SymbolRules,
    iso,
    parse_iso,
)
from trading_bot.core.types import BPS_DENOM, ZERO, dec, quantize_down
from trading_bot.exchange.errors import OrderRejectedError
from trading_bot.exchange.interface import Clock, ExchangeAdapter, MarketDataSource
from trading_bot.storage.repositories import SimStateRepo

log = logging.getLogger(__name__)

_BALANCES_KEY = "paper_balances"
_ORDERS_KEY = "paper_orders"


class PaperExchange(ExchangeAdapter):
    kind = "paper"

    def __init__(
        self,
        rules: SymbolRules,
        sim_cfg: PaperSimConfig,
        data_source: MarketDataSource,
        state_repo: SimStateRepo,
        clock: Clock,
    ) -> None:
        self.rules = rules
        self.cfg = sim_cfg
        self.data = data_source
        self.state = state_repo
        self.clock = clock
        self._ensure_initial_balances()

    # ------------------------------------------------------------- state
    def _ensure_initial_balances(self) -> None:
        if self.state.get(_BALANCES_KEY) is None:
            self.state.set(
                _BALANCES_KEY,
                {
                    self.rules.quote_asset: {"free": str(self.cfg.starting_quote), "locked": "0"},
                    self.rules.base_asset: {"free": "0", "locked": "0"},
                },
            )
            log.info(
                "paper account initialised with %s %s",
                self.cfg.starting_quote,
                self.rules.quote_asset,
            )

    def _load_balances(self) -> dict[str, dict[str, Decimal]]:
        raw = self.state.get(_BALANCES_KEY) or {}
        return {
            asset: {"free": dec(v["free"]), "locked": dec(v["locked"])} for asset, v in raw.items()
        }

    def _save_balances(self, balances: dict[str, dict[str, Decimal]]) -> None:
        self.state.set(
            _BALANCES_KEY,
            {a: {"free": str(v["free"]), "locked": str(v["locked"])} for a, v in balances.items()},
        )

    def _load_orders(self) -> dict[str, dict]:
        return self.state.get(_ORDERS_KEY) or {}

    def _save_orders(self, orders: dict[str, dict]) -> None:
        self.state.set(_ORDERS_KEY, orders)

    def _rng(self, request: OrderRequest, order_index: int) -> random.Random:
        """Deterministic randomness per order: derived from the configured
        seed, the persisted order sequence number and the order parameters —
        NOT from the (random) client order id — so an identical replay
        reproduces identical fills, and restarts continue the sequence."""
        material = (
            f"{self.cfg.seed}:{order_index}:{request.symbol}:{request.side.value}:"
            f"{request.qty}:{request.order_type.value}"
        ).encode()
        seed = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        return random.Random(seed)

    # ---------------------------------------------------------- adapter
    def server_time(self):
        return self.clock.now()

    def get_rules(self, symbol: str) -> SymbolRules:
        if symbol != self.rules.symbol:
            raise OrderRejectedError(f"unknown symbol {symbol}")
        return self.rules

    def get_balances(self) -> dict[str, AssetBalance]:
        return {
            asset: AssetBalance(asset=asset, free=v["free"], locked=v["locked"])
            for asset, v in self._load_balances().items()
        }

    def get_price(self, symbol: str) -> PriceQuote:
        return self.data.get_price(symbol)

    def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        return self.data.get_candles(symbol, interval, limit)

    # ------------------------------------------------------------ fills
    def _slippage_bps(self, rng: random.Random) -> Decimal:
        max_hundredths = int(self.cfg.slippage_bps_max * 100)
        return dec(rng.randint(0, max_hundredths)) / 100 if max_hundredths > 0 else ZERO

    def _fill_price(self, side: Side, quote: PriceQuote, rng: random.Random) -> Decimal:
        half_spread = self.cfg.spread_bps / 2
        slip = self._slippage_bps(rng)
        adjust_bps = half_spread + slip
        if side == Side.BUY:
            price = quote.mid * (BPS_DENOM + adjust_bps) / BPS_DENOM
        else:
            price = quote.mid * (BPS_DENOM - adjust_bps) / BPS_DENOM
        if self.rules.tick_size > ZERO:
            price = quantize_down(price, self.rules.tick_size)
        return price

    def _validate_filters(self, request: OrderRequest, ref_price: Decimal) -> str | None:
        r = self.rules
        if not r.is_trading:
            return f"symbol status {r.status}"
        if request.order_type.value not in r.order_types:
            return f"order type {request.order_type.value} not supported"
        if request.order_type == OrderType.STOP_LOSS_LIMIT:
            if request.price is None or request.stop_price is None:
                return "STOP_LOSS_LIMIT requires price and stopPrice"
            if request.side != Side.SELL:
                return "protective stops are SELL-only in this system"
        quantity_min = r.quantity_min(request.order_type)
        quantity_max = r.quantity_max(request.order_type)
        quantity_step = r.quantity_step(request.order_type)
        if request.qty < quantity_min:
            return f"qty {request.qty} < effective minQty {quantity_min}"
        if request.qty > quantity_max:
            return f"qty {request.qty} > effective maxQty {quantity_max}"
        if quantity_step > ZERO and (request.qty % quantity_step) != ZERO:
            return f"qty {request.qty} violates effective stepSize {quantity_step}"
        notional = request.qty * ref_price
        if notional < r.min_notional:
            return f"notional {notional} < minNotional {r.min_notional}"
        if r.max_notional > ZERO and notional > r.max_notional:
            return f"notional {notional} > maxNotional {r.max_notional}"
        return None

    def create_order(self, request: OrderRequest) -> OrderResponse:
        orders = self._load_orders()
        if request.client_order_id in orders:
            # Idempotency: duplicate client order id returns the existing order.
            return self._response_from_stored(orders[request.client_order_id])

        quote = self.get_price(request.symbol)
        rng = self._rng(request, order_index=len(orders))
        now = self.clock.now() + timedelta(milliseconds=self.cfg.latency_ms)

        reason = self._validate_filters(request, quote.mid)
        if reason is None and rng.random() < float(self.cfg.reject_probability):
            reason = "simulated exchange rejection"

        if reason is not None:
            stored: dict[str, Any] = {
                "request": self._request_dict(request),
                "state": OrderState.REJECTED.value,
                "raw_status": "REJECTED",
                "executed_qty": "0",
                "cumulative_quote": "0",
                "fills": [],
                "ts": iso(now),
                "reject_reason": reason,
            }
            orders[request.client_order_id] = stored
            self._save_orders(orders)
            log.info("paper order %s rejected: %s", request.client_order_id, reason)
            return self._response_from_stored(stored)

        # STOP_LOSS_LIMIT (protective sell): Binance rejects a stop that would
        # trigger immediately; otherwise the order rests until triggered.
        if request.order_type == OrderType.STOP_LOSS_LIMIT:
            assert request.stop_price is not None  # validated above
            if quote.bid <= request.stop_price:
                stored = {
                    "request": self._request_dict(request),
                    "state": OrderState.REJECTED.value,
                    "raw_status": "REJECTED",
                    "executed_qty": "0",
                    "cumulative_quote": "0",
                    "fills": [],
                    "ts": iso(now),
                    "reject_reason": "stop price would trigger immediately",
                }
                orders[request.client_order_id] = stored
                self._save_orders(orders)
                return self._response_from_stored(stored)
            self._lock_for_order(request, quote.mid)
            stored = {
                "request": self._request_dict(request),
                "state": OrderState.ACKNOWLEDGED.value,
                "raw_status": "NEW",
                "executed_qty": "0",
                "cumulative_quote": "0",
                "fills": [],
                "ts": iso(now),
                "triggered": False,
            }
            orders[request.client_order_id] = stored
            self._save_orders(orders)
            return self._response_from_stored(stored)

        fill_price = self._fill_price(request.side, quote, rng)

        # LIMIT orders: rest if not marketable at creation time.
        if request.order_type == OrderType.LIMIT and request.price is not None:
            marketable = (request.side == Side.BUY and request.price >= quote.ask) or (
                request.side == Side.SELL and request.price <= quote.bid
            )
            if not marketable:
                self._lock_for_order(request, quote.mid)
                stored = {
                    "request": self._request_dict(request),
                    "state": OrderState.ACKNOWLEDGED.value,
                    "raw_status": "NEW",
                    "executed_qty": "0",
                    "cumulative_quote": "0",
                    "fills": [],
                    "ts": iso(now),
                }
                orders[request.client_order_id] = stored
                self._save_orders(orders)
                return self._response_from_stored(stored)
            fill_price = (
                min(fill_price, request.price)
                if request.side == Side.BUY
                else max(fill_price, request.price)
            )

        # Partial fill simulation (completes on a later query_order).
        exec_qty = request.qty
        partial = rng.random() < float(self.cfg.partial_fill_probability)
        if partial:
            fraction = dec("0.4")
            step = self.rules.quantity_step(request.order_type)
            candidate = quantize_down(request.qty * fraction, step)
            remainder = request.qty - candidate
            if candidate >= self.rules.quantity_min(request.order_type) and remainder >= step:
                exec_qty = candidate

        try:
            fills = self._settle(request, exec_qty, fill_price)
        except OrderRejectedError as exc:
            stored = {
                "request": self._request_dict(request),
                "state": OrderState.REJECTED.value,
                "raw_status": "REJECTED",
                "executed_qty": "0",
                "cumulative_quote": "0",
                "fills": [],
                "ts": iso(now),
                "reject_reason": str(exc),
            }
            orders[request.client_order_id] = stored
            self._save_orders(orders)
            return self._response_from_stored(stored)

        fully_filled = exec_qty == request.qty
        stored = {
            "request": self._request_dict(request),
            "state": (OrderState.FILLED if fully_filled else OrderState.PARTIALLY_FILLED).value,
            "raw_status": "FILLED" if fully_filled else "PARTIALLY_FILLED",
            "executed_qty": str(exec_qty),
            "cumulative_quote": str(exec_qty * fill_price),
            "fills": [
                {
                    "price": str(f.price),
                    "qty": str(f.qty),
                    "fee": str(f.fee),
                    "fee_asset": f.fee_asset,
                }
                for f in fills
            ],
            "ts": iso(now),
            "pending_qty": str(request.qty - exec_qty),
            "pending_price": str(fill_price),
            "partial_query_fills": "0",
        }
        orders[request.client_order_id] = stored
        self._save_orders(orders)
        return self._response_from_stored(stored)

    def _lock_for_order(self, request: OrderRequest, ref_price: Decimal) -> None:
        balances = self._load_balances()
        if request.side == Side.BUY:
            price = request.price if request.price is not None else ref_price
            need = request.qty * price
            asset = self.rules.quote_asset
        else:
            need = request.qty
            asset = self.rules.base_asset
        if balances[asset]["free"] < need:
            raise OrderRejectedError(f"insufficient {asset} balance")
        balances[asset]["free"] -= need
        balances[asset]["locked"] += need
        self._save_balances(balances)

    def _settle(self, request: OrderRequest, qty: Decimal, price: Decimal) -> list[Fill]:
        """Move balances for an execution of ``qty`` at ``price``."""
        balances = self._load_balances()
        base = self.rules.base_asset
        quote_asset = self.rules.quote_asset
        cost = qty * price
        fee_rate = self.cfg.taker_fee_bps / BPS_DENOM
        if request.side == Side.BUY:
            if balances[quote_asset]["free"] < cost:
                raise OrderRejectedError(
                    f"insufficient {quote_asset} balance ({balances[quote_asset]['free']} < {cost})"
                )
            fee = qty * fee_rate  # fee in base asset, like Binance spot taker on BUY
            balances[quote_asset]["free"] -= cost
            balances[base]["free"] += qty - fee
            fill = Fill(price=price, qty=qty, fee=fee, fee_asset=base)
        else:
            if balances[base]["free"] < qty:
                raise OrderRejectedError(
                    f"insufficient {base} balance ({balances[base]['free']} < {qty})"
                )
            fee = cost * fee_rate  # fee in quote asset on SELL
            balances[base]["free"] -= qty
            balances[quote_asset]["free"] += cost - fee
            fill = Fill(price=price, qty=qty, fee=fee, fee_asset=quote_asset)
        self._save_balances(balances)
        return [fill]

    # ---------------------------------------------------------- queries
    def query_order(self, symbol: str, client_order_id: str) -> OrderResponse | None:
        orders = self._load_orders()
        stored = orders.get(client_order_id)
        if stored is None:
            return None

        # Progress simulated partial fills on query. The first query fills
        # another 30% of requested quantity and leaves the remaining 30%
        # cancelable; a later query completes the order.
        if stored["state"] == OrderState.PARTIALLY_FILLED.value:
            pending = dec(stored.get("pending_qty", "0"))
            price = dec(stored.get("pending_price", "0"))
            if pending > ZERO and price > ZERO:
                request = self._request_from_dict(stored["request"])
                n_queries = int(stored.get("partial_query_fills", "0"))
                fill_qty = pending
                if n_queries == 0:
                    step = self.rules.quantity_step(request.order_type)
                    fill_qty = min(pending, quantize_down(request.qty * dec("0.3"), step))
                fills = self._settle(request, fill_qty, price)
                stored["executed_qty"] = str(dec(stored["executed_qty"]) + fill_qty)
                stored["cumulative_quote"] = str(dec(stored["cumulative_quote"]) + fill_qty * price)
                stored["fills"].extend(
                    {
                        "price": str(f.price),
                        "qty": str(f.qty),
                        "fee": str(f.fee),
                        "fee_asset": f.fee_asset,
                    }
                    for f in fills
                )
                remaining = pending - fill_qty
                stored["pending_qty"] = str(remaining)
                stored["partial_query_fills"] = str(n_queries + 1)
                if remaining <= ZERO:
                    stored["state"] = OrderState.FILLED.value
                    stored["raw_status"] = "FILLED"
                orders[client_order_id] = stored
                self._save_orders(orders)

        # Resting STOP_LOSS_LIMIT: trigger when bid touches the stop; after
        # triggering it becomes a limit sell that fills only while the market
        # still trades at or above the limit price. A gap straight through
        # the limit leaves it TRIGGERED-BUT-UNFILLED — exactly the real-world
        # failure mode the engine's software monitor escalates on.
        elif stored["state"] == OrderState.ACKNOWLEDGED.value and (
            stored["request"].get("order_type") == OrderType.STOP_LOSS_LIMIT.value
        ):
            request = self._request_from_dict(stored["request"])
            quote = self.get_price(symbol)
            assert request.stop_price is not None and request.price is not None
            if not stored.get("triggered") and quote.bid <= request.stop_price:
                stored["triggered"] = True
            if stored.get("triggered") and quote.bid >= request.price:
                self._unlock_for_order(request)
                fills = self._settle(request, request.qty, request.price)
                stored["executed_qty"] = str(request.qty)
                stored["cumulative_quote"] = str(request.qty * request.price)
                stored["fills"] = [
                    {
                        "price": str(f.price),
                        "qty": str(f.qty),
                        "fee": str(f.fee),
                        "fee_asset": f.fee_asset,
                    }
                    for f in fills
                ]
                stored["state"] = OrderState.FILLED.value
                stored["raw_status"] = "FILLED"
            orders[client_order_id] = stored
            self._save_orders(orders)

        # Resting LIMIT orders: check if now crossed.
        elif stored["state"] == OrderState.ACKNOWLEDGED.value:
            request = self._request_from_dict(stored["request"])
            if request.order_type == OrderType.LIMIT and request.price is not None:
                quote = self.get_price(symbol)
                crossed = (request.side == Side.BUY and quote.ask <= request.price) or (
                    request.side == Side.SELL and quote.bid >= request.price
                )
                if crossed:
                    self._unlock_for_order(request)
                    fills = self._settle(request, request.qty, request.price)
                    stored["executed_qty"] = str(request.qty)
                    stored["cumulative_quote"] = str(request.qty * request.price)
                    stored["fills"] = [
                        {
                            "price": str(f.price),
                            "qty": str(f.qty),
                            "fee": str(f.fee),
                            "fee_asset": f.fee_asset,
                        }
                        for f in fills
                    ]
                    stored["state"] = OrderState.FILLED.value
                    stored["raw_status"] = "FILLED"
                    orders[client_order_id] = stored
                    self._save_orders(orders)

        return self._response_from_stored(stored)

    def _unlock_for_order(self, request: OrderRequest) -> None:
        balances = self._load_balances()
        if request.side == Side.BUY and request.price is not None:
            asset, amount = self.rules.quote_asset, request.qty * request.price
        else:
            asset, amount = self.rules.base_asset, request.qty
        balances[asset]["locked"] -= amount
        balances[asset]["free"] += amount
        self._save_balances(balances)

    def cancel_order(self, symbol: str, client_order_id: str) -> OrderResponse:
        orders = self._load_orders()
        stored = orders.get(client_order_id)
        if stored is None:
            raise OrderRejectedError(f"unknown order {client_order_id}")
        if stored["state"] == OrderState.ACKNOWLEDGED.value:
            request = self._request_from_dict(stored["request"])
            self._unlock_for_order(request)
            stored["state"] = OrderState.CANCELLED.value
            stored["raw_status"] = "CANCELED"
            orders[client_order_id] = stored
            self._save_orders(orders)
        elif stored["state"] == OrderState.PARTIALLY_FILLED.value:
            stored["pending_qty"] = "0"
            stored["state"] = OrderState.CANCELLED.value
            stored["raw_status"] = "CANCELED"
            orders[client_order_id] = stored
            self._save_orders(orders)
        return self._response_from_stored(stored)

    # ------------------------------------------------------------ util
    @staticmethod
    def _request_dict(request: OrderRequest) -> dict:
        return {
            "symbol": request.symbol,
            "side": request.side.value,
            "order_type": request.order_type.value,
            "qty": str(request.qty),
            "price": str(request.price) if request.price is not None else None,
            "stop_price": str(request.stop_price) if request.stop_price is not None else None,
            "client_order_id": request.client_order_id,
        }

    @staticmethod
    def _request_from_dict(d: dict) -> OrderRequest:
        return OrderRequest(
            symbol=d["symbol"],
            side=Side(d["side"]),
            order_type=OrderType(d["order_type"]),
            qty=dec(d["qty"]),
            price=dec(d["price"]) if d.get("price") else None,
            stop_price=dec(d["stop_price"]) if d.get("stop_price") else None,
            client_order_id=d["client_order_id"],
        )

    def _response_from_stored(self, stored: dict) -> OrderResponse:
        request = self._request_from_dict(stored["request"])
        return OrderResponse(
            client_order_id=request.client_order_id,
            exchange_order_id=f"paper-{request.client_order_id[:12]}",
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            state=OrderState(stored["state"]),
            requested_qty=request.qty,
            executed_qty=dec(stored["executed_qty"]),
            cumulative_quote=dec(stored["cumulative_quote"]),
            fills=tuple(
                Fill(
                    price=dec(f["price"]),
                    qty=dec(f["qty"]),
                    fee=dec(f["fee"]),
                    fee_asset=f["fee_asset"],
                )
                for f in stored.get("fills", [])
            ),
            ts=parse_iso(stored["ts"]),
            raw_status=stored.get("raw_status", stored["state"]),
        )
