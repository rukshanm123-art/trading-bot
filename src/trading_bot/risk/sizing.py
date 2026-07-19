"""Fee-aware, Decimal-exact position sizing.

Sizing derives quantity from the risk budget and stop distance, then applies
every cap and exchange filter, always rounding DOWN. If the exchange minimum
order cannot be met inside the risk budget and cash reserve, the trade is
rejected — the size is never rounded up to satisfy the exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_bot.config.models import RiskConfig
from trading_bot.core.enums import ReasonCode
from trading_bot.core.models import SymbolRules
from trading_bot.core.types import BPS_DENOM, HUNDRED, ZERO, quantize_down


@dataclass(frozen=True)
class SizingInputs:
    equity: Decimal  # total account equity in quote terms
    quote_free: Decimal  # free quote balance
    est_entry_price: Decimal  # expected fill price (ask side)
    rules: SymbolRules
    risk: RiskConfig
    stop_loss_pct: Decimal  # protective stop distance in percent
    fee_bps: Decimal  # taker fee estimate


@dataclass(frozen=True)
class SizingResult:
    qty: Decimal
    stop_price: Decimal
    est_notional: Decimal
    est_fee: Decimal
    risk_amount: Decimal
    protective_exit_notional: Decimal
    codes: tuple[ReasonCode, ...]

    @property
    def ok(self) -> bool:
        return not self.codes and self.qty > ZERO


def size_entry(inputs: SizingInputs) -> SizingResult:
    codes: list[ReasonCode] = []
    r = inputs.rules
    entry = inputs.est_entry_price

    def reject(*cs: ReasonCode) -> SizingResult:
        return SizingResult(ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, tuple(cs))

    if entry <= ZERO or inputs.equity <= ZERO:
        return reject(ReasonCode.DATA_VALIDATION_FAILED)

    # 1. Protective invalidation level. If it cannot be represented on the
    #    exchange price grid, there is no valid trade.
    stop_price = entry * (HUNDRED - inputs.stop_loss_pct) / HUNDRED
    if r.tick_size > ZERO:
        stop_price = quantize_down(stop_price, r.tick_size)
    stop_distance = entry - stop_price
    if stop_price <= ZERO or stop_distance <= ZERO:
        return reject(ReasonCode.NO_VALID_STOP)

    # 2. Risk budget -> raw quantity.
    risk_budget = inputs.equity * inputs.risk.max_risk_per_trade_pct / HUNDRED
    qty_from_risk = risk_budget / stop_distance

    # 3. Allocation cap (percent of equity).
    alloc_cap_quote = inputs.equity * inputs.risk.max_position_allocation_pct / HUNDRED
    qty_from_alloc = alloc_cap_quote / entry

    # 4. Cash reserve: after paying cost + fees, the reserve must remain.
    reserve_required = inputs.equity * inputs.risk.min_cash_reserve_pct / HUNDRED
    fee_multiplier = (BPS_DENOM + inputs.fee_bps) / BPS_DENOM
    spendable = inputs.quote_free - reserve_required
    if spendable <= ZERO:
        return reject(ReasonCode.CASH_RESERVE_BREACH)
    qty_from_cash = spendable / (entry * fee_multiplier)

    qty = min(qty_from_risk, qty_from_alloc, qty_from_cash)
    if r.step_size > ZERO:
        qty = quantize_down(qty, r.step_size)

    # 5. Exchange minimums — reject, never round up.
    if qty <= ZERO or qty < r.min_qty:
        # Could the minimum even be afforded inside the constraints?
        return reject(ReasonCode.MIN_NOTIONAL_EXCEEDS_RISK)
    notional = qty * entry
    if notional < r.min_notional:
        return reject(ReasonCode.MIN_NOTIONAL_EXCEEDS_RISK)

    est_fee = notional * inputs.fee_bps / BPS_DENOM
    if inputs.quote_free < notional + est_fee:
        return reject(ReasonCode.INSUFFICIENT_BALANCE)
    if inputs.quote_free - (notional + est_fee) < reserve_required:
        return reject(ReasonCode.CASH_RESERVE_BREACH)

    risk_amount = qty * stop_distance
    # Invariant: the floor-rounding above can only shrink risk, never grow it.
    if risk_amount > risk_budget:
        codes.append(ReasonCode.RISK_BUDGET_EXCEEDED)  # defensive; should be unreachable

    # 6. A new position is unsafe unless its protective exit is representable
    #    at the stop after conservative deductions. The buffer is expressed in
    #    bps and covers: exit trading fees, configured slippage allowance,
    #    additional stop gap-through/evaluation margin, and the exchange's
    #    minimum-notional behaviour. Quantity is never rounded up to pass this.
    exit_buffer_bps = inputs.fee_bps + inputs.risk.max_slippage_bps
    exit_buffer_bps += inputs.risk.protective_exit_buffer_bps
    exit_multiplier = (BPS_DENOM - exit_buffer_bps) / BPS_DENOM
    protective_exit_notional = qty * stop_price * exit_multiplier
    if protective_exit_notional < r.min_notional:
        codes.append(ReasonCode.PROTECTIVE_EXIT_NOT_REPRESENTABLE)

    return SizingResult(
        qty=qty,
        stop_price=stop_price,
        est_notional=notional,
        est_fee=est_fee,
        risk_amount=risk_amount,
        protective_exit_notional=protective_exit_notional,
        codes=tuple(codes),
    )


def size_exit_qty(position_qty: Decimal, base_free: Decimal, rules: SymbolRules) -> Decimal | None:
    """Sellable quantity for closing a position: floor(min(position, balance), step).

    Returns None when nothing meaningful can be sold (dust below exchange minimums).
    """
    qty = min(position_qty, base_free)
    if rules.step_size > ZERO:
        qty = quantize_down(qty, rules.step_size)
    if qty <= ZERO or qty < rules.min_qty:
        return None
    return qty
