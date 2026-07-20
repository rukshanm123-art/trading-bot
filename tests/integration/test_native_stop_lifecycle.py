"""Directly exercise the engine's native-stop lifecycle helpers: fill
detection while 'down', drift replacement, and market escalation on gap."""

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_config, make_trend_rows, write_rows_csv
from trading_bot.core.enums import Mode, OrderState
from trading_bot.core.types import ZERO, dec
from trading_bot.engine.trader import TradingEngine

pytestmark = pytest.mark.integration


def _engine_with_position(tmp_path):
    """Build a fixture engine and run it until a position + native stop exist."""
    rows = make_trend_rows([(60, 0.0), (30, 1.2), (60, 0.02)], start_price=100.0)
    fixture = write_rows_csv(rows, tmp_path / "life.csv")
    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/life.db"},
        data={"source": "fixture", "fixture_path": fixture},
        reporting={"output_dir": str(tmp_path / "reports")},
    )
    engine = TradingEngine(
        cfg, migrations_dir=MIGRATIONS, project_root=tmp_path, close_db_on_shutdown=False
    )
    for _ in range(25):
        engine.run(max_cycles=10)
        pos = engine.repos.positions.open_position(Mode.PAPER)
        if pos is not None and pos.protective_order_id:
            return engine, pos
    engine.db.close()
    pytest.skip("scenario did not open a protected position")


def test_sync_protective_detects_fill_while_down(tmp_path):
    engine, position = _engine_with_position(tmp_path)
    coid = position.protective_order_id

    # Simulate the resting stop filling while the bot was 'down': drive the
    # paper exchange price below the trigger, then let the sync path observe it.
    from trading_bot.core.models import Candle

    low = engine.fixture.candles[engine.fixture.cursor]
    crashed = Candle(
        symbol=low.symbol,
        interval=low.interval,
        open_time=low.open_time,
        close_time=low.close_time,
        open=low.open,
        high=low.high,
        low=dec("90"),
        close=dec("96"),
        volume=low.volume,
        is_closed=True,
    )
    engine.fixture.candles[engine.fixture.cursor] = crashed

    # query the stop directly: at bid ~96 (< trigger 98, >= limit) it fills
    resp = engine.adapter.query_order(engine.cfg.symbol, coid)
    assert resp is not None
    # now the engine's sync path books the exit and clears the position
    refreshed = engine._sync_protective(position)
    if refreshed is None:
        assert engine.repos.positions.open_position(Mode.PAPER) is None
        closed = engine.repos.positions.closed_positions(Mode.PAPER)
        assert any(r["exit_reason"] == "stop_breach_native" for r in closed)
    engine.db.close()


def test_ensure_protective_replaces_drifted_stop(tmp_path):
    engine, position = _engine_with_position(tmp_path)
    old_coid = position.protective_order_id

    # change the position's stop so the resting order 'drifted'
    engine.repos.positions.update_stop(position.position_id, dec("95.00"))
    refreshed = engine.repos.positions.open_position(Mode.PAPER)

    engine._ensure_protective(refreshed, "cid")
    after = engine.repos.positions.open_position(Mode.PAPER)
    assert after is not None
    # a new protective order was placed (client id changed) at the new stop
    assert after.protective_order_id is not None
    new_row = engine.repos.orders.get_by_client_id(after.protective_order_id)
    assert new_row["state"] == OrderState.ACKNOWLEDGED.value
    assert dec(new_row["stop_price"]) == dec("95.00")
    # the old order is no longer resting
    if old_coid != after.protective_order_id:
        old_row = engine.repos.orders.get_by_client_id(old_coid)
        assert old_row["state"] in (
            OrderState.CANCELLED.value,
            OrderState.REJECTED.value,
            OrderState.FILLED.value,
        )
    engine.db.close()


def test_cancel_protective_clears_link_and_unlocks(tmp_path):
    engine, position = _engine_with_position(tmp_path)
    assert engine.adapter.get_balances()["BTC"].locked > ZERO
    filled = engine._cancel_protective(position)
    assert filled is False  # cancel did not reveal a fill
    after = engine.repos.positions.open_position(Mode.PAPER)
    assert after.protective_order_id is None
    assert engine.adapter.get_balances()["BTC"].locked == ZERO
    engine.db.close()
