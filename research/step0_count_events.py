#!/usr/bin/env python3
"""STEP 0 — the go/no-go gate, before a single feature is written.

Under EMA meta-labelling the binding constraint is NOT the number of candles,
it is the number of EMA entry events the model would ever get to judge. If
eight years of history yields only a few hundred, the approach is statistically
dead and no amount of feature engineering fixes it — better to learn that in a
day than after weeks of modelling.

This script answers exactly that, and nothing more:
  * how many tradeable EMA entry events exist (position-gated, as runtime does);
  * what fraction are profitable under the EXECUTION-CONGRUENT label;
  * how they are distributed across years (event supply must not be a
    2018-only artefact);
  * a crude effective-sample estimate from observed holding periods.

The label is whichever RUNTIME exit fires first — protective stop or EMA exit.
There is deliberately no take-profit and no timeout, because the running bot
has neither (see research_spec.yaml). Costs, ambiguity policy and EMA
parameters all come from that spec.

It imports the real ema_series from trading_bot so the event definition cannot
silently drift from the deployed strategy.

Usage:
    python3 research/step0_count_events.py
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_bot.strategies.interface import ema_series  # noqa: E402

DATA = ROOT / "research" / "data" / "BTCUSDT-1h.csv"
OBSERVED_THROUGH = 2024  # later years are burned; never summarise them here
SPEC = ROOT / "research" / "research_spec.yaml"
BPS = Decimal(10000)


def load_spec() -> dict:
    """Minimal reader for the handful of scalars we need (no yaml dependency)."""
    text = SPEC.read_text(encoding="utf-8")
    out: dict = {}
    for line in text.splitlines():
        s = line.strip()
        for key in (
            "fast:",
            "slow:",
            "stop_loss_pct:",
            "taker_fee_bps:",
            "spread_bps:",
            "slippage_bps:",
        ):
            if s.startswith(key):
                value = s.split(":", 1)[1]
                if " #" in value:  # strip inline comment before parsing
                    value = value.split(" #", 1)[0]
                out[key.rstrip(":")] = value.strip().strip('"')
    return out


def load_candles() -> list[dict]:
    if not DATA.exists():
        raise SystemExit(f"missing {DATA}\nRun: python3 research/import_binance.py")
    rows = []
    with DATA.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append(
                {
                    "t": datetime.fromisoformat(r["open_time"]),
                    "o": Decimal(r["open"]),
                    "h": Decimal(r["high"]),
                    "low": Decimal(r["low"]),
                    "c": Decimal(r["close"]),
                }
            )
    return rows


def main() -> int:
    spec = load_spec()
    fast_n, slow_n = int(spec["fast"]), int(spec["slow"])
    stop_pct = Decimal(spec["stop_loss_pct"])
    fee = Decimal(spec["taker_fee_bps"])
    half_spread = Decimal(spec["spread_bps"]) / 2
    slip = Decimal(spec["slippage_bps"])

    candles = load_candles()
    closes = [c["c"] for c in candles]
    n = len(candles)
    if n < slow_n + 3:
        raise SystemExit("not enough candles")

    # One-pass EMAs. ema_series output aligns to values[period-1:].
    fast = ema_series(closes, fast_n)
    slow = ema_series(closes, slow_n)

    def diff_at(i: int) -> Decimal | None:
        fi, si = i - (fast_n - 1), i - (slow_n - 1)
        if fi < 0 or si < 0:
            return None
        return fast[fi] - slow[si]

    # A gap means the previous bar is not exactly one hour earlier. Any event
    # whose window crosses one is discarded rather than silently bridged.
    contiguous_from = [0] * n
    for i in range(1, n):
        gap = (candles[i]["t"] - candles[i - 1]["t"]).total_seconds() != 3600
        contiguous_from[i] = i if gap else contiguous_from[i - 1]

    entry_cost = (half_spread + slip) / BPS
    exit_cost = (half_spread + slip) / BPS
    round_trip_fees = (fee * 2) / BPS

    events = []
    discarded_gap = 0
    i = slow_n
    while i < n - 1:
        d_now, d_prev = diff_at(i), diff_at(i - 1)
        if d_now is None or d_prev is None:
            i += 1
            continue
        if not (d_prev <= 0 < d_now):  # edge-triggered cross up, flat only
            i += 1
            continue

        # Entry fills on the NEXT candle's executable ask, never this close.
        entry_idx = i + 1
        eff_entry = candles[entry_idx]["o"] * (Decimal(1) + entry_cost)
        stop_price = eff_entry * (Decimal(1) - stop_pct / Decimal(100))

        exit_idx, exit_px, reason = None, None, None
        j = entry_idx
        while j < n - 1:
            if contiguous_from[j] > entry_idx:  # a gap opened inside the trade
                break
            bar = candles[j]
            if j > entry_idx and bar["low"] <= stop_price:
                # Ambiguity policy: stop wins ties; a gap-through fills at the
                # next executable bid (the open), not at the stop price.
                exit_px = bar["o"] if bar["o"] < stop_price else stop_price
                exit_idx, reason = j, "stop"
                break
            dj, dj_prev = diff_at(j), diff_at(j - 1)
            if dj is not None and dj_prev is not None and j > entry_idx:
                if (dj_prev >= 0 > dj) or dj < 0:  # EMA exit / safety net
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
                "entry_time": candles[entry_idx]["t"],
                "hold_hours": exit_idx - entry_idx,
                "exit_reason": reason,
                "net_return": net,
                "profitable": net > 0,
            }
        )
        # Runtime cannot re-enter while holding: resume scanning after the exit.
        i = max(exit_idx, i + 1)

    if not events:
        raise SystemExit("no events found — check the data range")

    # SCOPE. This script originally reported expectancy, profit factor and
    # buy-and-hold across ALL events, including 2025-2026 — which BURNED that
    # period as a holdout before any model was fitted. Analysis is now limited
    # to OBSERVED_THROUGH, and the excluded count is stated rather than hidden.
    # (The damage is historical and cannot be undone: a genuine holdout now
    # accrues prospectively from 2026-08. See research_spec.yaml.)
    burned = [e for e in events if e["entry_time"].year > OBSERVED_THROUGH]
    events = [e for e in events if e["entry_time"].year <= OBSERVED_THROUGH]

    wins = sum(1 for e in events if e["profitable"])
    win_rets = [e["net_return"] for e in events if e["profitable"]]
    loss_rets = [e["net_return"] for e in events if not e["profitable"]]
    total_net = sum(e["net_return"] for e in events)
    expectancy = total_net / len(events)
    avg_win = (sum(win_rets) / len(win_rets)) if win_rets else Decimal(0)
    avg_loss = (sum(loss_rets) / len(loss_rets)) if loss_rets else Decimal(0)
    gross_win = sum(win_rets)
    gross_loss = -sum(loss_rets)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else Decimal(0)
    holds = [e["hold_hours"] for e in events]
    avg_hold = sum(holds) / len(holds)
    by_year = Counter(e["entry_time"].year for e in events)
    by_reason = Counter(e["exit_reason"] for e in events)
    span_days = (candles[-1]["t"] - candles[0]["t"]).days

    print("=" * 62)
    print("STEP 0 — EMA meta-label event supply")
    print("=" * 62)
    print(f"candle rows              : {n}")
    print(
        f"scope                    : events through {OBSERVED_THROUGH} "
        f"({len(burned)} later events excluded, unreported)"
    )
    print(
        f"data span                : {candles[0]['t'].date()} -> {candles[-1]['t'].date()}"
        f"  ({span_days} days)"
    )
    print(f"tradeable EMA events     : {len(events)}")
    print(f"  discarded (data gap)   : {discarded_gap}")
    print(f"events per year (mean)   : {len(events) / max(span_days / 365.25, 0.01):.1f}")
    print(f"profitable after costs   : {wins} / {len(events)}  ({100 * wins / len(events):.1f}%)")
    print(
        f"avg holding period       : {avg_hold:.1f} h   (median {sorted(holds)[len(holds)//2]} h)"
    )
    print(f"exit reasons             : {dict(by_reason)}")
    print("\n--- EMA baseline economics (the benchmark a model must beat) ---")
    print(f"expectancy / trade       : {expectancy * 100:+.3f}%  after costs")
    print(f"sum of net returns       : {total_net * 100:+.1f}%  over {len(events)} trades")
    print(f"avg winner / avg loser   : {avg_win * 100:+.2f}% / {avg_loss * 100:+.2f}%")
    print(f"profit factor            : {profit_factor:.3f}   (>1 = profitable)")
    print("\nevents by year:")
    for year in sorted(by_year):
        print(f"  {year}: {by_year[year]:>4}")

    # Effective sample: overlapping outcomes are the concern, but these events
    # are non-overlapping by construction (no re-entry while holding), so the
    # count IS the independent sample. The real limit is the smallest fold.
    print("\n" + "-" * 62)
    print("READ THIS BEFORE BUILDING FEATURES")
    print("-" * 62)
    verdict = "GO" if len(events) >= 800 else ("MARGINAL" if len(events) >= 300 else "NO-GO")
    print(f"verdict: {verdict}")
    if verdict == "NO-GO":
        print("  Too few events to learn an accept/reject rule. Do not proceed to")
        print("  features; revisit the interval or the entry rule instead.")
    elif verdict == "MARGINAL":
        print("  Enough for logistic regression only, with few features. Histogram")
        print("  gradient boosting would almost certainly overfit this sample.")
    else:
        print("  Sufficient supply to proceed to leakage-safe features.")
    print(f"  Positive rate {100 * wins / len(events):.1f}% is the base rate the model must beat")
    print("  by enough to cover its own selectivity — not 50%.")

    out = ROOT / "research" / "data" / "step0_events.json"
    out.write_text(
        json.dumps(
            {
                "candle_rows": n,
                "events": len(events),
                "discarded_gap": discarded_gap,
                "profitable": wins,
                "positive_rate": float(wins / len(events)),
                "avg_hold_hours": float(avg_hold),
                "by_year": dict(sorted(by_year.items())),
                "by_exit_reason": dict(by_reason),
                "verdict": verdict,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
