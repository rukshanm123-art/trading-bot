#!/usr/bin/env python3
"""STEP 1 — does a volatility-scaled stop fix the mechanism step 0b exposed?

Step 0b showed the fixed 2% stop sits 6.7 hourly sigma away in calm regimes
but only 2.9 in volatile ones, so it is hit by noise before a trade can
develop (stop-outs 14.8% -> 43.5%). An ATR-scaled stop adapts automatically.
If that is the real defect, scaling should lift expectancy WITHOUT any
classifier.

DISCIPLINE
  * Train years only (<= 2024). 2025-2026 stays sealed.
  * The k grid is fixed in advance and EVERY value is reported. No searching
    for a k that happens to work.
  * Changing the stop changes the labels, so this is a NEW experiment series
    -- it does not reuse step-0 numbers except as the baseline row.
  * Logged to the experiment ledger.

SIZING IS PART OF THE TEST, NOT AN AFTERTHOUGHT. Under risk-based sizing a
wider stop means a SMALLER position (risk_budget / stop_distance), which can
fall below the exchange minimum. A k that improves expectancy but cannot be
traded at the intended live equity is not a win. Feasibility is checked with
the production size_entry, not an approximation.

Usage:
    python3 research/step1_atr_stop.py
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import UTC, datetime
from decimal import Decimal

from events import ROOT, atr_series, build_events, load_candles, load_spec

sys.path.insert(0, str(ROOT / "src"))

from trading_bot.config.loader import load_config
from trading_bot.core.models import SymbolRules
from trading_bot.core.types import dec
from trading_bot.risk.sizing import SizingInputs, size_entry

LEDGER = ROOT / "research" / "experiments.jsonl"
TRAIN_MAX_YEAR = 2024
ATR_PERIOD = 14

# Pre-committed grid. Reported in full, whatever the outcome.
K_GRID = [Decimal("1.5"), Decimal("2.0"), Decimal("2.5"), Decimal("3.0"), Decimal("4.0")]

# Guard rails so a volatility spike cannot produce an absurd stop.
MIN_STOP_PCT = Decimal("0.5")
MAX_STOP_PCT = Decimal("12.0")

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


def evaluate(events: list[dict], risk, equity: Decimal, candles: list[dict]) -> dict:
    """Expectancy plus how many of these trades the live account could size."""
    train = [e for e in events if e["entry_time"].year <= TRAIN_MAX_YEAR]
    if not train:
        return {"n": 0}
    wins = [e for e in train if e["profitable"]]
    total = sum((e["net_return"] for e in train), Decimal(0))
    stops = sum(1 for e in train if e["exit_reason"] == "stop")

    tradeable = 0
    for e in train:
        res = size_entry(
            SizingInputs(
                equity=equity,
                quote_free=equity,
                est_entry_price=candles[e["signal_idx"]]["c"],
                rules=RULES,
                risk=risk,
                stop_loss_pct=e["stop_pct"],
                fee_bps=Decimal(10),
            )
        )
        if res.ok:
            tradeable += 1

    # Expectancy over trades the account could ACTUALLY take is the number
    # that matters; an untradeable edge is worth zero.
    return {
        "n": len(train),
        "win_rate_pct": round(100 * len(wins) / len(train), 1),
        "expectancy_pct": round(float(total / len(train)) * 100, 4),
        "stop_rate_pct": round(100 * stops / len(train), 1),
        "median_stop_pct": round(float(statistics.median(e["stop_pct"] for e in train)), 2),
        "tradeable_at_equity": tradeable,
        "tradeable_pct": round(100 * tradeable / len(train), 1),
    }


def main() -> int:
    spec = load_spec()
    candles = load_candles()
    cfg = load_config(str(ROOT / "config" / "paper.yaml"))
    equity = Decimal("35")  # intended live equity, per research_spec.yaml

    atr = atr_series(candles, ATR_PERIOD)
    closes = [c["c"] for c in candles]

    print("=" * 78)
    print("STEP 1 — ATR-scaled stop sweep (TRAIN YEARS ONLY, <= 2024)")
    print("=" * 78)
    print(f"equity assumption {equity} USDT   min_notional {RULES.min_notional} USDT")
    print(
        f"risk/trade {cfg.risk.max_risk_per_trade_pct}%   alloc cap "
        f"{cfg.risk.max_position_allocation_pct}%\n"
    )
    header = (
        f"{'stop rule':<18}{'n':>5}{'win%':>7}{'stop-out%':>11}{'med stop':>10}"
        f"{'expectancy':>13}{'tradeable':>11}"
    )
    print(header)
    print("-" * 78)

    results = []

    baseline_events, _ = build_events(candles, spec)
    base = evaluate(baseline_events, cfg.risk, equity, candles)
    base["rule"] = "flat 2.0% (live)"
    results.append(base)
    print(
        f"{base['rule']:<18}{base['n']:>5}{base['win_rate_pct']:>6}%{base['stop_rate_pct']:>10}%"
        f"{base['median_stop_pct']:>9}%{base['expectancy_pct']:>+12.4f}%"
        f"{base['tradeable_pct']:>10}%"
    )

    for k in K_GRID:

        def stop_pct_at(i: int, _k: Decimal = k) -> Decimal | None:
            a = atr[i]
            if a is None or closes[i] <= 0:
                return None
            pct = _k * a / closes[i] * Decimal(100)
            if pct < MIN_STOP_PCT or pct > MAX_STOP_PCT:
                return None
            return pct

        evs, _ = build_events(candles, spec, stop_pct_at=stop_pct_at)
        r = evaluate(evs, cfg.risk, equity, candles)
        r["rule"] = f"{k} x ATR{ATR_PERIOD}"
        results.append(r)
        print(
            f"{r['rule']:<18}{r['n']:>5}{r['win_rate_pct']:>6}%{r['stop_rate_pct']:>10}%"
            f"{r['median_stop_pct']:>9}%{r['expectancy_pct']:>+12.4f}%"
            f"{r['tradeable_pct']:>10}%"
        )

    print("-" * 78)
    print("VERDICT")
    print("-" * 78)
    improved = [r for r in results[1:] if r["expectancy_pct"] > base["expectancy_pct"]]
    positive = [r for r in results[1:] if r["expectancy_pct"] > 0]
    viable = [r for r in positive if r["tradeable_pct"] >= 50]

    if viable:
        best = max(viable, key=lambda r: r["expectancy_pct"])
        print(
            f"{len(positive)} of {len(K_GRID)} k values reach POSITIVE expectancy, "
            f"{len(viable)} of those remain tradeable at {equity} USDT."
        )
        print(
            f"best viable: {best['rule']}  expectancy {best['expectancy_pct']:+.4f}%  "
            f"tradeable {best['tradeable_pct']}%"
        )
        print("\nThe mechanism was the stop, not the entry. Meta-labelling now has a")
        print("profitable base to filter rather than a losing one to rescue.")
    elif positive:
        print(f"{len(positive)} k value(s) turn expectancy positive, but NONE stays")
        print(f"tradeable at {equity} USDT — a wider stop shrinks the position below")
        print("the exchange minimum. The edge exists but this account cannot take it.")
    elif improved:
        print(f"{len(improved)} k value(s) improve on the flat stop but none reaches")
        print("positive expectancy. Scaling helps; it is not sufficient.")
    else:
        print("No k beats the flat 2% stop. Widening DOES cut stop-outs exactly as")
        print("predicted, but expectancy does not follow — so the stopped trades were")
        print("genuinely losing, not noise-stopped winners. The 'stop too tight in")
        print("volatile regimes' mechanism is FALSIFIED.")
        print("\nThis does NOT clear the classifier: step 0b's conditional structure")
        print("(calm/strong-signal entries score better) stands on its own, whatever")
        print("its cause. It does mean the only remaining candidate edge is regime")
        print("SELECTION, and 0b sized that at roughly +0.06%/trade in-sample.")

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "experiment": "step1_atr_stop",
                    "ran_at": datetime.now(UTC).isoformat(),
                    "scope": f"train years <= {TRAIN_MAX_YEAR} (final test sealed)",
                    "atr_period": ATR_PERIOD,
                    "k_grid": [str(k) for k in K_GRID],
                    "equity_usdt": str(equity),
                    "results": results,
                }
            )
            + "\n"
        )
    print(f"\nlogged to {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
