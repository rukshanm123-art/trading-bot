"""Enumerations shared across the system."""

from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class EndpointEnvironment(str, Enum):
    """Explicit endpoint class. Do not infer safety from arbitrary URLs alone."""

    FIXTURE = "fixture"
    LIVE_PUBLIC = "live_public"
    TESTNET = "testnet"
    LIVE = "live"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    # Exchange-native protective stop: triggers at stop_price, rests as a
    # limit sell at limit_price. Used ONLY for protective exits.
    STOP_LOSS_LIMIT = "STOP_LOSS_LIMIT"


class OrderState(str, Enum):
    """Execution state machine states. Transitions enforced in execution.state_machine."""

    PROPOSED = "PROPOSED"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


TERMINAL_ORDER_STATES = frozenset(
    {
        OrderState.RISK_REJECTED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
    }
)


class SignalAction(str, Enum):
    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"
    HOLD = "HOLD"


class ReasonCode(str, Enum):
    """Structured reason codes for every risk decision and rejection."""

    OK = "OK"
    # sizing / balance
    MIN_NOTIONAL_EXCEEDS_RISK = "MIN_NOTIONAL_EXCEEDS_RISK"
    PROTECTIVE_EXIT_NOT_REPRESENTABLE = "PROTECTIVE_EXIT_NOT_REPRESENTABLE"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    CASH_RESERVE_BREACH = "CASH_RESERVE_BREACH"
    ALLOCATION_EXCEEDED = "ALLOCATION_EXCEEDED"
    RISK_BUDGET_EXCEEDED = "RISK_BUDGET_EXCEEDED"
    QTY_BELOW_MIN = "QTY_BELOW_MIN"
    # loss limits
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    CONSECUTIVE_LOSS_PAUSE = "CONSECUTIVE_LOSS_PAUSE"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    MAX_ENTRIES_PER_DAY = "MAX_ENTRIES_PER_DAY"
    # market data
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    DATA_VALIDATION_FAILED = "DATA_VALIDATION_FAILED"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    GAP_TOLERANCE_EXCEEDED = "GAP_TOLERANCE_EXCEEDED"
    # state / control
    POSITION_ALREADY_OPEN = "POSITION_ALREADY_OPEN"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"
    TRADING_NOT_APPROVED = "TRADING_NOT_APPROVED"
    UNKNOWN_ORDER_PENDING = "UNKNOWN_ORDER_PENDING"
    ENTRY_ORDER_ACTIVE = "ENTRY_ORDER_ACTIVE"
    EXIT_ORDER_ACTIVE = "EXIT_ORDER_ACTIVE"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    # exchange
    EXCHANGE_UNAVAILABLE = "EXCHANGE_UNAVAILABLE"
    SYMBOL_NOT_TRADING = "SYMBOL_NOT_TRADING"
    API_ERROR_THRESHOLD = "API_ERROR_THRESHOLD"
    MODE_MISMATCH = "MODE_MISMATCH"
    ORDER_TYPE_UNSUPPORTED = "ORDER_TYPE_UNSUPPORTED"
    # protective exit
    NO_VALID_STOP = "NO_VALID_STOP"


class KillSwitchSource(str, Enum):
    CLI = "cli"
    ENV = "env"
    DB_FLAG = "db_flag"
    FILE = "file"
    CIRCUIT_BREAKER = "circuit_breaker"


class Recommendation(str, Enum):
    CONTINUE = "continue"
    CONTINUE_WITH_CAUTION = "continue_with_caution"
    PAUSE = "pause"
    INVESTIGATE = "investigate"


class ComponentHealth(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


class ContinuationMode(str, Enum):
    AUTO_CONTINUE = "auto_continue"
    DAILY_APPROVAL = "daily_approval"


class EmergencyPositionPolicy(str, Enum):
    """What to do with an EXISTING position when a kill switch fires.

    HOLD_AND_MONITOR: keep the position, keep the protective exit monitor running,
    never open new entries. This is the default — blind liquidation into a bad
    market can be worse than holding a stop-protected position.
    """

    HOLD_AND_MONITOR = "hold_and_monitor"
    CLOSE_AT_MARKET = "close_at_market"
