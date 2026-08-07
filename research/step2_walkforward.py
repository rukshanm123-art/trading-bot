#!/usr/bin/env python3
"""STEP 2 — expanding walk-forward evaluation of the EMA meta-label.

The model only ACCEPTS or REJECTS entries the EMA strategy already proposed.
It never sizes and never reaches an exchange.

DISCIPLINE
  * The prospective holdout is excluded PHYSICALLY, in the loader, before any
    event or feature exists — not filtered out afterwards. Filtering after the
    fact still computes outcomes over the protected period first, which is
    exactly how a holdout leaks.
  * 2018-01..2026-07 is BURNED, not sealed: step 0 originally summarised
    outcomes over 2025-2026 before any model was fitted. Folds therefore stop
    at the last validation year in the spec, and the excluded count is stated.
  * PURGING is by each event's actual EXIT timestamp plus an embargo, because
    the label has no timeout and holds run to 285h.
  * Internal CV (threshold selection AND calibration) uses TimeSeriesSplit.
  * The accept threshold is chosen on TRAIN ONLY, from out-of-fold
    probabilities.
  * The benchmark matches the model on trade count AND holding-time
    distribution, so the null has comparable exposure.
  * EVERY parameter is read from research_spec.yaml and validated at startup,
    so the hash-stamped spec is the one that actually ran.

Usage:
    python3 research/step2_walkforward.py [--stress]
"""

from __future__ import annotations

import argparse
import sys
import warnings
from decimal import Decimal

import numpy as np
from events import ROOT, SpecViolation, build_events, load_candles, load_spec
from features import FEATURE_NAMES, build_feature_matrix
from provenance import LEDGER
from provenance import append as provenance_append
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from validation import expectancy, matched_random, percentile_of, purge_train

sys.path.insert(0, str(ROOT / "src"))
from trading_bot.strategies.interface import ema_series

# numpy 2.x + scipy 1.18 + sklearn 1.6.1 emit spurious matmul RuntimeWarnings
# from the BLAS path. VERIFIED COSMETIC: lbfgs, liblinear and newton-cholesky
# all return identical probabilities, so the optimisation is unaffected.
# Never suppress without checking the numbers first.
warnings.filterwarnings("ignore", category=RuntimeWarning, module="sklearn")
warnings.filterwarnings("ignore", message="Unknown solver options")


