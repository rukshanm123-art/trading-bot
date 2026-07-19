"""Backtester: deterministic, honest, walk-forward keeps the test segment
untouched by selection."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_config, make_trend_rows, write_rows_csv
from trading_bot.backtest.engine import run_backtest, walk_forward
from trading_bot.backtest.synth import generate_rows, write_csv
from trading_bot.core.enums import SignalAction
from trading_bot.core.models import SignalDecision
from trading_bot.engine.trader import TradingEngine

pytestmark = pytest.mark.integration


@pytest.fixture
def fixture_csv(tmp_path):
    rows = generate_rows(n=400, seed=13)
    path = tmp_path / "bt.csv"
    write_csv(rows, path)
    return str(path)


def test_backtest_produces_full_metric_set(fixture_csv):
    res = run_backtest(make_config(), fixture_csv)
    for key in (
        "total_return_pct",
        "buy_and_hold_return_pct",
        "no_trade_return_pct",
        "max_drawdown_pct",
        "trades",
        "win_rate_pct",
        "profit_factor",
        "expectancy",
        "avg_win",
        "avg_loss",
        "total_fees",
        "exposure_time_pct",
        "turnover_quote",
        "decisions",
        "disclaimer",
    ):
        assert key in res, f"missing metric {key}"
    assert res["decisions"] > 300
    assert "guarantee" in res["disclaimer"]


def test_backtest_is_deterministic(fixture_csv):
    a = run_backtest(make_config(), fixture_csv)
    b = run_backtest(make_config(), fixture_csv)
    assert a == b


def test_backtest_exposure_and_costs_are_sane(tmp_path):
    rows = make_trend_rows([(60, 0.0), (30, 1.0), (30, -0.8), (30, 0.0)], 100.0)
    path = write_rows_csv(rows, tmp_path / "trend.csv")
    res = run_backtest(make_config(), path)
    assert 0 <= res["exposure_time_pct"] <= 100
    assert Decimal(res["max_drawdown_pct"]) >= 0
    if res["trades"] > 0:
        assert Decimal(res["total_fees"]) > 0


def test_walk_forward_out_of_sample_untouched(tmp_path):
    rows = generate_rows(n=600, seed=29)
    path = tmp_path / "wf.csv"
    write_csv(rows, path)
    res = walk_forward(make_config(), path, work_dir=tmp_path / "wf_work")
    assert res["selected_params"]["fast"] < res["selected_params"]["slow"]
    assert len(res["train"]) == 5
    assert res["out_of_sample_test"]["label"].startswith("out-of-sample")
    assert "overstate" in res["honesty_note"]
    # the chosen parameters come from validation, not from the test segment
    chosen = (res["selected_params"]["fast"], res["selected_params"]["slow"])
    val_params = {(r["strategy"]["fast"], r["strategy"]["slow"]) for r in res["validation"]}
    assert chosen in val_params


def test_next_candle_backtest_execution_uses_subsequent_open(tmp_path):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    rows = []
    prices = [100, 100, 100, 150, 150]
    for i, close in enumerate(prices):
        open_p = 150 if i == 3 else close
        rows.append(
            {
                "open_time": (start + timedelta(hours=i)).isoformat(),
                "open": str(open_p),
                "high": str(max(open_p, close)),
                "low": str(min(open_p, close)),
                "close": str(close),
                "volume": "10",
            }
        )
    fixture = write_rows_csv(rows, tmp_path / "gap_exec.csv")
    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/bt_timing.db"},
        data={"source": "fixture", "fixture_path": fixture},
        risk={"max_gap_pct": "60"},
    )
    engine = TradingEngine(
        cfg,
        migrations_dir=MIGRATIONS,
        project_root=tmp_path,
        close_db_on_shutdown=False,
    )

    class OneShotStrategy:
        name = "one_shot"
        version = "test"
        warmup = 2
        fired = False

        def evaluate(self, candles, has_position):
            assert all(c.open != Decimal("150") for c in candles)
            self.fired = True
            return SignalDecision(
                strategy=self.name,
                strategy_version=self.version,
                symbol="BTCUSDT",
                action=SignalAction.ENTER_LONG,
                candle_open_time=candles[-1].open_time,
                reason="forced",
            )

    engine.strategy = OneShotStrategy()
    engine.market_data.min_candles = engine.strategy.warmup
    assert engine.fixture is not None
    engine.fixture.cursor = 3
    engine.cycle()
    row = engine.repos.db.query_one("SELECT intent_json, response_json FROM orders")
    assert row is not None
    intent = json.loads(row["intent_json"])
    response = json.loads(row["response_json"])
    assert intent["est_entry_price"] == "150.0375"
    assert response["cumulative_quote"] != "6.0"
    engine.db.close()
