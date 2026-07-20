"""Targeted coverage for the v1.1.0 additions: dust sweep, quality verifier
happy path, key-permission refusal, fills-recovery failure modes, protective
housekeeping branches."""

import json
from datetime import UTC, datetime

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import (
    RULES,
    T0,
    FakeQuoteSource,
    make_config,
    make_trend_rows,
    write_rows_csv,
)
from trading_bot.core.enums import Mode, OrderState, OrderType, Side
from trading_bot.core.models import Fill, OrderRequest, OrderResponse
from trading_bot.core.types import ZERO, dec
from trading_bot.exchange.interface import FrozenClock
from trading_bot.exchange.paper import PaperExchange
from trading_bot.portfolio.accounting import PortfolioService

CFG = make_config()


# ------------------------------------------------------------- dust sweep
def test_dust_sweep_end_to_end(repos):
    from trading_bot.execution.gateway import ExecutionGateway
    from trading_bot.risk.engine import RiskEngine
    from trading_bot.storage.audit import AuditLog

    clock = FrozenClock(T0)
    paper = PaperExchange(RULES, CFG.paper, FakeQuoteSource("100"), repos.sim_state, clock)
    risk = RiskEngine(CFG, dec("10"))
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

    # two dust residues totalling 0.06 BTC (= 6 USDT at 100 > minNotional 5)
    dust_ids = []
    for i, qty in enumerate(("0.04", "0.02")):
        pid = repos.positions.insert_open(
            mode=Mode.PAPER,
            symbol="BTCUSDT",
            qty=dec(qty),
            avg_entry_price=dec("100"),
            stop_price=dec("98"),
            entry_order_id=f"tb-en-dust{i}00000000000000000d",
            entry_fee=dec("0"),
        )
        repos.positions.mark_dust(
            pid,
            dec(qty),
            dec("0"),
            f"tb-ex-dust{i}00000000000000000d",
            dec("0"),
            dec("0"),
            "test:dust",
        )
        dust_ids.append(pid)
    balances = paper._load_balances()
    balances["BTC"]["free"] = dec("0.06")
    paper._save_balances(balances)

    quote = paper.get_price("BTCUSDT")
    decision = risk.evaluate_dust_sweep(dec("0.06"), quote, RULES, dec("0.06"))
    assert decision.approved, decision.codes
    result = gateway.submit(decision.order, decision.approval_token, "cid", "exit")
    assert result.response is not None and result.response.executed_qty > ZERO
    proceeds = portfolio.record_dust_sweep(
        repos.positions.dust_positions(Mode.PAPER), result.response
    )
    assert proceeds > ZERO
    assert repos.positions.dust_positions(Mode.PAPER) == []
    for pid in dust_ids:
        row = repos.db.query_one("SELECT * FROM positions WHERE id = ?", (pid,))
        assert row["status"] == "closed"
        assert row["exit_reason"] == "dust_sweep"
        # Paper fills just below the 100 entry price, so the sweep correctly
        # includes the residue cost basis and realizes a small loss.
        assert dec(row["realized_pnl"]) < ZERO
    # replaying the same sweep response books nothing further
    assert portfolio.record_dust_sweep([], result.response) == ZERO


def test_dust_sweep_rejects_below_minimums(repos):
    from trading_bot.risk.engine import RiskEngine

    risk = RiskEngine(CFG, dec("10"))
    quote = FakeQuoteSource("100").get_price("BTCUSDT")
    tiny = risk.evaluate_dust_sweep(dec("0.000004"), quote, RULES, dec("0.000004"))
    assert not tiny.approved
    small = risk.evaluate_dust_sweep(dec("0.01"), quote, RULES, dec("0.01"))  # 1 USDT
    assert not small.approved


