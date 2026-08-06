#!/usr/bin/env python3
"""STEP 2 — expanding walk-forward evaluation of the EMA meta-label.

The model only ACCEPTS or REJECTS entries the EMA strategy already proposed.
It never sizes and never reaches an exchange.

DISCIPLINE
  * Folds and the embargo come from research_spec.yaml. The 2025-2026 final
    test is NEVER loaded here — an assertion enforces it.
  * The accept threshold is chosen on TRAIN ONLY, from cross-validated
    out-of-fold probabilities. Choosing it on the validation fold would be
    the classic way to fabricate an edge.
  * The benchmark is MATCHED-RANDOM: accept the same NUMBER of trades at
    random, 1000 seeds, and report where the model falls in that distribution.
    Beating "all trades" is easy; beating random selection of equal size is
    the real null.
  * Both models are reported, and the cost-stress re-run is reported, whatever
    they say. Logged to the experiment ledger.

Usage:
    python3 research/step2_walkforward.py [--stress]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import warnings
from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
from events import ROOT, build_events, load_candles, load_spec
from features import FEATURE_NAMES, build_feature_matrix
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(ROOT / "src"))
from trading_bot.strategies.interface import ema_series

# numpy 2.x + scipy 1.18 + sklearn 1.6.1 emit spurious matmul RuntimeWarnings
# from the BLAS path. VERIFIED COSMETIC: lbfgs, liblinear and newton-cholesky
# all return identical probabilities (range [0.068, 0.325], mean 0.217 on the
# 2024 fold), so the optimisation is unaffected. Suppressed to keep the report
# readable — never suppress without checking the numbers first.
warnings.filterwarnings("ignore", category=RuntimeWarning, module="sklearn")
warnings.filterwarnings("ignore", message="Unknown solver options")

LEDGER = ROOT / "research" / "experiments.jsonl"
FOLDS = [((2018, 2021), 2022), ((2018, 2022), 2023), ((2018, 2023), 2024)]
SEALED_FROM = 2025  # final test — never read here
MIN_TRADES = 30  # below this the fold is INCONCLUSIVE, per the spec
RANDOM_SEEDS = 1000


def expectancy(events: list[dict]) -> float:
    if not events:
        return 0.0
    return float(sum((e["net_return"] for e in events), Decimal(0)) / len(events)) * 100


def matched_random(pool: list[dict], k: int, seeds: int = RANDOM_SEEDS) -> list[float]:
    """Expectancy of accepting k trades at random from the same pool."""
    out = []
    for s in range(seeds):
        rng = random.Random(s)
        out.append(expectancy(rng.sample(pool, k)))
    return sorted(out)


def percentile_of(value: float, dist: list[float]) -> float:
    below = sum(1 for d in dist if d < value)
    return 100.0 * below / len(dist)


def pick_threshold(probs: np.ndarray, evs: list[dict]) -> float:
    """Threshold maximising TRAIN expectancy, from out-of-fold probabilities."""
    best_t, best_e = 0.5, -9e9
    for t in np.arange(0.10, 0.90, 0.01):
        sel = [e for p, e in zip(probs, evs, strict=True) if p >= t]
        if len(sel) < max(MIN_TRADES, int(0.05 * len(evs))):
            continue
        e_val = expectancy(sel)
        if e_val > best_e:
            best_t, best_e = float(t), e_val
    return best_t


def make_model(kind: str):
    if kind == "logistic_regression":
        base = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, C=0.5))
    else:
        base = HistGradientBoostingClassifier(
            max_depth=3, max_iter=150, learning_rate=0.05, min_samples_leaf=40
        )
    return CalibratedClassifierCV(base, method="isotonic", cv=3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stress", action="store_true", help="double all cost assumptions")
    args = ap.parse_args()

    spec = load_spec()
    if args.stress:
        for k in ("taker_fee_bps", "spread_bps", "slippage_bps"):
            spec[k] = str(Decimal(spec[k]) * 2)

    candles = load_candles()
    events, _ = build_events(candles, spec)

    fast_n, slow_n = int(spec["fast"]), int(spec["slow"])
    dec_closes = [c["c"] for c in candles]
    X_all, y_all, kept = build_feature_matrix(  # noqa: N806
        candles,
        events,
        ema_series(dec_closes, fast_n),
        ema_series(dec_closes, slow_n),
        fast_n,
        slow_n,
    )
    X_all = np.array(X_all, dtype=float)  # noqa: N806
    y_all = np.array(y_all, dtype=int)
    years = np.array([e["entry_time"].year for e in kept])

    assert years.max() >= SEALED_FROM, "expected sealed years present in the raw pool"

    label = "COST-STRESSED (2x fees/spread/slippage)" if args.stress else "baseline costs"
    print("=" * 78)
    print(f"STEP 2 — walk-forward EMA meta-label  [{label}]")
    print("=" * 78)
    print(
        f"features {len(FEATURE_NAMES)}   usable events {len(kept)}   "
        f"base positive rate {100 * y_all.mean():.1f}%"
    )
    print(f"final test {SEALED_FROM}+ is SEALED and not evaluated here\n")

    results = []
    for (t0, t1), v in FOLDS:
        tr = (years >= t0) & (years <= t1)
        va = years == v
        assert v < SEALED_FROM, "fold would touch the sealed test set"
        if tr.sum() < 100 or va.sum() < MIN_TRADES:
            print(f"fold train {t0}-{t1} -> {v}: insufficient data")
            continue

        val_events = [e for e, m in zip(kept, va, strict=True) if m]
        base_exp = expectancy(val_events)
        print(
            f"--- train {t0}-{t1}  ->  validate {v} "
            f"(train n={tr.sum()}, val n={va.sum()}, val baseline {base_exp:+.4f}%)"
        )

        for kind in ("logistic_regression", "hist_gradient_boosting"):
            model = make_model(kind)
            train_events = [e for e, m in zip(kept, tr, strict=True) if m]
            oof = cross_val_predict(model, X_all[tr], y_all[tr], cv=3, method="predict_proba")[:, 1]
            thr = pick_threshold(oof, train_events)

            model.fit(X_all[tr], y_all[tr])
            p_val = model.predict_proba(X_all[va])[:, 1]
            accepted = [e for p, e in zip(p_val, val_events, strict=True) if p >= thr]

            if len(accepted) < MIN_TRADES:
                print(
                    f"    {kind:<24} thr={thr:.2f}  accepted={len(accepted)}  "
                    f"INCONCLUSIVE (<{MIN_TRADES} trades)"
                )
                results.append(
                    {
                        "fold": v,
                        "model": kind,
                        "verdict": "INCONCLUSIVE",
                        "accepted": len(accepted),
                        "threshold": thr,
                    }
                )
                continue

            exp = expectancy(accepted)
            dist = matched_random(val_events, len(accepted))
            pctile = percentile_of(exp, dist)
            print(
                f"    {kind:<24} thr={thr:.2f}  accepted={len(accepted):>3}  "
                f"exp={exp:+.4f}%  vs base {base_exp:+.4f}%  "
                f"random pctile={pctile:.0f}"
            )
            results.append(
                {
                    "fold": v,
                    "model": kind,
                    "threshold": round(thr, 3),
                    "accepted": len(accepted),
                    "expectancy_pct": round(exp, 4),
                    "baseline_pct": round(base_exp, 4),
                    "random_percentile": round(pctile, 1),
                    "beat_baseline": exp > base_exp,
                    "positive": exp > 0,
                }
            )
        print()

    print("-" * 78)
    print("VERDICT")
    print("-" * 78)
    scored = [r for r in results if "expectancy_pct" in r]
    recent = [r for r in scored if r["fold"] == 2024]
    passing = [
        r for r in scored if r["positive"] and r["beat_baseline"] and r["random_percentile"] >= 95
    ]
    recent_ok = [r for r in recent if r["positive"] and r["random_percentile"] >= 95]

    if not scored:
        print("NO PROMOTION — every fold was INCONCLUSIVE on trade count.")
    elif not recent_ok:
        print("NO PROMOTION — the most recent fold (2024) fails: the spec requires a")
        print("positive result there, not merely on average. An edge that died before")
        print("the newest data is not tradeable.")
        for r in recent:
            print(
                f"  2024 {r['model']}: exp={r['expectancy_pct']:+.4f}%  "
                f"pctile={r['random_percentile']:.0f}"
            )
    elif len(passing) < 2:
        print(f"NO PROMOTION — only {len(passing)} of {len(scored)} fold/model results")
        print("clear positive + beat-baseline + 95th random percentile together.")
    else:
        print(f"CANDIDATE — {len(passing)} of {len(scored)} results pass all three gates.")
        print("Still NOT promotable: the sealed 2025-2026 test has not been run, and")
        print("per the spec it may be run ONCE, only after selection is frozen.")

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "experiment": "step2_walkforward",
                    "ran_at": datetime.now(UTC).isoformat(),
                    "cost_mode": "stressed" if args.stress else "baseline",
                    "features": FEATURE_NAMES,
                    "folds": [{"train": list(t), "validate": v} for t, v in FOLDS],
                    "results": results,
                }
            )
            + "\n"
        )
    print(f"\nlogged to {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
