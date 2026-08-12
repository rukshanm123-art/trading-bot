"""Daily reports use configured local-day boundaries converted to UTC."""

from datetime import UTC, datetime, timedelta

from tests.helpers import RULES, make_config
from trading_bot.core.enums import Mode
from trading_bot.core.models import iso
from trading_bot.core.types import dec
from trading_bot.reporting.daily import DailyReportBuilder, local_day_bounds_utc


def insert_snapshot(repos, ts: datetime, equity: str = "100", price: str = "100") -> None:
    repos.db.execute(
        "INSERT INTO balance_snapshots (id, ts, mode, balances_json, equity, quote_price) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts.isoformat(), ts.isoformat(), Mode.PAPER.value, "{}", equity, price),
    )


def insert_realization(repos, ts: datetime, pnl: str, fee: str) -> None:
    repos.db.execute(
        "INSERT INTO position_realizations (id, position_id, mode, symbol, exit_order_id, qty, "
        "avg_entry_price, exit_price, entry_fee_allocated, exit_fee, realized_pnl, "
        "exit_reason, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts.isoformat(),
            "p1",
            Mode.PAPER.value,
            "BTCUSDT",
            f"exit-{ts.timestamp()}",
            "0.1",
            "100",
            "110",
            "0.01",
            fee,
            pnl,
            "strategy_exit",
            ts.isoformat(),
        ),
    )


def test_auckland_midnight_report_includes_each_event_once(repos):
    cfg = make_config(timezone="Pacific/Auckland")
    before_local_midnight = datetime(2025, 6, 2, 11, 59, tzinfo=UTC)
    after_local_midnight = datetime(2025, 6, 2, 12, 1, tzinfo=UTC)
    insert_snapshot(repos, before_local_midnight, "100", "100")
    insert_snapshot(repos, after_local_midnight, "101", "100")
    insert_realization(repos, before_local_midnight, "1", "0.1")
    insert_realization(repos, after_local_midnight, "2", "0.2")
    repos.events.alert("info", "before", "before", {})
    repos.db.execute(
        "UPDATE alerts SET ts = ? WHERE kind = ?", (iso(before_local_midnight), "before")
    )
    repos.events.alert("info", "after", "after", {})
    repos.db.execute(
        "UPDATE alerts SET ts = ? WHERE kind = ?", (iso(after_local_midnight), "after")
    )

    builder = DailyReportBuilder(
        repos, cfg, RULES, clock=type("C", (), {"now": lambda s: after_local_midnight})()
    )
    report, _ = builder.build("2025-06-03", {}, False, "", False)
    assert report["realized_pnl_today"] == "2"
    assert report["fees_today"] == "0.2"
    assert [e["kind"] for e in report["risk_limit_events"]] == ["after"]

    prior, _ = builder.build("2025-06-02", {}, False, "", False)
    assert prior["realized_pnl_today"] == "1"
    assert prior["fees_today"] == "0.1"


def test_auckland_daylight_saving_boundaries_are_utc_intervals():
    start, end = local_day_bounds_utc("2025-09-28", "Pacific/Auckland")
    assert start == datetime(2025, 9, 27, 12, 0, tzinfo=UTC)
    assert end == datetime(2025, 9, 28, 11, 0, tzinfo=UTC)


def test_daily_report_surfaces_latched_loss_pause(repos):
    cfg = make_config(timezone="Pacific/Auckland")
    clock_now = datetime(2025, 6, 3, 18, 0, tzinfo=UTC)
    insert_snapshot(repos, clock_now, "97", "65000")
    for index in range(3):
        ts = clock_now - timedelta(hours=15 - index)
        position_id = repos.positions.insert_open(
            Mode.PAPER,
            "BTCUSDT",
            dec("0.00010"),
            dec("65000"),
            dec("63700"),
            f"entry-{index}",
            dec("0.01"),
            ts,
        )
        repos.positions.close(
            position_id,
            f"exit-{index}",
            dec("0.01"),
            dec("-1"),
            "test",
            ts + timedelta(minutes=30),
        )

    builder = DailyReportBuilder(
        repos, cfg, RULES, clock=type("C", (), {"now": lambda self: clock_now})()
    )
    report, markdown = builder.build("2025-06-04", {}, False, "", False)

    assert report["consecutive_loss_pause"]["active"]
    assert report["consecutive_loss_pause"]["clears_automatically"] is False
    assert "THIS DOES NOT CLEAR WITH TIME" in markdown
    assert "acknowledge-loss-pause" in markdown
