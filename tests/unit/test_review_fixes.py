"""Regression tests for the external review findings (v1.1.0)."""

from dataclasses import replace
from datetime import datetime

import pytest

from tests.helpers import RULES, T0, FakeQuoteSource, make_config, make_ctx, make_quote, make_state
from trading_bot.core.enums import Mode, OrderState, OrderType, ReasonCode, Side
from trading_bot.core.models import Fill, OrderResponse, SymbolRules
from trading_bot.core.types import dec
from trading_bot.exchange.interface import FrozenClock
from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.sizing import SizingInputs, size_entry

CFG = make_config()


# ---------------------------------------------------------------- finding 1
def test_protective_exit_check_uses_net_quantity_not_gross():
    """Entry commission (base asset) + step flooring shrink the SELLABLE
    quantity; the stop-notional check must use that, not the gross buy."""
    rules = SymbolRules(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        status="TRADING",
        min_qty=dec("0.00000001"),
        step_size=dec("0.00000001"),
        tick_size=dec("0.01"),
        min_notional=dec("5"),
    )
    result = size_entry(
        SizingInputs(
            equity=dec("26.25"),
            quote_free=dec("26.25"),
            est_entry_price=dec("100"),
            rules=rules,
            risk=CFG.risk,
            stop_loss_pct=dec("2.0"),
            fee_bps=dec("100"),  # 1% fee widens the gross-vs-net wedge
        )
    )
    # gross stop notional clears the exchange minimum...
    gross_stop_notional = result.qty * dec("98") * (dec("10000") - dec("250")) / dec("10000")
    assert gross_stop_notional >= rules.min_notional
    # ...but the NET sellable quantity does not: the entry must be rejected.
    assert ReasonCode.PROTECTIVE_EXIT_NOT_REPRESENTABLE in result.codes


# ---------------------------------------------------------------- finding 3
def test_second_exit_blocked_while_exit_order_active():
    engine = RiskEngine(CFG, dec("10"))
    from tests.unit.test_risk_engine import make_position

    ctx = make_ctx(
        open_position=make_position(),
        base_free=dec("0.05"),
        state=make_state(active_exit_orders=1),
    )
    decision = engine.evaluate_exit(ctx, "strategy_exit")
    assert not decision.approved
    assert ReasonCode.EXIT_ORDER_ACTIVE in decision.codes


def test_exit_blocked_while_unknown_order_pending():
    engine = RiskEngine(CFG, dec("10"))
    from tests.unit.test_risk_engine import make_position

    ctx = make_ctx(
        open_position=make_position(),
        base_free=dec("0.05"),
        state=make_state(unknown_orders=1),
    )
    decision = engine.evaluate_exit(ctx, "strategy_exit")
    assert not decision.approved
    assert ReasonCode.UNKNOWN_ORDER_PENDING in decision.codes


# ---------------------------------------------------------------- finding 5
def test_token_covers_stop_price_and_risk_amount():
    engine = RiskEngine(CFG, dec("10"))
    decision = engine.evaluate_entry(make_ctx())
    assert decision.approved and decision.order is not None
    tampered_stop = replace(decision.order, stop_price=dec("1"))
    assert not engine.verify_and_consume(tampered_stop, decision.approval_token)
    decision2 = engine.evaluate_entry(make_ctx())
    tampered_risk = replace(decision2.order, risk_amount=dec("999999"))
    assert not engine.verify_and_consume(tampered_risk, decision2.approval_token)
    # untampered still verifies
    decision3 = engine.evaluate_entry(make_ctx())
    assert engine.verify_and_consume(decision3.order, decision3.approval_token)


def test_token_expires(monkeypatch):
    import time as time_mod

    engine = RiskEngine(CFG, dec("10"))
    decision = engine.evaluate_entry(make_ctx())
    assert decision.approved
    real_time = time_mod.time
    monkeypatch.setattr(time_mod, "time", lambda: real_time() + 999)
    assert not engine.verify_and_consume(decision.order, decision.approval_token)


