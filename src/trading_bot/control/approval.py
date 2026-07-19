"""Daily continuation control: AUTO_CONTINUE vs DAILY_APPROVAL.

AUTO_CONTINUE: trading continues while every health/risk check passes.
DAILY_APPROVAL: after each daily report the approval window is consumed;
entries stop until the operator runs ``trading-bot approve --hours N`` from
the local machine. An unauthenticated message (email reply etc.) can never
grant approval — the only inputs are the CLI on the host and the DB flag it
sets.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from trading_bot.config.models import AppConfig
from trading_bot.core.enums import ContinuationMode
from trading_bot.core.models import iso, parse_iso
from trading_bot.exchange.interface import Clock
from trading_bot.storage.repositories import Repositories

log = logging.getLogger(__name__)


class ApprovalService:
    def __init__(self, repos: Repositories, cfg: AppConfig, clock: Clock) -> None:
        self.repos = repos
        self.cfg = cfg
        self.clock = clock

    # ------------------------------------------------------------------
    def is_paused(self) -> bool:
        return self.repos.flags.is_true(self.repos.flags.PAUSED)

    def pause(self, actor: str, note: str = "") -> None:
        self.repos.flags.set(self.repos.flags.PAUSED, "true")
        self.repos.events.approval("pause", None, actor, note)
        log.warning("trading paused by %s", actor)

    def resume(self, actor: str, note: str = "") -> None:
        self.repos.flags.set(self.repos.flags.PAUSED, "false")
        self.repos.events.approval("resume", None, actor, note)
        log.info("trading resumed by %s", actor)

    # ------------------------------------------------------------------
    def approve(self, hours: int | None, actor: str) -> str:
        hours = hours or self.cfg.continuation.approve_default_hours
        until = self.clock.now() + timedelta(hours=hours)
        self.repos.flags.set(self.repos.flags.APPROVAL_UNTIL, iso(until))
        self.repos.events.approval("approve", hours, actor)
        log.info("trading approved for %sh by %s (until %s)", hours, actor, iso(until))
        return iso(until)

    def consume_after_daily_report(self) -> None:
        """DAILY_APPROVAL: the report ends the approved window."""
        if self.cfg.continuation.mode == ContinuationMode.DAILY_APPROVAL:
            self.repos.flags.set(self.repos.flags.APPROVAL_UNTIL, iso(self.clock.now()))
            self.repos.events.approval("consumed_by_daily_report", None, "system")
            log.info("daily report generated — awaiting operator approval for the next day")

    # ------------------------------------------------------------------
    def entries_allowed(self, health_ok: bool) -> bool:
        if self.is_paused():
            return False
        if self.cfg.continuation.mode == ContinuationMode.AUTO_CONTINUE:
            return health_ok
        raw = self.repos.flags.get(self.repos.flags.APPROVAL_UNTIL)
        if not raw:
            return False
        try:
            return parse_iso(raw) > self.clock.now()
        except ValueError:
            return False
