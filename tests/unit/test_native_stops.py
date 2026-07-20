"""Exchange-native protective stops: paper STOP_LOSS_LIMIT semantics, risk
evaluation, gateway acceptance under kill switch."""

import pytest

from tests.helpers import RULES, T0, FakeQuoteSource, make_config
from trading_bot.core.enums import Mode, OrderState, OrderType, ReasonCode, Side
from trading_bot.core.models import OrderRequest, PositionState
from trading_bot.core.types import ZERO, dec
from trading_bot.exchange.interface import FrozenClock
from trading_bot.exchange.paper import PaperExchange
from trading_bot.execution.gateway import ExecutionGateway
from trading_bot.risk.engine import RiskEngine
from trading_bot.storage.audit import AuditLog

CFG = make_config()


@pytest.fixture
def quote_source():
    return FakeQuoteSource("100")


@pytest.fixture
def paper(repos, quote_source):
    return PaperExchange(RULES, CFG.paper, quote_source, repos.sim_state, FrozenClock(T0))


def seed_base(paper, qty="0.06"):
    balances = paper._load_balances()
    balances["BTC"]["free"] = dec(qty)
    paper._save_balances(balances)


def stop_request(coid="tb-ps-aaaaaaaaaaaaaaaaaaaaaaa1", qty="0.06", stop="98", limit="97"):
    return OrderRequest(
        symbol="BTCUSDT",
        side=Side.SELL,
        order_type=OrderType.STOP_LOSS_LIMIT,
        qty=dec(qty),
        price=dec(limit),
        stop_price=dec(stop),
        client_order_id=coid,
    )


def test_stop_rests_and_locks_base(paper):
    seed_base(paper)
    resp = paper.create_order(stop_request())
    assert resp.state == OrderState.ACKNOWLEDGED
    balances = paper.get_balances()
    assert balances["BTC"].locked == dec("0.06")
    assert balances["BTC"].free == ZERO


def test_stop_triggers_and_fills_at_limit(paper, quote_source):
    seed_base(paper)
    resp = paper.create_order(stop_request())
    quote_source.price = dec("97.5")  # bid <= stop(98) and >= limit(97)
    final = paper.query_order("BTCUSDT", resp.client_order_id)
    assert final.state == OrderState.FILLED
    assert final.avg_fill_price == dec("97")  # conservative: fills at the limit
    balances = paper.get_balances()
    assert balances["BTC"].locked == ZERO
    assert balances["USDT"].free > dec("30")  # proceeds arrived


def test_gap_through_limit_leaves_stop_unfilled(paper, quote_source):
    """The real-world failure mode: market gaps BELOW the limit price."""
    seed_base(paper)
    resp = paper.create_order(stop_request())
    quote_source.price = dec("90")  # far below limit 97
    still = paper.query_order("BTCUSDT", resp.client_order_id)
    assert still.state == OrderState.ACKNOWLEDGED  # triggered but UNFILLED
    assert still.executed_qty == ZERO
    # if price recovers to/above the limit, the resting limit sell fills
    quote_source.price = dec("97.2")
    final = paper.query_order("BTCUSDT", resp.client_order_id)
    assert final.state == OrderState.FILLED


def test_stop_cancel_unlocks_base(paper):
    seed_base(paper)
    resp = paper.create_order(stop_request())
    cancelled = paper.cancel_order("BTCUSDT", resp.client_order_id)
    assert cancelled.state == OrderState.CANCELLED
    balances = paper.get_balances()
    assert balances["BTC"].free == dec("0.06")
    assert balances["BTC"].locked == ZERO


def test_stop_that_would_trigger_immediately_is_rejected(paper, quote_source):
    seed_base(paper)
    quote_source.price = dec("95")  # bid already below stop 98
    resp = paper.create_order(stop_request(coid="tb-ps-bbbbbbbbbbbbbbbbbbbbbbb2"))
    assert resp.state == OrderState.REJECTED


def test_buy_stop_rejected(paper):
    req = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.STOP_LOSS_LIMIT,
        qty=dec("0.06"),
        price=dec("103"),
        stop_price=dec("102"),
        client_order_id="tb-ps-ccccccccccccccccccccccc3",
    )
    resp = paper.create_order(req)
    assert resp.state == OrderState.REJECTED