# ---------------------------------------------------------------- finding 9
def test_instance_lock_cas_prevents_double_steal(db):
    from trading_bot.engine.scheduler import STALE_AFTER_S, InstanceLock

    clock = FrozenClock(T0)
    holder = InstanceLock(db, clock)
    assert holder.acquire()

    rival = InstanceLock(db, clock)
    assert not rival.acquire()  # fresh heartbeat -> refused

    clock.advance(STALE_AFTER_S + 5)  # holder goes stale
    thief_a = InstanceLock(db, clock)
    thief_b = InstanceLock(db, clock)
    assert thief_a.acquire()  # steals via conditional UPDATE (rowcount 1)
    assert not thief_b.acquire()  # heartbeat now fresh -> CAS matches 0 rows

    thief_a.release()
    assert thief_b.acquire()  # free lock -> INSERT path


# --------------------------------------------------------------- finding 13
def test_unexpected_order_submission_status_is_unknown_not_unavailable():
    from trading_bot.config import constants as C
    from trading_bot.exchange.binance import BinanceAdapter
    from trading_bot.exchange.errors import OrderStateUnknownError
    from trading_bot.security.secrets import StaticSecretProvider

    def transport(method, url, headers, params, timeout):
        if method == "POST" and url.endswith("/api/v3/order"):
            return 404, {"raw": "gateway lost the response"}
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
    from trading_bot.core.models import OrderRequest

    with pytest.raises(OrderStateUnknownError):
        adapter.create_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                qty=dec("0.001"),
                client_order_id="tb-en-40404040404040404040404a",
            )
        )


@pytest.mark.parametrize("code", [-1000, -1006, -1007])
def test_documented_unknown_execution_codes_never_become_rejections(code):
    from trading_bot.core.models import OrderRequest
    from trading_bot.exchange.errors import OrderStateUnknownError

    def transport(method, url, headers, params, timeout):
        if method == "POST" and url.endswith("/api/v3/order"):
            return 400, {"code": code, "msg": "execution status unknown"}
        return 200, {}

    with pytest.raises(OrderStateUnknownError, match="reconcile by client order id"):
        _testnet_adapter(transport).create_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                qty=dec("0.001"),
                client_order_id=f"tb-en-unknown{abs(code)}0000000000a",
            )
        )


# ---------------------------------------------------------------- finding 4
def _testnet_adapter(transport):
    from trading_bot.config import constants as C
    from trading_bot.exchange.binance import BinanceAdapter
    from trading_bot.security.secrets import StaticSecretProvider

    return BinanceAdapter(
        Mode.TESTNET,
        StaticSecretProvider(
            {
                C.ENV_TESTNET_KEY: "testnet-key-0123456789abcdef",
                C.ENV_TESTNET_SECRET: "testnet-secret-0123456789abcdef",
            }
        ),
        transport=transport,
    )


def test_query_order_recovers_fills_from_my_trades():
    def transport(method, url, headers, params, timeout):
        if url.endswith("/api/v3/order"):
            return 200, {
                "clientOrderId": "tb-en-x",
                "orderId": 555,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "status": "FILLED",
                "origQty": "0.10000000",
                "executedQty": "0.10000000",
                "cummulativeQuoteQty": "5000.00",
                "updateTime": 1750000000000,
            }
        if url.endswith("/api/v3/myTrades"):
            assert params.get("orderId") == "555" or params.get("orderId") == 555
            return 200, [
                {
                    "id": 77,
                    "orderId": 555,
                    "price": "50000.00",
                    "qty": "0.10000000",
                    "commission": "0.00010000",
                    "commissionAsset": "BTC",
                }
            ]
        return 200, {}

    adapter = _testnet_adapter(transport)
    resp = adapter.query_order("BTCUSDT", "tb-en-x")
    assert resp is not None
    assert resp.fills, "fills must be recovered from myTrades"
    assert resp.fills[0].trade_id == "77"
    assert resp.fills[0].fee_asset == "BTC"
    assert resp.fills[0].fee == dec("0.0001")


def test_third_asset_fees_excluded_from_quote_equiv():
    resp = OrderResponse(
        client_order_id="x",
        exchange_order_id="1",
        symbol="BTCUSDT",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        state=OrderState.FILLED,
        requested_qty=dec("0.1"),
        executed_qty=dec("0.1"),
        cumulative_quote=dec("5000"),
        fills=(
            Fill(price=dec("50000"), qty=dec("0.05"), fee=dec("2.5"), fee_asset="USDT"),
            Fill(price=dec("50000"), qty=dec("0.05"), fee=dec("0.01"), fee_asset="BNB"),
        ),
        ts=T0,
    )
    assert resp.total_fees_quote_equiv == dec("2.5")  # BNB fee NOT silently valued
    assert resp.third_asset_fees == {"BNB": dec("0.01")}