def spec_params(spec: dict) -> dict:
    """Every knob comes from research_spec.yaml, validated at startup.

    These were previously duplicated as module constants, so a spec edit could
    be hash-stamped into the ledger while the run silently ignored it.
    """
    raw = spec["_raw"]
    try:
        v, promo = raw["validation"], raw["promotion"]
        folds = [
            ((int(f["train"][0][:4]), int(f["train"][1][:4])), int(f["validate"][0][:4]))
            for f in v["folds"]
        ]
        params = {
            "folds": folds,
            "observed_through": max(val for _, val in folds),
            "embargo_hours": int(v["embargo_hours"]),
            "min_trades": int(promo["min_oos_trades_final_fold"]),
            "seeds": int(raw["matched_random"]["seeds"]),
            "required_percentile": float(promo["require_beat_random_percentile"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecViolation(f"research_spec.yaml is unusable: {exc}") from exc
    if v.get("purge_by") != "exit_timestamp":
        raise SpecViolation("spec requires purge_by: exit_timestamp")
    if v.get("internal_cv") != "time_series_split":
        raise SpecViolation("spec requires internal_cv: time_series_split")
    return params


def pick_threshold(probs: np.ndarray, evs: list[dict], min_trades: int) -> float:
    """Threshold maximising TRAIN expectancy, from out-of-fold probabilities."""
    best_t, best_e = 0.5, -9e9
    for t in np.arange(0.10, 0.90, 0.01):
        sel = [e for p, e in zip(probs, evs, strict=True) if p >= t]
        if len(sel) < max(min_trades, int(0.05 * len(evs))):
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


def time_series_oof(model_factory, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-fold probabilities under TimeSeriesSplit.

    cross_val_predict refuses TimeSeriesSplit because it is not a partition:
    the earliest block is only ever training data. That is inherent to honest
    time-series CV, so the threshold is chosen on the subset that DOES get an
    out-of-fold prediction, and indices are returned for alignment.
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stress", action="store_true", help="double all cost assumptions")
    args = ap.parse_args()

    spec = load_spec()
    prm = spec_params(spec)
    if args.stress:
        for k in ("taker_fee_bps", "spread_bps", "slippage_bps"):
            spec[k] = str(Decimal(spec[k]) * 2)

    # The loader truncates before the prospective holdout: nothing after it
    # is read, so no outcome over it can be computed even transiently.
    candles = load_candles(spec)
    events, _ = build_events(candles, spec)

    fast_n, slow_n = int(spec["fast"]), int(spec["slow"])
    dec_closes = [c["c"] for c in candles]
    x_all, y_all, kept = build_feature_matrix(
        candles,
        events,
        ema_series(dec_closes, fast_n),
        ema_series(dec_closes, slow_n),
        fast_n,
        slow_n,
    )
    x_all = np.array(x_all, dtype=float)
    y_all = np.array(y_all, dtype=int)
    years = np.array([e["entry_time"].year for e in kept])

    label = "COST-STRESSED (2x fees/spread/slippage)" if args.stress else "baseline costs"
    print("=" * 78)
    print(f"STEP 2 — walk-forward EMA meta-label  [{label}]")
    print("=" * 78)

    in_scope = years <= prm["observed_through"]
    dropped = int((~in_scope).sum())
    x_all, y_all = x_all[in_scope], y_all[in_scope]
    kept = [e for e, m in zip(kept, in_scope, strict=True) if m]
    years = years[in_scope]

    print(
        f"features {len(FEATURE_NAMES)}   in-scope events {len(kept)}   "
        f"base positive rate {100 * y_all.mean():.1f}%"
    )
    print(
        f"post-{prm['observed_through']} events excluded: {dropped}  "
        f"(burned period — no statistic over it is reported)\n"
    )

    results: list[dict] = []
    for (t0, t1), v in prm["folds"]:
        va = years == v
        tr = np.array(purge_train(kept, (t0, t1), v, prm["embargo_hours"]))
        purged = int(((years >= t0) & (years <= t1)).sum() - tr.sum())
        if purged:
            print(f"    [purge] {purged} train label(s) overlapped validate {v}")
        if tr.sum() < 100 or va.sum() < prm["min_trades"]:
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
            oof_idx, oof = time_series_oof(lambda k=kind: make_model(k), x_all[tr], y_all[tr])
            if len(oof_idx) < prm["min_trades"]:
                print(f"    {kind:<24} insufficient out-of-fold sample for a threshold")
                continue
            thr = pick_threshold(oof, [train_events[i] for i in oof_idx], prm["min_trades"])

            model = make_model(kind)
            model.fit(x_all[tr], y_all[tr])
            p_val = model.predict_proba(x_all[va])[:, 1]
            accepted = [e for pr, e in zip(p_val, val_events, strict=True) if pr >= thr]

            if len(accepted) < prm["min_trades"]:
                print(
                    f"    {kind:<24} thr={thr:.2f}  accepted={len(accepted)}  "
                    f"INCONCLUSIVE (<{prm['min_trades']} trades)"
                )
                results.append(
                    {
                        "fold": v,
                        "model": kind,
                        "verdict": "INCONCLUSIVE",
                        "accepted": len(accepted),
                        "threshold": round(thr, 3),
                    }
                )
                continue

            exp = expectancy(accepted)
            dist = matched_random(val_events, accepted, prm["seeds"])
            pctile = percentile_of(exp, dist)
            print(
                f"    {kind:<24} thr={thr:.2f}  accepted={len(accepted):>3}  "
                f"exp={exp:+.4f}%  vs base {base_exp:+.4f}%  random pctile={pctile:.0f}"
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
    need = prm["required_percentile"]
    scored = [r for r in results if "expectancy_pct" in r]
    newest = max(v for _, v in prm["folds"])
    recent = [r for r in scored if r["fold"] == newest]
    passing = [
        r for r in scored if r["positive"] and r["beat_baseline"] and r["random_percentile"] >= need
    ]
    recent_ok = [r for r in recent if r["positive"] and r["random_percentile"] >= need]

    if not scored:
        print("NO PROMOTION — every fold was INCONCLUSIVE on trade count.")
    elif not recent_ok:
        print(f"NO PROMOTION — the most recent fold ({newest}) fails: the spec requires a")
        print("positive result there, not merely on average. An edge that died before")
        print("the newest data is not tradeable.")
        for r in recent:
            print(
                f"  {newest} {r['model']}: exp={r['expectancy_pct']:+.4f}%  "
                f"pctile={r['random_percentile']:.0f}"
            )
    elif len(passing) < 2:
        print(f"NO PROMOTION — only {len(passing)} of {len(scored)} fold/model results")
        print("clear positive + beat-baseline + the required random percentile together.")
    else:
        print(f"CANDIDATE — {len(passing)} of {len(scored)} results pass all three gates.")
        print("Still NOT promotable: the prospective holdout has not been evaluated,")
        print("and per the spec it may be evaluated ONCE, after selection is frozen.")

    provenance_append(
        {
            "experiment": "step2_walkforward",
            "cost_mode": "stressed" if args.stress else "baseline",
            "features": FEATURE_NAMES,
            "params": {k: str(val) for k, val in prm.items()},
            "internal_cv": "TimeSeriesSplit(3)",
            "benchmark": "count+duration-matched random",
            "scope": f"events through {prm['observed_through']}; later years burned, excluded",
            "results": results,
        }
    )
    print(f"\nlogged to {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
