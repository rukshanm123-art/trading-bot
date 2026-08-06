#!/usr/bin/env python3
"""STEP 0b — feasibility probe: can ANY crude regime split move the base rate?

Rationale: the EMA baseline loses money (expectancy -0.172%/trade). Meta-
labelling can only rescue it if trade quality is CONDITIONAL on something
observable at decision time. If four obvious, standard splits cannot move the
22.4% base rate at all, a fifteen-feature model is unlikely to either — and
that is worth knowing before building one.

DISCIPLINE
  * TRAIN YEARS ONLY (<= 2024). The 2025-2026 final test stays sealed; it is
    evaluated once, after all selection, per research_spec.yaml.
  * ALL splits are reported, not the best one. Reporting only the winner is
    exactly how multiple-comparison noise becomes a "discovery".
  * Splits are standard and fixed in advance (trend / direction / volatility /
    signal strength). No searching for a threshold that works.
  * Every run appends to the experiment ledger.

Features are computed from the SIGNAL candle only (the last closed bar at
decision time), so nothing here can see the future.

Usage:
    python3 research/step0b_regime_split.py
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from decimal import Decimal

from events import ROOT, build_events, load_candles, load_spec

from trading_bot.strategies.interface import ema_series

LEDGER = ROOT / "research" / "experiments.jsonl"
TRAIN_MAX_YEAR = 2024  # 2025-2026 is the sealed final test

# Break-even needs expectancy > 0; that is the only bar that matters per subset,
# because the payoff ratio differs between subsets.


def pct(x: Decimal | float) -> float:
    return float(x) * 100


def summarise(name: str, subset: list[dict]) -> dict:
    if not subset:
        return {"name": name, "n": 0}
    wins = [e for e in subset if e["profitable"]]
    total = sum((e["net_return"] for e in subset), Decimal(0))
    return {
        "name": name,
        "n": len(subset),
        "win_rate_pct": round(100 * len(wins) / len(subset), 1),
        "expectancy_pct": round(pct(total / len(subset)), 4),
        "total_pct": round(pct(total), 1),
    }


def main() -> int:
    spec = load_spec()
    candles = load_candles()
    events, _ = build_events(candles, spec)

    train = [e for e in events if e["entry_time"].year <= TRAIN_MAX_YEAR]
    sealed = len(events) - len(train)

    closes = [float(c["c"]) for c in candles]
    n = len(closes)

    # ---- features at the SIGNAL candle only ---------------------------------
    # 200h EMA (trend context), computed once.
    period = 200
    k = 2.0 / (period + 1)
    ema200: list[float] = [statistics.fmean(closes[:period])]
    for v in closes[period:]:
        ema200.append(ema200[-1] + k * (v - ema200[-1]))
    # ema200[x] corresponds to closes[period - 1 + x]

    def ema200_at(i: int) -> float | None:
        x = i - (period - 1)
        return ema200[x] if 0 <= x < len(ema200) else None

    rets = [0.0] + [
        (closes[i] / closes[i - 1] - 1.0) if closes[i - 1] else 0.0 for i in range(1, n)
    ]

    def vol24_at(i: int) -> float | None:
        if i < 24:
            return None
        return statistics.pstdev(rets[i - 23 : i + 1])

    def ret30d_at(i: int) -> float | None:
        j = i - 720  # 30 days of hours
        return (closes[i] / closes[j] - 1.0) if j >= 0 and closes[j] else None

    # EMA separation at signal, as a fraction of price (strength of the cross)
    fast_n, slow_n = int(spec["fast"]), int(spec["slow"])
    dec_closes = [c["c"] for c in candles]
    fast_s = ema_series(dec_closes, fast_n)
    slow_s = ema_series(dec_closes, slow_n)

    def sep_at(i: int) -> float | None:
        fi, si = i - (fast_n - 1), i - (slow_n - 1)
        if fi < 0 or si < 0:
            return None
        return float((fast_s[fi] - slow_s[si]) / dec_closes[i])

    # attach
    usable = []
    for e in train:
        i = e["signal_idx"]
        e2 = dict(e)
        e2["above_ema200"] = (lambda a: None if a is None else closes[i] > a)(ema200_at(i))
        e2["vol24"] = vol24_at(i)
        e2["ret30d"] = ret30d_at(i)
        e2["sep"] = sep_at(i)
        if None in (e2["above_ema200"], e2["vol24"], e2["ret30d"], e2["sep"]):
            continue
        usable.append(e2)

    base = summarise("ALL TRAIN EVENTS", usable)
    med_vol = statistics.median(e["vol24"] for e in usable)
    med_sep = statistics.median(e["sep"] for e in usable)

    splits = [
        (
            "A. trend: price above 200h EMA",
            [e for e in usable if e["above_ema200"]],
            [e for e in usable if not e["above_ema200"]],
            "above / below",
        ),
        (
            "B. direction: trailing 30d return > 0",
            [e for e in usable if e["ret30d"] > 0],
            [e for e in usable if e["ret30d"] <= 0],
            "bull / bear",
        ),
        (
            "C. volatility: 24h realised vol",
            [e for e in usable if e["vol24"] <= med_vol],
            [e for e in usable if e["vol24"] > med_vol],
            "calm / volatile",
        ),
        (
            "D. signal strength: EMA separation",
            [e for e in usable if e["sep"] > med_sep],
            [e for e in usable if e["sep"] <= med_sep],
            "strong / weak",
        ),
    ]

    print("=" * 72)
    print("STEP 0b — regime feasibility probe (TRAIN YEARS ONLY, <= 2024)")
    print("=" * 72)
    print(f"train events : {base['n']}   (sealed 2025-2026 events held out: {sealed})")
    print(
        f"baseline     : win {base['win_rate_pct']}%   "
        f"expectancy {base['expectancy_pct']:+.4f}%   total {base['total_pct']:+.1f}%"
    )
    print("\n{:<40} {:>5} {:>8} {:>13}".format("subset", "n", "win%", "expectancy%"))
    print("-" * 72)

    rows = []
    for title, side_a, side_b, labels in splits:
        la, lb = labels.split(" / ")
        for lbl, subset in ((la, side_a), (lb, side_b)):
            s = summarise(f"{title} [{lbl}]", subset)
            rows.append(s)
            if s["n"]:
                print(
                    f"{title[:28]:<30}{lbl:<10} {s['n']:>5} {s['win_rate_pct']:>7}% "
                    f"{s['expectancy_pct']:>+12.4f}%"
                )
        print()

    positive = [r for r in rows if r.get("n", 0) >= 100 and r.get("expectancy_pct", 0) > 0]
    best = max(
        (r for r in rows if r.get("n", 0) >= 100), key=lambda r: r["expectancy_pct"], default=None
    )

    print("-" * 72)
    print("VERDICT")
    print("-" * 72)
    if positive:
        print(f"{len(positive)} subset(s) with n>=100 show POSITIVE expectancy:")
        for r in positive:
            print(f"  {r['name']}  n={r['n']}  exp={r['expectancy_pct']:+.4f}%")
        print("\nTrade quality IS conditional on observable state -> features are")
        print("worth building. Treat these as HYPOTHESES ONLY: they are in-sample")
        print("and chosen from 8 comparisons, so expect substantial shrinkage.")
    else:
        print("NO subset with n>=100 reaches positive expectancy.")
        if best:
            print(f"best was {best['name']}  n={best['n']}  exp={best['expectancy_pct']:+.4f}%")
        print("\nFour standard splits cannot make these entries profitable even")
        print("IN-SAMPLE. A fifteen-feature model would be fitting noise. The")
        print("honest read is that the EDGE IS NOT IN THE FILTER — it is the entry")
        print("rule itself. Revisit the entry, not the classifier.")

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "experiment": "step0b_regime_split",
                    "ran_at": datetime.now(UTC).isoformat(),
                    "scope": f"train years <= {TRAIN_MAX_YEAR} (final test sealed)",
                    "comparisons": len(rows),
                    "baseline": base,
                    "subsets": rows,
                    "positive_subsets": len(positive),
                }
            )
            + "\n"
        )
    print(f"\nlogged to {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