def test_engine_sweeps_dust_in_housekeeping(tmp_path):
    from trading_bot.engine.trader import TradingEngine

    rows = make_trend_rows([(70, 0.0)], start_price=100.0)
    fixture = write_rows_csv(rows, tmp_path / "sweep.csv")
    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/sweep.db"},
        data={"source": "fixture", "fixture_path": fixture},
        reporting={"output_dir": str(tmp_path / "reports")},
    )
    engine = TradingEngine(
        cfg, migrations_dir=MIGRATIONS, project_root=tmp_path, close_db_on_shutdown=False
    )
    pid = engine.repos.positions.insert_open(
        mode=Mode.PAPER,
        symbol="BTCUSDT",
        qty=dec("0.06"),
        avg_entry_price=dec("100"),
        stop_price=dec("98"),
        entry_order_id="tb-en-sweep000000000000000000d",
        entry_fee=dec("0"),
    )
    engine.repos.positions.mark_dust(
        pid,
        dec("0.06"),
        dec("0"),
        "tb-ex-sweep000000000000000000d",
        dec("0"),
        dec("0"),
        "test:dust",
    )
    balances = engine.adapter._load_balances()
    balances["BTC"]["free"] = dec("0.06")
    engine.adapter._save_balances(balances)

    engine.run(max_cycles=3)
    assert engine.repos.positions.dust_positions(Mode.PAPER) == []
    engine.db.close()


# ------------------------------------------------- quality verifier (happy)
def test_quality_verifier_accepts_genuine_record(tmp_path):
    from trading_bot.security.quality import expected_hashes, sha256_file, verify_quality_record

    quality_dir = tmp_path / "var" / "quality"
    quality_dir.mkdir(parents=True)
    junit = quality_dir / "junit.xml"
    junit.write_text(
        '<testsuite tests="150" failures="0" errors="0" skipped="0"/>', encoding="utf-8"
    )
    (quality_dir / "coverage.json").write_text(
        json.dumps({"totals": {"percent_covered": 91.0}}), encoding="utf-8"
    )
    record = {
        "passed": True,
        "tests_collected": 150,
        "tests_run": 150,
        "tests_passed": 150,
        "tests_failed": 0,
        "tests_skipped": 0,
        "coverage_percent": 91.0,
        "required_safety_tests_missing": [],
        "results_hash": sha256_file(junit),
        "ran_at": datetime.now(UTC).isoformat(),
        "formatter": {"rc": 0},
        "linter": {"rc": 0},
        "type_check": {"rc": 0},
        "security_scan": {"rc": 0},
        "git_state": "no_repo",
        "git_commit": None,
        "git_dirty": False,
        **expected_hashes(tmp_path),
    }
    # the record lives under var/ (skipped by source_tree_hash), matching how
    # scripts/record_test_run.py writes var/quality/latest_test_run.json
    path = quality_dir / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    result = verify_quality_record(tmp_path, path)
    assert result.ok, result.failures

    # and any tampering with the recorded counts is caught by the artifacts
    record["tests_passed"] = 149
    path.write_text(json.dumps(record), encoding="utf-8")
    assert not verify_quality_record(tmp_path, path).ok


# ------------------------------------------------- key permission refusal
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


def test_verify_key_permissions_refuses_withdrawal_enabled():
    from trading_bot.config import constants as C
    from trading_bot.exchange.binance import BinanceAdapter
    from trading_bot.exchange.errors import ExchangeAuthError
    from trading_bot.security.secrets import StaticSecretProvider

    def transport(method, url, headers, params, timeout):
        if url.endswith("/sapi/v1/account/apiRestrictions"):
            return 200, {"enableWithdrawals": True, "enableSpotAndMarginTrading": True}
        return 200, {}

    adapter = BinanceAdapter(
        Mode.LIVE,
        StaticSecretProvider(
            {
                C.ENV_LIVE_KEY: "live-key-0123456789abcdef",
                C.ENV_LIVE_SECRET: "live-secret-0123456789abcdef",
            }
        ),
        transport=transport,
    )
    with pytest.raises(ExchangeAuthError, match="WITHDRAWALS ENABLED"):
        adapter.verify_key_permissions()
    # testnet: restrictions endpoint unavailable -> permissive no-op
    assert _testnet_adapter(transport).api_restrictions() is None


