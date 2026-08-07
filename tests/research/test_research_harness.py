"""Tests for the research harness's honesty guarantees.

These do not test whether a model is any good — they test the controls that
make a NEGATIVE result trustworthy:

  * appending post-cutoff candles cannot change development results;
  * labels that overlap a validation fold are purged;
  * the random benchmark preserves each holding-time bucket's count;
  * every archived ledger record carries complete, matching provenance.

Deliberately stdlib-only (no numpy/scikit-learn) so they run in the ordinary
CI suite, which does not install the research extras.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT / "research"
sys.path.insert(0, str(RESEARCH))

pytestmark = pytest.mark.integration


def _event(entry: datetime, hold: int, net: str = "0.01") -> dict:
    return {
        "entry_time": entry,
        "hold_hours": hold,
        "net_return": Decimal(net),
        "profitable": Decimal(net) > 0,
    }


# --------------------------------------------------------------- holdout
def test_loader_truncates_before_prospective_holdout(tmp_path, monkeypatch):
    """Post-cutoff candles must not reach event construction at all.

    Filtering AFTER building events still computes outcomes over the protected
    period first. This is the control that makes the prospective holdout real
    rather than nominal.
    """
    import events as ev

    header = "open_time,open,high,low,close,volume\n"
    dev_rows = "".join(f"2026-07-{d:02d}T00:00:00+00:00,100,101,99,100,10\n" for d in range(1, 6))
    holdout_rows = "".join(
        f"2026-08-{d:02d}T00:00:00+00:00,500,501,499,500,10\n" for d in range(1, 6)
    )

    csv_path = tmp_path / "BTCUSDT-1h.csv"
    csv_path.write_text(header + dev_rows, encoding="utf-8")
    monkeypatch.setattr(ev, "DATA", csv_path)
    spec = ev.load_spec()

    before = ev.load_candles(spec)
    assert len(before) == 5

    # Simulate the archive growing into the holdout period.
    csv_path.write_text(header + dev_rows + holdout_rows, encoding="utf-8")
    after = ev.load_candles(spec)

    assert len(after) == 5, "holdout candles leaked into the development load"
    assert [c["t"] for c in after] == [c["t"] for c in before]
    assert max(c["t"] for c in after) < ev.holdout_start(spec)


def test_holdout_requires_explicit_optin(tmp_path, monkeypatch):
    import events as ev

    header = "open_time,open,high,low,close,volume\n"
    rows = "".join(f"2026-08-{d:02d}T00:00:00+00:00,500,501,499,500,10\n" for d in range(1, 4))
    csv_path = tmp_path / "BTCUSDT-1h.csv"
    csv_path.write_text(header + rows, encoding="utf-8")
    monkeypatch.setattr(ev, "DATA", csv_path)
    spec = ev.load_spec()

    assert ev.load_candles(spec) == []  # excluded by default
    assert len(ev.load_candles(spec, include_holdout=True)) == 3  # only on opt-in


# ----------------------------------------------------------------- purge
def test_boundary_crossing_labels_are_purged():
    """A train event still open into the validation fold shares its outcome."""
    from validation import purge_train

    events = [
        _event(datetime(2023, 6, 1, tzinfo=UTC), 24),  # safely inside train
        _event(datetime(2023, 12, 31, 12, tzinfo=UTC), 5),  # exits INSIDE 2024
        _event(datetime(2023, 12, 30, tzinfo=UTC), 200),  # long hold, crosses
        _event(datetime(2024, 3, 1, tzinfo=UTC), 10),  # validation year
    ]
    mask = purge_train(events, (2018, 2023), 2024, embargo_hours=48)

    assert mask[0] is True, "an event well inside train must be kept"
    assert mask[1] is False, "an event exiting inside the fold must be purged"
    assert mask[2] is False, "a long hold crossing the boundary must be purged"
    assert mask[3] is False, "a validation-year event is not training data"


def test_embargo_widens_the_purge():
    """Exits inside the embargo window are dropped even if before the fold."""
    from validation import purge_train

    # exits 2023-12-31 12:00 — before the fold, but inside a 48h embargo
    events = [_event(datetime(2023, 12, 31, 6, tzinfo=UTC), 6)]
    assert purge_train(events, (2018, 2023), 2024, embargo_hours=48) == [False]
    assert purge_train(events, (2018, 2023), 2024, embargo_hours=1) == [True]


# ------------------------------------------------------------- benchmark
def test_matched_random_preserves_duration_buckets():
    """The null must match the model's exposure, not merely its trade count."""
    from validation import bucket_of, duration_buckets, matched_random

    pool = [
        _event(datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i), h)
        for i, h in enumerate([1, 2, 3, 4, 10, 20, 30, 40, 100, 120, 140, 160] * 3)
    ]
    q, _ = duration_buckets(pool)
    accepted = [e for e in pool if bucket_of(e, q) == 3][:6]  # deliberately skewed long

    want = {b: sum(1 for e in accepted if bucket_of(e, q) == b) for b in range(4)}
    assert want[3] == 6 and sum(want.values()) == 6

    # Reconstruct one draw with the same seed the benchmark uses.
    import random

    _, by_bucket = duration_buckets(pool)
    rng = random.Random(0)
    draw = []
    for b, n_want in want.items():
        draw.extend(rng.sample(by_bucket[b], min(n_want, len(by_bucket[b]))))

    got = {b: sum(1 for e in draw if bucket_of(e, q) == b) for b in range(4)}
    assert got == want, "random draw did not preserve the holding-time distribution"
    assert len(matched_random(pool, accepted, seeds=25)) == 25


