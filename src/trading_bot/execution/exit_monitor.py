"""Protective exit monitor.

Binance spot has no native linked stop for an existing holding placed by a
separate order in all cases, so the stop is SOFTWARE-MONITORED: every cycle
the monitor compares the live bid against the position's stop level and, if
breached, requests a market exit through risk engine + gateway.

Honesty note (docs/RISK_MODEL.md): a software stop cannot guarantee an
execution price. If the process is down or the market gaps, the realised loss
can exceed the planned stop distance. This is a monitored exit, not a
guaranteed stop.
"""

from __future__ import annotations

import logging

from trading_bot.core.models import PositionState, PriceQuote

log = logging.getLogger(__name__)


class ExitCheck:
    STOP_BREACH = "stop_breach"
    STRATEGY_EXIT = "strategy_exit"
    NONE = ""


def check_protective_exit(
    position: PositionState | None,
    quote: PriceQuote | None,
) -> str:
    """Returns an exit reason ('' when no exit is required)."""
    if position is None or quote is None:
        return ExitCheck.NONE
    if quote.bid <= position.stop_price:
        log.warning(
            "protective stop breached: bid %s <= stop %s (position %s)",
            quote.bid,
            position.stop_price,
            position.position_id,
        )
        return ExitCheck.STOP_BREACH
    return ExitCheck.NONE
