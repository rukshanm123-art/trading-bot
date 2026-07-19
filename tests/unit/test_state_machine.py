"""Order state machine: every transition is explicit; illegal ones raise."""

import pytest

from trading_bot.core.enums import OrderState as S
from trading_bot.execution.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    assert_transition,
)


def test_happy_path_market_order():
    assert_transition(S.PROPOSED, S.RISK_APPROVED)
    assert_transition(S.RISK_APPROVED, S.SUBMITTED)
    assert_transition(S.SUBMITTED, S.FILLED)


def test_partial_fill_path():
    assert_transition(S.SUBMITTED, S.PARTIALLY_FILLED)
    assert_transition(S.PARTIALLY_FILLED, S.FILLED)


def test_unknown_then_reconciled():
    assert_transition(S.SUBMITTED, S.UNKNOWN)
    assert_transition(S.UNKNOWN, S.FILLED)
    assert_transition(S.UNKNOWN, S.REJECTED)
    assert_transition(S.UNKNOWN, S.RECONCILIATION_REQUIRED)
    assert_transition(S.RECONCILIATION_REQUIRED, S.CANCELLED)


def test_abandoned_intent_path():
    # persisted intent from a dead process is invalidated, never resubmitted
    assert_transition(S.RISK_APPROVED, S.REJECTED)


def test_terminal_states_are_terminal():
    for terminal in (S.FILLED, S.CANCELLED, S.REJECTED, S.RISK_REJECTED):
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()
        with pytest.raises(InvalidTransitionError):
            assert_transition(terminal, S.SUBMITTED)


def test_cannot_skip_risk_approval():
    with pytest.raises(InvalidTransitionError):
        assert_transition(S.PROPOSED, S.SUBMITTED)


def test_self_transition_is_noop():
    assert_transition(S.SUBMITTED, S.SUBMITTED)  # no exception
