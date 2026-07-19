"""Strategies B and C: benchmarks, not trading recommendations.

Buy-and-hold enters once and never exits; no-trade never enters. Reports
compare the active strategy against BOTH analytically (see
reporting/performance.py) so benchmark comparison never depends on running
these as live strategies.
"""

from __future__ import annotations

from trading_bot.core.enums import SignalAction
from trading_bot.core.models import Candle, SignalDecision
from trading_bot.strategies.interface import Strategy


class BuyAndHoldStrategy(Strategy):
    name = "buy_and_hold"
    version = "1.0.0"
    warmup = 2

    def evaluate(self, candles: list[Candle], has_position: bool) -> SignalDecision:
        last = candles[-1]
        if has_position:
            return SignalDecision(
                strategy=self.name,
                strategy_version=self.version,
                symbol=last.symbol,
                action=SignalAction.HOLD,
                candle_open_time=last.open_time,
                reason="benchmark: holding",
            )
        return SignalDecision(
            strategy=self.name,
            strategy_version=self.version,
            symbol=last.symbol,
            action=SignalAction.ENTER_LONG,
            candle_open_time=last.open_time,
            reason="benchmark: initial entry",
        )


class NoTradeStrategy(Strategy):
    name = "no_trade"
    version = "1.0.0"
    warmup = 1

    def evaluate(self, candles: list[Candle], has_position: bool) -> SignalDecision:
        last = candles[-1]
        return SignalDecision(
            strategy=self.name,
            strategy_version=self.version,
            symbol=last.symbol,
            action=SignalAction.HOLD,
            candle_open_time=last.open_time,
            reason="benchmark: cash position",
        )
