from __future__ import annotations

import shutil
from datetime import timedelta

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import T0, make_config
from trading_bot.core.enums import Mode
from trading_bot.core.models import iso
from trading_bot.core.types import dec
from trading_bot.exchange.interface import FrozenClock
from trading_bot.risk.loss_pause import (
    ConsecutiveLossPauseService,
    LossPauseAcknowledgementError,
    LossPauseStateError,
)
from trading_bot.risk.state import RiskStateService
from trading_bot.storage.audit import AuditLog
from trading_bot.storage.db import Database
from trading_bot.storage.repositories import Repositories


def _close(
    repos: Repositories,
    index: int,
    *,
    mode: Mode = Mode.PAPER,
    pnl: str = "-1",
    dust: bool = False,
):
    ts = T0 + timedelta(hours=index)
    position_id = repos.positions.insert_open(
        mode=mode,
        symbol="BTCUSDT",
        qty=dec("0.00010"),
        avg_entry_price=dec("65000"),
        stop_price=dec("63700"),
        entry_order_id=f"entry-{mode.value}-{index}",
        entry_fee=dec("0.01"),
        ts=ts,
    )
    if dust:
        repos.positions.mark_dust(
            position_id=position_id,
            remaining_qty=dec("0.00000991"),
            remaining_entry_fee=dec("0.001"),
            exit_order_id=f"exit-{mode.value}-{index}",
            cumulative_exit_fee=dec("0.01"),
            cumulative_realized_pnl=dec(pnl),
            exit_reason="strategy_exit:dust_below_exchange_minimum",
            ts=ts + timedelta(minutes=30),
        )
    else:
        repos.positions.close(
            position_id,
            f"exit-{mode.value}-{index}",
            dec("0.01"),
            dec(pnl),
            "test",
            ts + timedelta(minutes=30),
        )
    return position_id


def _service(repos: Repositories, *, mode: Mode = Mode.PAPER):
    cfg = make_config(mode=mode.value)
    clock = FrozenClock(T0 + timedelta(hours=30))
    return ConsecutiveLossPauseService(repos, cfg, clock), clock


def _make_active(repos: Repositories, *, dust_last: bool = False, mode=Mode.PAPER):
    _close(repos, 1, mode=mode)
    _close(repos, 2, mode=mode)
    return _close(repos, 3, mode=mode, dust=dust_last)


def _reconcile_ok(repos: Repositories, clock: FrozenClock) -> None:
    repos.events.reconciliation(True, {}, Mode.PAPER)
    repos.db.execute(
        "UPDATE reconciliation_results SET ts = ? WHERE id = "
        "(SELECT id FROM reconciliation_results ORDER BY ts DESC LIMIT 1)",
        (iso(clock.now()),),
    )


def test_pause_is_latched_and_time_does_not_clear_it(repos):
    _make_active(repos)
    service, clock = _service(repos)

    first = service.status()
    clock.advance(60 * 60 * 24 * 365)
    later = service.status()

    assert first.active and later.active
    assert later.effective_streak == 3
    assert not later.as_dict()["clears_automatically"]
    message = service.active_alert_message(later)
    assert "DOES NOT CLEAR WITH TIME" in message
    assert "acknowledge-loss-pause" in message
    assert "cooldown 12h" not in message


def test_acknowledgement_refuses_before_review_interval_and_with_open_position(repos):
    _make_active(repos)
    service, clock = _service(repos)
    clock.set(T0 + timedelta(hours=4))

    with pytest.raises(LossPauseAcknowledgementError, match="review interval"):
        service.acknowledge("operator", "reviewed losses")

    clock.set(T0 + timedelta(hours=30))
    repos.positions.insert_open(
        Mode.PAPER,
        "BTCUSDT",
        dec("0.00010"),
        dec("65000"),
        dec("63700"),
        "still-open",
        dec("0.01"),
        T0 + timedelta(hours=5),
    )
    _reconcile_ok(repos, clock)
    with pytest.raises(LossPauseAcknowledgementError, match="open position"):
        service.acknowledge("operator", "reviewed losses")


def test_acknowledgement_requires_current_clean_reconciliation_and_no_unknown_orders(repos):
    _make_active(repos)
    service, clock = _service(repos)

    with pytest.raises(LossPauseAcknowledgementError, match="no reconciliation"):
        service.acknowledge("operator", "reviewed losses")

    repos.events.reconciliation(False, {}, Mode.PAPER)
    repos.db.execute("UPDATE reconciliation_results SET ts = ?", (iso(clock.now()),))
    with pytest.raises(LossPauseAcknowledgementError, match="did not pass"):
        service.acknowledge("operator", "reviewed losses")

    repos.db.execute("DELETE FROM reconciliation_results")
    repos.events.reconciliation(True, {}, Mode.PAPER)
    repos.db.execute("UPDATE reconciliation_results SET ts = ?", (iso(T0),))
    with pytest.raises(LossPauseAcknowledgementError, match="stale"):
        service.acknowledge("operator", "reviewed losses")

    repos.db.execute("UPDATE reconciliation_results SET ts = ?", (iso(clock.now()),))
    repos.db.execute(
        "INSERT INTO orders (id, client_order_id, correlation_id, mode, symbol, side, "
        "order_type, qty, stop_price, state, purpose, intent_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "unknown-order",
            "unknown-client-order",
            "correlation",
            Mode.PAPER.value,
            "BTCUSDT",
            "BUY",
            "MARKET",
            "0.00010",
            "63700",
            "UNKNOWN",
            "entry",
            "{}",
            iso(clock.now()),
            iso(clock.now()),
        ),
    )
    with pytest.raises(LossPauseAcknowledgementError, match="unknown execution state"):
        service.acknowledge("operator", "reviewed losses")


