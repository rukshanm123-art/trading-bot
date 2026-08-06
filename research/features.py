"""Leakage-safe feature construction for EMA meta-labelling.

Every feature is computed from the SIGNAL candle and earlier bars only — the
last CLOSED candle when the entry decision is made. The fill happens on the
next bar, so nothing here can see its own outcome.

Gap policy: a feature whose lookback window crosses a data gap is invalid and
the event is dropped, rather than silently bridging missing hours (which would
manufacture fake zero-return bars).

Feature count is capped by research_spec.yaml and must not grow after seeing
validation results.
"""

from __future__ import annotations

import statistics
from decimal import Decimal

from events import atr_series

FEATURE_NAMES = [
    "ret_1h",
    "ret_3h",
    "ret_6h",
    "ret_12h",
    "ret_24h",
    "ema_distance",
    "ema_slope",
    "rsi_14",
    "atr_pct",
    "realized_vol_24h",
    "candle_range",
    "body_size",
    "volume_change",
    "volume_zscore_24h",
    "dist_from_high_24h",
]

# Longest lookback any feature needs. Warmup is DERIVED from this, never
# hardcoded, per the spec.
MAX_LOOKBACK = 24


def _rsi(closes: list[float], i: int, period: int = 14) -> float | None:
    if i < period:
        return None
    gains, losses = 0.0, 0.0
    for j in range(i - period + 1, i + 1):
        change = closes[j] - closes[j - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def build_feature_matrix(
    candles: list[dict],
    events: list[dict],
    fast_series: list[Decimal],
    slow_series: list[Decimal],
    fast_n: int,
    slow_n: int,
) -> tuple[list[list[float]], list[int], list[dict]]:
    """Return (X, y, kept_events) with rows aligned to kept_events."""
    closes = [float(c["c"]) for c in candles]
    highs = [float(c["h"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    opens = [float(c["o"]) for c in candles]
    vols = [float(c["v"]) for c in candles]
    n = len(candles)

    atr = atr_series(candles, 14)

    # contiguity: index of the first bar of the current unbroken run
    contiguous_from = [0] * n
    for i in range(1, n):
        gap = (candles[i]["t"] - candles[i - 1]["t"]).total_seconds() != 3600
        contiguous_from[i] = i if gap else contiguous_from[i - 1]

    def ret(i: int, k: int) -> float | None:
        j = i - k
        if j < 0 or closes[j] <= 0:
            return None
        return closes[i] / closes[j] - 1.0

    X: list[list[float]] = []  # noqa: N806
    y: list[int] = []
    kept: list[dict] = []

    for e in events:
        i = e["signal_idx"]
        # every lookback must sit inside one contiguous run
        if i - MAX_LOOKBACK < 0 or contiguous_from[i] > i - MAX_LOOKBACK:
            continue
        fi, si = i - (fast_n - 1), i - (slow_n - 1)
        if fi < 1 or si < 1:
            continue
        a = atr[i]
        if a is None or closes[i] <= 0:
            continue

        r1, r3, r6, r12, r24 = (ret(i, k) for k in (1, 3, 6, 12, 24))
        if None in (r1, r3, r6, r12, r24):
            continue
        rsi = _rsi(closes, i)
        if rsi is None:
            continue

        f_now, s_now = float(fast_series[fi]), float(slow_series[si])
        f_prev = float(fast_series[fi - 1])
        window = closes[i - 23 : i + 1]
        rets24 = [
            closes[j] / closes[j - 1] - 1.0 for j in range(i - 23, i + 1) if closes[j - 1] > 0
        ]
        vol_window = vols[i - 23 : i + 1]
        vol_mean = statistics.fmean(vol_window)
        vol_sd = statistics.pstdev(vol_window)
        rng = highs[i] - lows[i]

        row = [
            r1,
            r3,
            r6,
            r12,
            r24,
            (f_now - s_now) / closes[i],  # ema_distance
            (f_now - f_prev) / closes[i],  # ema_slope
            rsi / 100.0,
            float(a) / closes[i],  # atr_pct
            statistics.pstdev(rets24) if len(rets24) > 1 else 0.0,
            rng / closes[i],  # candle_range
            abs(closes[i] - opens[i]) / closes[i],  # body_size
            (vols[i] / vols[i - 1] - 1.0) if vols[i - 1] > 0 else 0.0,
            ((vols[i] - vol_mean) / vol_sd) if vol_sd > 0 else 0.0,
            (closes[i] / max(window) - 1.0) if max(window) > 0 else 0.0,
        ]
        if any(v != v or v in (float("inf"), float("-inf")) for v in row):  # NaN/inf guard
            continue

        X.append(row)
        y.append(1 if e["profitable"] else 0)
        kept.append(e)

    return X, y, kept
