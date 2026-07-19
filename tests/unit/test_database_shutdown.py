"""Database and engine resources close deterministically."""

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_config, make_trend_rows, write_rows_csv
from trading_bot.engine.trader import TradingEngine
from trading_bot.storage.db import Database, DatabaseError


def test_database_context_manager_closes(tmp_path):
    path = tmp_path / "ctx.db"
    with Database(f"sqlite:///{path}") as db:
        db.migrate(MIGRATIONS)
        assert not db.closed
    assert db.closed
    with pytest.raises(DatabaseError):
        db.query("SELECT 1")


def test_database_close_twice_is_safe(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/twice.db")
    db.close()
    db.close()
    assert db.closed


def test_engine_shutdown_closes_database_and_is_idempotent(tmp_path):
    fixture = write_rows_csv(make_trend_rows([(40, 0.0)], 100.0), tmp_path / "fixture.csv")
    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/engine.db"},
        data={"source": "fixture", "fixture_path": fixture},
    )
    engine = TradingEngine(cfg, migrations_dir=MIGRATIONS, project_root=tmp_path)
    engine.shutdown()
    engine.shutdown()
    assert engine.db.closed


def test_restart_can_reopen_database_immediately(tmp_path):
    path = tmp_path / "restart.db"
    db = Database(f"sqlite:///{path}")
    db.migrate(MIGRATIONS)
    db.close()
    reopened = Database(f"sqlite:///{path}")
    reopened.migrate(MIGRATIONS)
    assert reopened.integrity_check()
    reopened.close()
