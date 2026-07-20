"""Test helpers: config factory, candle/quote builders, fake sources."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_bot.config.models import AppConfig
from trading_bot.core.models import Candle, PriceQuote, SymbolRules
from trading_bot.core.types import BPS_DENOM, dec
from trading_bot.market_data.validation import ValidationResult
from trading_bot.risk.engine import GateContext
from trading_bot.risk.state import RiskStateSnapshot

T0 = datetime(2025, 6, 1, tzinfo=UTC)

RULES = SymbolRules(
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    status="TRADING",
    min_qty=dec("0.00001"),
    step_size=dec("0.00001"),
    tick_size=dec("0.01"),
    min_notional=dec("5"),
)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def make_config(**overrides: Any) -> AppConfig:
    base: dict[str, Any] = {
        "mode": "paper",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "timezone": "Pacific/Auckland",
        "db": {"url": "sqlite:///:memory:"},
        "data": {"source": "exchange"},
        "strategy": {
            "name": "ema_trend",
            "version": "1.0.0",
            "params": {"fast": 12, "slow": 26, "stop_loss_pct": "2.0"},
        },
        "paper": {
            "starting_quote": "30",
            "seed": 42,
            "taker_fee_bps": "10",
            "spread_bps": "5",
            "slippage_bps_max": "8",
            "reject_probability": "0",
            "partial_fill_probability": "0",
            "latency_ms": 0,
        },
        "notifications": {
            "console": False,
            "email": {"enabled": False},
            "telegram": {"enabled": False},
        },
        "monitoring": {"enabled": False, "host": "127.0.0.1", "port": 9754},
    }
    return AppConfig.model_validate(_deep_merge(base, overrides))


def make_candles(
    closes: list[str | float],
    start: datetime = T0,
    interval_s: int = 3600,
    symbol: str = "BTCUSDT",
) -> list[Candle]:
    out: list[Candle] = []
    prev_close: Decimal | None = None
    for i, c in enumerate(closes):
        close = dec(str(c))
        open_p = prev_close if prev_close is not None else close
        hi = max(open_p, close) * dec("1.001")
        lo = min(open_p, close) * dec("0.999")
        open_time = start + timedelta(seconds=interval_s * i)
        out.append(
            Candle(
                symbol=symbol,
                interval="1h",
                open_time=open_time,
                close_time=open_time + timedelta(seconds=interval_s - 1),
                open=open_p,
                high=hi,
                low=lo,
                close=close,
                volume=dec("10"),
                is_closed=True,
            )
        )
        prev_close = close
    return out


def make_quote(
    price: str | Decimal = "100",
    spread_bps: str = "5",
    ts: datetime | None = None,
    symbol: str = "BTCUSDT",
) -> PriceQuote:
    p = dec(str(price))
    half = p * dec(spread_bps) / 2 / BPS_DENOM
    return PriceQuote(symbol=symbol, bid=p - half, ask=p + half, last=p, ts=ts or T0)


class FakeQuoteSource:
    """Controllable MarketDataSource for paper-exchange and service tests."""

    def __init__(self, price: str = "100", ts: datetime | None = None) -> None:
        self.price = dec(price)
        self.ts = ts or T0
        self.candles: list[Candle] = make_candles(["100"] * 40)

    def get_price(self, symbol: str) -> PriceQuote:
        return make_quote(self.price, ts=self.ts, symbol=symbol)

    def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        return self.candles[-limit:]


def make_state(**overrides: Any) -> RiskStateSnapshot:
    defaults: dict[str, Any] = {
        "day": "2025-06-01",
        "start_of_day_equity": dec("1000"),
        "realized_pnl_today": dec("0"),
        "entries_today": 0,
        "pnl_7d_pct": dec("0"),
        "drawdown_pct": dec("0"),
        "peak_equity": dec("1000"),
        "consecutive_losses": 0,
        "cooldown_until": None,
        "unknown_orders": 0,
        "active_entry_orders": 0,
        "active_exit_orders": 0,
        "reconciliation_blocked": False,
        "api_errors_last_hour": 0,
    }
    defaults.update(overrides)
    return RiskStateSnapshot(**defaults)


def make_ctx(**overrides: Any) -> GateContext:
    """Benign context that passes every entry gate unless overridden."""
    from trading_bot.core.enums import ContinuationMode

    defaults: dict[str, Any] = {
        "now": T0,
        "equity": dec("1000"),
        "quote_free": dec("900"),
        "base_free": dec("0"),
        "quote": make_quote("100"),
        "quote_validation": ValidationResult.success(),
        "candle_validation": ValidationResult.success(),
        "rules": RULES,
        "state": make_state(),
        "open_position": None,
        "kill_switch_active": False,
        "kill_switch_reason": "",
        "circuit_breaker_open": False,
        "approval_ok": True,
        "continuation_mode": ContinuationMode.AUTO_CONTINUE,
        "exchange_available": True,
        "duplicate_signal": False,
    }
    defaults.update(overrides)
    return GateContext(**defaults)


def make_trend_rows(
    segments: list[tuple[int, float]],
    start_price: float = 100.0,
    start: datetime = datetime(2025, 1, 1, tzinfo=UTC),
    interval_s: int = 3600,
) -> list[dict[str, str]]:
    """Piecewise-drift candle rows for engine scenarios.

    segments: list of (n_candles, pct_change_per_candle).
    """
    rows: list[dict[str, str]] = []
    price = start_price
    t = start
    for n, pct in segments:
        for _ in range(n):
            open_p = price
            close_p = max(1.0, open_p * (1 + pct / 100))
            hi = max(open_p, close_p) * 1.0005
            lo = min(open_p, close_p) * 0.9995
            rows.append(
                {
                    "open_time": t.isoformat(),
                    "open": f"{open_p:.2f}",
                    "high": f"{hi:.2f}",
                    "low": f"{lo:.2f}",
                    "close": f"{close_p:.2f}",
                    "volume": "10",
                }
            )
            price = close_p
            t += timedelta(seconds=interval_s)
    return rows


def write_rows_csv(rows: list[dict[str, str]], path) -> str:
    import csv

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["open_time", "open", "high", "low", "close", "volume"]
        )
        writer.writeheader()
        writer.writerows(rows)
    return str(path)
