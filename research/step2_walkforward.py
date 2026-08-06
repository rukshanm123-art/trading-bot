#!/usr/bin/env python3
"""STEP 2 — expanding walk-forward evaluation of the EMA meta-label.

The model only ACCEPTS or REJECTS entries the EMA strategy already proposed.
It never sizes and never reaches an exchange.

DISCIPLINE
  * Events after OBSERVED_THROUGH are DROPPED before anything is computed.
    2025-2026 is BURNED, not sealed: step 0 reported aggregate outcomes over
    it before any model was fitted. An earlier version of this file asserted
    those years were PRESENT and printed a positive rate spanning them, which
    is the opposite of holding them out. A genuine holdout now accrues
    prospectively from 2026-08 (see research_spec.yaml).
  * PURGING is by each event's actual EXIT timestamp plus an embargo, because
    the label has no timeout and holds run up to 285h. Splitting on entry year
    alone leaked labels across the fold boundary.
  * Internal CV (threshold selection AND probability calibration) uses
    TimeSeriesSplit. Ordinary KFold shuffles time inside the training set.
  * The accept threshold is chosen on TRAIN ONLY, from cross-validated
    out-of-fold probabilities. Choosing it on the validation fold would be
    the classic way to fabricate an edge.
  * The benchmark matches the model on trade count AND holding-time
    distribution, so the null has comparable exposure rather than merely
    comparable size.
  * Both models are reported, and the cost-stress re-run is reported, whatever
    they say. Every run is logged with spec/data/feature/git hashes.

Usage:
    python3 research/step2_walkforward.py [--stress]
"""

from __future__ import annotations

import argparse
import random
import sys
import warnings
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
from events import ROOT, build_events, load_candles, load_spec
from features import FEATURE_NAMES, build_feature_matrix
from provenance import LEDGER
from provenance import append as provenance_append
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit
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

FOLDS = [((2018, 2021), 2022), ((2018, 2022), 2023), ((2018, 2023), 2024)]
OBSERVED_THROUGH = 2024  # folds stop here; 2025-2026 is BURNED, not sealed
EMBARGO_HOURS = 48  # applied on top of the exit-timestamp purge
MIN_TRADES = 30  # below this the fold is INCONCLUSIVE, per the spec
RANDOM_SEEDS = 1000


def expectancy(events: list[dict]) -> float:
    if not events:
        return 0.0
    return float(sum((e["net_return"] for e in events), Decimal(0)) / len(events)) * 100


def matched_random(
    pool: list[dict], accepted: list[dict], seeds: int = RANDOM_SEEDS
) -> list[float]:
    """Random selections matched on BOTH count and holding-time distribution.

    Count-only matching lets the null differ from the model in average
    exposure, so a duration-biased selection could beat it for reasons
    unrelated to skill. Here the pool is bucketed by holding-time quartile and
    each draw takes the same number from each bucket as the model did, making
    exposure comparable by construction.
    """
    cuts = sorted(e["hold_hours"] for e in pool)
    q = [cuts[int(len(cuts) * f)] for f in (0.25, 0.5, 0.75)]

    def bucket(e: dict) -> int:
        h = e["hold_hours"]
        return 0 if h <= q[0] else 1 if h <= q[1] else 2 if h <= q[2] else 3

    by_bucket: dict[int, list[dict]] = {0: [], 1: [], 2: [], 3: []}
    for e in pool:
        by_bucket[bucket(e)].append(e)
    want: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}
    for e in accepted:
        want[bucket(e)] += 1

    out = []
    for s in range(seeds):
        rng = random.Random(s)
        draw: list[dict] = []
        for b, n_want in want.items():
            avail = by_bucket[b]
            draw.extend(rng.sample(avail, min(n_want, len(avail))))
        if draw:
            out.append(expectancy(draw))
    return sorted(out)


def percentile_of(value: float, dist: list[float]) -> float:
    below = sum(1 for d in dist if d < value)
    return 100.0 * below / len(dist)


