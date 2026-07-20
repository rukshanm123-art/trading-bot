"""Validated configuration models.

Rules:
- All financial numbers must be YAML strings (e.g. "0.5"), never bare floats.
- Risk settings may only TIGHTEN the hard caps in config.constants; anything
  looser fails validation and the bot refuses to start (fail closed).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from trading_bot.config import constants as C
from trading_bot.core.enums import ContinuationMode, EmergencyPositionPolicy, Mode, OrderType
from trading_bot.core.types import dec

VALID_INTERVALS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def _no_float(v: Any, field_name: str) -> Any:
    if isinstance(v, float):
        raise ValueError(
            f"{field_name}: financial values must be quoted strings in YAML "
            f'(got float {v!r}; write "{v}" instead)'
        )
    return v


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DecimalField:
    """Reusable validator: accept str/int/Decimal, refuse float."""

    @staticmethod
    def parse(v: Any, name: str) -> Decimal:
        _no_float(v, name)
        return dec(v)


class DbConfig(StrictModel):
    url: str = "sqlite:///var/trading_bot.db"

    @field_validator("url")
    @classmethod
    def _check_scheme(cls, v: str) -> str:
        if not (v.startswith("sqlite:///") or v.startswith("postgresql://")):
            raise ValueError("db.url must be sqlite:///path or postgresql://...")
        return v


class DataConfig(StrictModel):
    source: str = "exchange"  # "exchange" (public market data) | "fixture" (CSV replay)
    fixture_path: str | None = None

    @model_validator(mode="after")
    def _check(self) -> DataConfig:
        if self.source not in ("exchange", "fixture"):
            raise ValueError("data.source must be 'exchange' or 'fixture'")
        if self.source == "fixture" and not self.fixture_path:
            raise ValueError("data.source=fixture requires data.fixture_path")
        return self


class EmaParams(StrictModel):
    fast: int = 12
    slow: int = 26
    stop_loss_pct: Decimal = Decimal("2.0")

    @field_validator("stop_loss_pct", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        return DecimalField.parse(v, "strategy.params.stop_loss_pct")

    @model_validator(mode="after")
    def _check(self) -> EmaParams:
        if not (1 < self.fast < self.slow <= 500):
            raise ValueError("require 1 < fast < slow <= 500")
        if not (Decimal("0.2") <= self.stop_loss_pct <= Decimal("10")):
            raise ValueError("stop_loss_pct must be within [0.2, 10] percent")
        return self


class StrategyConfig(StrictModel):
    name: str = "ema_trend"
    version: str = "1.0.0"
    params: EmaParams = EmaParams()

    @field_validator("name")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in ("ema_trend", "buy_and_hold", "no_trade"):
            raise ValueError(f"unknown strategy '{v}'")
        return v


class RiskConfig(StrictModel):
    max_open_positions: int = 1
    max_position_allocation_pct: Decimal = Decimal("20")
    min_cash_reserve_pct: Decimal = Decimal("50")
    max_risk_per_trade_pct: Decimal = Decimal("0.5")
    max_daily_loss_pct: Decimal = Decimal("2")
    max_7d_loss_pct: Decimal = Decimal("5")
    max_drawdown_pct: Decimal = Decimal("8")
    max_entries_per_day: int = 2
    cooldown_after_loss_hours: int = 12
    pause_after_consecutive_losses: int = 3
    max_slippage_bps: Decimal = Decimal("50")
    max_spread_bps: Decimal = Decimal("20")
    max_quote_age_s: int = 120
    candle_grace_s: int = 90
    max_gap_pct: Decimal = Decimal("10")
    max_api_errors_per_hour: int = 10
    max_reconciliation_mismatch_quote: Decimal = Decimal("0.05")
    protective_exit_buffer_bps: Decimal = Decimal("100")
    max_clock_skew_s: int = 30
    market_data_recovery_successes: int = 2

    @field_validator(
        "max_position_allocation_pct",
        "min_cash_reserve_pct",
        "max_risk_per_trade_pct",
        "max_daily_loss_pct",
        "max_7d_loss_pct",
        "max_drawdown_pct",
        "max_slippage_bps",
        "max_spread_bps",
        "max_gap_pct",
        "max_reconciliation_mismatch_quote",
        "protective_exit_buffer_bps",
        mode="before",
    )
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        return DecimalField.parse(v, "risk.*")

    @model_validator(mode="after")
    def _enforce_hard_caps(self) -> RiskConfig:
        checks: list[tuple[str, bool]] = [
            ("max_open_positions", self.max_open_positions <= C.HARD_MAX_OPEN_POSITIONS),
            ("max_open_positions>0", self.max_open_positions >= 1),
            (
                "max_position_allocation_pct",
                Decimal("0")
                < self.max_position_allocation_pct
                <= C.HARD_MAX_POSITION_ALLOCATION_PCT,
            ),
            ("min_cash_reserve_pct", self.min_cash_reserve_pct >= C.HARD_MIN_CASH_RESERVE_PCT),
            (
                "max_risk_per_trade_pct",
                Decimal("0") < self.max_risk_per_trade_pct <= C.HARD_MAX_RISK_PER_TRADE_PCT,
            ),
            (
                "max_daily_loss_pct",
                Decimal("0") < self.max_daily_loss_pct <= C.HARD_MAX_DAILY_LOSS_PCT,
            ),
            ("max_7d_loss_pct", Decimal("0") < self.max_7d_loss_pct <= C.HARD_MAX_7D_LOSS_PCT),
            ("max_drawdown_pct", Decimal("0") < self.max_drawdown_pct <= C.HARD_MAX_DRAWDOWN_PCT),
            ("max_entries_per_day", 1 <= self.max_entries_per_day <= C.HARD_MAX_ENTRIES_PER_DAY),
            (
                "cooldown_after_loss_hours",
                self.cooldown_after_loss_hours >= C.HARD_MIN_COOLDOWN_AFTER_LOSS_HOURS,
            ),
            ("pause_after_consecutive_losses", self.pause_after_consecutive_losses >= 1),
            ("max_slippage_bps", Decimal("0") < self.max_slippage_bps <= C.HARD_MAX_SLIPPAGE_BPS),
            ("max_spread_bps", Decimal("0") < self.max_spread_bps <= C.HARD_MAX_SPREAD_BPS),
            (
                "protective_exit_buffer_bps",
                Decimal("0")
                <= self.protective_exit_buffer_bps
                <= C.HARD_MAX_PROTECTIVE_EXIT_BUFFER_BPS,
            ),
            ("max_clock_skew_s", 0 <= self.max_clock_skew_s <= C.HARD_MAX_CLOCK_SKEW_S),
            ("market_data_recovery_successes", self.market_data_recovery_successes >= 1),
        ]
        failures = [name for name, ok in checks if not ok]
        if failures:
            raise ValueError(
                "risk config exceeds hard safety caps (see src/trading_bot/config/constants.py): "
                + ", ".join(failures)
            )
        return self


class ExecutionConfig(StrictModel):
    order_type: OrderType = OrderType.MARKET
    limit_timeout_s: int = 60  # cancel unfilled limit entries after this
    max_order_age_s: int = 120  # any order older than this without terminal state -> reconcile
    submit_timeout_s: int = 10
    # Exchange-native protective stops (STOP_LOSS_LIMIT resting on the
    # exchange) so a dead process no longer means an unprotected position.
    # The software monitor remains as the escalation backstop.
    use_native_stops: bool = True
    protective_limit_offset_bps: Decimal = Decimal("100")  # limit below trigger
    protective_escalation_cycles: int = 3  # breached-but-unfilled cycles before market exit
    stop_monitor_interval_s: int = 20  # cycle cadence in live/paper (seconds)

    @field_validator("protective_limit_offset_bps", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        return DecimalField.parse(v, "execution.protective_limit_offset_bps")

    @model_validator(mode="after")
    def _check(self) -> ExecutionConfig:
        if self.order_type == OrderType.STOP_LOSS_LIMIT:
            raise ValueError("execution.order_type is for ENTRIES; stops are protective-only")
        if not (
            Decimal("0")
            < self.protective_limit_offset_bps
            <= C.HARD_MAX_PROTECTIVE_LIMIT_OFFSET_BPS
        ):
            raise ValueError(
                "protective_limit_offset_bps must be within "
                f"(0, {C.HARD_MAX_PROTECTIVE_LIMIT_OFFSET_BPS}]"
            )
        if not (1 <= self.protective_escalation_cycles <= 20):
            raise ValueError("protective_escalation_cycles must be within [1, 20]")
        if not (5 <= self.stop_monitor_interval_s <= 300):
            raise ValueError("stop_monitor_interval_s must be within [5, 300] seconds")
        return self


class PaperSimConfig(StrictModel):
    starting_quote: Decimal = Decimal("30")  # ~NZD 50 in USDT
    seed: int = 42
    taker_fee_bps: Decimal = Decimal("10")  # 0.10%
    spread_bps: Decimal = Decimal("5")
    slippage_bps_max: Decimal = Decimal("8")
    reject_probability: Decimal = Decimal("0.02")
    partial_fill_probability: Decimal = Decimal("0.10")
    latency_ms: int = 100

    @field_validator(
        "starting_quote",
        "taker_fee_bps",
        "spread_bps",
        "slippage_bps_max",
        "reject_probability",
        "partial_fill_probability",
        mode="before",
    )
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        return DecimalField.parse(v, "paper.*")

    @model_validator(mode="after")
    def _check(self) -> PaperSimConfig:
        if not (Decimal("0") <= self.reject_probability < Decimal("0.5")):
            raise ValueError("reject_probability must be in [0, 0.5)")
        if not (Decimal("0") <= self.partial_fill_probability <= Decimal("0.9")):
            raise ValueError("partial_fill_probability must be in [0, 0.9]")
        if self.starting_quote <= Decimal("0"):
            raise ValueError("starting_quote must be positive")
        return self


class ContinuationConfig(StrictModel):
    mode: ContinuationMode = ContinuationMode.AUTO_CONTINUE
    approve_default_hours: int = 24
    # Required acknowledgement if someone insists on AUTO_CONTINUE in live mode.
    acknowledge_auto_continue_risk: bool = False


class EmailConfig(StrictModel):
    enabled: bool = False
    use_tls: bool = True
    # host/port/credentials come from env (SMTP_*) — never from config files.


class TelegramConfig(StrictModel):
    enabled: bool = False
    # token/chat id come from env (TELEGRAM_*) — never from config files.


class NotificationsConfig(StrictModel):
    console: bool = True
    email: EmailConfig = EmailConfig()
    telegram: TelegramConfig = TelegramConfig()


class MonitoringConfig(StrictModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 9754

    @field_validator("host")
    @classmethod
    def _local_only_warning(cls, v: str) -> str:
        # Binding beyond loopback is allowed only deliberately (e.g. docker),
        # and SECURITY.md requires a reverse proxy + auth in that case.
        return v


class ReportingConfig(StrictModel):
    daily_time_local: str = "17:00"
    output_dir: str = "var/reports"

    @field_validator("daily_time_local")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not (0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59):
            raise ValueError("daily_time_local must be HH:MM")
        return v


class AppConfig(StrictModel):
    mode: Mode = Mode.PAPER
    symbol: str = "BTCUSDT"
    interval: str = "1h"
    timezone: str = "Pacific/Auckland"
    work_dir: str = "var"
    db: DbConfig = DbConfig()
    data: DataConfig = DataConfig()
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    paper: PaperSimConfig = PaperSimConfig()
    continuation: ContinuationConfig = ContinuationConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    reporting: ReportingConfig = ReportingConfig()
    emergency_position_policy: EmergencyPositionPolicy = EmergencyPositionPolicy.HOLD_AND_MONITOR

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        v = v.replace("/", "").upper()
        if not v.isalnum() or not (5 <= len(v) <= 20):
            raise ValueError(f"invalid symbol '{v}'")
        return v

    @field_validator("interval")
    @classmethod
    def _interval(cls, v: str) -> str:
        if v not in VALID_INTERVALS:
            raise ValueError(f"interval must be one of {sorted(VALID_INTERVALS)}")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        ZoneInfo(v)  # raises if unknown
        return v

    @model_validator(mode="after")
    def _mode_rules(self) -> AppConfig:
        if self.mode == Mode.LIVE:
            if (
                self.continuation.mode == ContinuationMode.AUTO_CONTINUE
                and not self.continuation.acknowledge_auto_continue_risk
            ):
                raise ValueError(
                    "live mode defaults to DAILY_APPROVAL; to use AUTO_CONTINUE you must set "
                    "continuation.acknowledge_auto_continue_risk: true (not recommended)"
                )
        if self.mode != Mode.PAPER and self.data.source == "fixture":
            raise ValueError("fixture data source is only allowed in paper mode")
        return self

    @property
    def interval_seconds(self) -> int:
        return VALID_INTERVALS[self.interval]
