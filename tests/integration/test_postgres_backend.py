"""Production PostgreSQL backend contract (enabled in CI via service container)."""

from __future__ import annotations

import os
import uuid

import pytest

from tests.conftest import MIGRATIONS
from trading_bot.core.enums import Mode
from trading_bot.core.types import dec
from trading_bot.storage.db import Database
from trading_bot.storage.repositories import Repositories

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("TEST_POSTGRES_URL"),
        reason="TEST_POSTGRES_URL is only supplied by the CI PostgreSQL service",
    ),
]


def test_postgres_migrations_transactions_and_idempotency():
    db = Database(os.environ["TEST_POSTGRES_URL"])
    try:
        db.migrate(MIGRATIONS)
        expected_migrations = {path.stem for path in MIGRATIONS.glob("*.sql")}
        actual_migrations = {
            row["version"] for row in db.query("SELECT version FROM schema_migrations")
        }
        assert expected_migrations <= actual_migrations
        repos = Repositories(db)
        probe_id = uuid.uuid4().hex

        with pytest.raises(RuntimeError, match="rollback probe"):
            with db.transaction():
                repos.flags.set(f"postgres_atomic_probe_{probe_id}", "written")
                raise RuntimeError("rollback probe")
        assert repos.flags.get(f"postgres_atomic_probe_{probe_id}") is None

        kwargs = {
            "position_id": f"postgres-position-{probe_id}",
            "mode": Mode.PAPER,
            "symbol": "BTCUSDT",
            "exit_order_id": f"postgres-exit-{probe_id}",
            "qty": dec("0.01"),
            "avg_entry_price": dec("100"),
            "exit_price": dec("110"),
            "entry_fee_allocated": dec("0.01"),
            "exit_fee": dec("0.011"),
            "realized_pnl": dec("0.079"),
            "exit_reason": "postgres_smoke",
            "cum_qty_after": dec("0.01"),
        }
        assert repos.positions.add_realization(**kwargs)
        assert not repos.positions.add_realization(**kwargs)
    finally:
        db.close()
