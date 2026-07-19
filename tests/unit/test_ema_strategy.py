"""EMA trend strategy: edge-triggered, no duplicate entries, no look-ahead."""

from decimal import Decimal

import pytest

from tests.helpers import make_candles, make_config
from trading_bot.core.enums import SignalAction
from trading_bot.strategies.ema_trend import EmaTrendStrategy
from trading_bot.strategies.interface import ema_series

PARAMS = make_config().strategy.params  # fast 12 / slow 26
STRAT = EmaTrendStrategy(PARAMS)


def flat_then_ramp(n_flat=40, n_ramp=12, step=2.0):
    closes = [100.0] * n_flat
    p = 100.0
    for _ in range(n_ramp):
        p *= 1 + step / 100
        closes.append(round(p, 2))
    return [str(c) for c in closes]


def find_cross_index(closes):
    """Index where the strategy first signals ENTER_LONG when flat."""
    for i in range(STRAT.warmup, len(closes) + 1):
        sig = STRAT.evaluate(make_candles(closes[:i]), has_position=False)
        if sig.action == SignalAction.ENTER_LONG:
            return i
    return None


def test_cross_up_triggers_entry_once():
    closes = flat_then_ramp()
    i = find_cross_index(closes)
    assert i is not None, "ramp must produce a cross"
    # every later candle with the trend STILL active must not re-signal
    for j in range(i + 1, len(closes) + 1):
        sig = STRAT.evaluate(make_candles(closes[:j]), has_position=False)
        assert sig.action != SignalAction.ENTER_LONG, f"duplicate entry at {j}"


def test_exit_on_cross_down():
    closes = flat_then_ramp()
    p = float(closes[-1])
    for _ in range(20):
        p *= 0.985
        closes.append(f"{p:.2f}")
    exits = [
        STRAT.evaluate(make_candles(closes[:i]), has_position=True).action
        for i in range(len(closes) - 19, len(closes) + 1)
    ]
    assert SignalAction.EXIT_LONG in exits


def test_hold_when_flat():
    sig = STRAT.evaluate(make_candles(["100"] * 60), has_position=False)
    assert sig.action == SignalAction.HOLD


def test_uses_only_provided_candles():
    """No look-ahead: the decision for candle k ignores anything after k."""
    closes = flat_then_ramp()
    i = find_cross_index(closes)
    assert i is not None
    at_cross = STRAT.evaluate(make_candles(closes[:i]), has_position=False)
    # append a crash AFTER k — decision at k must be identical
    extended = closes + ["1.00"] * 5
    again = STRAT.evaluate(make_candles(extended[:i]), has_position=False)
    assert at_cross.action == again.action == SignalAction.ENTER_LONG
    assert at_cross.candle_open_time == again.candle_open_time


def test_signal_carries_indicators_and_candle_time():
    candles = make_candles(["100"] * 40)
    sig = STRAT.evaluate(candles, has_position=False)
    assert sig.candle_open_time == candles[-1].open_time
    assert f"ema_{PARAMS.fast}" in sig.indicators
    assert f"ema_{PARAMS.slow}" in sig.indicators


def test_safety_net_exit_when_holding_in_downtrend():
    closes = [str(100 - i) for i in range(60)]  # steady decline
    sig = STRAT.evaluate(make_candles(closes), has_position=True)
    assert sig.action == SignalAction.EXIT_LONG


def test_ema_series_math():
    values = [Decimal("1")] * 10
    out = ema_series(values, 5)
    assert all(v == Decimal("1") for v in out)
    with pytest.raises(ValueError):
        ema_series(values[:3], 5)