def test_taker_fee_bps_parses_account_commission():
    def transport(method, url, headers, params, timeout):
        if url.endswith("/api/v3/account/commission"):
            return 200, {"standardCommission": {"taker": "0.00075", "maker": "0.00075"}}
        return 200, {}

    adapter = _testnet_adapter(transport)
    assert adapter.taker_fee_bps("BTCUSDT") == dec("7.5")


def test_taker_fee_bps_failure_returns_none():
    def transport(method, url, headers, params, timeout):
        return 500, {"msg": "boom"}

    adapter = _testnet_adapter(transport)
    assert adapter.taker_fee_bps("BTCUSDT") is None


# --------------------------------------------------------------- finding 12
def test_market_lot_size_parsed_and_enforced():
    from trading_bot.exchange.binance import _parse_rules

    info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "orderTypes": ["MARKET", "LIMIT", "STOP_LOSS_LIMIT"],
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.00001",
                        "maxQty": "900",
                        "stepSize": "0.00001",
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "minQty": "0.00003",
                        "maxQty": "10",
                        "stepSize": "0.00003",
                    },
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": "5",
                        "maxNotional": "1000",
                    },
                ],
            }
        ]
    }
    rules = _parse_rules(info, "BTCUSDT")
    assert rules.max_qty == dec("900")
    assert rules.market_min_qty == dec("0.00003")
    assert rules.market_step_size == dec("0.00003")
    assert rules.market_max_qty == dec("10")
    assert rules.max_notional == dec("1000")
    # MARKET must satisfy both 0.00001 LOT_SIZE and 0.00003 MARKET_LOT_SIZE.
    assert rules.quantity_step(OrderType.MARKET) == dec("0.00003")

    tiny_max = replace(RULES, market_max_qty=dec("0.00005"))
    result = size_entry(
        SizingInputs(
            equity=dec("1000"),
            quote_free=dec("900"),
            est_entry_price=dec("100"),
            rules=tiny_max,
            risk=CFG.risk,
            stop_loss_pct=dec("2.0"),
            fee_bps=dec("10"),
        )
    )
    assert result.qty <= dec("0.00005") or not result.ok

    market_grid = replace(RULES, market_min_qty=dec("0.00003"), market_step_size=dec("0.00003"))
    result = size_entry(
        SizingInputs(
            equity=dec("1000"),
            quote_free=dec("900"),
            est_entry_price=dec("100"),
            rules=market_grid,
            risk=CFG.risk,
            stop_loss_pct=dec("2.0"),
            fee_bps=dec("10"),
        )
    )
    assert result.qty % dec("0.00003") == 0


# ------------------------------------------------------- rate limit tracker
def test_used_weight_tracker_delays(monkeypatch):
    import time as time_mod

    from trading_bot.exchange.ratelimit import UsedWeightTracker

    tracker = UsedWeightTracker(limit_per_minute=1000)
    assert tracker.suggested_delay() == 0.0
    tracker.update_from_headers({"x-mbx-used-weight-1m": "800"})
    assert tracker.suggested_delay() > 0.0
    tracker.update_from_headers({"X-MBX-USED-WEIGHT-1M": "950"})
    assert tracker.suggested_delay() >= 10.0
    tracker.update_exchange_limits(
        [
            {
                "rateLimitType": "REQUEST_WEIGHT",
                "interval": "MINUTE",
                "intervalNum": 1,
                "limit": 2000,
            }
        ]
    )
    assert tracker.limit == 2000
    tracker.update_from_headers({"Retry-After": "7"})
    assert tracker.suggested_delay() > 6.9
    # stale observations stop throttling after the window passes
    real = time_mod.monotonic
    monkeypatch.setattr(time_mod, "monotonic", lambda: real() + 120)
    assert tracker.suggested_delay() == 0.0
    tracker.update_from_headers({"x-mbx-used-weight-1m": "garbage"})  # ignored


