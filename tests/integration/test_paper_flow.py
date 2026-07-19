"""End-to-end paper flow over fixture data: the engine trades, records
decisions, produces daily reports, and keeps a valid audit chain."""

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_config, make_trend_rows, write_rows_csv
from trading_bot.core.enums import Mode
from trading_bot.engine.trader import TradingEngine
from trading_bot.storage.audit import AuditLog

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario_engine(tmp_path):
    """Flat -> rally (forces an EMA cross entry) -> slow fade -> crash."""
    rows = make_trend_rows(
        [(60, 0.0), (25, 1.2), (20, -0.1), (10, -1.5), (20, 0.0)], start_price=100.0
    )
    fixture = write_rows_csv(rows, tmp_path / "scenario.csv")
    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/flow.db"},
        data={"source": "fixture", "fixture_path": fixture},
        reporting={"daily_time_local": "17:00", "output_dir": str(tmp_path / "reports")},
    )
    engine = TradingEngine(
        cfg, migrations_dir=MIGRATIONS, project_root=tmp_path, close_db_on_shutdown=False
    )
    return engine


def test_full_paper_run(scenario_engine, tmp_path):
    engine = scenario_engine
    engine.run(max_cycles=200)

    repos = engine.repos
    # decisions recorded for every processed candle (HOLD included)
    assert repos.decisions.count(Mode.PAPER) > 50
    # equity snapshots recorded continuously
    snaps = repos.db.query("SELECT COUNT(*) AS n FROM balance_snapshots")
    assert snaps[0]["n"] > 50
    # the rally produced an entry, and the crash forced an exit
    closed = repos.positions.closed_positions(Mode.PAPER)
    opened_count = repos.db.query("SELECT COUNT(*) AS n FROM positions")[0]["n"]
    assert opened_count >= 1, "rally should have triggered at least one entry"
    assert closed, "crash should have closed the position"
    reasons = {row["exit_reason"] for row in closed}
    assert any(r.startswith(("stop_breach", "strategy_exit")) for r in reasons), reasons
    # daily reports were generated along the way
    assert repos.db.query("SELECT COUNT(*) AS n FROM reports")[0]["n"] >= 1
    # audit chain intact
    ok, bad = AuditLog(repos.db).verify_chain()
    assert ok, f"audit chain broken at {bad}"
    engine.db.close()


def test_stop_loss_bounded_single_trade_loss(scenario_engine):
    """Realised loss per trade stays near the planned risk (stop distance +
    fees + slippage) — never a runaway loss."""
    from trading_bot.core.types import dec

    engine = scenario_engine
    engine.run(max_cycles=200)
    closed = engine.repos.positions.closed_positions(Mode.PAPER)
    assert closed
    for row in closed:
        pnl = dec(str(row["realized_pnl"]))
        if pnl < 0:
            entry_notional = dec(str(row["qty"])) * dec(str(row["avg_entry_price"]))
            # 2% stop + generous allowance for gap-through, fees, slippage
            assert -pnl <= entry_notional * dec(
                "0.05"
            ), f"loss {pnl} too large for notional {entry_notional}"
    engine.db.close()


def test_no_entries_after_gap_candle(tmp_path):
    """A >10% single-candle gap must freeze entries (GAP_TOLERANCE_EXCEEDED)."""
    rows = make_trend_rows([(60, 0.0), (1, -15.0), (40, 1.5)], start_price=100.0)
    fixture = write_rows_csv(rows, tmp_path / "gap.csv")
    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/gap.db"},
        data={"source": "fixture", "fixture_path": fixture},
        reporting={"output_dir": str(tmp_path / "reports")},
    )
    engine = TradingEngine(
        cfg, migrations_dir=MIGRATIONS, project_root=tmp_path, close_db_on_shutdown=False
    )
    engine.run(max_cycles=120)
    # the gap candle stays inside the 60-candle validation window for the whole
    # post-gap rally, so no entry order may exist at all
    orders = engine.repos.db.query("SELECT COUNT(*) AS n FROM orders WHERE purpose='entry'")
    assert orders[0]["n"] == 0
    engine.db.close()
