"""Production PostgreSQL backend contract (enabled in CI via service container)."""

from __future__ import annotations

import os
import uuid

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import RULES, T0
from trading_bot.core.enums import Mode, OrderState, OrderType, Side
from trading_bot.core.models import Fill, OrderResponse, SizedOrder
from trading_bot.core.types import dec
from trading_bot.engine.scheduler import InstanceLock
from trading_bot.exchange.interface import FrozenClock
from trading_bot.portfolio.accounting import PortfolioService
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


def test_postgres_fill_accounting_rolls_back_and_retries_atomically(monkeypatch):
    db = Database(os.environ["TEST_POSTGRES_URL"])
    try:
        db.migrate(MIGRATIONS)
        repos = Repositories(db)
        probe_id = uuid.uuid4().hex
        client_id = f"pg-exit-{probe_id}"
        sized = SizedOrder(
            symbol="BTCUSDT",
            side=Side.SELL,
            order_type=OrderType.MARKET,
            qty=dec("0.4"),
            limit_price=None,
            stop_price=dec("98"),
            est_entry_price=dec("110"),
            est_notional=dec("44"),
            est_fee=dec("0.044"),
            risk_amount=dec("0"),
            client_order_id=client_id,
        )
        repos.orders.insert_intent(sized, Mode.PAPER, probe_id, "exit")
        position_id = repos.positions.insert_open(
            Mode.PAPER,
            "BTCUSDT",
            dec("1"),
            dec("100"),
            dec("98"),
            f"pg-entry-{probe_id}",
            dec("1"),
        )
        position = repos.positions.open_position(Mode.PAPER)
        assert position is not None
        response = OrderResponse(
            client_order_id=client_id,
            exchange_order_id=f"exchange-{probe_id}",
            symbol="BTCUSDT",
            side=Side.SELL,
            order_type=OrderType.MARKET,
            state=OrderState.PARTIALLY_FILLED,
            requested_qty=dec("0.4"),
            executed_qty=dec("0.4"),
            cumulative_quote=dec("44"),
            fills=(Fill(dec("110"), dec("0.4"), dec("0.044"), "USDT", "trade-1"),),
            ts=T0,
        )
        service = PortfolioService(repos, Mode.PAPER, RULES)
        original_update = repos.positions.update_open_after_exit

        def interrupt(*args, **kwargs):
            raise RuntimeError("postgres accounting interruption")

        monkeypatch.setattr(repos.positions, "update_open_after_exit", interrupt)
        with pytest.raises(RuntimeError, match="accounting interruption"):
            service.record_exit(position, response, "postgres_probe")

        assert (
            repos.db.query_one("SELECT qty FROM positions WHERE id = ?", (position_id,))["qty"]
            == "1"
        )
        assert repos.orders.accounted_totals(client_id) == (dec("0"), dec("0"), dec("0"))
        assert repos.orders.get_by_client_id(client_id)["state"] == OrderState.RISK_APPROVED.value
        assert (
            repos.db.query(
                "SELECT * FROM position_realizations WHERE exit_order_id = ?", (client_id,)
            )
            == []
        )
        assert repos.db.query("SELECT * FROM fills WHERE client_order_id = ?", (client_id,)) == []

        monkeypatch.setattr(repos.positions, "update_open_after_exit", original_update)
        assert service.record_exit(position, response, "postgres_probe") == dec("3.556")
        assert (
            repos.db.query_one("SELECT qty FROM positions WHERE id = ?", (position_id,))["qty"]
            == "0.6"
        )
        assert (
            len(repos.db.query("SELECT * FROM fills WHERE client_order_id = ?", (client_id,))) == 1
        )
        assert (
            repos.orders.get_by_client_id(client_id)["state"] == OrderState.PARTIALLY_FILLED.value
        )
    finally:
        db.close()


def test_postgres_instance_lock_excludes_second_connection():
    url = os.environ["TEST_POSTGRES_URL"]
    first_db = Database(url)
    second_db = Database(url)
    try:
        first_db.migrate(MIGRATIONS)
        second_db.migrate(MIGRATIONS)
        clock = FrozenClock(T0)
        first = InstanceLock(first_db, clock)
        second = InstanceLock(second_db, clock)
        assert first.acquire()
        assert not second.acquire()
        first.release()
        assert second.acquire()
        second.release()
    finally:
        first_db.close()
        second_db.close()
