"""Hash-chained audit log + daily continuation control."""

from datetime import UTC, datetime

from tests.helpers import make_config
from trading_bot.control.approval import ApprovalService
from trading_bot.exchange.interface import FrozenClock
from trading_bot.storage.audit import AuditLog

T = datetime(2025, 6, 1, tzinfo=UTC)


# ------------------------------------------------------------- audit
def test_audit_chain_verifies(db):
    log = AuditLog(db)
    for i in range(5):
        log.append("test.event", {"i": i})
    ok, bad = log.verify_chain()
    assert ok and bad is None


def test_audit_tamper_detected(db):
    log = AuditLog(db)
    log.append("a", {"x": 1})
    log.append("b", {"x": 2})
    db.execute("UPDATE audit_log SET payload_json = '{\"x\": 999}' WHERE seq = 1")
    ok, bad = log.verify_chain()
    assert not ok and bad == 1


def test_audit_deletion_detected(db):
    log = AuditLog(db)
    for i in range(3):
        log.append("e", {"i": i})
    db.execute("DELETE FROM audit_log WHERE seq = 2")
    ok, bad = log.verify_chain()
    assert not ok


# ----------------------------------------------------------- approval
def approval(repos, mode: str) -> ApprovalService:
    cfg = make_config(continuation={"mode": mode})
    return ApprovalService(repos, cfg, FrozenClock(T))


def test_daily_approval_requires_explicit_grant(repos):
    svc = approval(repos, "daily_approval")
    assert not svc.entries_allowed(health_ok=True)
    svc.approve(24, "tester")
    assert svc.entries_allowed(health_ok=True)


def test_daily_approval_window_expires(repos):
    clock = FrozenClock(T)
    cfg = make_config(continuation={"mode": "daily_approval"})
    svc = ApprovalService(repos, cfg, clock)
    svc.approve(2, "tester")
    clock.advance(3 * 3600)
    assert not svc.entries_allowed(health_ok=True)


def test_daily_report_consumes_approval(repos):
    svc = approval(repos, "daily_approval")
    svc.approve(48, "tester")
    assert svc.entries_allowed(health_ok=True)
    svc.consume_after_daily_report()
    assert not svc.entries_allowed(health_ok=True)


def test_auto_continue_follows_health(repos):
    svc = approval(repos, "auto_continue")
    assert svc.entries_allowed(health_ok=True)
    assert not svc.entries_allowed(health_ok=False)


def test_pause_blocks_both_modes(repos):
    for mode in ("auto_continue", "daily_approval"):
        svc = approval(repos, mode)
        svc.approve(24, "tester")
        svc.pause("tester")
        assert not svc.entries_allowed(health_ok=True)
        svc.resume("tester")
    assert svc.entries_allowed(health_ok=True)


def test_auto_continue_ignores_stale_approval_flag(repos):
    svc = approval(repos, "auto_continue")
    # approval flag irrelevant in auto mode; only health matters
    svc.approve(1, "tester")
    assert svc.entries_allowed(health_ok=True)
    assert not svc.entries_allowed(health_ok=False)
