"""Position sizing: risk budget, allocation cap, cash reserve, exchange
minimums. The cardinal rule: sizes are floored, NEVER rounded up."""

from dataclasses import replace

from tests.helpers import RULES, make_config
from trading_bot.core.enums import ReasonCode
from trading_bot.core.types import dec
from trading_bot.risk.sizing import SizingInputs, size_entry, size_exit_qty

CFG = make_config()


def inputs(**kw) -> SizingInputs:
    defaults = dict(
        equity=dec("1000"),
        quote_free=dec("900"),
        est_entry_price=dec("100"),
        rules=RULES,
        risk=CFG.risk,
        stop_loss_pct=dec("2.0"),
        fee_bps=dec("10"),
    )
    defaults.update(kw)
    return SizingInputs(**defaults)


def test_happy_path_produces_compliant_order():
    r = size_entry(inputs())
    assert r.ok
    assert r.qty > 0
    assert (r.qty % RULES.step_size) == 0
    assert r.qty * dec("100") >= RULES.min_notional
    assert r.stop_price < dec("100")
    assert (r.stop_price % RULES.tick_size) == 0


def test_risk_amount_never_exceeds_budget():
    r = size_entry(inputs())
    budget = dec("1000") * CFG.risk.max_risk_per_trade_pct / 100
    assert r.risk_amount <= budget


def test_allocation_cap_enforced():
    # huge risk allowance via wide stop: allocation must be the binding cap
    r = size_entry(inputs(stop_loss_pct=dec("10")))
    assert r.ok
    alloc_cap = dec("1000") * CFG.risk.max_position_allocation_pct / 100
    assert r.est_notional <= alloc_cap


def test_min_notional_rejected_never_rounded_up():
    """NZD ~20 account: 20 USDT equity, 20% allocation = 4 USDT < 5 minNotional.
    The correct behaviour is rejection, not rounding up into a bigger trade."""
    r = size_entry(inputs(equity=dec("20"), quote_free=dec("20"), est_entry_price=dec("60000")))
    assert not r.ok
    assert r.qty == 0
    assert ReasonCode.MIN_NOTIONAL_EXCEEDS_RISK in r.codes


def test_trapped_exit_below_minimum_notional_rejected():
    """Entry notional clears 5 USDT, but the conservative stop exit does not."""
    r = size_entry(inputs(equity=dec("25.5"), quote_free=dec("25.5"), est_entry_price=dec("100")))
    assert not r.ok
    assert r.est_notional >= RULES.min_notional
    assert r.protective_exit_notional < RULES.min_notional
    assert ReasonCode.PROTECTIVE_EXIT_NOT_REPRESENTABLE in r.codes


def test_entry_and_stop_exit_both_valid_may_proceed():
    r = size_entry(inputs(equity=dec("30"), quote_free=dec("30"), est_entry_price=dec("100")))
    assert r.ok
    assert r.protective_exit_notional >= RULES.min_notional


def test_never_increases_quantity_to_satisfy_exit_minimum_when_risk_would_increase():
    r = size_entry(inputs(equity=dec("25.5"), quote_free=dec("25.5"), est_entry_price=dec("100")))
    budget = dec("25.5") * CFG.risk.max_risk_per_trade_pct / 100
    assert not r.ok
    assert r.qty == dec("0.051")
    assert r.risk_amount <= budget
    assert ReasonCode.PROTECTIVE_EXIT_NOT_REPRESENTABLE in r.codes


def test_rounding_down_can_make_stop_exit_invalid():
    coarse_rules = replace(RULES, step_size=dec("0.001"), min_notional=dec("5.15"))
    r = size_entry(
        inputs(
            equity=dec("26"),
            quote_free=dec("26"),
            est_entry_price=dec("100"),
            rules=coarse_rules,
        )
    )
    assert not r.ok
    assert r.est_notional >= coarse_rules.min_notional
    assert r.protective_exit_notional < coarse_rules.min_notional
    assert ReasonCode.PROTECTIVE_EXIT_NOT_REPRESENTABLE in r.codes


def test_fees_slippage_and_buffer_make_borderline_exit_invalid():
    strict_rules = replace(RULES, min_notional=dec("5.83"))
    r = size_entry(inputs(equity=dec("30"), quote_free=dec("30"), rules=strict_rules))
    assert not r.ok
    assert r.qty * r.stop_price >= strict_rules.min_notional
    assert r.protective_exit_notional < strict_rules.min_notional
    assert ReasonCode.PROTECTIVE_EXIT_NOT_REPRESENTABLE in r.codes


def test_thirty_usdt_account_can_trade_at_min_notional():
    r = size_entry(inputs(equity=dec("30"), quote_free=dec("30"), est_entry_price=dec("100")))
    assert r.ok
    assert r.est_notional >= RULES.min_notional
    # 20% allocation cap of 30 = 6
    assert r.est_notional <= dec("6")


def test_cash_reserve_blocks_when_free_below_reserve():
    r = size_entry(inputs(equity=dec("1000"), quote_free=dec("400")))  # reserve = 500
    assert not r.ok
    assert ReasonCode.CASH_RESERVE_BREACH in r.codes


def test_reserve_survives_after_purchase():
    r = size_entry(inputs())
    reserve = dec("1000") * CFG.risk.min_cash_reserve_pct / 100
    assert dec("900") - (r.est_notional + r.est_fee) >= reserve


def test_step_rounding_down():
    # price chosen so raw qty is not step-aligned
    r = size_entry(inputs(est_entry_price=dec("333.33")))
    assert r.ok
    assert (r.qty % RULES.step_size) == 0


def test_no_valid_stop_rejected():
    # price so small the 2% stop quantizes to zero on the 0.01 tick grid
    r = size_entry(
        inputs(est_entry_price=dec("0.005"), equity=dec("1000000"), quote_free=dec("900000"))
    )
    assert not r.ok
    assert ReasonCode.NO_VALID_STOP in r.codes


def test_zero_equity_rejected():
    r = size_entry(inputs(equity=dec("0"), quote_free=dec("0")))
    assert not r.ok


def test_exit_qty_floors_and_respects_dust():
    assert size_exit_qty(dec("0.0512345"), dec("1"), RULES) == dec("0.05123")
    # below exchange minimum -> unsellable dust
    assert size_exit_qty(dec("0.000005"), dec("1"), RULES) is None
    # limited by available balance
    assert size_exit_qty(dec("1"), dec("0.5"), RULES) == dec("0.5")


def test_fee_aware_sizing_stays_affordable():
    r = size_entry(inputs(quote_free=dec("506")))  # barely above the 500 reserve
    if r.ok:
        assert r.est_notional + r.est_fee <= dec("506") - dec("500")


def test_tiny_account_produces_no_trade_instead_of_unsafe_sizing():
    r = size_entry(inputs(equity=dec("24"), quote_free=dec("24"), est_entry_price=dec("100")))
    assert not r.ok
    assert r.qty == 0


def test_previous_trapped_position_fixture_cannot_open():
    r = size_entry(inputs(equity=dec("25.5"), quote_free=dec("25.5"), est_entry_price=dec("100")))
    assert not r.ok
    assert r.est_notional >= dec("5")
    assert r.protective_exit_notional < dec("5")
