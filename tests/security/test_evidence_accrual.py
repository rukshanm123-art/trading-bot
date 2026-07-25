"""Qualification evidence must accrue while the engine RUNS, not only when it
stops.

The regression: evidence was written once, from shutdown(). A 24/7 deployment
never shuts down, so it accumulated zero qualification days no matter how long
it ran — `live status` would still read 0/30 after a month of perfect
operation.

The fix flushes periodically. The danger introduced by periodic flushing is the
mirror image: summary() SUMS per-day coverage across records, so windows that
overlap would fabricate wall-clock time (48 half-hour flushes all starting at
session start would "prove" ~24h after 30 minutes of runtime). These tests pin
both halves: evidence appears without a shutdown, AND consecutive windows are
contiguous and disjoint.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.helpers import make_config
from trading_bot.core.enums import Mode
from trading_bot.engine import trader as trader_mod
from trading_bot.engine.trader import TradingEngine
from trading_bot.security.qualification import (
    EVIDENCE_FLUSH_INTERVAL_S,
    EVIDENCE_MIN_WINDOW_S,
    QualificationEvidenceStore,
    get_or_create_evidence_key,
)

pytestmark = pytest.mark.security

T0 = datetime(2025, 1, 1, tzinfo=UTC)


class _Flags:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key):
        return self._d.get(key)

    def set(self, key, value) -> None:
        self._d[key] = str(value)


class _Decisions:
    def __init__(self) -> None:
        self.n = 0

    def count(self, mode) -> int:
        return self.n


class _FakeDatetime:
    """Controls only what the flush reads: the real wall clock."""

    current = T0

    @classmethod
    def now(cls, tz=None):
        return cls.current


def _stub_engine(tmp_path, monkeypatch):
    """A TradingEngine carrying exactly the state the flush touches."""
    monkeypatch.setattr(trader_mod, "datetime", _FakeDatetime)
    monkeypatch.setattr(trader_mod, "build_provenance", lambda root: ("deadbeef", "repo"))
    _FakeDatetime.current = T0

    engine = TradingEngine.__new__(TradingEngine)
    engine.cfg = make_config()  # PAPER
    engine.fixture = None
    engine.db = SimpleNamespace(backend="postgres")
    engine.repos = SimpleNamespace(flags=_Flags(), decisions=_Decisions())
    engine.root = tmp_path
    engine.cfg_hash = "cfghash"
    engine.strategy = SimpleNamespace(name="ema", version="1")
    engine._session_wall_start = T0
    engine._session_decisions_start = 0
    return engine


def _payloads(engine, tmp_path):
    store = QualificationEvidenceStore(tmp_path, key=get_or_create_evidence_key(engine.repos.flags))
    return [row["payload"] for row in store.records(validate=True)]


# ---------------------------------------------------------------- the bug
def test_evidence_is_written_without_any_shutdown(tmp_path, monkeypatch):
    engine = _stub_engine(tmp_path, monkeypatch)

    _FakeDatetime.current = T0 + timedelta(seconds=EVIDENCE_FLUSH_INTERVAL_S)
    engine.repos.decisions.n = 30
    engine._record_session_evidence()

    payloads = _payloads(engine, tmp_path)
    assert len(payloads) == 1
    assert payloads[0]["eligible_decisions"] == 30
    assert not getattr(engine, "_shutdown_done", False)  # never stopped


def test_consecutive_flush_windows_are_contiguous_and_disjoint(tmp_path, monkeypatch):
    engine = _stub_engine(tmp_path, monkeypatch)

    for step in range(1, 5):
        _FakeDatetime.current = T0 + timedelta(seconds=EVIDENCE_FLUSH_INTERVAL_S * step)
        engine.repos.decisions.n = step * 10
        engine._record_session_evidence()

    payloads = _payloads(engine, tmp_path)
    assert len(payloads) == 4
    starts = [datetime.fromisoformat(p["wall_clock_start"]) for p in payloads]
    ends = [datetime.fromisoformat(p["wall_clock_end"]) for p in payloads]
    # each window begins exactly where the previous one ended: no gap, no overlap
    assert starts[1:] == ends[:-1]
    assert starts[0] == T0
    covered = sum((e - s).total_seconds() for s, e in zip(starts, ends, strict=True))
    assert covered == pytest.approx(4 * EVIDENCE_FLUSH_INTERVAL_S)
    # decision deltas partition the total, never repeating the running count
    assert [p["eligible_decisions"] for p in payloads] == [10, 10, 10, 10]


def test_flush_shorter_than_minimum_writes_nothing_and_keeps_the_mark(tmp_path, monkeypatch):
    engine = _stub_engine(tmp_path, monkeypatch)

    _FakeDatetime.current = T0 + timedelta(seconds=EVIDENCE_MIN_WINDOW_S - 1)
    engine._record_session_evidence()
    assert _payloads(engine, tmp_path) == []
    assert engine._session_wall_start == T0  # mark NOT consumed by the no-op

    # the skipped seconds are not lost: the next real flush still covers them
    _FakeDatetime.current = T0 + timedelta(seconds=EVIDENCE_FLUSH_INTERVAL_S)
    engine.repos.decisions.n = 20
    engine._record_session_evidence()
    payload = _payloads(engine, tmp_path)[0]
    assert datetime.fromisoformat(payload["wall_clock_start"]) == T0


def test_a_full_day_of_flushes_credits_exactly_one_day(tmp_path, monkeypatch):
    """End to end: 48 half-hourly flushes across one UTC day must credit ONE
    day — not 48, and not zero."""
    engine = _stub_engine(tmp_path, monkeypatch)

    for step in range(1, 49):
        _FakeDatetime.current = T0 + timedelta(minutes=30 * step)
        engine.repos.decisions.n = step
        engine._record_session_evidence()

    store = QualificationEvidenceStore(tmp_path, key=get_or_create_evidence_key(engine.repos.flags))
    summary = store.summary(decision_days={"2025-01-01": 48})

    assert summary.ok, summary.failures
    assert summary.paper_days == 1
    assert summary.paper_decisions == 48


def test_repeated_flushes_cannot_manufacture_a_day(tmp_path, monkeypatch):
    """The anti-forgery half: flushing every 30 seconds for an hour is one
    hour of evidence, so the 12h coverage floor still rejects the day."""
    engine = _stub_engine(tmp_path, monkeypatch)

    for step in range(1, 121):  # 120 flushes x 30s = 1 hour of real runtime
        _FakeDatetime.current = T0 + timedelta(seconds=30 * step)
        engine.repos.decisions.n = step
        engine._record_session_evidence()

    store = QualificationEvidenceStore(tmp_path, key=get_or_create_evidence_key(engine.repos.flags))
    assert store.summary(decision_days={"2025-01-01": 500}).paper_days == 0


# --------------------------------------------------------------- provenance
def test_container_without_git_or_build_stamp_records_nothing(tmp_path, monkeypatch):
    """A Docker image ships no .git. Before the build stamp existed, every
    record it wrote was stamped no_repo — rejected by the live gate AND
    poisoning summary().ok, so a month of real runtime proved nothing."""
    engine = _stub_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(trader_mod, "build_provenance", lambda root: (None, "no_repo"))

    _FakeDatetime.current = T0 + timedelta(hours=2)
    engine.repos.decisions.n = 40
    engine._record_session_evidence()

    assert _payloads(engine, tmp_path) == []  # refused, not written unusable
    assert engine._session_wall_start == T0  # window kept for a fixed build


def test_build_stamp_supplies_provenance_when_there_is_no_git(tmp_path):
    """The image's recorded commit is accepted as provenance."""
    from trading_bot.security.quality import BUILD_INFO_FILE, build_provenance

    assert build_provenance(tmp_path) == (None, "no_repo")

    (tmp_path / BUILD_INFO_FILE).write_text(
        '{"git_commit": "abc123def456", "built_at": "2025-01-01T00:00:00Z"}', encoding="utf-8"
    )
    assert build_provenance(tmp_path) == ("abc123def456", "image")


