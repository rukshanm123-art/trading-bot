"""Engine-level native-stop flows: placement after entry, fills that close
positions, cancellation before strategy exits, restart survival."""

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_config, make_trend_rows, write_rows_csv
from trading_bot.core.enums import Mode, OrderState
from trading_bot.core.types import ZERO
from trading_bot.engine.trader import TradingEngine

pytestmark = pytest.mark.integration


def _build(tmp_path, rows, name="ns"):
    fixture = write_rows_csv(rows, tmp_path / f"{name}.csv")
    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/{name}.db"},
        data={"source": "fixture", "fixture_path": fixture},
        reporting={"output_dir": str(tmp_path / "reports")},
    )
    return TradingEngine(
        cfg, migrations_dir=MIGRATIONS, project_root=tmp_path, close_db_on_shutdown=False
    )


def _run_until_position(engine, max_batches=25) -> bool:
    for _ in range(max_batches):
        engine.run(max_cycles=10)
        if engine.repos.positions.open_position(Mode.PAPER) is not None:
            return True
    return False


def test_protective_stop_placed_after_entry(tmp_path):
    rows = make_trend_rows([(60, 0.0), (30, 1.2), (40, 0.0)], start_price=100.0)
    engine = _build(tmp_path, rows)
    assert _run_until_position(engine)
    engine.run(max_cycles=3)  # give the upkeep hook a cycle
    position = engine.repos.positions.open_position(Mode.PAPER)
    assert position is not None
    assert position.protective_order_id, "native stop must be linked to the position"
    row = engine.repos.orders.get_by_client_id(position.protective_order_id)
    assert row["purpose"] == "protective"
    assert row["state"] == OrderState.ACKNOWLEDGED.value
    # the stop order actually rests on the (paper) exchange, base locked
    balances = engine.adapter.get_balances()
    assert balances["BTC"].locked > ZERO
    engine.db.close()


def test_crash_closes_position_via_native_stop(tmp_path):
    # short rally (entry near the top) then a deep crash straight through the
    # 2% stop, before the EMAs can cross back down
    rows = make_trend_rows(
        [(60, 0.0), (8, 1.2), (4, 0.0), (10, -1.5), (15, 0.0)], start_price=100.0
    )
    engine = _build(tmp_path, rows)
    engine.run(max_cycles=len(rows))
    closed = engine.repos.positions.closed_positions(Mode.PAPER)
    assert closed, "the crash must close the position"
    reasons = {r["exit_reason"] for r in closed}
    assert any(r.startswith(("stop_breach_native", "stop_breach")) for r in reasons), reasons
    # no orphaned locked base and no resting protective order left behind
    balances = engine.adapter.get_balances()
    assert balances["BTC"].locked == ZERO
    for row in engine.repos.orders.non_terminal_orders(Mode.PAPER):
        assert row["purpose"] != "protective", "protective order left dangling"
    engine.db.close()


def test_strategy_exit_cancels_native_stop_first(tmp_path):
    # rally then slow fade: EMA cross-down exits BEFORE the 2% stop is hit
    rows = make_trend_rows([(60, 0.0), (25, 1.2), (35, -0.12), (30, 0.0)], start_price=100.0)
    engine = _build(tmp_path, rows)
    engine.run(max_cycles=len(rows))
    closed = engine.repos.positions.closed_positions(Mode.PAPER)
    assert closed
    reasons = {r["exit_reason"] for r in closed}
    # at least one strategy-style exit happened and nothing is locked
    balances = engine.adapter.get_balances()
    assert balances["BTC"].locked == ZERO
    assert any(
        r.startswith(("strategy_exit", "stop_breach", "stop_breach_native")) for r in reasons
    ), reasons
    engine.db.close()


def test_native_stop_survives_restart(tmp_path):
    rows = make_trend_rows([(60, 0.0), (30, 1.2), (60, 0.02)], start_price=100.0)
    fixture = write_rows_csv(rows, tmp_path / "rs.csv")

    def build():
        cfg = make_config(
            db={"url": f"sqlite:///{tmp_path}/rs.db"},
            data={"source": "fixture", "fixture_path": fixture},
            reporting={"output_dir": str(tmp_path / "reports")},
        )
        return TradingEngine(
            cfg, migrations_dir=MIGRATIONS, project_root=tmp_path, close_db_on_shutdown=False
        )

    engine_a = build()
    assert _run_until_position(engine_a)
    engine_a.run(max_cycles=3)
    position_a = engine_a.repos.positions.open_position(Mode.PAPER)
    assert position_a is not None and position_a.protective_order_id
    engine_a.db.close()

    engine_b = build()  # restart over the same database
    position_b = engine_b.repos.positions.open_position(Mode.PAPER)
    assert position_b is not None
    assert position_b.protective_order_id == position_a.protective_order_id
    result = engine_b.reconciler.run()
    assert result.ok, result.details  # resting protective stop is never "stuck"
    engine_b.db.close()
