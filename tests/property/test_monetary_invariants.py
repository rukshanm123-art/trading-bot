"""Property-based tests (hypothesis) for monetary invariants."""

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.helpers import RULES, T0, FakeQuoteSource, make_config
from trading_bot.core.enums import OrderState, OrderType, Side
from trading_bot.core.models import OrderRequest
from trading_bot.core.types import ZERO, dec, quantize_down
from trading_bot.exchange.interface import FrozenClock
from trading_bot.exchange.paper import PaperExchange
from trading_bot.execution.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    assert_transition,
)
from trading_bot.risk.sizing import SizingInputs, size_entry

pytestmark = pytest.mark.property

CFG = make_config()

decimals_pos = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("1000000"),
    allow_nan=False,
    allow_infinity=False,
    places=8,
)
steps = st.sampled_from(
    [Decimal("0.00001"), Decimal("0.0001"), Decimal("0.001"), Decimal("0.01"), Decimal("0.5")]
)


@given(value=decimals_pos, step=steps)
def test_quantize_down_never_rounds_up_and_is_step_aligned(value, step):
    q = quantize_down(value, step)
    assert q <= value
    assert q >= 0
    assert (q % step) == ZERO
    assert value - q < step  # floors to the NEAREST lower multiple


@given(
    equity=st.decimals(
        min_value=Decimal("1"),
        max_value=Decimal("100000"),
        allow_nan=False,
        allow_infinity=False,
        places=2,
    ),
    free_frac=st.decimals(min_value=Decimal("0"), max_value=Decimal("1"), places=3),
    price=st.decimals(
        min_value=Decimal("0.1"),
        max_value=Decimal("200000"),
        allow_nan=False,
        allow_infinity=False,
        places=2,
    ),
    stop_pct=st.decimals(min_value=Decimal("0.2"), max_value=Decimal("10"), places=1),
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.filter_too_much])
def test_sizing_invariants(equity, free_frac, price, stop_pct):
    quote_free = equity * free_frac
    result = size_entry(
        SizingInputs(
            equity=equity,
            quote_free=quote_free,
            est_entry_price=price,
            rules=RULES,
            risk=CFG.risk,
            stop_loss_pct=stop_pct,
            fee_bps=dec("10"),
        )
    )
    if not result.ok:
        return  # rejection is always a safe outcome
    # 1. cannot spend more than available balance
    assert result.est_notional + result.est_fee <= quote_free
    # 2. position size cannot exceed the configured allocation
    assert result.est_notional <= equity * CFG.risk.max_position_allocation_pct / 100
    # 3. estimated risk cannot exceed the risk budget
    assert result.risk_amount <= equity * CFG.risk.max_risk_per_trade_pct / 100
    # 4. cash reserve preserved after the purchase
    assert quote_free - (result.est_notional + result.est_fee) >= (
        equity * CFG.risk.min_cash_reserve_pct / 100
    )
    # 5. exchange filters respected — and never via upward rounding
    assert result.qty >= RULES.min_qty
    assert (result.qty % RULES.step_size) == ZERO
    assert result.est_notional >= RULES.min_notional
    # 6. the protective stop is representable and below entry
    assert ZERO < result.stop_price < price
    assert (result.stop_price % RULES.tick_size) == ZERO


@given(
    qty=st.decimals(min_value=Decimal("0.00001"), max_value=Decimal("0.2"), places=5),
    seed=st.integers(min_value=0, max_value=2**16),
)
@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_paper_exchange_balances_never_negative(tmp_path_factory, qty, seed):
    from tests.conftest import MIGRATIONS
    from trading_bot.storage.db import Database
    from trading_bot.storage.repositories import Repositories

    tmp = tmp_path_factory.mktemp("prop")
    db = Database(f"sqlite:///{tmp}/p.db")
    db.migrate(MIGRATIONS)
    repos = Repositories(db)
    sim = CFG.paper.model_copy(update={"seed": seed})
    px = PaperExchange(RULES, sim, FakeQuoteSource("100"), repos.sim_state, FrozenClock(T0))

    resp = px.create_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            qty=qty,
            client_order_id=f"tb-en-{seed:024x}"[:30],
        )
    )
    balances = px.get_balances()
    assert balances["USDT"].free >= ZERO
    assert balances["USDT"].locked >= ZERO
    assert balances["BTC"].free >= ZERO
    if resp.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
        # value conservation: total equity change is only costs, bounded by
        # fees + spread + slippage (generous 1% bound)
        equity = balances["USDT"].free + balances["BTC"].free * dec("100")
        assert equity >= dec("30") * dec("0.99")
        assert equity <= dec("30")  # simulator never creates money
    db.close()


@given(
    current=st.sampled_from(list(ALLOWED_TRANSITIONS.keys())),
    target=st.sampled_from(list(ALLOWED_TRANSITIONS.keys())),
)
def test_state_machine_total_and_consistent(current, target):
    allowed = target in ALLOWED_TRANSITIONS[current] or target == current
    if allowed:
        assert_transition(current, target)
    else:
        with pytest.raises(InvalidTransitionError):
            assert_transition(current, target)
