"""Kill switch fired while a position is open: entries stop, the protective
exit keeps working, and the emergency policy decides the position's fate."""

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_config, make_trend_rows, write_rows_csv
from trading_bot.config import constants as C
from trading_bot.core.enums import Mode
from trading_bot.engine.trader import TradingEngine

pytestmark = pytest.mark.integration


def _rows_rally_then_crash():
    # flat -> rally (entry) -> plateau -> crash breaching the 2% stop
    return make_trend_rows(
        [(60, 0.0), (25, 1.2), (15, 0.0), (12, -1.0), (20, 0.0)], start_price=100.0
    )


def _build(tmp_path, policy: str) -> TradingEngine:
    fixture = write_rows_csv(_rows_rally_then_crash(), tmp_path / "ks.csv")
    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/ks.db"},
        data={"source": "fixture", "fixture_path": fixture},
        reporting={"output_dir": str(tmp_path / "reports")},
        emergency_position_policy=policy,
    )
    return TradingEngine(
        cfg, migrations_dir=MIGRATIONS, project_root=tmp_path, close_db_on_shutdown=False
    )


def _run_until_position(engine, max_batches=20) -> bool:
    for _ in range(max_batches):
        engine.run(max_cycles=10)
        if engine.repos.positions.open_position(Mode.PAPER) is not None:
            return True
    return False


def test_hold_and_monitor_policy(tmp_path):
    engine = _build(tmp_path, "hold_and_monitor")
    assert _run_until_position(engine), "scenario must open a position"

    entries_before = engine.repos.db.query(
        "SELECT COUNT(*) AS n FROM orders WHERE purpose='entry'"
    )[0]["n"]
    (tmp_path / C.STOP_FILE_NAME).write_text("manual emergency stop\n")

    engine.run(max_cycles=20)  # position held, no liquidation, no new entries
    position = engine.repos.positions.open_position(Mode.PAPER)
    assert position is not None, "hold_and_monitor must not liquidate"
    entries_after = engine.repos.db.query("SELECT COUNT(*) AS n FROM orders WHERE purpose='entry'")[
        0
    ]["n"]
    assert entries_after == entries_before, "no new entries under kill switch"

    # a critical alert about the kill switch was recorded
    alerts = engine.repos.db.query("SELECT * FROM alerts WHERE kind='kill_switch'")
    assert alerts

    # ... and when the crash breaches the stop, the protective exit STILL fires
    engine.run(max_cycles=80)
    closed = engine.repos.positions.closed_positions(Mode.PAPER)
    assert closed and closed[0]["exit_reason"].startswith(("stop_breach", "strategy_exit"))
    engine.db.close()


def test_close_at_market_policy(tmp_path):
    engine = _build(tmp_path, "close_at_market")
    assert _run_until_position(engine), "scenario must open a position"
    (tmp_path / C.STOP_FILE_NAME).write_text("emergency: close everything\n")

    engine.run(max_cycles=15)
    assert engine.repos.positions.open_position(Mode.PAPER) is None
    closed = engine.repos.positions.closed_positions(Mode.PAPER)
    assert closed
    assert closed[0]["exit_reason"].startswith("kill_switch_close_policy")
    engine.db.close()
