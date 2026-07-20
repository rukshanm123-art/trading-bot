"""Execution gateway order-state paths beyond the main paper-flow tests."""

from __future__ import annotations

import pytest

from tests.helpers import RULES, T0, make_config
from trading_bot.core.enums import Mode, OrderState, OrderType, Side
from trading_bot.core.models import (
    AssetBalance,
    Fill,
    OrderResponse,
    PriceQuote,
    SizedOrder,
    set_time_provider,
)
from trading_bot.core.types import dec
from trading_bot.exchange.errors import ExchangeUnavailable, OrderRejectedError
from trading_bot.exchange.interface import FrozenClock
from trading_bot.execution.gateway import ExecutionGateway, GatewaySecurityError
from trading_bot.portfolio.accounting import PortfolioService
from trading_bot.risk.engine import RiskEngine
from trading_bot.storage.audit import AuditLog


def _order(client_id: str = "tb-en-gateway000000000001") -> SizedOrder:
    return SizedOrder(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=dec("0.06"),
        limit_price=None,
        stop_price=dec("98"),
        est_entry_price=dec("100"),
        est_notional=dec("6"),
        est_fee=dec("0.006"),
        risk_amount=dec("0.12"),
        client_order_id=client_id,
    )


def _response(
    client_id: str,
    state: OrderState,
    *,
    executed: str = "0",
    cumulative: str = "0",
    fills: tuple[Fill, ...] = (),
) -> OrderResponse:
    return OrderResponse(
        client_order_id=client_id,
        exchange_order_id="ex-" + client_id,
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        state=state,
        requested_qty=dec("0.06"),
        executed_qty=dec(executed),
        cumulative_quote=dec(cumulative),
        fills=fills,
        ts=T0,
        raw_status=state.value,
    )


class FakeAdapter:
    kind = "paper"

    def __init__(self) -> None:
        self.created = 0
        self.queries: list[OrderResponse | None | BaseException] = []
        self.cancels: list[OrderResponse | BaseException] = []

    def server_time(self):
        return T0

    def get_rules(self, symbol):
        return RULES

    def get_balances(self):
        return {"USDT": AssetBalance("USDT", dec("30"), dec("0"))}

    def get_price(self, symbol):
        return PriceQuote("BTCUSDT", dec("99"), dec("101"), dec("100"), T0)

    def get_candles(self, symbol, interval, limit=200):
        return []

    def create_order(self, request):
        self.created += 1
        return _response(
            request.client_order_id, OrderState.FILLED, executed="0.06", cumulative="6"
        )

    def query_order(self, symbol, client_order_id):
        item = self.queries.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def cancel_order(self, symbol, client_order_id):
        item = self.cancels.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _gateway(repos, adapter: FakeAdapter, clock: FrozenClock | None = None):
    cfg = make_config()
    risk = RiskEngine(cfg, dec("10"))
    clock = clock or FrozenClock(T0)
    set_time_provider(clock.now)
    return (
        ExecutionGateway(
            adapter,
            repos,
            risk,
            Mode.PAPER,
            AuditLog(repos.db),
            clock,
            kill_switch_check=lambda: (False, ""),
        ),
        risk,
        clock,
    )


def test_gateway_rejects_unknown_purpose(repos) -> None:
    adapter = FakeAdapter()
    gateway, risk, _clock = _gateway(repos, adapter)
    order = _order()

    try:
        token = risk._token_for(order)
        with pytest.raises(GatewaySecurityError):
            gateway.submit(order, token, "cid", "rebalance")
    finally:
        set_time_provider(None)


def test_gateway_kill_switch_blocks_entry_before_adapter_call(repos) -> None:
    adapter = FakeAdapter()
    cfg = make_config()
    risk = RiskEngine(cfg, dec("10"))
    clock = FrozenClock(T0)
    set_time_provider(clock.now)
    gateway = ExecutionGateway(
        adapter,
        repos,
        risk,
        Mode.PAPER,
        AuditLog(repos.db),
        clock,
        kill_switch_check=lambda: (True, "manual"),
    )
    order = _order("tb-en-gateway000000000002")
    token = risk._token_for(order)

    result = gateway.submit(order, token, "cid", "entry")

    assert result.state == OrderState.RISK_REJECTED
    assert adapter.created == 0
    assert (
        repos.orders.get_by_client_id(order.client_order_id)["state"]
        == OrderState.RISK_REJECTED.value
    )


def test_gateway_submission_rejection_and_unavailable_paths(repos) -> None:
    class Rejecting(FakeAdapter):
        def create_order(self, request):
            raise OrderRejectedError("below filter")

    gateway, risk, _clock = _gateway(repos, Rejecting())
    order = _order("tb-en-gateway000000000003")
    result = gateway.submit(order, risk._token_for(order), "cid", "entry")
    assert result.submitted is True
    assert result.state == OrderState.REJECTED

    class Down(FakeAdapter):
        def create_order(self, request):
            raise ExchangeUnavailable("local fixture data gap")

    gateway, risk, _clock = _gateway(repos, Down())
    order = _order("tb-en-gateway000000000004")
    result = gateway.submit(order, risk._token_for(order), "cid", "entry")
    assert result.submitted is False
    assert result.state == OrderState.REJECTED


