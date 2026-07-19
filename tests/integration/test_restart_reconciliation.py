"""Restart safety: balances survive, stale intents are abandoned (never
resubmitted), and reconciliation clears or blocks correctly."""

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
from trading_bot.core.models import SizedOrder, set_time_provider
from trading_bot.core.types import dec
from trading_bot.engine.trader import TradingEngine
from trading_bot.exchange.interface import FrozenClock
from trading_bot.exchange.paper import PaperExchange
from trading_bot.execution.gateway import ExecutionGateway
from trading_bot.portfolio.reconciliation import Reconciler
from trading_bot.risk.engine import RiskEngine
from trading_bot.storage.audit import AuditLog

pytestmark = pytest.mark.integration


def test_balances_and_position_survive_restart(tmp_path):
    rows = make_trend_rows([(60, 0.0), (30, 1.2), (60, 0.05)], start_price=100.0)
    fixture = write_rows_csv(rows, tmp_path / "restart.csv")

    def build():
        cfg = make_config(
            db={"url": f"sqlite:///{tmp_path}/restart.db"},
            data={"source": "fixture", "fixture_path": fixture},
            reporting={"output_dir": str(tmp_path / "reports")},
        )
        return TradingEngine(
            cfg, migrations_dir=MIGRATIONS, project_root=tmp_path, close_db_on_shutdown=False
        )

    engine_a = build()
    engine_a.run(max_cycles=80)
    balances_a = {
        a: (str(b.free), str(b.locked)) for a, b in engine_a.adapter.get_balances().items()
    }
    position_a = engine_a.repos.positions.open_position(Mode.PAPER)
    engine_a.db.close()

    engine_b = build()  # fresh process over the same database
    balances_b = {
        a: (str(b.free), str(b.locked)) for a, b in engine_b.adapter.get_balances().items()
    }
    assert balances_a == balances_b
    position_b = engine_b.repos.positions.open_position(Mode.PAPER)
    if position_a is None:
        assert position_b is None
    else:
        assert position_b is not None
        assert position_b.qty == position_a.qty
        assert position_b.stop_price == position_a.stop_price
    # startup reconciliation over restored state must be clean
    result = engine_b.reconciler.run()
    assert result.ok, result.details
    engine_b.db.close()


def _components(repos, clock):
    cfg = make_config()
    risk = RiskEngine(cfg, dec("10"))
    paper = PaperExchange(RULES, cfg.paper, FakeQuoteSource(), repos.sim_state, clock)
    gateway = ExecutionGateway(
        paper,
        repos,
        risk,
        Mode.PAPER,
        AuditLog(repos.db),
        clock,
        kill_switch_check=lambda: (False, ""),
    )
    reconciler = Reconciler(paper, repos, cfg, RULES, gateway, clock)
    return cfg, risk, paper, gateway, reconciler


def _sized(coid: str) -> SizedOrder:
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
        client_order_id=coid,
    )


def test_stale_intent_abandoned_after_restart(repos):
    """An intent persisted before a crash (RISK_APPROVED, never submitted) is
    marked REJECTED by reconciliation after max_order_age — it is never
    retried, because approval tokens die with the process."""
    clock = FrozenClock(T0)
    set_time_provider(clock.now)
    cfg, risk, paper, gateway, reconciler = _components(repos, clock)

    order = _sized("tb-en-99999999999999999999999a")
    repos.orders.insert_intent(order, Mode.PAPER, "cid", "entry", state=OrderState.RISK_APPROVED)
    assert len(repos.orders.non_terminal_orders(Mode.PAPER)) == 1

    clock.advance(cfg.execution.max_order_age_s + 5)
    result = reconciler.run()
    assert result.ok, result.details
    row = repos.orders.get_by_client_id(order.client_order_id)
    assert row["state"] == OrderState.REJECTED.value
    assert repos.orders.non_terminal_orders(Mode.PAPER) == []
    # and the paper account was never charged
    assert paper.get_balances()["USDT"].free == dec("30")


def test_unknown_order_blocks_until_resolved(repos):
    """Submission timeout -> UNKNOWN -> entries blocked -> reconciliation
    resolves by client-order-id lookup -> block cleared."""
    from trading_bot.exchange.errors import OrderStateUnknownError

    clock = FrozenClock(T0)
    set_time_provider(clock.now)
    cfg, risk, paper, gateway, reconciler = _components(repos, clock)

    class TimeoutOnce:
        """Adapter proxy: create_order dies mid-flight exactly once."""

        def __init__(self, inner):
            self.inner = inner
            self.kind = inner.kind
            self.fired = False

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def create_order(self, request):
            if not self.fired:
                self.fired = True
                raise OrderStateUnknownError("socket timeout mid-submission")
            return self.inner.create_order(request)

    flaky = TimeoutOnce(paper)
    gateway_flaky = ExecutionGateway(
        flaky,
        repos,
        risk,
        Mode.PAPER,
        AuditLog(repos.db),
        clock,
        kill_switch_check=lambda: (False, ""),
    )
    order = _sized("tb-en-88888888888888888888888b")
    token = risk._token_for(order)
    result = gateway_flaky.submit(order, token, "cid", "entry")
    assert result.state == OrderState.UNKNOWN
    assert repos.flags.is_true(repos.flags.UNKNOWN_ORDER_BLOCK)
    assert len(repos.orders.unknown_orders(Mode.PAPER)) == 1

    # reconcile: paper exchange has no such order -> REJECTED, block cleared
    unresolved = gateway_flaky.resolve_unknown_orders()
    assert unresolved == 0
    assert not repos.flags.is_true(repos.flags.UNKNOWN_ORDER_BLOCK)
    row = repos.orders.get_by_client_id(order.client_order_id)
    assert row["state"] == OrderState.REJECTED.value
    # no duplicate order was ever created, and balances are intact
    assert paper.get_balances()["USDT"].free == dec("30")


def test_reconciliation_detects_unexplained_holdings(repos):
    """Exchange holds base asset with no recorded position -> block entries."""
    clock = FrozenClock(T0)
    set_time_provider(clock.now)
    cfg, risk, paper, gateway, reconciler = _components(repos, clock)

    # money appears out of band (simulates manual trading on the same account)
    balances = paper._load_balances()
    balances["BTC"]["free"] = dec(
        "0.15"
    )  # 15 USDT at price 100: above minNotional, sellable, unexplained
    paper._save_balances(balances)

    result = reconciler.run()
    assert not result.ok
    assert repos.flags.is_true(repos.flags.RECONCILIATION_BLOCK)
    # and the risk engine consequently blocks entries
    from tests.helpers import make_ctx, make_state
    from trading_bot.core.enums import ReasonCode

    decision = risk.evaluate_entry(make_ctx(state=make_state(reconciliation_blocked=True)))
    assert ReasonCode.RECONCILIATION_MISMATCH in decision.codes
