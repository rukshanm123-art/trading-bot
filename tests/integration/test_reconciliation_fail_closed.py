"""Reconciliation failures persistently block new entries."""

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_config, make_trend_rows, write_rows_csv
from trading_bot.engine.trader import TradingEngine
from trading_bot.risk.state import RiskStateService

pytestmark = pytest.mark.integration


def build_engine(tmp_path):
    fixture = write_rows_csv(make_trend_rows([(80, 0.0)], start_price=100.0), tmp_path / "r.csv")
    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/r.db"},
        data={"source": "fixture", "fixture_path": fixture},
        reporting={"output_dir": str(tmp_path / "reports")},
    )
    return TradingEngine(
        cfg,
        migrations_dir=MIGRATIONS,
        project_root=tmp_path,
        close_db_on_shutdown=False,
    )


def test_reconciliation_exception_fail_closed(tmp_path):
    engine = build_engine(tmp_path)

    def boom():
        raise RuntimeError("unexpected database/exchange reconciliation crash")

    engine.reconciler.run = boom
    assert engine._reconcile() is False
    assert engine.repos.flags.is_true(engine.repos.flags.RECONCILIATION_BLOCK)
    last = engine.repos.events.last_reconciliation()
    assert last is not None
    assert last["ok"] == 0
    alerts = engine.repos.db.query("SELECT * FROM alerts WHERE kind='reconciliation_exception'")
    assert alerts
    engine.db.close()


def test_reconciliation_block_survives_restart_and_blocks_risk(tmp_path):
    engine = build_engine(tmp_path)
    engine.repos.flags.set(engine.repos.flags.RECONCILIATION_BLOCK, "true")
    engine.db.close()

    restarted = build_engine(tmp_path)
    state = RiskStateService(restarted.repos, restarted.cfg, restarted.clock).snapshot(
        restarted.cfg.paper.starting_quote
    )
    assert state.reconciliation_blocked
    restarted.db.close()


def test_market_data_success_does_not_clear_reconciliation_block(tmp_path):
    engine = build_engine(tmp_path)
    engine.repos.flags.set(engine.repos.flags.RECONCILIATION_BLOCK, "true")
    candles, cval = engine.market_data.closed_candles()
    quote, qval = engine.market_data.quote()
    assert candles and cval.ok and quote is not None and qval.ok
    assert engine.repos.flags.is_true(engine.repos.flags.RECONCILIATION_BLOCK)
    engine.db.close()


def test_successful_reconciliation_clears_block_only_through_reconciler(tmp_path):
    engine = build_engine(tmp_path)
    engine.repos.flags.set(engine.repos.flags.RECONCILIATION_BLOCK, "true")
    result = engine.reconciler.run()
    assert result.ok, result.details
    assert not engine.repos.flags.is_true(engine.repos.flags.RECONCILIATION_BLOCK)
    engine.db.close()