# ---------------------------------------------------------------- risk side
def make_position(qty="0.06", stop="98") -> PositionState:
    return PositionState(
        position_id="p1",
        symbol="BTCUSDT",
        qty=dec(qty),
        avg_entry_price=dec("100"),
        stop_price=dec(stop),
        opened_at=T0,
        entry_fee=dec("0.006"),
        entry_order_id="tb-en-x",
    )


def test_evaluate_protective_stop_produces_valid_order():
    engine = RiskEngine(CFG, dec("10"))
    decision = engine.evaluate_protective_stop(make_position(), RULES, dec("0.06"))
    assert decision.approved, decision.codes
    order = decision.order
    assert order.order_type == OrderType.STOP_LOSS_LIMIT
    assert order.side == Side.SELL
    assert order.stop_price == dec("98")
    assert order.limit_price < order.stop_price
    assert (order.limit_price % RULES.tick_size) == 0
    # limit = trigger * (1 - 100bps)
    assert order.limit_price == dec("97.02")


def test_evaluate_protective_stop_rejects_dust():
    engine = RiskEngine(CFG, dec("10"))
    decision = engine.evaluate_protective_stop(
        make_position(qty="0.000004"), RULES, dec("0.000004")
    )
    assert not decision.approved
    assert ReasonCode.QTY_BELOW_MIN in decision.codes


def test_evaluate_protective_stop_rejects_unrepresentable_notional():
    engine = RiskEngine(CFG, dec("10"))
    # 0.00006 * ~97 = ~0.0058 USDT << minNotional 5
    decision = engine.evaluate_protective_stop(make_position(qty="0.00006"), RULES, dec("0.00006"))
    assert not decision.approved
    assert ReasonCode.PROTECTIVE_EXIT_NOT_REPRESENTABLE in decision.codes


def test_gateway_accepts_protective_purpose_under_kill_switch(repos, paper):
    """Protective orders are protective: the kill switch must not block them."""
    seed_base(paper)
    engine = RiskEngine(CFG, dec("10"))
    gateway = ExecutionGateway(
        paper,
        repos,
        engine,
        Mode.PAPER,
        AuditLog(repos.db),
        FrozenClock(T0),
        kill_switch_check=lambda: (True, "env:test"),  # kill switch ACTIVE
    )
    decision = engine.evaluate_protective_stop(make_position(), RULES, dec("0.06"))
    assert decision.approved
    result = gateway.submit(decision.order, decision.approval_token, "cid", "protective")
    assert result.submitted
    assert result.state == OrderState.ACKNOWLEDGED


def test_gateway_rejects_unknown_purpose(repos, paper):
    engine = RiskEngine(CFG, dec("10"))
    gateway = ExecutionGateway(
        paper,
        repos,
        engine,
        Mode.PAPER,
        AuditLog(repos.db),
        FrozenClock(T0),
        kill_switch_check=lambda: (False, ""),
    )
    decision = engine.evaluate_protective_stop(make_position(), RULES, dec("0.06"))
    from trading_bot.execution.gateway import GatewaySecurityError

    with pytest.raises(GatewaySecurityError):
        gateway.submit(decision.order, decision.approval_token, "cid", "liquidate-everything")


def test_binance_stop_order_parameters():
    from trading_bot.config import constants as C
    from trading_bot.exchange.binance import BinanceAdapter
    from trading_bot.security.secrets import StaticSecretProvider

    captured: dict = {}

    def transport(method, url, headers, params, timeout):
        if method == "POST" and url.endswith("/api/v3/order"):
            captured.update(params)
            return 200, {
                "clientOrderId": params["newClientOrderId"],
                "orderId": 9,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_LOSS_LIMIT",
                "status": "NEW",
                "origQty": params["quantity"],
                "executedQty": "0",
                "cummulativeQuoteQty": "0",
                "transactTime": 1750000000000,
            }
        return 200, {}

    adapter = BinanceAdapter(
        Mode.TESTNET,
        StaticSecretProvider(
            {
                C.ENV_TESTNET_KEY: "testnet-key-0123456789abcdef",
                C.ENV_TESTNET_SECRET: "testnet-secret-0123456789abcdef",
            }
        ),
        transport=transport,
    )
    resp = adapter.create_order(stop_request())
    assert resp.state == OrderState.ACKNOWLEDGED
    assert captured["type"] == "STOP_LOSS_LIMIT"
    assert captured["stopPrice"] == "98"
    assert captured["price"] == "97"
    assert captured["timeInForce"] == "GTC"