@pytest.mark.parametrize("body", ["", "{}", "not json", '{"git_commit": "  "}'])
def test_unusable_build_stamps_are_not_provenance(tmp_path, body):
    from trading_bot.security.quality import BUILD_INFO_FILE, build_provenance

    (tmp_path / BUILD_INFO_FILE).write_text(body, encoding="utf-8")
    assert build_provenance(tmp_path) == (None, "no_repo")


def test_image_provenance_evidence_is_accepted_by_the_live_gate(tmp_path, monkeypatch):
    engine = _stub_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(trader_mod, "build_provenance", lambda root: ("abc123", "image"))

    for step in range(1, 49):
        _FakeDatetime.current = T0 + timedelta(minutes=30 * step)
        engine.repos.decisions.n = step
        engine._record_session_evidence()

    store = QualificationEvidenceStore(tmp_path, key=get_or_create_evidence_key(engine.repos.flags))
    summary = store.summary(decision_days={"2025-01-01": 48})
    assert summary.ok, summary.failures
    assert summary.paper_days == 1


# ------------------------------------------------------- non-qualifying modes
def test_sqlite_and_fixture_sessions_still_flush_nothing(tmp_path, monkeypatch):
    engine = _stub_engine(tmp_path, monkeypatch)
    _FakeDatetime.current = T0 + timedelta(hours=1)

    engine.db = SimpleNamespace(backend="sqlite")
    engine._record_session_evidence()
    engine.db = SimpleNamespace(backend="postgres")
    engine.fixture = object()
    engine._record_session_evidence()
    engine.fixture = None
    engine.cfg = make_config(mode=Mode.TESTNET.value)
    engine._record_session_evidence()

    assert _payloads(engine, tmp_path) == []
    assert engine._session_wall_start == T0  # nothing consumed the window
