"""Kill switches (all five) and the automatic circuit breaker."""

from tests.helpers import RULES, T0, FakeQuoteSource, make_config, make_state
from trading_bot.config import constants as C
from trading_bot.control.circuit import CircuitBreaker
from trading_bot.control.killswitch import KillSwitch
from trading_bot.core.enums import KillSwitchSource, Mode, OrderState, OrderType, Side
from trading_bot.core.models import SizedOrder
from trading_bot.core.types import dec
from trading_bot.exchange.interface import FrozenClock
from trading_bot.exchange.paper import PaperExchange
from trading_bot.execution.gateway import ExecutionGateway
from trading_bot.risk.engine import RiskEngine
from trading_bot.storage.audit import AuditLog

CFG = make_config()


def test_env_kill_switch(repos, tmp_path):
    ks = KillSwitch(repos, tmp_path, env={C.ENV_KILL_SWITCH: "true"})
    active, reason = ks.check()
    assert active and "env" in reason


def test_file_kill_switch_blocks(repos, tmp_path):
    """The STOP_TRADING file alone must stop entries — even at the gateway,
    even with a perfectly valid risk approval."""
    stop = tmp_path / C.STOP_FILE_NAME
    stop.write_text("emergency\n")
    ks = KillSwitch(repos, tmp_path, env={})
    active, reason = ks.check()
    assert active and "file" in reason

    risk = RiskEngine(CFG, dec("10"))
    paper = PaperExchange(RULES, CFG.paper, FakeQuoteSource(), repos.sim_state, FrozenClock(T0))
    gateway = ExecutionGateway(
        paper,
        repos,
        risk,
        Mode.PAPER,
        AuditLog(repos.db),
        FrozenClock(T0),
        kill_switch_check=ks.check,
    )
    order = SizedOrder(
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
        client_order_id="tb-en-011111111111111111111111",
    )
    token = risk._token_for(order)
    result = gateway.submit(order, token, "cid", "entry")
    assert not result.submitted
    assert result.state == OrderState.RISK_REJECTED
    assert "kill_switch" in result.error
    # the paper account was never touched
    assert paper.get_balances()["USDT"].free == dec("30")


def test_db_flag_kill_switch(repos, tmp_path):
    ks = KillSwitch(repos, tmp_path, env={})
    ks.activate(KillSwitchSource.CLI, "operator stop")
    (tmp_path / C.STOP_FILE_NAME).unlink()  # remove file: DB flag must still hold
    active, reason = ks.check()
    assert active and "operator stop" in reason


def test_kill_switch_reset_is_manual_and_explicit(repos, tmp_path):
    ks = KillSwitch(repos, tmp_path, env={})
    ks.activate(KillSwitchSource.CLI, "x")
    assert ks.check()[0]
    blockers = ks.reset("tester", "resolved")
    assert blockers == []
    assert not ks.check()[0]


def test_reset_cannot_clear_env_switch(repos, tmp_path):
    ks = KillSwitch(repos, tmp_path, env={C.ENV_KILL_SWITCH: "true"})
    blockers = ks.reset("tester")
    assert any(C.ENV_KILL_SWITCH in b for b in blockers)
    assert ks.check()[0]  # still active via env


def test_exit_orders_still_allowed_when_killed(repos, tmp_path):
    """Kill switch blocks ENTRIES; protective exits must still work."""
    ks = KillSwitch(repos, tmp_path, env={C.ENV_KILL_SWITCH: "true"})
    risk = RiskEngine(CFG, dec("10"))
    paper = PaperExchange(RULES, CFG.paper, FakeQuoteSource(), repos.sim_state, FrozenClock(T0))
    # seed a holding to sell
    paper.create_order(
        __import__("trading_bot.core.models", fromlist=["OrderRequest"]).OrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=dec("0.06"),
            client_order_id="tb-en-e2222222222222222222222e",
        )
    )
    gateway = ExecutionGateway(
        paper,
        repos,
        risk,
        Mode.PAPER,
        AuditLog(repos.db),
        FrozenClock(T0),
        kill_switch_check=ks.check,
    )
    btc = paper.get_balances()["BTC"].free
    from trading_bot.core.types import quantize_down

    sell_qty = quantize_down(btc, RULES.step_size)
    order = SizedOrder(
        symbol="BTCUSDT",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        qty=sell_qty,
        limit_price=None,
        stop_price=dec("98"),
        est_entry_price=dec("100"),
        est_notional=sell_qty * dec("100"),
        est_fee=dec("0.006"),
        risk_amount=dec("0"),
        client_order_id="tb-ex-e3333333333333333333333e",
    )
    token = risk._token_for(order)
    result = gateway.submit(order, token, "cid", "exit")
    assert result.submitted
    assert result.state == OrderState.FILLED


# ------------------------------------------------------------- circuit
def _breaker(repos, tmp_path, latch_after=3) -> CircuitBreaker:
    ks = KillSwitch(repos, tmp_path, env={})
    return CircuitBreaker(CFG, ks, latch_after=latch_after)


def test_circuit_opens_on_api_errors(repos, tmp_path):
    cb = _breaker(repos, tmp_path)
    status = cb.evaluate(make_state(api_errors_last_hour=11), data_failures=0, db_healthy=True)
    assert status.open


def test_circuit_opens_on_data_failures(repos, tmp_path):
    cb = _breaker(repos, tmp_path)
    status = cb.evaluate(make_state(), data_failures=3, db_healthy=True)
    assert status.open


def test_circuit_closed_when_healthy(repos, tmp_path):
    cb = _breaker(repos, tmp_path)
    status = cb.evaluate(make_state(), data_failures=0, db_healthy=True)
    assert not status.open


def test_circuit_latches_kill_switch_on_persistent_hard_trip(repos, tmp_path):
    cb = _breaker(repos, tmp_path, latch_after=3)
    ks = cb.kill_switch
    bad = make_state(unknown_orders=1)
    for _ in range(3):
        cb.evaluate(bad, data_failures=0, db_healthy=True)
    active, reason = ks.check()
    assert active
    assert "unknown order" in reason


def test_circuit_hard_trip_counter_resets_when_healthy(repos, tmp_path):
    cb = _breaker(repos, tmp_path, latch_after=3)
    bad = make_state(reconciliation_blocked=True)
    cb.evaluate(bad, 0, True)
    cb.evaluate(bad, 0, True)
    cb.evaluate(make_state(), 0, True)  # recovers
    cb.evaluate(bad, 0, True)
    cb.evaluate(bad, 0, True)
    assert not cb.kill_switch.check()[0]  # never reached 3 consecutive
