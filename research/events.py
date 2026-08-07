"""Shared EMA meta-label event construction.

Single source of truth for "what is an event and what is its label", imported
by every research step so the definition cannot drift between them. The EMA
itself comes from trading_bot, so it cannot drift from the deployed strategy
either.

Label = whichever RUNTIME exit fires first (protective stop or EMA exit).
No take-profit, no timeout — the running bot has neither. Ambiguity policy
from research_spec.yaml: stop wins ties, gap-through fills at the open,
events spanning a data gap are discarded.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_bot.strategies.interface import ema_series  # noqa: E402

DATA = ROOT / "research" / "data" / "BTCUSDT-1h.csv"
SPEC = ROOT / "research" / "research_spec.yaml"
BPS = Decimal(10000)


class SpecViolation(RuntimeError):
    """The spec and the code disagree. Fail closed rather than run anyway."""


def load_spec() -> dict:
    """Parse research_spec.yaml in full.

    Previously this scraped six scalars line-by-line, so every other value
    (folds, cutoff, embargo, minimum trades, seed count) lived as a duplicated
    constant in the scripts. A spec edit could then be hash-stamped into the
    ledger while being silently ignored. Callers now read their parameters
    from here, so the stamped spec is the one that actually ran.

    Flat aliases for the hot scalars are kept so existing callers still work;
    the full document is available under ``_raw``.
    """
    raw = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    try:
        flat = {
            "fast": raw["strategy"]["params"]["fast"],
            "slow": raw["strategy"]["params"]["slow"],
            "stop_loss_pct": raw["stop_loss_pct"],
            "taker_fee_bps": raw["costs"]["taker_fee_bps"],
            "spread_bps": raw["costs"]["spread_bps"],
            "slippage_bps": raw["costs"]["slippage_bps"],
        }
    except (KeyError, TypeError) as exc:
        raise SpecViolation(f"research_spec.yaml missing required key: {exc}") from exc
    return {**flat, "_raw": raw}


def holdout_start(spec: dict) -> datetime:
    """First instant of the prospective holdout. Data at/after this is off limits."""
    raw = spec.get("_raw") or {}
    value = (raw.get("holdout") or {}).get("prospective_start")
    if not value:
        raise SpecViolation("research_spec.yaml: holdout.prospective_start is required")
    return datetime.fromisoformat(f"{value}T00:00:00+00:00")


def load_candles(spec: dict | None = None, include_holdout: bool = False) -> list[dict]:
    """Load candles, PHYSICALLY truncated before the prospective holdout.

    Filtering after event construction is not a holdout: outcomes over the
    protected period get computed first and only then discarded, which is
    exactly how a holdout leaks into a researcher's head. Truncation happens
    here, before any event, label or feature exists.

    ``include_holdout=True`` is reserved for the single permitted final
    evaluation and announces itself loudly.
    """
    if not DATA.exists():
        raise SystemExit(f"missing {DATA}\nRun: python3 research/import_binance.py")
    cutoff = None if include_holdout else holdout_start(spec or load_spec())
    if include_holdout:
        print("!! LOADING THE PROSPECTIVE HOLDOUT — permitted ONCE, after selection !!")

    rows, excluded = [], 0
    with DATA.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            ts = datetime.fromisoformat(r["open_time"])
            if cutoff is not None and ts >= cutoff:
                excluded += 1
                continue
            rows.append(
                {
                    "t": ts,
                    "o": Decimal(r["open"]),
                    "h": Decimal(r["high"]),
                    "low": Decimal(r["low"]),
                    "c": Decimal(r["close"]),
                    "v": Decimal(r["volume"]),
                }
            )
    if excluded:
        print(f"[holdout] {excluded} candle(s) at/after {cutoff.date()} not loaded")
    return rows


def atr_series(candles: list[dict], period: int = 14) -> list[Decimal | None]:
    """Wilder-style ATR aligned to candles (None until warmed up).

    True range uses the previous close, so atr[i] is fully known at the close
    of candle i — safe to use for a decision made on that bar.
    """
    trs: list[Decimal] = [candles[0]["h"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        prev_close = candles[i - 1]["c"]
        hi, lo = candles[i]["h"], candles[i]["low"]
        trs.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
    out: list[Decimal | None] = [None] * len(candles)
    if len(trs) < period:
        return out
    running = sum(trs[:period], Decimal(0)) / Decimal(period)
    out[period - 1] = running
    for i in range(period, len(trs)):
        running = (running * Decimal(period - 1) + trs[i]) / Decimal(period)
        out[i] = running
    return out


def build_events(
    candles: list[dict],
    spec: dict,
    stop_pct_at=None,
) -> tuple[list[dict], int]:
    """Return (events, discarded_for_gap).

    Each event carries signal_idx (the last CLOSED candle at decision time —
    the only bar features may use) and entry_idx (the fill bar).

    ``stop_pct_at``: optional callable(signal_idx) -> Decimal stop percentage,
    for testing volatility-scaled stops. Defaults to the flat stop_loss_pct in
    the spec. Returning None skips the event (e.g. ATR not warmed up).
    """
    fast_n, slow_n = int(spec["fast"]), int(spec["slow"])
    stop_pct = Decimal(spec["stop_loss_pct"])
    fee = Decimal(spec["taker_fee_bps"])
    half_spread = Decimal(spec["spread_bps"]) / 2
    slip = Decimal(spec["slippage_bps"])

    closes = [c["c"] for c in candles]
    n = len(candles)
    fast = ema_series(closes, fast_n)
    slow = ema_series(closes, slow_n)

    def diff_at(i: int) -> Decimal | None:
        fi, si = i - (fast_n - 1), i - (slow_n - 1)
        if fi < 0 or si < 0:
            return None
        return fast[fi] - slow[si]

    contiguous_from = [0] * n
    for i in range(1, n):
        gap = (candles[i]["t"] - candles[i - 1]["t"]).total_seconds() != 3600
        contiguous_from[i] = i if gap else contiguous_from[i - 1]

    entry_cost = (half_spread + slip) / BPS
    exit_cost = (half_spread + slip) / BPS
    round_trip_fees = (fee * 2) / BPS

    events: list[dict] = []
    discarded_gap = 0
    i = slow_n
    while i < n - 1:
        d_now, d_prev = diff_at(i), diff_at(i - 1)
        if d_now is None or d_prev is None or not (d_prev <= 0 < d_now):
            i += 1
            continue

        entry_idx = i + 1
        eff_entry = candles[entry_idx]["o"] * (Decimal(1) + entry_cost)
        this_stop_pct = stop_pct if stop_pct_at is None else stop_pct_at(i)
        if this_stop_pct is None or this_stop_pct <= 0:
            i += 1
            continue
        stop_price = eff_entry * (Decimal(1) - this_stop_pct / Decimal(100))

        exit_idx, exit_px, reason = None, None, None
        j = entry_idx
        while j < n - 1:
            if contiguous_from[j] > entry_idx:
                break
            bar = candles[j]
            if j > entry_idx and bar["low"] <= stop_price:
                exit_px = bar["o"] if bar["o"] < stop_price else stop_price
                exit_idx, reason = j, "stop"
                break
            dj, dj_prev = diff_at(j), diff_at(j - 1)
            if dj is not None and dj_prev is not None and j > entry_idx:
                if (dj_prev >= 0 > dj) or dj < 0:
                    exit_idx, exit_px, reason = j + 1, candles[j + 1]["o"], "ema_exit"
                    break
            j += 1

        if exit_idx is None or exit_px is None:
            discarded_gap += 1
            i += 1
            continue

        eff_exit = exit_px * (Decimal(1) - exit_cost)
        net = (eff_exit / eff_entry) - Decimal(1) - round_trip_fees
        events.append(
            {
                "signal_idx": i,
                "entry_idx": entry_idx,
                "entry_time": candles[entry_idx]["t"],
                "stop_pct": this_stop_pct,
                "hold_hours": exit_idx - entry_idx,
                "exit_reason": reason,
                "net_return": net,
                "profitable": net > 0,
            }
        )
        i = max(exit_idx, i + 1)

    return events, discarded_gap