def test_order_trades_lookup_failure_returns_empty():
    def transport(method, url, headers, params, timeout):
        if url.endswith("/api/v3/myTrades"):
            return 500, {"msg": "down"}
        return 200, {}

    adapter = _testnet_adapter(transport)
    assert adapter._order_trades("BTCUSDT", "555") == ()
    assert adapter._order_trades("BTCUSDT", "") == ()


# ------------------------------------------------- accounting guard rails
def test_record_exit_rejects_regressed_cumulative(repos):
    portfolio = PortfolioService(repos, Mode.PAPER, RULES)
    repos.positions.insert_open(
        mode=Mode.PAPER,
        symbol="BTCUSDT",
        qty=dec("1"),
        avg_entry_price=dec("100"),
        stop_price=dec("98"),
        entry_order_id="tb-en-reg00000000000000000000d",
        entry_fee=dec("0.1"),
    )
    position = repos.positions.open_position(Mode.PAPER)
    coid = "tb-ex-reg00000000000000000000d"
    repos.orders.set_accounted_totals(coid, dec("0.5"), dec("55"), dec("0.05"))
    resp = OrderResponse(
        client_order_id=coid,
        exchange_order_id="e",
        symbol="BTCUSDT",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        state=OrderState.FILLED,
        requested_qty=dec("1"),
        executed_qty=dec("0.8"),
        cumulative_quote=dec("40"),  # LESS than accounted 55: corrupt feed
        fills=(),
        ts=T0,
    )
    with pytest.raises(RuntimeError, match="regressed"):
        portfolio.record_exit(position, resp, "strategy_exit")


def test_exit_with_no_fills_assumes_taker_fee(repos):
    portfolio = PortfolioService(repos, Mode.PAPER, RULES, fee_bps=dec("10"))
    repos.positions.insert_open(
        mode=Mode.PAPER,
        symbol="BTCUSDT",
        qty=dec("0.05000"),
        avg_entry_price=dec("100"),
        stop_price=dec("98"),
        entry_order_id="tb-en-nof00000000000000000000d",
        entry_fee=dec("0.005"),
    )
    position = repos.positions.open_position(Mode.PAPER)
    coid = "tb-ex-nof00000000000000000000d"
    resp = OrderResponse(
        client_order_id=coid,
        exchange_order_id="e",
        symbol="BTCUSDT",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        state=OrderState.FILLED,
        requested_qty=dec("0.05"),
        executed_qty=dec("0.05"),
        cumulative_quote=dec("5.5"),
        fills=(),
        ts=T0,
    )
    realized = portfolio.record_exit(position, resp, "strategy_exit")
    row = repos.db.query_one(
        "SELECT exit_fee FROM position_realizations WHERE exit_order_id = ?", (coid,)
    )
    assert dec(row["exit_fee"]) == dec("5.5") * dec("10") / dec("10000")
    assert realized < dec("0.5")  # fee-adjusted, not the naive 5.5 - 5.0 - fees'-free


def test_third_asset_fee_logged_not_valued(repos, caplog):
    import logging

    portfolio = PortfolioService(repos, Mode.PAPER, RULES)
    resp = OrderResponse(
        client_order_id="tb-en-bnb00000000000000000000d",
        exchange_order_id="e",
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        state=OrderState.FILLED,
        requested_qty=dec("0.05"),
        executed_qty=dec("0.05"),
        cumulative_quote=dec("5"),
        fills=(Fill(price=dec("100"), qty=dec("0.05"), fee=dec("0.001"), fee_asset="BNB"),),
        ts=T0,
    )
    with caplog.at_level(logging.WARNING):
        portfolio.record_entry(resp, stop_price=dec("98"))
    assert any("third asset" in r.message for r in caplog.records)