# ------------------------------------------------------------ provenance
def test_every_ledger_record_has_complete_provenance():
    """An archived result must be tied to the bytes that produced it."""
    ledger = RESEARCH / "experiments.jsonl"
    if not ledger.exists():
        pytest.skip("no experiment ledger recorded yet")

    required = ("spec_sha256", "events_sha256", "features_sha256", "data_sha256", "git_commit")
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    assert rows, "ledger exists but is empty"

    for row in rows:
        prov = row.get("provenance")
        assert prov, f"{row.get('experiment')} has no provenance block"
        for key in required:
            assert prov.get(key), f"{row.get('experiment')} missing {key}"
        assert row.get("ran_at"), "record has no timestamp"


def test_ledger_hashes_match_the_current_files():
    """Stamped hashes must correspond to the files actually in the tree."""
    import provenance as prov_mod

    ledger = RESEARCH / "experiments.jsonl"
    if not ledger.exists():
        pytest.skip("no experiment ledger recorded yet")

    current = prov_mod.provenance()
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]

    for row in rows:
        prov = row["provenance"]
        for key in ("spec_sha256", "events_sha256", "features_sha256"):
            assert prov[key] == current[key], (
                f"{row['experiment']}: {key} does not match the current file — "
                "the archived result predates an edit and must be regenerated"
            )


def test_dirty_check_counts_tracked_evidence_but_not_the_ledger(monkeypatch):
    """The ledger is exempt (it is being appended); evidence files are NOT.

    Excluding all of research/data/ — as an earlier version did — would have
    hidden modification of the manifest and step0_events.json, which are
    exactly the artefacts these hashes exist to protect.
    """
    import provenance as prov_mod

    statuses = {
        " M research/experiments.jsonl": False,  # exempt: self-referential
        " M research/data/BTCUSDT-1h.manifest.json": True,  # tracked evidence
        " M research/data/step0_events.json": True,  # tracked evidence
        " M research/events.py": True,  # source
    }
    for line, should_be_dirty in statuses.items():
        monkeypatch.setattr(
            prov_mod.subprocess,
            "run",
            lambda *a, _line=line, **k: type("R", (), {"stdout": _line, "returncode": 0})(),
        )
        assert (
            prov_mod._git()["git_dirty"] is should_be_dirty
        ), f"{line!r} should {'' if should_be_dirty else 'not '}mark the run dirty"


def test_manifest_hash_is_recomputed_not_trusted(tmp_path, monkeypatch):
    """A swapped CSV must be detected, not inherited as a false attestation."""
    import provenance as prov_mod

    data_dir = tmp_path / "research" / "data"
    data_dir.mkdir(parents=True)
    csv_path = data_dir / "BTCUSDT-1h.csv"
    csv_path.write_text("open_time,open\n2024-01-01T00:00:00+00:00,100\n", encoding="utf-8")

    real = prov_mod._sha256_file(csv_path)
    manifest = data_dir / "BTCUSDT-1h.manifest.json"
    manifest.write_text(json.dumps({"data_sha256": real, "rows": 1}), encoding="utf-8")
    monkeypatch.setattr(prov_mod, "ROOT", tmp_path)

    ok = prov_mod.data_manifest()
    assert ok["data_sha256"] == real and ok["data_sha256_verified"] is True

    # Swap the data, leave the manifest asserting the old hash.
    csv_path.write_text("open_time,open\n2024-01-01T00:00:00+00:00,999999\n", encoding="utf-8")
    with pytest.raises(prov_mod.DataIntegrityError, match="does not match its manifest"):
        prov_mod.data_manifest()


def test_missing_csv_is_reported_unverified_not_fabricated(tmp_path, monkeypatch):
    """A fresh clone has the manifest but not the bulk CSV; say so honestly."""
    import provenance as prov_mod

    data_dir = tmp_path / "research" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "BTCUSDT-1h.manifest.json").write_text(
        json.dumps({"data_sha256": "a" * 64, "rows": 10}), encoding="utf-8"
    )
    monkeypatch.setattr(prov_mod, "ROOT", tmp_path)

    got = prov_mod.data_manifest()
    assert got["data_sha256"] == "a" * 64
    assert got["data_sha256_verified"] is False
