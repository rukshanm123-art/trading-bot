"""Reconciliation: DB truth vs exchange truth, at startup and periodically.

On mismatch beyond tolerance: block new entries (RECONCILIATION_BLOCK flag),
alert, and require the discrepancy to clear or be manually acknowledged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from trading_bot.config.models import AppConfig
from trading_bot.core.enums import OrderState
from trading_bot.core.models import SymbolRules, parse_iso
from trading_bot.core.types import ZERO, dec
from trading_bot.exchange.errors import ExchangeUnavailable
from trading_bot.exchange.interface import Clock, ExchangeAdapter
from trading_bot.execution.gateway import ExecutionGateway
from trading_bot.portfolio.accounting import PortfolioService
from trading_bot.storage.repositories import Repositories

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileResult:
    ok: bool
    details: dict[str, Any]


class ReconciliationFailure(RuntimeError):
    """Typed fail-closed reconciliation exception."""


class Reconciler:
    def __init__(
        self,
        adapter: ExchangeAdapter,
        repos: Repositories,
        cfg: AppConfig,
        rules: SymbolRules,
        gateway: ExecutionGateway,
        clock: Clock,
        portfolio: PortfolioService | None = None,
    ) -> None:
        self.adapter = adapter
        self.repos = repos
        self.cfg = cfg
        self.rules = rules
        self.gateway = gateway
        self.clock = clock
        self.portfolio = portfolio

    def run(self) -> ReconcileResult:
        details: dict[str, Any] = {}
        problems: list[str] = []

        # 1. Resolve any UNKNOWN orders first (by client order id).
        try:
            unresolved = self.gateway.resolve_unknown_orders()
        except ExchangeUnavailable as exc:
            problems.append(f"cannot resolve unknown orders: {exc}")
            unresolved = -1
        details["unresolved_unknown_orders"] = unresolved
        if unresolved != 0:
            problems.append("unknown orders pending")

        # 2. Stuck non-terminal orders older than max_order_age_s.
        #    An intent that never reached the exchange (RISK_APPROVED/SUBMITTED
        #    persisted, then crash/restart) is ABANDONED: approval tokens are
        #    process-scoped, so the safe behaviour is to invalidate the intent
        #    and let the strategy re-evaluate — never to retry the old order.
        now = self.clock.now()
        stuck = 0
        abandoned = 0
        for row in self.repos.orders.non_terminal_orders(self.cfg.mode):
            age = (now - parse_iso(row["created_at"])).total_seconds()
            if age > self.cfg.execution.max_order_age_s:
                try:
                    found = self.adapter.query_order(row["symbol"], row["client_order_id"])
                except ExchangeUnavailable:
                    stuck += 1
                    continue
                if found is not None:
                    self.repos.orders.update_state(row["client_order_id"], found.state, found)
                    if found.fills:
                        self.repos.orders.add_fills(row["id"], row["client_order_id"], found.fills)
                    self._apply_order_response(row, found)
                    if found.state.value in ("ACKNOWLEDGED", "PARTIALLY_FILLED"):
                        stuck += 1
                else:
                    self.repos.orders.update_state(
                        row["client_order_id"],
                        OrderState.REJECTED,
                        note="abandoned intent: not found on exchange after max age",
                    )
                    abandoned += 1
                    log.warning(
                        "abandoned stale order intent %s (state was %s)",
                        row["client_order_id"],
                        row["state"],
                    )
        details["stuck_orders"] = stuck
        details["abandoned_intents"] = abandoned
        if stuck:
            problems.append(f"{stuck} order(s) exceeded max age without terminal state")

        # 3. Balance vs position consistency.
        try:
            balances = self.adapter.get_balances()
            price = self.adapter.get_price(self.cfg.symbol).mid
        except ExchangeUnavailable as exc:
            problems.append(f"cannot fetch balances/price: {exc}")
            result = ReconcileResult(False, {**details, "problems": problems})
            self._record(result)
            return result

        base_total = ZERO
        if self.rules.base_asset in balances:
            base_total = balances[self.rules.base_asset].total
        position = self.repos.positions.open_position(self.cfg.mode)
        tolerance_qty = self.rules.step_size * 2
        tolerance_quote = self.cfg.risk.max_reconciliation_mismatch_quote

        if position is not None:
            deficit = position.qty - base_total
            details["position_qty"] = str(position.qty)
            details["base_balance"] = str(base_total)
            if deficit > tolerance_qty and deficit * price > tolerance_quote:
                problems.append(
                    f"exchange holds {base_total} {self.rules.base_asset} but DB position "
                    f"is {position.qty} — unexplained deficit"
                )
        else:
            # Exit orders floor quantities to the exchange step size, so a
            # residue below minNotional is EXPECTED, unsellable dust — not a
            # discrepancy. Anything at or above minNotional could have been
            # sold, so holdings that large without a recorded position are
            # genuinely unexplained.
            surplus_value = base_total * price
            dust_threshold = max(self.rules.min_notional, tolerance_quote)
            details["base_balance"] = str(base_total)
            details["dust_threshold_quote"] = str(dust_threshold)
            if surplus_value >= dust_threshold:
                problems.append(
                    f"no open position but exchange holds {base_total} "
                    f"{self.rules.base_asset} (~{surplus_value} {self.rules.quote_asset})"
                )

        result = ReconcileResult(ok=not problems, details={**details, "problems": problems})
        self._record(result)
        return result

    def _record(self, result: ReconcileResult) -> None:
        self.repos.events.reconciliation(result.ok, result.details)
        flag = self.repos.flags.RECONCILIATION_BLOCK
        if result.ok:
            self.repos.flags.set(flag, "false")
        else:
            self.repos.flags.set(flag, "true")
            log.error("reconciliation mismatch: %s", result.details.get("problems"))

    def fail_closed(self, exc: BaseException) -> ReconcileResult:
        result = ReconcileResult(
            False,
            {
                "exception_type": type(exc).__name__,
                "exception": str(exc)[:300],
                "problems": ["reconciliation_exception"],
            },
        )
        self._record(result)
        return result

    def _apply_order_response(self, row: dict[str, Any], response) -> None:
        if self.portfolio is None or response.executed_qty <= ZERO:
            return
        if row["purpose"] == "entry":
            self.portfolio.record_entry(response, dec(row["stop_price"]))
        elif row["purpose"] == "exit":
            position = self.repos.positions.open_position(self.cfg.mode)
            if position is not None:
                self.portfolio.record_exit(position, response, "reconciliation")
