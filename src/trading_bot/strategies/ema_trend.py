"""Strategy A: EMA trend filter with edge-triggered entries.

Enter long only on the candle where the fast EMA crosses ABOVE the slow EMA
(transition, not level) — this prevents repeated buys while the condition
stays active. Exit when the fast EMA crosses back below, or when the
protective stop (managed by the exit monitor) triggers.

Uses completed candles only; the market data service already drops the
in-progress candle, and evaluate() never reads beyond the last closed one.
"""

from __future__ import annotations

from trading_bot.config.models import EmaParams
from trading_bot.core.enums import SignalAction
from trading_bot.core.models import Candle, SignalDecision
from trading_bot.strategies.interface import Strategy, ema_series


class EmaTrendStrategy(Strategy):
    name = "ema_trend"

    def __init__(self, params: EmaParams, version: str = "1.0.0") -> None:
        self.params = params
        self.version = version
        self.warmup = params.slow + 2  # need a previous diff for edge detection

    def evaluate(self, candles: list[Candle], has_position: bool) -> SignalDecision:
        last = candles[-1]
        closes = [c.close for c in candles]
        fast = ema_series(closes, self.params.fast)
        slow = ema_series(closes, self.params.slow)
        # align tails (ema_series output is aligned to the end of the input)
        n = min(len(fast), len(slow))
        fast, slow = fast[-n:], slow[-n:]
        diff_now = fast[-1] - slow[-1]
        diff_prev = fast[-2] - slow[-2]

        indicators = {
            "close": str(last.close),
            f"ema_{self.params.fast}": str(fast[-1]),
            f"ema_{self.params.slow}": str(slow[-1]),
            "diff_now": str(diff_now),
            "diff_prev": str(diff_prev),
        }

        crossed_up = diff_prev <= 0 < diff_now
        crossed_down = diff_prev >= 0 > diff_now

        if crossed_up and not has_position:
            action, reason = (
                SignalAction.ENTER_LONG,
                (f"EMA{self.params.fast} crossed above EMA{self.params.slow}"),
            )
        elif crossed_down and has_position:
            action, reason = (
                SignalAction.EXIT_LONG,
                (f"EMA{self.params.fast} crossed below EMA{self.params.slow}"),
            )
        elif has_position and diff_now < 0:
            # Safety net: if we hold while trend is down (e.g. entry preceded a
            # restart and the cross candle was missed), exit rather than drift.
            action, reason = SignalAction.EXIT_LONG, "trend negative while holding"
        else:
            action, reason = SignalAction.HOLD, "no cross transition"

        return SignalDecision(
            strategy=self.name,
            strategy_version=self.version,
            symbol=last.symbol,
            action=action,
            candle_open_time=last.open_time,
            reason=reason,
            indicators=indicators,
        )
