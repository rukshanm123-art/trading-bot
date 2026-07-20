"""Risk state: everything the risk engine needs to know about history.

Derived from the database (positions, balances, orders, flags) rather than
held in memory, so limits survive restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_bot.config.models import AppConfig
from trading_bot.core.models import parse_iso
from trading_bot.core.types import HUNDRED, ZERO
from trading_bot.exchange.interface import Clock
from trading_bot.storage.repositories import Repositories


@dataclass(frozen=True)
class RiskStateSnapshot:
    day: str
    start_of_day_equity: Decimal
    realized_pnl_today: Decimal
    entries_today: int
    pnl_7d_pct: Decimal
    drawdown_pct: Decimal
    peak_equity: Decimal
    consecutive_losses: int
    cooldown_until: datetime | None
    unknown_orders: int
    active_entry_orders: int
    active_exit_orders: int
    reconciliation_blocked: bool
    api_errors_last_hour: int

    def as_inputs(self) -> dict[str, str]:
        return {
            "day": self.day,
            "start_of_day_equity": str(self.start_of_day_equity),
            "realized_pnl_today": str(self.realized_pnl_today),
            "entries_today": str(self.entries_today),
            "pnl_7d_pct": str(self.pnl_7d_pct),
            "drawdown_pct": str(self.drawdown_pct),
            "peak_equity": str(self.peak_equity),
            "consecutive_losses": str(self.consecutive_losses),
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else "",
            "unknown_orders": str(self.unknown_orders),
            "active_entry_orders": str(self.active_entry_orders),
            "active_exit_orders": str(self.active_exit_orders),
            "reconciliation_blocked": str(self.reconciliation_blocked),
            "api_errors_last_hour": str(self.api_errors_last_hour),
        }


class RiskStateService:
    def __init__(self, repos: Repositories, cfg: AppConfig, clock: Clock) -> None:
        self.repos = repos
        self.cfg = cfg
        self.clock = clock
        self.mode = cfg.mode

    def utc_day(self, now: datetime | None = None) -> str:
        return (now or self.clock.now()).strftime("%Y-%m-%d")

    def snapshot(self, equity_now: Decimal) -> RiskStateSnapshot:
        now = self.clock.now()
        day = self.utc_day(now)

        start_equity = self.repos.daily_equity.ensure_day_start(day, self.mode, equity_now)

        day_start = datetime.fromisoformat(f"{day}T00:00:00+00:00")
        realized_today = self.repos.positions.realized_pnl_between(
            self.mode, day_start, day_start + timedelta(days=1)
        )
        entries_today = self.repos.orders.entries_on_day(self.mode, day)

        week_ago = now - timedelta(days=7)
        equity_7d = self.repos.balances.equity_at_or_before(self.mode, week_ago)
        if equity_7d is None:
            first = self.repos.balances.first(self.mode)
            equity_7d = Decimal(str(first["equity"])) if first else equity_now
        pnl_7d_pct = (equity_now - equity_7d) / equity_7d * HUNDRED if equity_7d > ZERO else ZERO

        peak = self.repos.balances.peak_equity(self.mode) or equity_now
        peak = max(peak, equity_now)
        drawdown_pct = (peak - equity_now) / peak * HUNDRED if peak > ZERO else ZERO

        consecutive = self.repos.positions.consecutive_losses(self.mode)
        cooldown_until: datetime | None = None
        last_loss = self.repos.positions.last_losing_close_time(self.mode)
        if last_loss is not None and consecutive > 0:
            cooldown_until = last_loss + timedelta(hours=self.cfg.risk.cooldown_after_loss_hours)
            if cooldown_until <= now:
                cooldown_until = None

        unknown = len(self.repos.orders.unknown_orders(self.mode))
        active_entries = len(self.repos.orders.active_entry_orders(self.mode))
        active_exits = len(self.repos.orders.active_exit_orders(self.mode))
        recon_blocked = self.repos.flags.is_true(self.repos.flags.RECONCILIATION_BLOCK)
        api_errors = self.repos.events.api_errors_last_hour(now)

        return RiskStateSnapshot(
            day=day,
            start_of_day_equity=start_equity,
            realized_pnl_today=realized_today,
            entries_today=entries_today,
            pnl_7d_pct=pnl_7d_pct,
            drawdown_pct=drawdown_pct,
            peak_equity=peak,
            consecutive_losses=consecutive,
            cooldown_until=cooldown_until,
            unknown_orders=unknown,
            active_entry_orders=active_entries,
            active_exit_orders=active_exits,
            reconciliation_blocked=recon_blocked,
            api_errors_last_hour=api_errors,
        )

    def approval_valid(self, now: datetime | None = None) -> bool:
        raw = self.repos.flags.get(self.repos.flags.APPROVAL_UNTIL)
        if not raw:
            return False
        try:
            return parse_iso(raw) > (now or self.clock.now())
        except ValueError:
            return False
