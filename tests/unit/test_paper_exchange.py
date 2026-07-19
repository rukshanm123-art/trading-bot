"""Paper exchange: deterministic fills, correct fees, exchange-filter
enforcement, partial fills, idempotency, persistence."""

from decimal import Decimal

import pytest

from tests.helpers import RULES, T0, FakeQuoteSource, make_config
from trading_bot.core.enums import OrderState, OrderType, Side
from trading_bot.core.models import OrderRequest
from trading_bot.core.types import ZERO, dec
from trading_bot.exchange.interface import FrozenClock
from trading_bot.exchange.paper import PaperExchange


def paper(repos, *, price="100", **paper_overrides) -> PaperExchange:
    sim = make_config().paper
    if paper_overrides:
        # model_copy skips validation on purpose: tests exercise extreme
        # simulation probabilities the config schema would refuse.
        sim = sim.model_copy(
            update={k: (dec(v) if isinstance(v, str) else v) for k, v in paper_overrides.items()}
        )
    return PaperExchange(RULES, sim, FakeQuoteSource(price), repos.sim_state, FrozenClock(T0))


def buy(qty="0.06", coid="tb-en-aaaaaaaaaaaaaaaaaaaaaaaa") -> OrderRequest:
    return OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=dec(qty),
        client_order_id=coid,
    )


def sell(qty, coid="tb-ex-bbbbbbbbbbbbbbbbbbbbbbbb") -> OrderRequest:
    return OrderRequest(
        symbol="BTCUSDT", side=Side.SELL, order_type=OrderType.MARKET, qty=qty, client_order_id=coid
    )


def test_initial_balances_seeded(repos):
    px = paper(repos)
    balances = px.get_balances()
    assert balances["USDT"].free == dec("30")
    assert balances["BTC"].free == ZERO


def test_market_buy_moves_balances_with_base_fee(repos):
    px = paper(repos)
    resp = px.create_order(buy())
    assert resp.state == OrderState.FILLED
    balances = px.get_balances()
    # fee charged in BTC on buys (Binance spot convention)
    fee = sum(f.fee for f in resp.fills)
    assert resp.fills[0].fee_asset == "BTC"
    assert balances["BTC"].free == resp.executed_qty - fee
    assert balances["USDT"].free == dec("30") - resp.cumulative_quote
    assert balances["USDT"].free >= ZERO
    # buy fills at or above mid (spread + slippage never favours the trader)
    assert resp.avg_fill_price >= dec("100") - dec("0.01")


def test_roundtrip_costs_are_bounded(repos):
    px = paper(repos)
    px.create_order(buy())
    btc = px.get_balances()["BTC"].free
    r2 = px.create_order(sell(btc))
    assert r2.state == OrderState.FILLED
    equity_after = px.get_balances()["USDT"].free
    # loss bounded by fees (2x10bps) + spread (5bps) + slippage (2x8bps) + ticks
    max_cost_frac = Decimal("0.0050")
    assert equity_after >= dec("30") * (1 - max_cost_frac)
    assert equity_after < dec("30")  # trading is never free


def test_sell_fee_in_quote(repos):
    px = paper(repos)
    px.create_order(buy())
    btc = px.get_balances()["BTC"].free
    resp = px.create_order(sell(btc))
    assert resp.fills[0].fee_asset == "USDT"


def test_deterministic_for_same_seed(repos, tmp_path):
    from trading_bot.storage.db import Database
    from trading_bot.storage.repositories import Repositories

    results = []
    for i in range(2):
        db = Database(f"sqlite:///{tmp_path}/d{i}.db")
        db.migrate(__import__("tests.conftest", fromlist=["MIGRATIONS"]).MIGRATIONS)
        px = paper(Repositories(db))
        resp = px.create_order(buy())
        results.append((resp.state, resp.executed_qty, resp.avg_fill_price))
        db.close()
    assert results[0] == results[1]


def test_duplicate_client_order_id_is_idempotent(repos):
    px = paper(repos)
    r1 = px.create_order(buy())
    quote_after_first = px.get_balances()["USDT"].free
    r2 = px.create_order(buy())  # same client order id
    assert r2.executed_qty == r1.executed_qty
    assert px.get_balances()["USDT"].free == quote_after_first  # no double spend


def test_simulated_rejection(repos):
    px = paper(repos, reject_probability="1")
    resp = px.create_order(buy())
    assert resp.state == OrderState.REJECTED
    assert px.get_balances()["USDT"].free == dec("30")  # untouched


def test_filter_rejects_step_size_violation(repos):
    px = paper(repos)
    resp = px.create_order(buy(qty="0.000015"))  # not a multiple of 0.00001
    assert resp.state == OrderState.REJECTED


def test_filter_rejects_below_min_qty(repos):
    px = paper(repos)
    resp = px.create_order(buy(qty="0.000000"))  # zero / below min
    assert resp.state == OrderState.REJECTED


def test_filter_rejects_below_min_notional(repos):
    px = paper(repos)
    resp = px.create_order(buy(qty="0.00002"))  # ~0.002 USDT << 5
    assert resp.state == OrderState.REJECTED


def test_insufficient_balance_rejected(repos):
    px = paper(repos)
    resp = px.create_order(buy(qty="1"))  # 100 USDT needed, 30 available
    assert resp.state == OrderState.REJECTED
    assert px.get_balances()["USDT"].free == dec("30")


def test_partial_fill_completes(repos):
    px = paper(repos, partial_fill_probability="1")
    resp = px.create_order(buy())
    assert resp.state == OrderState.PARTIALLY_FILLED
    assert ZERO < resp.executed_qty < dec("0.06")
    second = px.query_order("BTCUSDT", resp.client_order_id)
    assert second is not None
    assert second.state == OrderState.PARTIALLY_FILLED
    assert second.executed_qty == dec("0.042")
    final = px.query_order("BTCUSDT", resp.client_order_id)
    assert final is not None
    assert final.state == OrderState.FILLED
    assert final.executed_qty == dec("0.06")
    balances = px.get_balances()
    total_fee = sum(f.fee for f in final.fills)
    assert balances["BTC"].free == dec("0.06") - total_fee


def test_resting_limit_order_and_cancel(repos):
    px = paper(repos)
    req = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=dec("0.06"),
        price=dec("90"),  # below market: rests
        client_order_id="tb-en-cccccccccccccccccccccccc",
    )
    resp = px.create_order(req)
    assert resp.state == OrderState.ACKNOWLEDGED
    locked = px.get_balances()["USDT"].locked
    assert locked == dec("0.06") * dec("90")
    cancelled = px.cancel_order("BTCUSDT", req.client_order_id)
    assert cancelled.state == OrderState.CANCELLED
    balances = px.get_balances()
    assert balances["USDT"].locked == ZERO
    assert balances["USDT"].free == dec("30")


def test_state_survives_restart(repos):
    px = paper(repos)
    px.create_order(buy())
    balances_before = {a: (b.free, b.locked) for a, b in px.get_balances().items()}
    # new instance over the same repo = process restart
    px2 = PaperExchange(
        RULES, make_config().paper, FakeQuoteSource("100"), repos.sim_state, FrozenClock(T0)
    )
    balances_after = {a: (b.free, b.locked) for a, b in px2.get_balances().items()}
    assert balances_before == balances_after
    # and the order history survived too (idempotency intact)
    assert px2.query_order("BTCUSDT", "tb-en-aaaaaaaaaaaaaaaaaaaaaaaa") is not None


def test_unknown_symbol_rejected(repos):
    px = paper(repos)
    with pytest.raises(Exception):
        px.get_rules("ETHUSDT")