@pytest.mark.parametrize("status", [418, 429])
def test_binance_transport_headers_enforce_retry_after(status, monkeypatch):
    import trading_bot.exchange.binance as binance
    import trading_bot.exchange.ratelimit as ratelimit

    clock = {"now": 100.0}
    delays: list[float] = []
    tracker = ratelimit.UsedWeightTracker(limit_per_minute=1000)
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(binance, "RATE_TRACKER", tracker)

    def sleep(seconds):
        delays.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(binance.time, "sleep", sleep)
    responses = iter(
        [
            (status, {"code": -1003}, {"Retry-After": "7"}),
            (200, {"bidPrice": "99", "askPrice": "101"}, {}),
        ]
    )

    public = binance.BinancePublicData(transport=lambda *args: next(responses))
    assert public.get_price("BTCUSDT").bid == dec("99")
    assert delays and delays[0] >= 7


# ------------------------------------------------------------ cross-check
def test_cross_check_quote_divergence():
    from trading_bot.market_data.validation import cross_check_quote

    ok = cross_check_quote(make_quote("100"), dec("101"), dec("10"))
    assert ok.ok
    bad = cross_check_quote(make_quote("115"), dec("100"), dec("10"))
    assert not bad.ok
    assert "cross-check" in bad.issues[0]


# ------------------------------------------------------- quality verifier
def test_quality_verifier_rejects_failed_tools(tmp_path):
    import json
    from datetime import UTC

    from trading_bot.security.quality import verify_quality_record

    record = {
        "passed": True,
        "tests_collected": 200,
        "tests_run": 200,
        "tests_passed": 200,
        "tests_failed": 0,
        "tests_skipped": 0,
        "coverage_percent": 90.0,
        "required_safety_tests_missing": [],
        "results_hash": "abc",
        "ran_at": datetime.now(UTC).isoformat(),
        "formatter": {"rc": 99},
        "linter": {"rc": 99},
        "type_check": {"rc": 99},
        "security_scan": {"rc": 99},
        "git_state": "no_repo",
    }
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    result = verify_quality_record(tmp_path, path)
    assert not result.ok
    assert any("formatter failed" in f for f in result.failures)
    assert any("linter failed" in f for f in result.failures)
    assert any("type_check failed" in f for f in result.failures)
    assert any("security_scan failed" in f for f in result.failures)
    assert any("junit" in f for f in result.failures)  # artifacts must exist too


# ---------------------------------------------------------- dust (finding 10)
def test_reconciliation_tolerates_tracked_dust_only(repos):
    from tests.helpers import make_config as mc
    from trading_bot.exchange.paper import PaperExchange
    from trading_bot.execution.gateway import ExecutionGateway
    from trading_bot.portfolio.accounting import PortfolioService
    from trading_bot.portfolio.reconciliation import Reconciler
    from trading_bot.storage.audit import AuditLog

    cfg = mc()
    clock = FrozenClock(T0)
    paper = PaperExchange(RULES, cfg.paper, FakeQuoteSource("100"), repos.sim_state, clock)
    risk = RiskEngine(cfg, dec("10"))
    gateway = ExecutionGateway(
        paper,
        repos,
        risk,
        Mode.PAPER,
        AuditLog(repos.db),
        clock,
        kill_switch_check=lambda: (False, ""),
    )
    portfolio = PortfolioService(repos, Mode.PAPER, RULES)
    reconciler = Reconciler(paper, repos, cfg, RULES, gateway, clock, portfolio)

    # 0.02 BTC (= 2 USDT at price 100) appears in balances
    balances = paper._load_balances()
    balances["BTC"]["free"] = dec("0.02")
    paper._save_balances(balances)

    # untracked -> only tolerated because it is below minNotional threshold
    assert reconciler.run().ok

    # at/above minNotional it must block... unless the DB tracks it as dust
    balances = paper._load_balances()
    balances["BTC"]["free"] = dec("0.06")  # 6 USDT > minNotional 5
    paper._save_balances(balances)
    assert not reconciler.run().ok

    pid = repos.positions.insert_open(
        mode=Mode.PAPER,
        symbol="BTCUSDT",
        qty=dec("0.06"),
        avg_entry_price=dec("100"),
        stop_price=dec("98"),
        entry_order_id="tb-en-dust0000000000000000000d",
        entry_fee=dec("0"),
    )
    repos.positions.mark_dust(
        pid,
        dec("0.06"),
        dec("0"),
        "tb-ex-dust0000000000000000000d",
        dec("0"),
        dec("0"),
        "test:dust",
    )
    result = reconciler.run()
    assert result.ok, result.details  # tracked dust is explained holdings
