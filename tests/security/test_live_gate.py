"""Live-mode gate: locked by default, every prerequisite independently
enforced, quality records must prove a real test run."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import make_config
from trading_bot.config import constants as C
from trading_bot.core.models import set_time_provider
from trading_bot.security.livegate import LiveGate
from trading_bot.security.qualification import QualificationEvidenceStore
from trading_bot.security.quality import expected_hashes
from trading_bot.security.secrets import StaticSecretProvider

NOW = datetime(2025, 6, 1, tzinfo=UTC)


def gate(repos, tmp_path, secrets=None, config_path=None) -> LiveGate:
    cfg = make_config()
    return LiveGate(
        repos,
        cfg,
        StaticSecretProvider(secrets or {}),
        config_path=config_path,
        project_root=tmp_path,
    )


def test_live_locked_by_default(repos, tmp_path):
    g = gate(repos, tmp_path)
    prereqs = g.prerequisites()
    assert any(not p.ok for p in prereqs)
    failed = {p.name for p in prereqs if not p.ok}
    # every major gate fails on a fresh system
    assert {
        "paper_days",
        "paper_decisions",
        "test_suite",
        "env_live_enabled",
        "live_credentials",
        "out_of_band_alerting",
        "production_database",
    } <= failed
    with pytest.raises(PermissionError, match="LIVE mode locked"):
        g.assert_live_start_allowed()


def test_no_automatic_promotion_env_alone_is_insufficient(repos, tmp_path):
    g = gate(
        repos,
        tmp_path,
        secrets={
            C.ENV_LIVE_ENABLED: "true",
            C.ENV_LIVE_KEY: "live-key-0123456789abcdef",
            C.ENV_LIVE_SECRET: "live-secret-0123456789abcdef",
        },
    )
    with pytest.raises(PermissionError):
        g.assert_live_start_allowed()


def test_live_gate_requires_configured_out_of_band_alerting(repos, tmp_path):
    disabled = gate(
        repos,
        tmp_path,
        secrets={"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"},
    )
    check = next(p for p in disabled.prerequisites() if p.name == "out_of_band_alerting")
    assert not check.ok

    cfg = make_config(notifications={"telegram": {"enabled": True}})
    enabled = LiveGate(
        repos,
        cfg,
        StaticSecretProvider({"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}),
        project_root=tmp_path,
        external_alert_probe=lambda: {"telegram": True},
    )
    check = next(p for p in enabled.prerequisites() if p.name == "out_of_band_alerting")
    assert check.ok


def test_live_gate_requires_postgres_database(repos, tmp_path, monkeypatch):
    sqlite_check = next(
        p for p in gate(repos, tmp_path).prerequisites() if p.name == "production_database"
    )
    assert not sqlite_check.ok

    cfg = make_config(db={"url": "postgresql://localhost/trading_bot"})
    postgres_gate = LiveGate(
        repos,
        cfg,
        StaticSecretProvider({}),
        project_root=tmp_path,
    )
    monkeypatch.setattr(repos.db, "backend", "postgres")
    postgres_check = postgres_gate._production_database()
    assert postgres_check.ok


def test_live_gate_rejects_failed_external_connectivity(repos, tmp_path):
    cfg = make_config(notifications={"telegram": {"enabled": True}})
    failed = LiveGate(
        repos,
        cfg,
        StaticSecretProvider({"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"}),
        project_root=tmp_path,
        external_alert_probe=lambda: {"telegram": False},
    )
    check = next(p for p in failed.prerequisites() if p.name == "out_of_band_alerting")
    assert not check.ok
    assert "connectivity" in check.detail


def _write_quality(tmp_path, **overrides):
    payload = {
        "passed": True,
        "tests_collected": 150,
        "tests_passed": 150,
        "tests_failed": 0,
        "coverage_percent": 85.0,
        "required_safety_tests_missing": [],
        "results_hash": "abc123def456",
        "git_commit": None,
        "git_dirty": False,
        "git_state": "no_repo",
        "ran_at": datetime.now(UTC).isoformat(),
    }
    payload.update(overrides)
    path = tmp_path / C.QUALITY_GATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    payload.update(expected_hashes(tmp_path))
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_quality_gate_rejects_zero_test_run(repos, tmp_path):
    """A bare {'passed': true} with no real tests must NOT satisfy the gate."""
    _write_quality(tmp_path, tests_collected=0, tests_passed=0)
    g = gate(repos, tmp_path)
    q = next(p for p in g.prerequisites() if p.name == "test_suite")
    assert not q.ok
    assert "0 tests collected" in q.detail


def test_quality_gate_rejects_low_coverage(repos, tmp_path):
    _write_quality(tmp_path, coverage_percent=40.0)
    q = next(p for p in gate(repos, tmp_path).prerequisites() if p.name == "test_suite")
    assert not q.ok and "coverage" in q.detail


def test_quality_gate_rejects_missing_safety_tests(repos, tmp_path):
    _write_quality(tmp_path, required_safety_tests_missing=["tests/unit/test_sizing.py::x"])
    q = next(p for p in gate(repos, tmp_path).prerequisites() if p.name == "test_suite")
    assert not q.ok and "safety tests" in q.detail


def test_quality_gate_rejects_failures_and_staleness(repos, tmp_path):
    _write_quality(tmp_path, tests_failed=2, passed=False)
    q = next(p for p in gate(repos, tmp_path).prerequisites() if p.name == "test_suite")
    assert not q.ok
    _write_quality(
        tmp_path,
        ran_at=(datetime.now(UTC) - timedelta(hours=100)).isoformat(),
    )
    q = next(p for p in gate(repos, tmp_path).prerequisites() if p.name == "test_suite")
    assert not q.ok and "old" in q.detail


def test_quality_gate_rejects_dirty_tree_and_missing_hash(repos, tmp_path):
    _write_quality(tmp_path, git_dirty=True)
    q = next(p for p in gate(repos, tmp_path).prerequisites() if p.name == "test_suite")
    assert not q.ok and "dirty" in q.detail
    _write_quality(tmp_path, results_hash="")
    q = next(p for p in gate(repos, tmp_path).prerequisites() if p.name == "test_suite")
    assert not q.ok and "hash" in q.detail


def test_quality_gate_rejects_no_repo_for_live_qualification(repos, tmp_path):
    _write_quality(tmp_path)
    q = next(p for p in gate(repos, tmp_path).prerequisites() if p.name == "test_suite")
    assert not q.ok
    assert "real git repository" in q.detail


def test_backtest_and_fixture_evidence_count_zero_paper_days(repos, tmp_path):
    store = QualificationEvidenceStore(tmp_path)
    base = {
        "source_mode": "paper",
        "wall_clock_start": datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
        "wall_clock_end": datetime(2025, 1, 2, tzinfo=UTC).isoformat(),
        "eligible_decisions": 500,
        "configuration_hash": "cfg",
        "strategy_version": "1",
        "git_commit": "deadbeef",
        "git_state": "repo",
    }
    store.append({**base, "data_source_class": "historical_backtest"})
    store.append({**base, "data_source_class": "offline_fixture"})
    summary = store.summary()
    assert summary.paper_days == 0
    assert summary.paper_decisions == 0


def _paper_payload(**overrides):
    payload = {
        "source_mode": "paper",
        "data_source_class": "live_market",
        "wall_clock_start": datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
        "wall_clock_end": datetime(2025, 1, 1, 23, tzinfo=UTC).isoformat(),
        "recorded_at": datetime(2025, 1, 1, 23, 1, tzinfo=UTC).isoformat(),
        "eligible_decisions": 12,
        "configuration_hash": "cfg",
        "strategy_version": "1",
        "git_commit": "deadbeef",
        "git_state": "repo",
    }
    payload.update(overrides)
    return payload


def test_one_real_wall_clock_paper_day_counts_once(repos, tmp_path):
    store = QualificationEvidenceStore(tmp_path)
    store.append(_paper_payload())
    store.append(
        _paper_payload(
            eligible_decisions=5,
            recorded_at=datetime(2025, 1, 1, 23, 2, tzinfo=UTC).isoformat(),
        )
    )
    summary = store.summary(decision_days={"2025-01-01": 20})
    assert summary.ok, summary.failures
    assert summary.paper_days == 1
    assert summary.paper_decisions == 17


def test_days_require_database_confirmation(repos, tmp_path):
    """Evidence about days the operational DB never saw counts zero — the
    fabrication path from the review: 30 fake days, no real decisions."""
    store = QualificationEvidenceStore(tmp_path)
    store.append(_paper_payload())
    # no decision_days at all -> fail closed
    assert store.summary(decision_days=None).paper_days == 0
    # DB shows too few decisions that day -> not credited, flagged
    summary = store.summary(decision_days={"2025-01-01": 3})
    assert summary.paper_days == 0
    assert any("not confirmed by database" in f for f in summary.failures)


def test_one_second_sessions_credit_no_days(repos, tmp_path):
    store = QualificationEvidenceStore(tmp_path)
    store.append(
        _paper_payload(
            wall_clock_end=datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC).isoformat(),
            recorded_at=datetime(2025, 1, 1, 0, 0, 2, tzinfo=UTC).isoformat(),
        )
    )
    summary = store.summary(decision_days={"2025-01-01": 100})
    assert summary.paper_days == 0  # 1s of coverage is not a paper day


def test_backdated_evidence_rejected(repos, tmp_path):
    store = QualificationEvidenceStore(tmp_path)
    store.append(
        _paper_payload(
            recorded_at=datetime(2025, 1, 1, 12, tzinfo=UTC).isoformat(),  # before end
        )
    )
    summary = store.summary(decision_days={"2025-01-01": 100})
    assert summary.paper_days == 0
    assert any("backdated" in f for f in summary.failures)


def test_unsigned_evidence_rejected_when_key_established(repos, tmp_path):
    """Once an HMAC key exists, a hand-built sha256 chain (no key) is refused."""
    from trading_bot.security.qualification import get_or_create_evidence_key

    # attacker writes a self-consistent UNSIGNED chain
    forger = QualificationEvidenceStore(tmp_path)
    forger.append(_paper_payload())
    # the real verifier uses the DB-held key
    key = get_or_create_evidence_key(repos.flags)
    verifier = QualificationEvidenceStore(tmp_path, key=key)
    summary = verifier.summary(decision_days={"2025-01-01": 100})
    assert not summary.ok
    assert summary.paper_days == 0


def test_signed_evidence_roundtrip(repos, tmp_path):
    from trading_bot.security.qualification import get_or_create_evidence_key

    key = get_or_create_evidence_key(repos.flags)
    assert key == get_or_create_evidence_key(repos.flags)  # stable per DB
    store = QualificationEvidenceStore(tmp_path, key=key)
    store.append(_paper_payload())
    summary = QualificationEvidenceStore(tmp_path, key=key).summary(
        decision_days={"2025-01-01": 100}
    )
    assert summary.ok, summary.failures
    assert summary.paper_days == 1


def test_tampered_qualification_evidence_is_rejected(repos, tmp_path):
    store = QualificationEvidenceStore(tmp_path)
    store.append(
        {
            "source_mode": "paper",
            "data_source_class": "live_market",
            "wall_clock_start": datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
            "wall_clock_end": datetime(2025, 1, 2, tzinfo=UTC).isoformat(),
            "eligible_decisions": 12,
            "configuration_hash": "cfg",
            "strategy_version": "1",
            "git_commit": "deadbeef",
            "git_state": "repo",
        }
    )
    text = store.path.read_text(encoding="utf-8")
    store.path.write_text(text.replace("12", "99", 1), encoding="utf-8")
    summary = store.summary()
    assert not summary.ok
    assert "invalid" in summary.failures[0]


# ------------------------------------------------------------ ceremony
def test_unlock_phrase_ceremony(repos, tmp_path):
    g = gate(repos, tmp_path)
    unlock_id, phrase = g.start_unlock()
    assert len(phrase.split()) == C.LIVE_CONFIRMATION_WORDS
    assert not g.confirm(unlock_id, "wrong words entirely typed here now")
    assert not g.is_unlocked()
    assert g.confirm(unlock_id, phrase)
    assert g.is_unlocked()


def test_unlock_expires(repos, tmp_path):
    from trading_bot.exchange.interface import FrozenClock

    clock = FrozenClock(NOW)
    set_time_provider(clock.now)
    g = gate(repos, tmp_path)
    unlock_id, phrase = g.start_unlock()
    assert g.confirm(unlock_id, phrase)
    assert g.is_unlocked()
    clock.advance((C.LIVE_UNLOCK_VALID_HOURS + 1) * 3600)
    assert not g.is_unlocked()


def test_phrases_are_random():
    assert LiveGate.generate_phrase() != LiveGate.generate_phrase()


def test_explicit_risk_config_check(repos, tmp_path):
    cfg_file = tmp_path / "live.yaml"
    cfg_file.write_text("mode: paper\nrisk: {}\n", encoding="utf-8")
    g = gate(repos, tmp_path, config_path=str(cfg_file))
    p = next(x for x in g.prerequisites() if x.name == "explicit_risk_config")
    assert not p.ok and "missing keys" in p.detail

    cfg_file.write_text(
        "mode: paper\nrisk:\n  max_daily_loss_pct: '2'\n  max_drawdown_pct: '8'\n"
        "  max_risk_per_trade_pct: '0.5'\n",
        encoding="utf-8",
    )
    p = next(
        x
        for x in gate(repos, tmp_path, config_path=str(cfg_file)).prerequisites()
        if x.name == "explicit_risk_config"
    )
    assert p.ok
