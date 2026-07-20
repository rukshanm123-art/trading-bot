"""Binance Spot REST adapter (official API: https://developers.binance.com).

Security invariants enforced here:
- (mode, base_url, credential env vars) must agree, or construction fails
  (EndpointMismatchError). Testnet creds can never touch live endpoints.
- No withdrawal capability exists. Live keys are checked via the
  apiRestrictions endpoint and the adapter refuses to run if withdrawals
  are enabled on the key.
- API keys, secrets and signatures never appear in logs or exceptions
  (redaction registers the values; errors carry sanitized text only).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from trading_bot.config import constants as C
from trading_bot.core.enums import EndpointEnvironment, Mode, OrderState, OrderType, Side
from trading_bot.core.models import (
    AssetBalance,
    Candle,
    Fill,
    OrderRequest,
    OrderResponse,
    PriceQuote,
    SymbolRules,
)
from trading_bot.core.types import ZERO, dec
from trading_bot.exchange.errors import (
    EndpointMismatchError,
    ExchangeAuthError,
    ExchangeUnavailable,
    OrderNotFoundError,
    OrderRejectedError,
    OrderStateUnknownError,
)
from trading_bot.exchange.interface import ExchangeAdapter
from trading_bot.exchange.ratelimit import TRACKER as RATE_TRACKER
from trading_bot.security.secrets import SecretProvider

log = logging.getLogger(__name__)

# transport(method, url, headers, params, timeout_s) -> (status_code, parsed_json)
Transport = Callable[[str, str, dict[str, str], dict[str, Any], float], tuple[int, Any]]

_STATUS_MAP: dict[str, OrderState] = {
    "NEW": OrderState.ACKNOWLEDGED,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELLED,
    "PENDING_CANCEL": OrderState.CANCEL_REQUESTED,
    "REJECTED": OrderState.REJECTED,
    "EXPIRED": OrderState.REJECTED,
    "EXPIRED_IN_MATCH": OrderState.REJECTED,
}

# Binance explicitly documents these as unresolved execution outcomes.  They
# must never be treated as ordinary 4xx rejections because a retry could place
# a duplicate order; reconciliation by client order id is the only safe path.
_UNKNOWN_EXECUTION_CODES = {-1000, -1006, -1007}


def _endpoint_url(environment: EndpointEnvironment) -> str:
    if environment == EndpointEnvironment.TESTNET:
        return C.BINANCE_TESTNET_BASE_URL
    if environment in (EndpointEnvironment.LIVE, EndpointEnvironment.LIVE_PUBLIC):
        return C.BINANCE_LIVE_BASE_URL
    raise EndpointMismatchError(f"{environment.value} has no external Binance endpoint")


def _normalize_endpoint(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.netloc or parsed.path).lower().rstrip("/")
    if not scheme and host:
        scheme = "https"
    if parsed.path not in ("", "/") and parsed.netloc:
        raise EndpointMismatchError(f"endpoint URL must not include a path: {url}")
    if parsed.query or parsed.fragment:
        raise EndpointMismatchError(f"endpoint URL must not include query/fragment: {url}")
    return f"{scheme}://{host}".rstrip("/")


def validate_endpoint(environment: EndpointEnvironment, base_url: str | None = None) -> str:
    expected = _endpoint_url(environment)
    resolved = _normalize_endpoint(base_url or expected)
    if resolved != expected:
        raise EndpointMismatchError(
            f"endpoint environment {environment.value} requires {expected}, got {resolved}. "
            "There is no fallback between testnet and live endpoints."
        )
    return resolved


def _requests_transport(
    method: str, url: str, headers: dict[str, str], params: dict[str, Any], timeout_s: float
) -> tuple[int, Any]:
    resp = requests.request(
        method,
        url,
        headers=headers,
        params=params if method == "GET" else None,
        data=params if method != "GET" else None,
        timeout=timeout_s,
    )
    RATE_TRACKER.update_from_headers(resp.headers)
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:200]}
    return resp.status_code, body


def _respect_rate_limit() -> None:
    delay = RATE_TRACKER.suggested_delay()
    if delay > 0:
        log.warning(
            "approaching exchange weight limit (used %s/min); pausing %.1fs",
            RATE_TRACKER.used_weight(),
            delay,
        )
        time.sleep(delay)


def _parse_kline(symbol: str, interval: str, row: list[Any], now: datetime) -> Candle:
    open_time = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
    close_time = datetime.fromtimestamp(int(row[6]) / 1000, tz=UTC)
    return Candle(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=close_time,
        open=dec(str(row[1])),
        high=dec(str(row[2])),
        low=dec(str(row[3])),
        close=dec(str(row[4])),
        volume=dec(str(row[5])),
        is_closed=close_time <= now,
    )


def _parse_rules(info: dict[str, Any], symbol: str) -> SymbolRules:
    symbols = info.get("symbols", [])
    entry = next((s for s in symbols if s.get("symbol") == symbol), None)
    if entry is None:
        raise OrderRejectedError(f"Symbol {symbol} not found in exchangeInfo")
    min_qty = step = tick = min_notional = ZERO
    market_min_qty = market_step_size = max_notional = ZERO
    max_qty = market_max_qty = dec("9000000")
    for f in entry.get("filters", []):
        ftype = f.get("filterType")
        if ftype == "LOT_SIZE":
            min_qty = dec(str(f["minQty"]))
            step = dec(str(f["stepSize"]))
            if f.get("maxQty") is not None:
                max_qty = dec(str(f["maxQty"]))
        elif ftype == "MARKET_LOT_SIZE":
            if f.get("minQty") is not None:
                market_min_qty = dec(str(f["minQty"]))
            if f.get("stepSize") is not None:
                market_step_size = dec(str(f["stepSize"]))
            if f.get("maxQty") is not None:
                market_max_qty = dec(str(f["maxQty"]))
        elif ftype == "PRICE_FILTER":
            tick = dec(str(f["tickSize"]))
        elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
            parsed_min = dec(str(f.get("minNotional", f.get("notional", "0"))))
            min_notional = max(min_notional, parsed_min)
            if ftype == "NOTIONAL" and f.get("maxNotional") is not None:
                parsed_max = dec(str(f["maxNotional"]))
                if parsed_max > ZERO:
                    max_notional = (
                        parsed_max if max_notional == ZERO else min(max_notional, parsed_max)
                    )
    return SymbolRules(
        symbol=symbol,
        base_asset=entry["baseAsset"],
        quote_asset=entry["quoteAsset"],
        status=entry.get("status", "UNKNOWN"),
        min_qty=min_qty,
        step_size=step,
        tick_size=tick,
        min_notional=min_notional,
        order_types=tuple(entry.get("orderTypes", ["MARKET", "LIMIT"])),
        max_qty=max_qty,
        market_min_qty=market_min_qty,
        market_step_size=market_step_size,
        market_max_qty=market_max_qty,
        max_notional=max_notional,
    )


class BinancePublicData:
    """Unsigned market-data client (klines, book ticker, exchangeInfo).

    Used by PAPER mode for real prices without any credentials.
    """

    def __init__(
        self,
        environment: EndpointEnvironment = EndpointEnvironment.LIVE_PUBLIC,
        base_url: str | None = None,
        transport: Transport | None = None,
        on_api_error: Callable[[str, str], None] | None = None,
    ) -> None:
        if environment == EndpointEnvironment.FIXTURE:
            raise EndpointMismatchError("fixture data must not construct a Binance public client")
        self.environment = environment
        self.base_url = validate_endpoint(environment, base_url)
        self._transport = transport or _requests_transport
        self._on_api_error = on_api_error

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last_error = "unknown"
        for attempt in range(C.MAX_RETRIES):
            _respect_rate_limit()
            try:
                status, body = self._transport("GET", url, {}, params or {}, C.HTTP_TIMEOUT_S)
            except requests.RequestException as exc:
                last_error = type(exc).__name__
                status, body = 0, None
            if status == 200:
                return body
            last_error = f"http_{status}"
            if status in (418, 429) or status >= 500 or status == 0:
                time.sleep(min(C.RETRY_BACKOFF_BASE_S * (2**attempt), C.RETRY_BACKOFF_MAX_S))
                continue
            break
        if self._on_api_error:
            self._on_api_error(path, last_error)
        raise ExchangeUnavailable(f"GET {path} failed ({last_error})")

    def server_time(self) -> datetime:
        body = self._get("/api/v3/time")
        return datetime.fromtimestamp(int(body["serverTime"]) / 1000, tz=UTC)

    def get_rules(self, symbol: str) -> SymbolRules:
        info = self._get("/api/v3/exchangeInfo", {"symbol": symbol})
        if isinstance(info, dict):
            RATE_TRACKER.update_exchange_limits(info.get("rateLimits", []))
        return _parse_rules(info, symbol)

    def get_price(self, symbol: str) -> PriceQuote:
        book = self._get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        bid = dec(str(book["bidPrice"]))
        ask = dec(str(book["askPrice"]))
        return PriceQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=(bid + ask) / 2,
            ts=datetime.now(UTC),
        )

    def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        rows = self._get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        now = datetime.now(UTC)
        return [_parse_kline(symbol, interval, r, now) for r in rows]


class BinanceAdapter(ExchangeAdapter):
    """Signed Spot adapter for TESTNET and LIVE modes only.

    PAPER mode never constructs this class — it uses PaperExchange +
    BinancePublicData.
    """

    def __init__(
        self,
        mode: Mode,
        secrets: SecretProvider,
        base_url: str | None = None,
        transport: Transport | None = None,
        recv_window_ms: int = C.DEFAULT_RECV_WINDOW_MS,
        on_api_error: Callable[[str, str], None] | None = None,
    ) -> None:
        if mode == Mode.PAPER:
            raise EndpointMismatchError("PAPER mode must not construct a signed exchange adapter")
        environment = (
            EndpointEnvironment.TESTNET if mode == Mode.TESTNET else EndpointEnvironment.LIVE
        )
        resolved = validate_endpoint(environment, base_url)
        key_var, secret_var = (
            (C.ENV_TESTNET_KEY, C.ENV_TESTNET_SECRET)
            if mode == Mode.TESTNET
            else (C.ENV_LIVE_KEY, C.ENV_LIVE_SECRET)
        )
        api_key = secrets.get(key_var)
        api_secret = secrets.get(secret_var)
        if not api_key or not api_secret:
            raise ExchangeAuthError(
                f"Missing credentials: set {key_var} and {secret_var} in the environment"
            )
        # Cross-contamination guard: the OTHER mode's variables must not be
        # the source of these values (copy-paste .env mistakes).
        other_key = secrets.get(C.ENV_LIVE_KEY if mode == Mode.TESTNET else C.ENV_TESTNET_KEY)
        if other_key and other_key == api_key:
            raise EndpointMismatchError(
                "The same API key is set for both testnet and live variables; refusing to start."
            )

        self.kind = mode.value
        self.mode = mode
        self.base_url = resolved
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._transport = transport or _requests_transport
        self._recv_window_ms = recv_window_ms
        self._time_offset_ms = 0
        self._on_api_error = on_api_error
        self.public = BinancePublicData(environment, resolved, self._transport, on_api_error)

    # ------------------------------------------------------------------
    def sync_clock(self) -> int:
        """Measure server-local clock offset; returns drift in ms."""
        local_before = time.time() * 1000
        server = self.public.server_time().timestamp() * 1000
        local_after = time.time() * 1000
        midpoint = (local_before + local_after) / 2
        self._time_offset_ms = int(server - midpoint)
        if abs(self._time_offset_ms) > C.MAX_CLOCK_DRIFT_MS:
            log.warning(
                "clock drift %sms exceeds %sms; compensating with server offset",
                self._time_offset_ms,
                C.MAX_CLOCK_DRIFT_MS,
            )
        return self._time_offset_ms

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    def _signed_request(
        self, method: str, path: str, params: dict[str, Any], is_order: bool = False
    ) -> Any:
        params = dict(params)
        params["timestamp"] = self._timestamp_ms()
        params["recvWindow"] = self._recv_window_ms
        query = urlencode(params)
        signature = hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        params["signature"] = signature
        headers = {"X-MBX-APIKEY": self._api_key}
        url = f"{self.base_url}{path}"

        attempts = 1 if is_order else C.MAX_RETRIES
        last_error = "unknown"
        for attempt in range(attempts):
            if not is_order:
                # never delay an in-flight order decision; weight for orders
                # is tiny and certainty beats throttling there
                _respect_rate_limit()
            try:
                status, body = self._transport(method, url, headers, params, C.HTTP_TIMEOUT_S)
            except requests.RequestException as exc:
                if is_order:
                    # The order may or may not have reached the exchange.
                    raise OrderStateUnknownError(
                        f"{method} {path}: transport failure mid-submission "
                        f"({type(exc).__name__}); state unknown — reconcile before retrying"
                    ) from exc
                last_error = type(exc).__name__
                time.sleep(min(C.RETRY_BACKOFF_BASE_S * (2**attempt), C.RETRY_BACKOFF_MAX_S))
                continue
            if status == 200:
                return body
            code = body.get("code") if isinstance(body, dict) else None
            msg = str(body.get("msg", ""))[:200] if isinstance(body, dict) else ""
            if code == -2013:
                raise OrderNotFoundError(f"{method} {path}: order not found (code=-2013)", code)
            if status in (401, 403) or code in (-2014, -2015, -1022):
                raise ExchangeAuthError(f"{method} {path}: auth failure (code={code})")
            if is_order and code in _UNKNOWN_EXECUTION_CODES:
                raise OrderStateUnknownError(
                    f"{method} {path}: exchange reported unresolved execution "
                    f"status (code={code}); reconcile by client order id before retrying"
                )
            if status == 400 and is_order:
                raise OrderRejectedError(f"order rejected: code={code} {msg}")
            last_error = f"http_{status} code={code}"
            if status in (418, 429) or status >= 500:
                if is_order:
                    raise OrderStateUnknownError(
                        f"{method} {path}: {last_error} on order submission; "
                        "state unknown — reconcile before retrying"
                    )
                time.sleep(min(C.RETRY_BACKOFF_BASE_S * (2**attempt), C.RETRY_BACKOFF_MAX_S))
                continue
            if is_order:
                # ANY status not definitively classified above may still mean
                # the exchange accepted the order. Never assume "not placed":
                # mark UNKNOWN and reconcile by client order id.
                raise OrderStateUnknownError(
                    f"{method} {path}: unexpected {last_error} on order submission; "
                    "state unknown — reconcile before retrying"
                )
            break
        if self._on_api_error:
            self._on_api_error(path, last_error)
        if is_order:
            raise OrderStateUnknownError(
                f"{method} {path}: submission outcome unresolved ({last_error}); "
                "state unknown — reconcile before retrying"
            )
        raise ExchangeUnavailable(f"{method} {path} failed ({last_error})")

    # ------------------------------------------------------------------
    def server_time(self) -> datetime:
        return self.public.server_time()

    def get_rules(self, symbol: str) -> SymbolRules:
        return self.public.get_rules(symbol)

    def get_price(self, symbol: str) -> PriceQuote:
        return self.public.get_price(symbol)

    def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        return self.public.get_candles(symbol, interval, limit)

    def get_balances(self) -> dict[str, AssetBalance]:
        body = self._signed_request("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        out: dict[str, AssetBalance] = {}
        for b in body.get("balances", []):
            free = dec(str(b["free"]))
            locked = dec(str(b["locked"]))
            if free > ZERO or locked > ZERO:
                out[b["asset"]] = AssetBalance(asset=b["asset"], free=free, locked=locked)
        return out

    def create_order(self, request: OrderRequest) -> OrderResponse:
        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": request.order_type.value,
            "quantity": str(request.qty),
            "newClientOrderId": request.client_order_id,
            "newOrderRespType": "FULL",
        }
        if request.order_type == OrderType.LIMIT:
            if request.price is None:
                raise OrderRejectedError("LIMIT order requires a price")
            params["price"] = str(request.price)
            params["timeInForce"] = "GTC"
        elif request.order_type == OrderType.STOP_LOSS_LIMIT:
            if request.price is None or request.stop_price is None:
                raise OrderRejectedError("STOP_LOSS_LIMIT requires price and stopPrice")
            if request.side != Side.SELL:
                raise OrderRejectedError("protective stops are SELL-only in this system")
            params["price"] = str(request.price)
            params["stopPrice"] = str(request.stop_price)
            params["timeInForce"] = "GTC"
        body = self._signed_request("POST", "/api/v3/order", params, is_order=True)
        return self._to_response(body, request.symbol)

    def query_order(self, symbol: str, client_order_id: str) -> OrderResponse | None:
        try:
            body = self._signed_request(
                "GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id}
            )
        except OrderNotFoundError:
            return None
        except OrderRejectedError:
            return None
        except ExchangeUnavailable:
            raise
        if isinstance(body, dict) and body.get("code") == -2013:  # order does not exist
            return None
        response = self._to_response(body, symbol)
        # Order queries report cumulative totals but usually NOT the fills
        # array — without it, commissions (possibly charged in the base
        # asset) are invisible and the sellable quantity gets overstated.
        # Recover authoritative fills (with stable trade ids) from myTrades.
        if response.executed_qty > ZERO and not response.fills:
            fills = self._order_trades(symbol, response.exchange_order_id)
            if fills:
                import dataclasses

                response = dataclasses.replace(response, fills=fills)
            else:
                log.warning(
                    "order %s executed %s but fills are unrecoverable; downstream "
                    "accounting will assume worst-case fees",
                    client_order_id,
                    response.executed_qty,
                )
        return response

    def _order_trades(self, symbol: str, exchange_order_id: str) -> tuple[Fill, ...]:
        """Authoritative per-trade fills for one order (stable trade ids)."""
        if not exchange_order_id:
            return ()
        try:
            body = self._signed_request(
                "GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": exchange_order_id}
            )
        except (ExchangeUnavailable, ExchangeAuthError, OrderRejectedError) as exc:
            log.warning("myTrades lookup failed for order %s: %s", exchange_order_id, exc)
            return ()
        if not isinstance(body, list):
            return ()
        return tuple(
            Fill(
                price=dec(str(t["price"])),
                qty=dec(str(t["qty"])),
                fee=dec(str(t.get("commission", "0"))),
                fee_asset=str(t.get("commissionAsset", "")),
                trade_id=str(t.get("id", "")),
            )
            for t in body
        )

    def cancel_order(self, symbol: str, client_order_id: str) -> OrderResponse:
        body = self._signed_request(
            "DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id}
        )
        return self._to_response(body, symbol)

    # ------------------------------------------------------------------
    def taker_fee_bps(self, symbol: str) -> Decimal | None:
        """The account's actual taker commission for ``symbol`` in basis
        points, or None when the endpoint is unavailable (caller keeps its
        conservative default)."""
        try:
            body = self._signed_request("GET", "/api/v3/account/commission", {"symbol": symbol})
        except (ExchangeUnavailable, ExchangeAuthError, OrderRejectedError) as exc:
            log.warning("account commission unavailable (%s); using default fee", exc)
            return None
        rates = body.get("standardCommission") if isinstance(body, dict) else None
        taker = rates.get("taker") if isinstance(rates, dict) else None
        if taker is None:
            return None
        try:
            return dec(str(taker)) * 10000  # fraction (e.g. "0.001") -> bps
        except Exception:
            log.warning("unparseable taker commission %r; using default fee", taker)
            return None

    # ------------------------------------------------------------------
    def api_restrictions(self) -> dict[str, Any] | None:
        """Key permission flags. Live only — testnet does not expose sapi."""
        if self.mode != Mode.LIVE:
            return None
        return self._signed_request("GET", "/sapi/v1/account/apiRestrictions", {})

    def verify_key_permissions(self) -> None:
        """Refuse to run live if the key can withdraw or cannot spot-trade."""
        restrictions = self.api_restrictions()
        if restrictions is None:
            return
        if restrictions.get("enableWithdrawals", False):
            raise ExchangeAuthError(
                "SAFETY STOP: this API key has WITHDRAWALS ENABLED. "
                "Create a key with withdrawals disabled (docs/API_KEY_SETUP.md)."
            )
        if not restrictions.get("enableSpotAndMarginTrading", False):
            raise ExchangeAuthError("API key lacks spot trading permission")

    # ------------------------------------------------------------------
    def _to_response(self, body: dict[str, Any], symbol: str) -> OrderResponse:
        raw_status = str(body.get("status", "NEW"))
        state = _STATUS_MAP.get(raw_status, OrderState.UNKNOWN)
        fills = tuple(
            Fill(
                price=dec(str(f["price"])),
                qty=dec(str(f["qty"])),
                fee=dec(str(f.get("commission", "0"))),
                fee_asset=str(f.get("commissionAsset", "")),
            )
            for f in body.get("fills", [])
        )
        executed = dec(str(body.get("executedQty", "0")))
        cum_quote = dec(str(body.get("cummulativeQuoteQty", "0")))
        ts_ms = body.get("transactTime") or body.get("updateTime") or body.get("time")
        ts = datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC) if ts_ms else datetime.now(UTC)
        side = Side(str(body.get("side", "BUY")))
        order_type = OrderType(str(body.get("type", "MARKET")))
        return OrderResponse(
            client_order_id=str(body.get("clientOrderId", "")),
            exchange_order_id=str(body.get("orderId", "")),
            symbol=symbol,
            side=side,
            order_type=order_type,
            state=state,
            requested_qty=dec(str(body.get("origQty", "0"))),
            executed_qty=executed,
            cumulative_quote=cum_quote,
            fills=fills,
            ts=ts,
            raw_status=raw_status,
        )


def estimate_equity(
    balances: dict[str, AssetBalance], base_asset: str, quote_asset: str, price: Decimal
) -> Decimal:
    """Total equity in quote terms: quote balance + base balance * price."""
    equity = ZERO
    for asset, bal in balances.items():
        if asset == quote_asset:
            equity += bal.total
        elif asset == base_asset:
            equity += bal.total * price
    return equity
