"""Partial entry/exit accounting stays cumulative and exact."""

from datetime import UTC, datetime

from tests.helpers import RULES
from trading_bot.core.enums import Mode, OrderState, OrderType, Side
from trading_bot.core.models import Fill, OrderResponse
from trading_bot.core.types import dec
from trading_bot.portfolio.accounting import PortfolioService


def response(
    coid: str,
    side: Side,
    executed: str,
    quote: str,
    fills: tuple[Fill, ...],
    state: OrderState = OrderState.PARTIALLY_FILLED,
) -> OrderResponse:
    return OrderResponse(
        client_order_id=coid,
        exchange_order_id=f"ex-{coid}",
        symbol="BTCUSDT",
        side=side,
        order_type=OrderType.MARKET,
        state=state,
        requested_qty=dec("1"),
        executed_qty=dec(executed),
        cumulative_quote=dec(quote),
        fills=fills,
        ts=datetime(2025, 1, 1, tzinfo=UTC),
    )


def test_partial_entry_across_multiple_fills_updates_position(repos):
    svc = PortfolioService(repos, Mode.PAPER, RULES)
    first = response(
        "entry-1",
        Side.BUY,
        "0.4",
        "40",
        (Fill(dec("100"), dec("0.4"), dec("0.0004"), "BTC"),),
    )
    svc.record_entry(first, dec("98"))
    pos = repos.positions.open_position(Mode.PAPER)
    assert pos is not None
    assert pos.qty == dec("0.3996")
    assert pos.avg_entry_price == dec("100")
    assert pos.entry_fee == dec("0.0400")

    second = response(
        "entry-1",
        Side.BUY,
        "0.7",
        "70.6",
        (
            Fill(dec("100"), dec("0.4"), dec("0.0004"), "BTC"),
            Fill(dec("102"), dec("0.3"), dec("0.0003"), "BTC"),
        ),
    )
    svc.record_entry(second, dec("98"))
    pos = repos.positions.open_position(Mode.PAPER)
    assert pos is not None
    assert pos.qty == dec("0.6993")
    assert pos.avg_entry_price == dec("100.8571428571428571428571429")
    assert pos.entry_fee == dec("0.0706")


def test_partial_exit_leaves_residual_exposure_and_realizes_only_filled_qty(repos):
    svc = PortfolioService(repos, Mode.PAPER, RULES)
    repos.positions.insert_open(
        Mode.PAPER,
        "BTCUSDT",
        dec("1.0"),
        dec("100"),
        dec("98"),
        "entry-1",
        dec("1.0"),
    )
    pos = repos.positions.open_position(Mode.PAPER)
    assert pos is not None

    exit_resp = response(
        "exit-1",
        Side.SELL,
        "0.4",
        "44",
        (Fill(dec("110"), dec("0.4"), dec("0.044"), "USDT"),),
        state=OrderState.PARTIALLY_FILLED,
    )
    realized = svc.record_exit(pos, exit_resp, "strategy_exit")
    assert realized == dec("3.556")
    remaining = repos.positions.open_position(Mode.PAPER)
    assert remaining is not None
    assert remaining.qty == dec("0.6")
    assert remaining.entry_fee == dec("0.6")
    assert remaining.entry_order_id == "entry-1"

    rows = repos.positions.realizations_between(
        Mode.PAPER,
        datetime(2024, 12, 31, tzinfo=UTC),
        datetime(2025, 1, 2, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0]["qty"] == "0.4"
    assert rows[0]["entry_fee_allocated"] == "0.4"


def test_later_partial_exit_completion_closes_with_total_pnl(repos):
    svc = PortfolioService(repos, Mode.PAPER, RULES)
    pid = repos.positions.insert_open(
        Mode.PAPER,
        "BTCUSDT",
        dec("1.0"),
        dec("100"),
        dec("98"),
        "entry-1",
        dec("1.0"),
    )
    first_pos = repos.positions.open_position(Mode.PAPER)
    assert first_pos is not None
    svc.record_exit(
        first_pos,
        response(
            "exit-1",
            Side.SELL,
            "0.4",
            "44",
            (Fill(dec("110"), dec("0.4"), dec("0.044"), "USDT"),),
        ),
        "strategy_exit",
    )
    second_pos = repos.positions.open_position(Mode.PAPER)
    assert second_pos is not None
    svc.record_exit(
        second_pos,
        response(
            "exit-2",
            Side.SELL,
            "0.6",
            "63",
            (Fill(dec("105"), dec("0.6"), dec("0.063"), "USDT"),),
            state=OrderState.FILLED,
        ),
        "strategy_exit",
    )
    assert repos.positions.open_position(Mode.PAPER) is None
    row = repos.db.query_one("SELECT * FROM positions WHERE id = ?", (pid,))
    assert row["status"] == "closed"
    assert row["realized_pnl"] == "5.893"
    assert row["exit_fee"] == "0.107"


def test_dust_after_partial_exit_is_explicit(repos):
    svc = PortfolioService(repos, Mode.PAPER, RULES)
    repos.positions.insert_open(
        Mode.PAPER,
        "BTCUSDT",
        dec("0.05000"),
        dec("100"),
        dec("98"),
        "entry-1",
        dec("0.05"),
    )
    pos = repos.positions.open_position(Mode.PAPER)
    assert pos is not None
    svc.record_exit(
        pos,
        response(
            "exit-dust",
            Side.SELL,
            "0.04999",
            "5.4989",
            (Fill(dec("110"), dec("0.04999"), dec("0.0054989"), "USDT"),),
        ),
        "stop_breach",
    )
    assert repos.positions.open_position(Mode.PAPER) is None
    row = repos.db.query_one("SELECT status, qty, exit_reason FROM positions")
    assert row["status"] == "dust"
    assert row["qty"] == "0.00001"
    assert "dust_below_exchange_minimum" in row["exit_reason"]