def time_series_oof(model_factory, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-fold probabilities under TimeSeriesSplit.

    cross_val_predict refuses TimeSeriesSplit because it is not a partition:
    the earliest block is only ever training data, so those rows never receive
    an out-of-fold prediction. That is inherent to honest time-series CV, so
    the threshold is chosen on the subset that DOES get one, and the indices
    are returned alongside so the caller can align its events.
    """
    idx_out: list[int] = []
    prob_out: list[float] = []
    for tr_i, te_i in TimeSeriesSplit(n_splits=3).split(x):
        if len(np.unique(y[tr_i])) < 2:
            continue  # a single-class training block cannot fit a classifier
        m = model_factory()
        m.fit(x[tr_i], y[tr_i])
        prob_out.extend(m.predict_proba(x[te_i])[:, 1])
        idx_out.extend(te_i)
    return np.array(idx_out, dtype=int), np.array(prob_out, dtype=float)


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
    # TimeSeriesSplit, not KFold: calibration must not learn from the future
    return CalibratedClassifierCV(base, method="isotonic", cv=TimeSeriesSplit(n_splits=3))


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

    label = "COST-STRESSED (2x fees/spread/slippage)" if args.stress else "baseline costs"
    print("=" * 78)
    print(f"STEP 2 — walk-forward EMA meta-label  [{label}]")
    print("=" * 78)
    # Everything from OBSERVED_THROUGH+1 is DROPPED here. It is burned, not
    # sealed (see research_spec.yaml holdout block) — so it must not be
    # loaded, and no statistic over it may be printed. Previously this code
    # asserted the later years were PRESENT and printed a positive rate across
    # them, which is the opposite of holding them out.
    in_scope = years <= OBSERVED_THROUGH
    dropped = int((~in_scope).sum())
    X_all, y_all = X_all[in_scope], y_all[in_scope]  # noqa: N806
    kept = [e for e, m in zip(kept, in_scope, strict=True) if m]
    years = years[in_scope]
    exits = [e["entry_time"] + timedelta(hours=e["hold_hours"]) for e in kept]

    print(
        f"features {len(FEATURE_NAMES)}   in-scope events {len(kept)}   "
        f"base positive rate {100 * y_all.mean():.1f}%"
    )
    print(
        f"post-{OBSERVED_THROUGH} events excluded: {dropped}  "
        f"(burned period — no statistic over it is reported)\n"
    )

    results = []
    for (t0, t1), v in FOLDS:
        va = years == v
        # PURGE: a train event whose position was still open into the
        # validation fold (or within the embargo before it) shares outcome
        # information with it and must be dropped. Splitting on entry year
        # alone — the previous behaviour — leaks those labels.
        fold_start = datetime(v, 1, 1, tzinfo=UTC)
        embargo_edge = fold_start - timedelta(hours=EMBARGO_HOURS)
        tr = np.array(
            [(t0 <= yr <= t1) and (ex <= embargo_edge) for yr, ex in zip(years, exits, strict=True)]
        )
        purged = int(((years >= t0) & (years <= t1)).sum() - tr.sum())
        if purged:
            print(f"    [purge] {purged} train label(s) overlapped validate {v}")
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
            train_events = [e for e, m in zip(kept, tr, strict=True) if m]
            oof_idx, oof = time_series_oof(lambda k=kind: make_model(k), X_all[tr], y_all[tr])
            if len(oof_idx) < MIN_TRADES:
                print(f"    {kind:<24} insufficient out-of-fold sample for a threshold")
                continue
            thr = pick_threshold(oof, [train_events[i] for i in oof_idx])

            model = make_model(kind)
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
            dist = matched_random(val_events, accepted)
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

    provenance_append(
        {
            "experiment": "step2_walkforward",
            "cost_mode": "stressed" if args.stress else "baseline",
            "features": FEATURE_NAMES,
            "folds": [{"train": list(t), "validate": v} for t, v in FOLDS],
            "purge": {"by": "exit_timestamp", "embargo_hours": EMBARGO_HOURS},
            "internal_cv": "TimeSeriesSplit(3)",
            "benchmark": "count+duration-matched random, 1000 seeds",
            "scope": f"events through {OBSERVED_THROUGH}; later years burned and excluded",
            "results": results,
        }
    )
    print(f"\nlogged to {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
