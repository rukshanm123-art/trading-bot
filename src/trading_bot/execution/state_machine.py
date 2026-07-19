"""Order execution state machine. Illegal transitions raise; nothing is silent."""

from __future__ import annotations

from trading_bot.core.enums import OrderState

_S = OrderState

ALLOWED_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    _S.PROPOSED: frozenset({_S.RISK_APPROVED, _S.RISK_REJECTED}),
    _S.RISK_REJECTED: frozenset(),
    # RISK_APPROVED -> REJECTED covers abandoned intents: approval tokens are
    # process-scoped, so an intent persisted before a crash is invalidated and
    # marked rejected by reconciliation, never resubmitted.
    _S.RISK_APPROVED: frozenset({_S.SUBMITTED, _S.REJECTED}),
    _S.SUBMITTED: frozenset(
        {_S.ACKNOWLEDGED, _S.PARTIALLY_FILLED, _S.FILLED, _S.REJECTED, _S.UNKNOWN}
    ),
    _S.ACKNOWLEDGED: frozenset(
        {
            _S.PARTIALLY_FILLED,
            _S.FILLED,
            _S.CANCEL_REQUESTED,
            _S.CANCELLED,
            _S.REJECTED,
            _S.UNKNOWN,
            _S.RECONCILIATION_REQUIRED,
        }
    ),
    _S.PARTIALLY_FILLED: frozenset(
        {_S.FILLED, _S.CANCEL_REQUESTED, _S.CANCELLED, _S.UNKNOWN, _S.RECONCILIATION_REQUIRED}
    ),
    _S.FILLED: frozenset(),
    _S.CANCEL_REQUESTED: frozenset({_S.CANCELLED, _S.FILLED, _S.PARTIALLY_FILLED, _S.UNKNOWN}),
    _S.CANCELLED: frozenset(),
    _S.REJECTED: frozenset(),
    _S.UNKNOWN: frozenset(
        {
            _S.ACKNOWLEDGED,
            _S.PARTIALLY_FILLED,
            _S.FILLED,
            _S.CANCELLED,
            _S.REJECTED,
            _S.RECONCILIATION_REQUIRED,
        }
    ),
    _S.RECONCILIATION_REQUIRED: frozenset(
        {_S.ACKNOWLEDGED, _S.PARTIALLY_FILLED, _S.FILLED, _S.CANCELLED, _S.REJECTED}
    ),
}


class InvalidTransitionError(RuntimeError):
    pass


def assert_transition(current: OrderState, new: OrderState) -> None:
    if new == current:
        return
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if new not in allowed:
        raise InvalidTransitionError(f"illegal order state transition {current} -> {new}")
