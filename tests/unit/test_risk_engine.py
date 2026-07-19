"""Risk engine: every gate must block entries; approval tokens must be
unforgeable, single-use and process-scoped."""

from datetime import timedelta

import pytest

from tests.helpers import RULES, T0, FakeQuoteSource, make_config, make_ctx, make_state
from trading_bot.core.enums import Mode, OrderType, ReasonCode, Side
from trading_bot.core.models import PositionState, SizedOrder
from trading_bot.core.types import dec
from trading_bot.exchange.interface import FrozenClock
from trading_bot.exchange.paper import PaperExchange
from trading_bot.execution.gateway import ExecutionGateway, GatewaySecurityError
from trading_bot.market_data.validation import ValidationResult
from trading_bot.risk.engine import RiskEngine
from trading_bot.storage.audit import AuditLog

CFG = make_config()


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine(CFG, fee_bps=dec("10"))


def make_position() -> PositionState:
    return PositionState(
        position_id="p1",
        symbol="BTCUSDT",
        qty=dec("0.05"),
        avg_entry_price=dec("100"),
        stop_price=dec("98"),
        opened_at=T0,
        entry_fee=dec("0.005"),
        entry_order_id="tb-en-x",
    )


# ---------------------------------------------------------------- gates
def test_benign_context_is_approved(engine):
    d = engine.evaluate_entry(make_ctx())
    assert d.approved
    assert d.order is not None and d.approval_token is not None
    assert d.codes == (ReasonCode.OK,)


def test_daily_loss_limit_blocks_entry(engine):
    ctx = make_ctx(state=make_state(realized_pnl_today=dec("-25")))  # 2.5% of 1000
    d = engine.evaluate_entry(ctx)
    assert not d.approved
    assert ReasonCode.DAILY_LOSS_LIMIT in d.codes
    assert d.order is None and d.approval_token is None


def test_weekly_loss_limit_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(state=make_state(pnl_7d_pct=dec("-5.5"))))
    assert ReasonCode.WEEKLY_LOSS_LIMIT in d.codes


def test_max_drawdown_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(state=make_state(drawdown_pct=dec("9"))))
    assert not d.approved
    assert ReasonCode.MAX_DRAWDOWN in d.codes


def test_consecutive_loss_pause_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(state=make_state(consecutive_losses=3)))
    assert ReasonCode.CONSECUTIVE_LOSS_PAUSE in d.codes


def test_cooldown_blocks_entry(engine):
    d = engine.evaluate_entry(
        make_ctx(state=make_state(consecutive_losses=1, cooldown_until=T0 + timedelta(hours=2)))
    )
    assert ReasonCode.COOLDOWN_ACTIVE in d.codes


def test_max_entries_per_day_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(state=make_state(entries_today=2)))
    assert ReasonCode.MAX_ENTRIES_PER_DAY in d.codes


def test_position_already_open_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(open_position=make_position()))
    assert ReasonCode.POSITION_ALREADY_OPEN in d.codes


def test_kill_switch_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(kill_switch_active=True, kill_switch_reason="test"))
    assert ReasonCode.KILL_SWITCH_ACTIVE in d.codes


def test_circuit_breaker_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(circuit_breaker_open=True))
    assert ReasonCode.CIRCUIT_BREAKER_OPEN in d.codes


def test_missing_approval_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(approval_ok=False))
    assert ReasonCode.TRADING_NOT_APPROVED in d.codes


def test_duplicate_signal_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(duplicate_signal=True))
    assert ReasonCode.DUPLICATE_SIGNAL in d.codes


def test_unknown_orders_block_entry(engine):
    d = engine.evaluate_entry(make_ctx(state=make_state(unknown_orders=1)))
    assert ReasonCode.UNKNOWN_ORDER_PENDING in d.codes


def test_active_entry_order_blocks_duplicate_entry(engine):
    d = engine.evaluate_entry(make_ctx(state=make_state(active_entry_orders=1)))
    assert ReasonCode.ENTRY_ORDER_ACTIVE in d.codes


def test_reconciliation_mismatch_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(state=make_state(reconciliation_blocked=True)))
    assert ReasonCode.RECONCILIATION_MISMATCH in d.codes


def test_api_error_threshold_blocks_entry(engine):
    d = engine.evaluate_entry(make_ctx(state=make_state(api_errors_last_hour=11)))
    assert ReasonCode.API_ERROR_THRESHOLD in d.codes


def test_stale_candles_block_entry(engine):
    d = engine.evaluate_entry(
        make_ctx(candle_validation=ValidationResult.failure("stale candles: 999s"))
    )
    assert ReasonCode.STALE_MARKET_DATA in d.codes


