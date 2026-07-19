"""Strategy interface.

Strategies are pure functions of (closed candles, position flag) -> signal.
They have NO authority: every signal must pass the risk engine, and only the
execution gateway can reach an exchange.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from trading_bot.core.models import Candle, SignalDecision


class Strategy(ABC):
    name: str = "base"
    version: str = "0.0.0"
    warmup: int = 2  # minimum closed candles required before evaluating

    @abstractmethod
    def evaluate(self, candles: list[Candle], has_position: bool) -> SignalDecision:
        """Evaluate the latest CLOSED candle. Must not look at candles[i+1:]."""


def ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    """Exponential moving average, Decimal arithmetic, seeded with SMA."""
    if period <= 1 or len(values) < period:
        raise ValueError(f"need at least {period} values for EMA{period}")
    k = Decimal(2) / Decimal(period + 1)
    sma = sum(values[:period], Decimal(0)) / Decimal(period)
    out: list[Decimal] = [sma]
    for v in values[period:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out  # aligned to values[period-1:]
