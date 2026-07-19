"""Automatic circuit breaker.

Opens (blocking new entries) while unhealthy conditions persist; if a hard
condition holds for ``latch_after`` consecutive evaluations, it latches the
kill switch, which then requires manual reset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from trading_bot.config.models import AppConfig
from trading_bot.control.killswitch import KillSwitch
from trading_bot.core.enums import KillSwitchSource
from trading_bot.risk.state import RiskStateSnapshot

log = logging.getLogger(__name__)


@dataclass
class CircuitStatus:
    open: bool = False
    reasons: list[str] = field(default_factory=list)


class CircuitBreaker:
    def __init__(self, cfg: AppConfig, kill_switch: KillSwitch, latch_after: int = 5) -> None:
        self.cfg = cfg
        self.kill_switch = kill_switch
        self.latch_after = latch_after
        self._consecutive_hard_trips = 0

    def evaluate(
        self,
        state: RiskStateSnapshot,
        data_failures: int,
        db_healthy: bool,
    ) -> CircuitStatus:
        reasons: list[str] = []
        hard = False

        if state.api_errors_last_hour > self.cfg.risk.max_api_errors_per_hour:
            reasons.append(
                f"api errors {state.api_errors_last_hour} > "
                f"{self.cfg.risk.max_api_errors_per_hour}/h"
            )
        if data_failures >= 3:
            reasons.append(f"{data_failures} consecutive market-data failures")
        if state.unknown_orders > 0:
            reasons.append(f"{state.unknown_orders} unknown order(s)")
            hard = True
        if state.reconciliation_blocked:
            reasons.append("reconciliation mismatch")
            hard = True
        if not db_healthy:
            reasons.append("database integrity check failed")
            hard = True
        if state.drawdown_pct >= self.cfg.risk.max_drawdown_pct:
            reasons.append(
                f"drawdown {state.drawdown_pct:.2f}% >= {self.cfg.risk.max_drawdown_pct}%"
            )
            hard = True

        status = CircuitStatus(open=bool(reasons), reasons=reasons)

        if hard:
            self._consecutive_hard_trips += 1
            if self._consecutive_hard_trips >= self.latch_after:
                already, _ = self.kill_switch.check()
                if not already:
                    self.kill_switch.activate(
                        KillSwitchSource.CIRCUIT_BREAKER,
                        "; ".join(reasons)[:200],
                    )
        else:
            self._consecutive_hard_trips = 0

        if status.open:
            log.warning("circuit breaker OPEN: %s", "; ".join(reasons))
        return status