def test_gateway_defers_executed_state_until_atomic_accounting(repos) -> None:
    gateway, risk, _clock = _gateway(repos, FakeAdapter())
    order = _order("tb-en-gateway000000000010")

    result = gateway.submit(order, risk._token_for(order), "cid", "entry")

    assert result.response is not None
    assert (
        repos.orders.get_by_client_id(order.client_order_id)["state"] == OrderState.SUBMITTED.value
    )
    PortfolioService(repos, Mode.PAPER, RULES).record_entry(result.response, order.stop_price)
    assert repos.orders.get_by_client_id(order.client_order_id)["state"] == OrderState.FILLED.value
    assert repos.positions.open_position(Mode.PAPER) is not None


def test_await_completion_defers_fills_to_atomic_accounting(repos) -> None:
    adapter = FakeAdapter()
    gateway, risk, _clock = _gateway(repos, adapter)
    order = _order("tb-en-gateway000000000005")
    repos.orders.insert_intent(order, Mode.PAPER, "cid", "entry")
    first_fill = Fill(dec("100"), dec("0.02"), dec("0.00002"), "BTC")
    later_fill = Fill(dec("101"), dec("0.04"), dec("0.00004"), "BTC")
    first = _response(
        order.client_order_id,
        OrderState.PARTIALLY_FILLED,
        executed="0.02",
        cumulative="2",
        fills=(first_fill,),
    )
    repos.orders.update_state(order.client_order_id, OrderState.PARTIALLY_FILLED, first)
    adapter.queries = [
        _response(
            order.client_order_id,
            OrderState.FILLED,
            executed="0.06",
            cumulative="6.04",
            fills=(first_fill, later_fill),
        )
    ]

    final = gateway.await_completion(first, max_queries=2)

    assert final.state == OrderState.FILLED
    assert (
        repos.db.query("SELECT * FROM fills WHERE client_order_id = ?", (order.client_order_id,))
        == []
    )


def test_resolve_unknown_orders_handles_unavailable_found_and_not_found(repos) -> None:
    adapter = FakeAdapter()
    gateway, _risk, _clock = _gateway(repos, adapter)
    unavailable = _order("tb-en-gateway000000000006")
    found = _order("tb-en-gateway000000000007")
    missing = _order("tb-en-gateway000000000008")
    for order in (unavailable, found, missing):
        repos.orders.insert_intent(order, Mode.PAPER, "cid", "entry", state=OrderState.UNKNOWN)
    adapter.queries = [
        ExchangeUnavailable("timeout"),
        _response(
            found.client_order_id, OrderState.PARTIALLY_FILLED, executed="0.01", cumulative="1"
        ),
        None,
    ]

    unresolved, recovered = gateway.resolve_unknown_orders()

    assert unresolved == 2
    assert len(recovered) == 1
    assert recovered[0].response.client_order_id == found.client_order_id
    assert repos.orders.get_by_client_id(found.client_order_id)["state"] == OrderState.UNKNOWN.value
    assert (
        repos.orders.get_by_client_id(missing.client_order_id)["state"] == OrderState.REJECTED.value
    )
    assert not repos.flags.is_true(repos.flags.UNKNOWN_ORDER_BLOCK)


def test_stale_partial_entry_cancel_queries_first_and_preserves_fills(repos) -> None:
    adapter = FakeAdapter()
    gateway, _risk, clock = _gateway(repos, adapter)
    order = _order("tb-en-gateway000000000009")
    row_id = repos.orders.insert_intent(
        order, Mode.PAPER, "cid", "entry", state=OrderState.ACKNOWLEDGED
    )
    partial_fill = Fill(dec("100"), dec("0.02"), dec("0.00002"), "BTC")
    adapter.queries = [
        _response(
            order.client_order_id,
            OrderState.PARTIALLY_FILLED,
            executed="0.02",
            cumulative="2",
            fills=(partial_fill,),
        )
    ]
    adapter.cancels = [
        _response(
            order.client_order_id,
            OrderState.CANCELLED,
            executed="0.02",
            cumulative="2",
            fills=(partial_fill,),
        )
    ]
    assert row_id
    clock.advance(1_000)

    cancelled, recovered = gateway.cancel_stale_entry_orders(max_age_s=1)
    assert cancelled == 1
    assert len(recovered) == 1

    row = repos.orders.get_by_client_id(order.client_order_id)
    assert row["state"] == OrderState.ACKNOWLEDGED.value
    assert (
        repos.db.query("SELECT * FROM fills WHERE client_order_id = ?", (order.client_order_id,))
        == []
    )

    from trading_bot.portfolio.accounting import PortfolioService

    PortfolioService(repos, Mode.PAPER, RULES).record_entry(recovered[0].response, order.stop_price)
    fills = repos.db.query(
        "SELECT * FROM fills WHERE client_order_id = ?", (order.client_order_id,)
    )
    assert len(fills) == 1
    assert (
        repos.orders.get_by_client_id(order.client_order_id)["state"] == OrderState.CANCELLED.value
    )
    assert repos.positions.open_position(Mode.PAPER) is not None
