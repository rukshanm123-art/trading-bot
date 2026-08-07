"""Validation methodology: purging, benchmarking, scoring.

Deliberately stdlib-only and separate from the model fitting, so the parts
that decide whether a result is *honest* can be unit-tested without numpy or
scikit-learn — and so they cannot quietly diverge between scripts.

Every parameter is passed in by the caller, which reads it from
research_spec.yaml. Nothing here carries a default that could silently
disagree with the stamped spec.
"""

from __future__ import annotations

import random
from datetime import UTC as UTC_TZ
from datetime import datetime, timedelta
from decimal import Decimal


def expectancy(events: list[dict]) -> float:
    """Mean net return per trade, in percent."""
    if not events:
        return 0.0
    return float(sum((e["net_return"] for e in events), Decimal(0)) / len(events)) * 100


def exit_time(event: dict) -> datetime:
    return event["entry_time"] + timedelta(hours=event["hold_hours"])


def purge_train(
    events: list[dict],
    train_years: tuple[int, int],
    validate_year: int,
    embargo_hours: int,
) -> list[bool]:
    """Mask of train events that do NOT overlap the validation fold.

    The label has no timeout, so holds are unbounded (observed max 285h). An
    event entered inside the train window but still open into the validation
    fold shares outcome information with it. Splitting on entry year alone —
    the original behaviour — leaks those labels.
    """
    t0, t1 = train_years
    fold_start = datetime(validate_year, 1, 1, tzinfo=UTC_TZ)
    edge = fold_start - timedelta(hours=embargo_hours)
    return [(t0 <= e["entry_time"].year <= t1) and (exit_time(e) <= edge) for e in events]


def duration_buckets(pool: list[dict]) -> tuple[list[int], dict[int, list[dict]]]:
    """Split a pool into holding-time quartiles."""
    cuts = sorted(e["hold_hours"] for e in pool)
    q = [cuts[int(len(cuts) * f)] for f in (0.25, 0.5, 0.75)]
    by_bucket: dict[int, list[dict]] = {0: [], 1: [], 2: [], 3: []}
    for e in pool:
        by_bucket[bucket_of(e, q)].append(e)
    return q, by_bucket


def bucket_of(event: dict, q: list[int]) -> int:
    h = event["hold_hours"]
    return 0 if h <= q[0] else 1 if h <= q[1] else 2 if h <= q[2] else 3


def matched_random(pool: list[dict], accepted: list[dict], seeds: int) -> list[float]:
    """Random selections matched on trade count AND holding-time distribution.

    Count-only matching lets the null differ from the model in average
    exposure, so a duration-biased selection could beat it for reasons
    unrelated to skill. Each draw takes the same number from each holding-time
    quartile as the model did, making exposure comparable by construction.
    """
    q, by_bucket = duration_buckets(pool)
    want: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}
    for e in accepted:
        want[bucket_of(e, q)] += 1

    out: list[float] = []
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
    if not dist:
        return 0.0
    return 100.0 * sum(1 for d in dist if d < value) / len(dist)