# --------------------------------------------- reconciliation branches
def test_reconciliation_clears_vanished_protective_link(repos):
    from trading_bot.core.models import SizedOrder
    from trading_bot.execution.gateway import ExecutionGateway
    from trading_bot.portfolio.reconciliation import Reconciler
    from trading_bot.risk.engine import RiskEngine
    from trading_bot.storage.audit import AuditLog

    clock = FrozenClock(T0)
    paper = PaperExchange(RULES, CFG.paper, FakeQuoteSource("100"), repos.sim_state, clock)
    risk = RiskEngine(CFG, dec("10"))
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
    reconciler = Reconciler(paper, repos, CFG, RULES, gateway, clock, portfolio)

    pid = repos.positions.insert_open(
        mode=Mode.PAPER,
        symbol="BTCUSDT",
        qty=dec("0.06"),
        avg_entry_price=dec("100"),
        stop_price=dec("98"),
        entry_order_id="tb-en-van00000000000000000000d",
        entry_fee=dec("0"),
    )
    # protective intent persisted in DB but the exchange never saw it
    ghost = SizedOrder(
        symbol="BTCUSDT",
        side=Side.SELL,
        order_type=OrderType.STOP_LOSS_LIMIT,
        qty=dec("0.06"),
        limit_price=dec("97.02"),
        stop_price=dec("98"),
        est_entry_price=dec("97"),
        est_notional=dec("5.8"),
        est_fee=dec("0.006"),
        risk_amount=ZERO,
        client_order_id="tb-ps-van00000000000000000000d",
    )
    repos.orders.insert_intent(
        ghost, Mode.PAPER, "cid", "protective", state=OrderState.ACKNOWLEDGED
    )
    repos.positions.set_protective_order(pid, ghost.client_order_id)
    balances = paper._load_balances()
    balances["BTC"]["free"] = dec("0.06")
    paper._save_balances(balances)

    result = reconciler.run()
    assert result.ok, result.details
    position = repos.positions.open_position(Mode.PAPER)
    assert position.protective_order_id is None  # link cleared for re-placement
    row = repos.orders.get_by_client_id(ghost.client_order_id)
    assert row["state"] == OrderState.REJECTED.value


# --------------------------------------------- market data service edges
def test_market_data_service_failure_paths(base_cfg):
    from trading_bot.exchange.errors import DataUnavailableError, ExchangeUnavailable
    from trading_bot.market_data.service import MarketDataService

    class DeadSource:
        def get_price(self, symbol):
            raise ExchangeUnavailable("down")

        def get_candles(self, symbol, interval, limit=200):
            raise ExchangeUnavailable("down")

    service = MarketDataService(DeadSource(), base_cfg, FrozenClock(T0), min_candles=10)
    candles, cval = service.closed_candles()
    assert candles == [] and not cval.ok
    quote, qval = service.quote()
    assert quote is None and not qval.ok
    assert service.consecutive_failures >= 1
    with pytest.raises(DataUnavailableError):
        service.require_quote()


# --------------------------------------------- types odds and ends
def test_types_helpers():
    from decimal import Decimal

    from trading_bot.core.types import (
        DecimalEncoder,
        is_multiple_of,
        pct_change,
        round_display,
    )

    assert pct_change(Decimal("110"), Decimal("100")) == Decimal("10")
    assert pct_change(Decimal("1"), Decimal("0")) == Decimal("0")
    assert round_display(Decimal("1.234567891"), 4) == Decimal("1.2346")
    assert not is_multiple_of(Decimal("1"), Decimal("0"))
    with pytest.raises(TypeError):
        json.dumps(object(), cls=DecimalEncoder)


def test_paper_partial_then_cancel_releases_pending(repos):
    paper = PaperExchange(
        RULES,
        CFG.paper.model_copy(update={"partial_fill_probability": dec("1")}),
        FakeQuoteSource("100"),
        repos.sim_state,
        FrozenClock(T0),
    )
    resp = paper.create_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=dec("0.06"),
            client_order_id="tb-en-pc000000000000000000000d",
        )
    )
    assert resp.state == OrderState.PARTIALLY_FILLED
    cancelled = paper.cancel_order("BTCUSDT", resp.client_order_id)
    assert cancelled.state == OrderState.CANCELLED
    assert cancelled.executed_qty > ZERO  # partial fill preserved
