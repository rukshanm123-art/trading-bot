"""Domain models. Frozen dataclasses; all monetary fields are Decimal."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading_bot.core.enums import (
    Mode,
    OrderState,
    OrderType,
    ReasonCode,
    Side,
    SignalAction,
)
from trading_bot.core.types import BPS_DENOM, ZERO

# Injectable time source so backtests/fixture replays stamp records with
# SIMULATED time (otherwise cooldowns, daily limits and exposure accounting
# would silently mix wall-clock and fixture time).
_time_provider: Callable[[], datetime] | None = None


def set_time_provider(fn: Callable[[], datetime] | None) -> None:
    global _time_provider
    _time_provider = fn


def utcnow() -> datetime:
    if _time_provider is not None:
        return _time_provider()
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@dataclass(frozen=True)
class Candle:
    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": iso(self.open_time),
            "close_time": iso(self.close_time),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
            "is_closed": self.is_closed,
        }


@dataclass(frozen=True)
class PriceQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    ts: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        if self.mid <= ZERO:
            return BPS_DENOM  # degenerate quote: treat as maximally wide
        return (self.ask - self.bid) / self.mid * BPS_DENOM


@dataclass(frozen=True)
class SymbolRules:
    """Exchange trading rules for one symbol (Binance exchangeInfo filters)."""

    symbol: str
    base_asset: str
    quote_asset: str
    status: str  # "TRADING" when tradable
    min_qty: Decimal  # LOT_SIZE minQty
    step_size: Decimal  # LOT_SIZE stepSize
    tick_size: Decimal  # PRICE_FILTER tickSize
    min_notional: Decimal  # NOTIONAL minNotional
    order_types: tuple[str, ...] = ("MARKET", "LIMIT")

    @property
    def is_trading(self) -> bool:
        return self.status == "TRADING"


@dataclass(frozen=True)
class AssetBalance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True)
class Fill:
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_asset: str


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    order_type: OrderType
    qty: Decimal
    client_order_id: str
    price: Decimal | None = None  # required for LIMIT


@dataclass(frozen=True)
class OrderResponse:
    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    state: OrderState
    requested_qty: Decimal
    executed_qty: Decimal
    cumulative_quote: Decimal  # total quote spent/received so far
    fills: tuple[Fill, ...]
    ts: datetime
    raw_status: str = ""

    @property
    def avg_fill_price(self) -> Decimal:
        if self.executed_qty <= ZERO:
            return ZERO
        return self.cumulative_quote / self.executed_qty

    @property
    def total_fees_quote_equiv(self) -> Decimal:
        # Fees may be charged in base or quote; report both raw and a quote
        # estimate where fee_asset matches the quote leg. Base-asset fees are
        # converted at avg fill price (estimate for reporting only).
        total = ZERO
        avg = self.avg_fill_price
        for f in self.fills:
            if f.fee_asset == "" or avg == ZERO:
                total += f.fee
            elif self.symbol.startswith(f.fee_asset):  # fee in base asset
                total += f.fee * avg
            else:
                total += f.fee
        return total


@dataclass(frozen=True)
class SignalDecision:
    """A strategy's decision for one closed candle. Pure data; no authority."""

    strategy: str
    strategy_version: str
    symbol: str
    action: SignalAction
    candle_open_time: datetime
    reason: str
    indicators: dict[str, str] = field(default_factory=dict)  # name -> str(Decimal)


@dataclass(frozen=True)
class SizedOrder:
    """A concrete, exchange-rule-compliant order produced by the risk engine."""

    symbol: str
    side: Side
    order_type: OrderType
    qty: Decimal
    limit_price: Decimal | None
    stop_price: Decimal  # protective invalidation level (software-monitored)
    est_entry_price: Decimal
    est_notional: Decimal
    est_fee: Decimal
    risk_amount: Decimal  # amount at risk if stop is hit (est.)
    client_order_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "qty": str(self.qty),
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "stop_price": str(self.stop_price),
            "est_entry_price": str(self.est_entry_price),
            "est_notional": str(self.est_notional),
            "est_fee": str(self.est_fee),
            "risk_amount": str(self.risk_amount),
            "client_order_id": self.client_order_id,
        }


@dataclass(frozen=True)
class PositionState:
    """The single open spot position (base-asset holding with protective stop)."""

    position_id: str
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    stop_price: Decimal
    opened_at: datetime
    entry_fee: Decimal
    entry_order_id: str

    def unrealized_pnl(self, price: Decimal) -> Decimal:
        return (price - self.avg_entry_price) * self.qty

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "qty": str(self.qty),
            "avg_entry_price": str(self.avg_entry_price),
            "stop_price": str(self.stop_price),
            "opened_at": iso(self.opened_at),
            "entry_fee": str(self.entry_fee),
            "entry_order_id": self.entry_order_id,
        }


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    codes: tuple[ReasonCode, ...]
    order: SizedOrder | None
    approval_token: str | None
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_code(self) -> ReasonCode:
        return self.codes[0] if self.codes else ReasonCode.OK


@dataclass(frozen=True)
class ExecutionResult:
    submitted: bool
    state: OrderState
    response: OrderResponse | None
    error: str = ""


@dataclass(frozen=True)
class DecisionRecord:
    """Full audit trail of one pipeline evaluation."""

    decision_id: str
    correlation_id: str
    ts: datetime
    mode: Mode
    strategy: str
    strategy_version: str
    config_hash: str
    symbol: str
    market_data_ts: datetime | None
    signal: SignalDecision | None
    risk: RiskDecision | None
    execution: ExecutionResult | None
    explanation: str