def test_gap_blocks_entry(engine):
    d = engine.evaluate_entry(
        make_ctx(candle_validation=ValidationResult.failure("candle[5] abnormal jump 15%"))
    )
    assert ReasonCode.GAP_TOLERANCE_EXCEEDED in d.codes


def test_wide_spread_blocks_entry(engine):
    d = engine.evaluate_entry(
        make_ctx(quote_validation=ValidationResult.failure("spread 30bps > max 20bps"))
    )
    assert ReasonCode.SPREAD_TOO_WIDE in d.codes


def test_missing_quote_blocks_entry(engine):
    d = engine.evaluate_entry(
        make_ctx(quote=None, quote_validation=ValidationResult.failure("no quote"))
    )
    assert not d.approved


def test_symbol_not_trading_blocks_entry(engine):
    from dataclasses import replace

    halted = replace(RULES, status="HALT")
    d = engine.evaluate_entry(make_ctx(rules=halted))
    assert ReasonCode.SYMBOL_NOT_TRADING in d.codes


def test_rejection_includes_structured_inputs(engine):
    d = engine.evaluate_entry(make_ctx(state=make_state(drawdown_pct=dec("9"))))
    assert d.inputs["drawdown_pct"] == "9"
    assert "equity" in d.inputs


# ---------------------------------------------------------------- tokens
def _gateway(repos, risk_engine, clock=None) -> ExecutionGateway:
    clock = clock or FrozenClock(T0)
    paper = PaperExchange(RULES, CFG.paper, FakeQuoteSource(), repos.sim_state, clock)
    return ExecutionGateway(
        paper,
        repos,
        risk_engine,
        Mode.PAPER,
        AuditLog(repos.db),
        clock,
        kill_switch_check=lambda: (False, ""),
    )


def _sized(qty="0.06") -> SizedOrder:
    return SizedOrder(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=dec(qty),
        limit_price=None,
        stop_price=dec("98"),
        est_entry_price=dec("100"),
        est_notional=dec("6"),
        est_fee=dec("0.006"),
        risk_amount=dec("0.12"),
        client_order_id="tb-en-deadbeefdeadbeefdeadbeef",
    )


def test_rejected_risk_cannot_reach_gateway(engine, repos):
    """No token -> no order. A rejected evaluation carries no token, and the
    gateway refuses both a missing and a forged token."""
    gateway = _gateway(repos, engine)
    order = _sized()
    with pytest.raises(GatewaySecurityError):
        gateway.submit(order, None, "cid", "entry")
    with pytest.raises(GatewaySecurityError):
        gateway.submit(order, "f" * 64, "cid", "entry")
    # nothing reached the exchange, and no submitted order row exists
    assert repos.orders.get_by_client_id(order.client_order_id) is None


def test_token_is_single_use(engine, repos):
    d = engine.evaluate_entry(make_ctx())
    assert d.approved and d.order is not None
    assert engine.verify_and_consume(d.order, d.approval_token)
    assert not engine.verify_and_consume(d.order, d.approval_token)  # replay


def test_token_bound_to_order_params(engine):
    from dataclasses import replace

    d = engine.evaluate_entry(make_ctx())
    assert d.approved and d.order is not None
    tampered = replace(d.order, qty=d.order.qty * 2)
    assert not engine.verify_and_consume(tampered, d.approval_token)


def test_restart_invalidates_previous_tokens(engine, repos):
    """Process restart -> fresh HMAC key -> old approvals are dead. The safe
    behaviour is re-evaluation, never resubmission of a stored intent."""
    d = engine.evaluate_entry(make_ctx())
    assert d.approved and d.order is not None
    restarted_engine = RiskEngine(CFG, fee_bps=dec("10"))  # simulates restart
    gateway = _gateway(repos, restarted_engine)
    with pytest.raises(GatewaySecurityError):
        gateway.submit(d.order, d.approval_token, "cid", "entry")


# ---------------------------------------------------------------- exits
def test_exit_allowed_even_with_kill_switch(engine):
    ctx = make_ctx(open_position=make_position(), base_free=dec("0.05"), kill_switch_active=True)
    d = engine.evaluate_exit(ctx, "stop_breach")
    assert d.approved and d.order is not None
    assert d.order.side == Side.SELL
    assert d.order.order_type == OrderType.MARKET


def test_exit_dust_rejected(engine):
    from dataclasses import replace

    pos = replace(make_position(), qty=dec("0.000004"))
    d = engine.evaluate_exit(make_ctx(open_position=pos, base_free=dec("0.000004")), "x")
    assert not d.approved
    assert ReasonCode.QTY_BELOW_MIN in d.codes


def test_exit_requires_quote(engine):
    d = engine.evaluate_exit(
        make_ctx(open_position=make_position(), base_free=dec("0.05"), quote=None), "x"
    )
    assert not d.approved