def test_acknowledgement_succeeds_with_dust_and_is_idempotent(repos):
    dust_id = _make_active(repos, dust_last=True)
    service, clock = _service(repos)
    _reconcile_ok(repos, clock)

    first = service.acknowledge("operator", "market reviewed; remain on testnet")
    second = service.acknowledge("operator", "idempotent retry")

    assert first.created
    assert not second.created
    assert first.record["watermark_position_id"] == dust_id
    assert first.state.effective_streak == 0
    assert first.state.raw_streak == 3
    assert repos.positions.open_position(Mode.PAPER) is None
    assert len(repos.positions.dust_positions(Mode.PAPER)) == 1
    count = repos.db.query_one("SELECT COUNT(*) AS n FROM consecutive_loss_acknowledgements")
    assert count and int(count["n"]) == 1


def test_audit_failure_rolls_back_acknowledgement(repos, monkeypatch):
    _make_active(repos, dust_last=True)
    service, clock = _service(repos)
    _reconcile_ok(repos, clock)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(AuditLog, "append", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        with repos.db.transaction():
            service.acknowledge("operator", "atomic recovery review")
            AuditLog(repos.db).append("risk.ack", {})

    count = repos.db.query_one("SELECT COUNT(*) AS n FROM consecutive_loss_acknowledgements")
    assert count and int(count["n"]) == 0


def test_new_streak_starts_after_acknowledgement_and_modes_are_isolated(repos):
    _make_active(repos)
    service, clock = _service(repos)
    _reconcile_ok(repos, clock)
    service.acknowledge("operator", "paper losses reviewed")

    snapshot = RiskStateService(repos, service.cfg, clock, service).snapshot(dec("100"))
    assert snapshot.consecutive_losses == 0

    for index in range(4, 7):
        _close(repos, index)
    _make_active(repos, mode=Mode.TESTNET)
    testnet_service, _ = _service(repos, mode=Mode.TESTNET)

    assert service.status().active
    assert service.status().effective_streak == 3
    assert testnet_service.status().active
    assert testnet_service.status().latest_acknowledgement is None


def test_corrupt_watermark_fails_closed(repos):
    _make_active(repos)
    service, clock = _service(repos)
    _reconcile_ok(repos, clock)
    result = service.acknowledge("operator", "reviewed")
    repos.db.execute(
        "UPDATE consecutive_loss_acknowledgements SET watermark_closed_at = ? WHERE id = ?",
        (iso(T0), result.record["id"]),
    )

    with pytest.raises(LossPauseStateError, match="timestamp"):
        service.status()


def test_acknowledgement_survives_database_backup_and_restore(tmp_path):
    source_path = tmp_path / "source.db"
    backup_path = tmp_path / "backup.db"
    db = Database(f"sqlite:///{source_path}")
    db.migrate(MIGRATIONS)
    repos = Repositories(db)
    _make_active(repos, dust_last=True)
    service, clock = _service(repos)
    _reconcile_ok(repos, clock)
    service.acknowledge("operator", "backup recovery drill")
    db.close()

    shutil.copy2(source_path, backup_path)
    restored = Database(f"sqlite:///{backup_path}")
    restored.migrate(MIGRATIONS)
    restored_service, _ = _service(Repositories(restored))
    state = restored_service.status()

    assert not state.active
    assert state.effective_streak == 0
    assert state.latest_acknowledgement is not None
    assert state.latest_acknowledgement["note"] == "backup recovery drill"
    restored.close()


def test_migration_preserves_existing_reconciliation_history(tmp_path):
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if migration.name < "0006_consecutive_loss_acknowledgements.sql":
            shutil.copy2(migration, old_migrations / migration.name)

    db = Database(f"sqlite:///{tmp_path}/upgrade.db")
    db.migrate(old_migrations)
    db.execute(
        "INSERT INTO reconciliation_results (id, ts, ok, details_json) VALUES (?, ?, ?, ?)",
        ("legacy-reconciliation", iso(T0), 1, "{}"),
    )

    assert db.migrate(MIGRATIONS) == ["0006_consecutive_loss_acknowledgements"]
    legacy = db.query_one("SELECT * FROM reconciliation_results WHERE id = 'legacy-reconciliation'")
    assert legacy and legacy["mode"] is None

    repos = Repositories(db)
    repos.events.reconciliation(True, {"after": "upgrade"}, Mode.PAPER)
    current = repos.events.last_reconciliation(Mode.PAPER)
    assert current and current["mode"] == Mode.PAPER.value
    db.close()
