"""Finding 2: cumulative exchange responses must never double-book.

The reviewer's exact reproduction: process the same partial-exit response
twice — the position must lose 0.4, not 0.8, and exactly one realization row
may exist.
"""

from decimal import Decimal

import pytest

from tests.helpers import RULES, T0
from trading_bot.core.enums import Mode, OrderState, OrderType, Side
from trading_bot.core.models import Fill, OrderResponse
from trading_bot.core.types import dec
from trading_bot.portfolio.accounting import PortfolioService


def exit_response(coid: str, executed: str, quote: str, fills=(), ts=T0) -> OrderResponse:
    return OrderResponse(
        client_order_id=coid,
        exchange_order_id="e1",
        symbol="BTCUSDT",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        state=OrderState.PARTIALLY_FILLED,
        requested_qty=dec("1"),
        executed_qty=dec(executed),
        cumulative_quote=dec(quote),
        fills=tuple(fills),
        ts=ts,
    )


@pytest.fixture
def portfolio(repos):
    return PortfolioService(repos, Mode.PAPER, RULES, fee_bps=dec("10"))


@pytest.fixture
def open_position(repos, portfolio):
    repos.positions.insert_open(
        mode=Mode.PAPER,
        symbol="BTCUSDT",
        qty=dec("1"),
        avg_entry_price=dec("100"),
        stop_price=dec("98"),
        entry_order_id="tb-en-aaaaaaaaaaaaaaaaaaaaaaa1",
        entry_fee=dec("0.1"),
    )
    return repos.positions.open_position(Mode.PAPER)


def test_same_partial_exit_response_processed_twice_books_once(repos, portfolio, open_position):
    coid = "tb-ex-bbbbbbbbbbbbbbbbbbbbbbb2"
    fills = [Fill(price=dec("110"), qty=dec("0.4"), fee=dec("0.044"), fee_asset="USDT")]
    resp = exit_response(coid, "0.4", "44", fills)

    realized_1 = portfolio.record_exit(open_position, resp, "strategy_exit")
    assert realized_1 != Decimal("0")
    pos_after_1 = repos.positions.open_position(Mode.PAPER)
    assert pos_after_1.qty == dec("0.6")

    # the SAME cumulative response again (restart / repoll / reconciliation)
    realized_2 = portfolio.record_exit(pos_after_1, resp, "strategy_exit")
    assert realized_2 == Decimal("0")
    pos_after_2 = repos.positions.open_position(Mode.PAPER)
    assert pos_after_2.qty == dec("0.6"), "reprocessing must not shrink the position again"

    rows = repos.db.query("SELECT * FROM position_realizations WHERE exit_order_id = ?", (coid,))
    assert len(rows) == 1, "exactly one realization row for one real fill"


def test_progressing_cumulative_response_books_only_the_delta(repos, portfolio, open_position):
    coid = "tb-ex-ccccccccccccccccccccccc3"
    first = exit_response(
        coid,
        "0.4",
        "44",
        [Fill(price=dec("110"), qty=dec("0.4"), fee=dec("0.044"), fee_asset="USDT")],
    )
    portfolio.record_exit(open_position, first, "strategy_exit")

    # later the SAME order reports cumulative 1.0 executed (completion)
    pos = repos.positions.open_position(Mode.PAPER)
    second = exit_response(
        coid,
        "1.0",
        "110",
        [
            Fill(price=dec("110"), qty=dec("0.4"), fee=dec("0.044"), fee_asset="USDT"),
            Fill(price=dec("110"), qty=dec("0.6"), fee=dec("0.066"), fee_asset="USDT"),
        ],
    )
    import dataclasses

    second = dataclasses.replace(second, state=OrderState.FILLED)
    portfolio.record_exit(pos, second, "strategy_exit")

    assert repos.positions.open_position(Mode.PAPER) is None
    rows = repos.db.query(
        "SELECT qty FROM position_realizations WHERE exit_order_id = ? ORDER BY ts", (coid,)
    )
    assert [dec(r["qty"]) for r in rows] == [dec("0.4"), dec("0.6")]
    total_sold = sum((dec(r["qty"]) for r in rows), Decimal(0))
    assert total_sold == dec("1.0"), "cumulative response must book only the delta"


def test_entry_response_reprocessing_is_idempotent(repos, portfolio):
    coid = "tb-en-ddddddddddddddddddddddd4"
    resp = OrderResponse(
        client_order_id=coid,
        exchange_order_id="e2",
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        state=OrderState.FILLED,
        requested_qty=dec("0.5"),
        executed_qty=dec("0.5"),
        cumulative_quote=dec("50"),
        fills=(Fill(price=dec("100"), qty=dec("0.5"), fee=dec("0.0005"), fee_asset="BTC"),),
        ts=T0,
    )
    pid1 = portfolio.record_entry(resp, stop_price=dec("98"))
    pos1 = repos.positions.open_position(Mode.PAPER)
    pid2 = portfolio.record_entry(resp, stop_price=dec("98"))  # replay
    pos2 = repos.positions.open_position(Mode.PAPER)
    assert pid1 == pid2
    assert pos1.qty == pos2.qty == dec("0.4995")  # net of base fee, once


def test_missing_fills_assume_worst_case_fees(repos, portfolio):
    """Finding 4 downstream: a response without fill details must not
    overstate the sellable quantity."""
    coid = "tb-en-eeeeeeeeeeeeeeeeeeeeeee5"
    resp = OrderResponse(
        client_order_id=coid,
        exchange_order_id="e3",
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        state=OrderState.FILLED,
        requested_qty=dec("0.1"),
        executed_qty=dec("0.1"),
        cumulative_quote=dec("10"),
        fills=(),
        ts=T0,  # no fills reported
    )
    portfolio.record_entry(resp, stop_price=dec("98"))
    pos = repos.positions.open_position(Mode.PAPER)
    assert pos.qty < dec("0.1"), "worst-case base fee must be assumed"
    assert pos.qty == dec("0.1") - dec("0.1") * dec("10") / dec("10000")
