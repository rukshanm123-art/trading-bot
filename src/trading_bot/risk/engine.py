"""The risk engine — final authority over every order.

Design:
- ``evaluate_entry`` / ``evaluate_exit`` are pure over their context inputs
  and return a RiskDecision with structured reason codes.
- An approved decision carries a single-use, HMAC-signed approval token bound
  to the exact order parameters. The execution gateway refuses any order
  without a valid token, so a rejected (or absent) risk evaluation can never
  reach order submission — structurally, not by convention.
- Every hard limit here has a hard cap in config/constants.py that config
  cannot loosen.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_bot.config.models import AppConfig
from trading_bot.core.enums import ContinuationMode, OrderType, ReasonCode, Side
from trading_bot.core.models import PositionState, PriceQuote, RiskDecision, SizedOrder, SymbolRules
from trading_bot.core.types import ZERO
from trading_bot.execution.ids import new_client_order_id
from trading_bot.market_data.validation import ValidationResult
from trading_bot.risk.sizing import SizingInputs, size_entry, size_exit_qty
from trading_bot.risk.state import RiskStateSnapshot

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateContext:
    """Everything the risk engine needs for one evaluation."""

    now: datetime
    equity: Decimal
    quote_free: Decimal
    base_free: Decimal
    quote: PriceQuote | None
    quote_validation: ValidationResult
    candle_validation: ValidationResult
    rules: SymbolRules
    state: RiskStateSnapshot
    open_position: PositionState | None
    kill_switch_active: bool
    kill_switch_reason: str
    circuit_breaker_open: bool
    approval_ok: bool  # continuation gate (AUTO_CONTINUE health / DAILY_APPROVAL window)
    continuation_mode: ContinuationMode
    exchange_available: bool = True
    duplicate_signal: bool = False
    extras: dict[str, str] = field(default_factory=dict)


RISK_TOKEN_TTL_S = 120.0


class RiskEngine:
    def __init__(self, cfg: AppConfig, fee_bps: Decimal) -> None:
        self.cfg = cfg
        self.fee_bps = fee_bps
        self._hmac_key = os.urandom(32)
        self._consumed: set[str] = set()
        self._issued: dict[str, float] = {}  # token -> expiry (time.time())
        self._lock = threading.Lock()
        from trading_bot.config.loader import config_hash

        self._cfg_hash = config_hash(cfg)

    # ------------------------------------------------------------ tokens
    def _token_material(self, order: SizedOrder) -> bytes:
        """Canonical serialization of the COMPLETE approved proposal.

        Every field of the sized order (including stop price, estimated
        notional/fees and risk amount), plus the mode and the configuration
        hash, is under the HMAC — mutating ANY approved value after
        evaluation invalidates the token."""
        from trading_bot.core.types import json_dumps

        canonical = json_dumps(
            {
                "order": order.as_dict(),
                "mode": self.cfg.mode.value,
                "config_hash": self._cfg_hash,
            }
        )
        return canonical.encode("utf-8")

    def _token_for(self, order: SizedOrder) -> str:
        import time

        digest = hmac.new(self._hmac_key, self._token_material(order), hashlib.sha256).hexdigest()
        with self._lock:
            self._issued[digest] = time.time() + RISK_TOKEN_TTL_S
        return digest

    def verify_and_consume(self, order: SizedOrder, token: str) -> bool:
        import time

        expected = hmac.new(self._hmac_key, self._token_material(order), hashlib.sha256).hexdigest()
        with self._lock:
            if token in self._consumed:
                log.error("approval token replay attempt for %s", order.client_order_id)
                return False
            expiry = self._issued.get(token)
            if expiry is None or not hmac.compare_digest(expected, token):
                log.error("invalid approval token for %s", order.client_order_id)
                return False
            if time.time() > expiry:
                log.error("expired approval token for %s", order.client_order_id)
                del self._issued[token]
                return False
            self._consumed.add(token)
            del self._issued[token]
            return True

    # ------------------------------------------------------ entry checks
    def evaluate_entry(self, ctx: GateContext) -> RiskDecision:
        codes: list[ReasonCode] = []
        r = self.cfg.risk
        s = ctx.state

        # -- control gates -------------------------------------------------
        if ctx.kill_switch_active:
            codes.append(ReasonCode.KILL_SWITCH_ACTIVE)
        if ctx.circuit_breaker_open:
            codes.append(ReasonCode.CIRCUIT_BREAKER_OPEN)
        if not ctx.approval_ok:
            codes.append(ReasonCode.TRADING_NOT_APPROVED)
        if not ctx.exchange_available:
            codes.append(ReasonCode.EXCHANGE_UNAVAILABLE)
        if s.unknown_orders > 0:
            codes.append(ReasonCode.UNKNOWN_ORDER_PENDING)
        if s.active_entry_orders > 0:
            codes.append(ReasonCode.ENTRY_ORDER_ACTIVE)
        if s.reconciliation_blocked:
            codes.append(ReasonCode.RECONCILIATION_MISMATCH)
        if ctx.duplicate_signal:
            codes.append(ReasonCode.DUPLICATE_SIGNAL)

        # -- market data gates ---------------------------------------------
        if not ctx.candle_validation.ok:
            gap = any("jump" in i for i in ctx.candle_validation.issues)
            stale = any("stale" in i for i in ctx.candle_validation.issues)
            if gap:
                codes.append(ReasonCode.GAP_TOLERANCE_EXCEEDED)
            if stale:
                codes.append(ReasonCode.STALE_MARKET_DATA)
            if not gap and not stale:
                codes.append(ReasonCode.DATA_VALIDATION_FAILED)
        if ctx.quote is None or not ctx.quote_validation.ok:
            stale_q = any("stale" in i for i in ctx.quote_validation.issues)
            wide = any("spread" in i for i in ctx.quote_validation.issues)
            if wide:
                codes.append(ReasonCode.SPREAD_TOO_WIDE)
            if stale_q:
                codes.append(ReasonCode.STALE_MARKET_DATA)
            if ctx.quote is None or (not wide and not stale_q):
                codes.append(ReasonCode.DATA_VALIDATION_FAILED)

        # -- exchange gates --------------------------------------------------
        if not ctx.rules.is_trading:
            codes.append(ReasonCode.SYMBOL_NOT_TRADING)
        if self.cfg.execution.order_type.value not in ctx.rules.order_types:
            codes.append(ReasonCode.ORDER_TYPE_UNSUPPORTED)
        if s.api_errors_last_hour > r.max_api_errors_per_hour:
            codes.append(ReasonCode.API_ERROR_THRESHOLD)

        # -- position / cadence gates ----------------------------------------
        if ctx.open_position is not None:
            codes.append(ReasonCode.POSITION_ALREADY_OPEN)
        if s.entries_today >= r.max_entries_per_day:
            codes.append(ReasonCode.MAX_ENTRIES_PER_DAY)
        if s.cooldown_until is not None and s.cooldown_until > ctx.now:
            codes.append(ReasonCode.COOLDOWN_ACTIVE)
        if s.consecutive_losses >= r.pause_after_consecutive_losses:
            codes.append(ReasonCode.CONSECUTIVE_LOSS_PAUSE)

        # -- loss limits -------------------------------------------------------
        if s.start_of_day_equity > ZERO:
            day_loss_pct = -s.realized_pnl_today / s.start_of_day_equity * Decimal(100)
            if day_loss_pct >= r.max_daily_loss_pct:
                codes.append(ReasonCode.DAILY_LOSS_LIMIT)
        if -s.pnl_7d_pct >= r.max_7d_loss_pct:
            codes.append(ReasonCode.WEEKLY_LOSS_LIMIT)
        if s.drawdown_pct >= r.max_drawdown_pct:
            codes.append(ReasonCode.MAX_DRAWDOWN)

        inputs = {
            **s.as_inputs(),
            "equity": str(ctx.equity),
            "quote_free": str(ctx.quote_free),
            "kill_switch": str(ctx.kill_switch_active),
            "quote_issues": "; ".join(ctx.quote_validation.issues),
            "candle_issues": "; ".join(ctx.candle_validation.issues),
        }

        if codes:
            return RiskDecision(False, tuple(codes), None, None, inputs)

        # -- sizing (only reached when every gate passed) ---------------------
        if ctx.quote is None:
            return RiskDecision(False, (ReasonCode.DATA_VALIDATION_FAILED,), None, None, inputs)
        sizing = size_entry(
            SizingInputs(
                equity=ctx.equity,
                quote_free=ctx.quote_free,
                est_entry_price=ctx.quote.ask,
                rules=ctx.rules,
                risk=r,
                stop_loss_pct=self.cfg.strategy.params.stop_loss_pct,
                fee_bps=self.fee_bps,
                order_type=self.cfg.execution.order_type,
            )
        )
        inputs["sizing_stop"] = str(sizing.stop_price)
        inputs["sizing_qty"] = str(sizing.qty)
        inputs["protective_exit_notional"] = str(sizing.protective_exit_notional)
        if not sizing.ok:
            return RiskDecision(
                False, sizing.codes or (ReasonCode.MIN_NOTIONAL_EXCEEDS_RISK,), None, None, inputs
            )

        order = SizedOrder(
            symbol=self.cfg.symbol,
            side=Side.BUY,
            order_type=self.cfg.execution.order_type,
            qty=sizing.qty,
            limit_price=ctx.quote.ask if self.cfg.execution.order_type == OrderType.LIMIT else None,
            stop_price=sizing.stop_price,
            est_entry_price=ctx.quote.ask,
            est_notional=sizing.est_notional,
            est_fee=sizing.est_fee,
            risk_amount=sizing.risk_amount,
            client_order_id=new_client_order_id("en"),
        )
        return RiskDecision(True, (ReasonCode.OK,), order, self._token_for(order), inputs)

    # ------------------------------------------------------- dust sweep
    def evaluate_dust_sweep(
        self, dust_qty: Decimal, quote: PriceQuote, rules: SymbolRules, base_free: Decimal
    ) -> RiskDecision:
        """Approve an aggregate market sell of accumulated exchange dust.
        Only meaningful once the combined residue clears exchange minimums."""
        from trading_bot.core.types import quantize_down

        inputs = {"purpose": "dust_sweep", "dust_qty": str(dust_qty)}
        qty = min(dust_qty, base_free)
        step = rules.quantity_step(OrderType.MARKET)
        if step > ZERO:
            qty = quantize_down(qty, step)
        if qty <= ZERO or qty < rules.quantity_min(OrderType.MARKET):
            return RiskDecision(False, (ReasonCode.QTY_BELOW_MIN,), None, None, inputs)
        notional = qty * quote.bid
        if notional < rules.min_notional:
            return RiskDecision(False, (ReasonCode.MIN_NOTIONAL_EXCEEDS_RISK,), None, None, inputs)
        order = SizedOrder(
            symbol=rules.symbol,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            qty=qty,
            limit_price=None,
            stop_price=ZERO,
            est_entry_price=quote.bid,
            est_notional=notional,
            est_fee=notional * self.fee_bps / Decimal(10000),
            risk_amount=ZERO,
            client_order_id=new_client_order_id("ds"),
        )
        return RiskDecision(True, (ReasonCode.OK,), order, self._token_for(order), inputs)

    # ------------------------------------------------- protective stops
    def evaluate_protective_stop(
        self, position: PositionState, rules: SymbolRules, base_free: Decimal
    ) -> RiskDecision:
        """Size and approve the exchange-native STOP_LOSS_LIMIT that protects
        an open position. Trigger = the position's invalidation level; the
        limit rests protective_limit_offset_bps below it so ordinary
        stop-throughs still fill. Sizing already guaranteed the exit stays
        representable; this re-checks against the ACTUAL filled quantity."""
        from trading_bot.core.types import BPS_DENOM, quantize_down

        inputs = {"purpose": "protective_stop", "position_id": position.position_id}
        qty = size_exit_qty(position.qty, base_free, rules, OrderType.STOP_LOSS_LIMIT)
        if qty is None:
            return RiskDecision(False, (ReasonCode.QTY_BELOW_MIN,), None, None, inputs)
        trigger = position.stop_price
        offset = self.cfg.execution.protective_limit_offset_bps
        limit = trigger * (BPS_DENOM - offset) / BPS_DENOM
        if rules.tick_size > ZERO:
            limit = quantize_down(limit, rules.tick_size)
        if trigger <= ZERO or limit <= ZERO or limit >= trigger:
            return RiskDecision(False, (ReasonCode.NO_VALID_STOP,), None, None, inputs)
        if qty * limit < rules.min_notional:
            return RiskDecision(
                False, (ReasonCode.PROTECTIVE_EXIT_NOT_REPRESENTABLE,), None, None, inputs
            )
        order = SizedOrder(
            symbol=rules.symbol,
            side=Side.SELL,
            order_type=OrderType.STOP_LOSS_LIMIT,
            qty=qty,
            limit_price=limit,
            stop_price=trigger,
            est_entry_price=limit,
            est_notional=qty * limit,
            est_fee=qty * limit * self.fee_bps / Decimal(10000),
            risk_amount=ZERO,
            client_order_id=new_client_order_id("ps"),
        )
        inputs["trigger"] = str(trigger)
        inputs["limit"] = str(limit)
        return RiskDecision(True, (ReasonCode.OK,), order, self._token_for(order), inputs)

    # ------------------------------------------------------- exit checks
    def evaluate_exit(self, ctx: GateContext, reason: str) -> RiskDecision:
        """Exits are deliberately permissive: closing risk is usually safer than
        holding it. Still requires a live quote and a sellable quantity."""
        inputs = {"exit_reason": reason, "equity": str(ctx.equity)}
        if ctx.open_position is None:
            return RiskDecision(False, (ReasonCode.DATA_VALIDATION_FAILED,), None, None, inputs)
        if ctx.quote is None:
            return RiskDecision(False, (ReasonCode.STALE_MARKET_DATA,), None, None, inputs)
        if ctx.state.active_exit_orders > 0:
            # A prior exit is still submitted/acknowledged/partially filled:
            # a second concurrent sell risks double-selling the position.
            # Reconcile or cancel the existing order first.
            return RiskDecision(False, (ReasonCode.EXIT_ORDER_ACTIVE,), None, None, inputs)
        if ctx.state.unknown_orders > 0:
            return RiskDecision(False, (ReasonCode.UNKNOWN_ORDER_PENDING,), None, None, inputs)

        qty = size_exit_qty(ctx.open_position.qty, ctx.base_free, ctx.rules)
        if qty is None:
            return RiskDecision(False, (ReasonCode.QTY_BELOW_MIN,), None, None, inputs)

        order = SizedOrder(
            symbol=self.cfg.symbol,
            side=Side.SELL,
            order_type=OrderType.MARKET,  # exits always market: certainty over price
            qty=qty,
            limit_price=None,
            stop_price=ctx.open_position.stop_price,
            est_entry_price=ctx.quote.bid,
            est_notional=qty * ctx.quote.bid,
            est_fee=qty * ctx.quote.bid * self.fee_bps / Decimal(10000),
            risk_amount=ZERO,
            client_order_id=new_client_order_id("ex"),
        )
        return RiskDecision(True, (ReasonCode.OK,), order, self._token_for(order), inputs)
