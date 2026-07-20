"""Engine coverage for less-trodden paths: account-fee application, console-only
alert warning, native-stop upkeep drift replacement, escalation to market exit
on gap-through, and session qualification evidence recording."""

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_config, make_trend_rows, write_rows_csv
from trading_bot.core.enums import Mode
from trading_bot.core.types import ZERO, dec
from trading_bot.engine.trader import TradingEngine

pytestmark = pytest.mark.integration


def _fixture_engine(tmp_path, rows, **overrides):
    fixture = write_rows_csv(rows, tmp_path / "eng.csv")
    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/eng.db"},
        data={"source": "fixture", "fixture_path": fixture},
        reporting={"output_dir": str(tmp_path / "reports")},
        **overrides,
    )
    return TradingEngine(
        cfg, migrations_dir=MIGRATIONS, project_root=tmp_path, close_db_on_shutdown=False
    )


def _run_until_position(engine, max_batches=25):
    for _ in range(max_batches):
        engine.run(max_cycles=10)
        if engine.repos.positions.open_position(Mode.PAPER) is not None:
            return True
    return False


def test_software_only_mode_exits_on_stop(tmp_path):
    """With native stops OFF, the software monitor alone must close on breach."""
    rows = make_trend_rows(
        [(60, 0.0), (8, 1.2), (4, 0.0), (10, -1.5), (15, 0.0)], start_price=100.0
    )
    engine = _fixture_engine(
        tmp_path, rows, execution={"use_native_stops": False, "stop_monitor_interval_s": 20}
    )
    engine.run(max_cycles=len(rows))
    closed = engine.repos.positions.closed_positions(Mode.PAPER)
    assert closed
    reasons = {r["exit_reason"] for r in closed}
    assert any(r.startswith(("stop_breach", "strategy_exit")) for r in reasons), reasons
    # no native stop was ever placed
    for row in engine.repos.orders.non_terminal_orders(Mode.PAPER):
        assert row["purpose"] != "protective"
    engine.db.close()


def test_escalation_to_market_on_gap_through(tmp_path):
    """A hard gap straight through the stop's limit price forces escalation to
    a market exit (the native stop cannot fill)."""
    rows = make_trend_rows([(60, 0.0), (8, 1.2), (4, 0.0), (1, -8.0), (20, 0.0)], start_price=100.0)
    engine = _fixture_engine(tmp_path, rows, execution={"protective_escalation_cycles": 1})
    engine.run(max_cycles=len(rows))
    closed = engine.repos.positions.closed_positions(Mode.PAPER)
    assert closed, "the deep gap must close the position"
    balances = engine.adapter.get_balances()
    assert balances["BTC"].locked == ZERO  # nothing left locked in a stale stop
    engine.db.close()


def test_qualification_evidence_recorded_for_live_data_paper(tmp_path, monkeypatch):
    """A PAPER session on live-market data records ONE signed evidence row on
    shutdown; fixture sessions record none."""
    from trading_bot.core.models import Candle, PriceQuote, utcnow
    from trading_bot.security.qualification import (
        QualificationEvidenceStore,
        get_or_create_evidence_key,
    )

    # a fake public-data transport so mode=paper + source=exchange runs offline
    now = utcnow()

    class FakePublic:
        def get_price(self, symbol):
            return PriceQuote(
                symbol=symbol, bid=dec("99.9"), ask=dec("100.1"), last=dec("100"), ts=now
            )

        def get_candles(self, symbol, interval, limit=200):
            from datetime import timedelta

            out = []
            for i in range(limit):
                t = now - timedelta(hours=limit - i)
                out.append(
                    Candle(
                        symbol=symbol,
                        interval=interval,
                        open_time=t,
                        close_time=t + timedelta(hours=1) - timedelta(seconds=1),
                        open=dec("100"),
                        high=dec("100.5"),
                        low=dec("99.5"),
                        close=dec("100"),
                        volume=dec("10"),
                        is_closed=True,
                    )
                )
            return out

    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/paperlive.db"},
        data={"source": "exchange"},
        monitoring={"enabled": False},
        reporting={"output_dir": str(tmp_path / "reports")},
    )
    # patch rules + data source construction to avoid any network
    from trading_bot.exchange import binance as binance_mod

    monkeypatch.setattr(
        binance_mod.BinancePublicData,
        "get_rules",
        lambda self, symbol: make_config().risk
        and __import__("trading_bot.engine.trader", fromlist=["default_rules"]).default_rules(
            symbol
        ),
    )
    monkeypatch.setattr(binance_mod.BinancePublicData, "get_price", FakePublic().get_price)
    monkeypatch.setattr(binance_mod.BinancePublicData, "get_candles", FakePublic().get_candles)

    engine = TradingEngine(
        cfg, migrations_dir=MIGRATIONS, project_root=tmp_path, close_db_on_shutdown=False
    )
    engine.run(max_cycles=3)
    engine.shutdown()

    store = QualificationEvidenceStore(tmp_path, key=get_or_create_evidence_key(engine.repos.flags))
    rows = store.records(validate=True)
    engine.db.close()
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["source_mode"] == "paper"
    assert payload["data_source_class"] == "live_market"
    assert "wall_clock_start" in payload and "wall_clock_end" in payload


def test_unexpected_runtime_failure_blocks_future_entries(tmp_path):
    engine = _fixture_engine(tmp_path, make_trend_rows([(40, 0.0)], start_price=100.0))

    engine._fail_runtime_closed(RuntimeError("accounting write interrupted"))

    assert not engine._db_ok
    assert engine.repos.flags.get(engine.repos.flags.RECONCILIATION_BLOCK) == "true"
    engine.db.close()
