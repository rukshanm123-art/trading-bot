"""Execution gateway — the ONLY code path that may submit an order.

Enforced invariants:
- No order without a valid single-use approval token from the risk engine.
- Adapter kind must match the configured mode (paper order can never hit a
  real endpoint; live adapter can never be driven from paper mode).
- Intent is persisted BEFORE the network call; the response (or the UNKNOWN
  state) is persisted immediately after. A timeout never triggers a blind
  retry — the order is marked UNKNOWN and blocks new entries until reconciled
  by client-order-id lookup.
- Kill switch blocks entries here as a second, independent layer (the risk
  engine already rejected them once).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trading_bot.core.enums import Mode, OrderState, Side
from trading_bot.core.models import ExecutionResult, OrderRequest, OrderResponse, SizedOrder
from trading_bot.core.types import ZERO
from trading_bot.exchange.errors import (
    ExchangeUnavailable,
    OrderRejectedError,
    OrderStateUnknownError,
)
from trading_bot.exchange.interface import Clock, ExchangeAdapter
from trading_bot.execution.state_machine import assert_transition
from trading_bot.risk.engine import RiskEngine
from trading_bot.storage.audit import AuditLog
from trading_bot.storage.repositories import Repositories

log = logging.getLogger(__name__)


class GatewaySecurityError(RuntimeError):
    """An order tried to bypass risk approval or mode separation."""


@dataclass(frozen=True)
class RecoveredExecution:
    """An exchange response recovered outside the synchronous submit path.

    The caller must pass this to portfolio reconciliation so fill persistence
    and derived accounting commit together.
    """

    order_row: dict[str, Any]
    response: OrderResponse


class ExecutionGateway:
    def __init__(
        self,
        adapter: ExchangeAdapter,
        repos: Repositories,
        risk_engine: RiskEngine,
        mode: Mode,
        audit: AuditLog,
        clock: Clock,
        kill_switch_check: Callable[[], tuple[bool, str]],
    ) -> None:
        self.adapter = adapter
        self.repos = repos
        self.risk = risk_engine
        self.mode = mode
        self.audit = audit
        self.clock = clock
        self._kill_check = kill_switch_check
        self._verify_mode_separation()

    def _verify_mode_separation(self) -> None:
        expected = {
            Mode.PAPER: "paper",
            Mode.TESTNET: "testnet",
            Mode.LIVE: "live",
        }[self.mode]
        if self.adapter.kind != expected:
            raise GatewaySecurityError(
                f"mode {self.mode.value} cannot use adapter kind '{self.adapter.kind}'"
            )

    # ------------------------------------------------------------------
    def submit(
        self, order: SizedOrder, token: str | None, correlation_id: str, purpose: str
    ) -> ExecutionResult:
        # "protective" = the resting exchange-native stop guarding an open
        # position; like exits, it is ALLOWED under an active kill switch.
        if purpose not in ("entry", "exit", "protective"):
            raise GatewaySecurityError(f"unknown order purpose '{purpose}'")

        if token is None or not self.risk.verify_and_consume(order, token):
            self.audit.append(
                "gateway.token_rejected",
                {
                    "client_order_id": order.client_order_id,
                    "purpose": purpose,
                    "correlation_id": correlation_id,
                },
            )
            raise GatewaySecurityError(
                "order lacks a valid risk-approval token; submission refused"
            )

        killed, kill_reason = self._kill_check()
        if killed and purpose == "entry":
            log.warning("kill switch active (%s): entry order refused at gateway", kill_reason)
            self.repos.orders.insert_intent(
                order, self.mode, correlation_id, purpose, state=OrderState.RISK_REJECTED
            )
            return ExecutionResult(
                False, OrderState.RISK_REJECTED, None, error=f"kill_switch:{kill_reason}"
            )

        # 1. Persist intent BEFORE any network activity.
        order_row_id = self.repos.orders.insert_intent(
            order, self.mode, correlation_id, purpose, state=OrderState.RISK_APPROVED
        )
        self.audit.append(
            "gateway.intent",
            {
                "client_order_id": order.client_order_id,
                "purpose": purpose,
                "order": order.as_dict(),
                "correlation_id": correlation_id,
                "mode": self.mode.value,
            },
        )
        assert_transition(OrderState.RISK_APPROVED, OrderState.SUBMITTED)
        self.repos.orders.update_state(order.client_order_id, OrderState.SUBMITTED)

        from trading_bot.core.enums import OrderType

        request = OrderRequest(
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            qty=order.qty,
            price=order.limit_price,
            # stop_price is an exchange parameter only for native stops; for
            # entries it is the software invalidation level and stays local.
            stop_price=order.stop_price if order.order_type == OrderType.STOP_LOSS_LIMIT else None,
            client_order_id=order.client_order_id,
        )

        # 2. Submit exactly once.
        try:
            response = self.adapter.create_order(request)
        except OrderStateUnknownError as exc:
            assert_transition(OrderState.SUBMITTED, OrderState.UNKNOWN)
            self.repos.orders.update_state(order.client_order_id, OrderState.UNKNOWN)
            self.repos.flags.set(self.repos.flags.UNKNOWN_ORDER_BLOCK, "true")
            self.audit.append(
                "gateway.unknown",
                {"client_order_id": order.client_order_id, "error": str(exc)[:200]},
            )
            log.error("order %s state UNKNOWN: %s", order.client_order_id, exc)
            return ExecutionResult(True, OrderState.UNKNOWN, None, error=str(exc)[:200])
        except OrderRejectedError as exc:
            assert_transition(OrderState.SUBMITTED, OrderState.REJECTED)
            self.repos.orders.update_state(order.client_order_id, OrderState.REJECTED)
            self.audit.append(
                "gateway.rejected",
                {"client_order_id": order.client_order_id, "error": str(exc)[:200]},
            )
            return ExecutionResult(True, OrderState.REJECTED, None, error=str(exc)[:200])
        except ExchangeUnavailable as exc:
            # create_order paths raise OrderStateUnknownError for uncertainty;
            # reaching here means the request never left (e.g. paper data gap).
            assert_transition(OrderState.SUBMITTED, OrderState.REJECTED)
            self.repos.orders.update_state(order.client_order_id, OrderState.REJECTED)
            return ExecutionResult(False, OrderState.REJECTED, None, error=str(exc)[:200])

        # 3. Persist outcome immediately.
        assert_transition(OrderState.SUBMITTED, response.state)
        self._record_response(order_row_id, response)
        self.audit.append(
            "gateway.response",
            {
                "client_order_id": order.client_order_id,
                "state": response.state.value,
                "executed_qty": str(response.executed_qty),
                "cumulative_quote": str(response.cumulative_quote),
            },
        )
        return ExecutionResult(True, response.state, response)

    # ------------------------------------------------------------------
    def _record_response(self, order_row_id: str, response: OrderResponse) -> None:
        # Executed responses and their raw fills are deliberately NOT written
        # here. PortfolioService persists the response, fills and derived
        # accounting in one transaction. Until then the non-terminal DB state
        # makes a crash discoverable by reconciliation.
        _ = order_row_id
        if response.executed_qty > ZERO:
            return
        self.repos.orders.update_state(response.client_order_id, response.state, response)

    def await_completion(self, response: OrderResponse, max_queries: int = 5) -> OrderResponse:
        """Poll partially-filled/acknowledged orders to a terminal state."""
        current = response
        row = self.repos.orders.get_by_client_id(current.client_order_id)
        order_row_id = row["id"] if row else ""
        queries = 0
        while (
            current.state in (OrderState.PARTIALLY_FILLED, OrderState.ACKNOWLEDGED)
            and queries < max_queries
        ):
            queries += 1
            updated = self.adapter.query_order(current.symbol, current.client_order_id)
            if updated is None:
                break
            if updated.state != current.state:
                assert_transition(current.state, updated.state)
            if order_row_id:
                self._record_response(order_row_id, updated)
            else:
                self.repos.orders.update_state(current.client_order_id, updated.state, updated)
            current = updated
        return current

    def resolve_unknown_orders(self) -> tuple[int, tuple[RecoveredExecution, ...]]:
        """Query every UNKNOWN order by client id. Clears the entry block when
        all are resolved. Returns unresolved count and responses that require
        atomic portfolio accounting."""
        unresolved = 0
        recovered: list[RecoveredExecution] = []
        for row in self.repos.orders.unknown_orders(self.mode):
            client_id = row["client_order_id"]
            try:
                found = self.adapter.query_order(row["symbol"], client_id)
            except ExchangeUnavailable:
                unresolved += 1
                continue
            if found is None:
                # Never reached the exchange: safe to mark rejected.
                self.repos.orders.update_state(client_id, OrderState.REJECTED, note="not found")
                self.audit.append("reconcile.order_not_found", {"client_order_id": client_id})
            else:
                assert_transition(OrderState(row["state"]), found.state)
                self._record_response(row["id"], found)
                if found.executed_qty > 0:
                    recovered.append(RecoveredExecution(row, found))
                self.audit.append(
                    "reconcile.order_resolved",
                    {"client_order_id": client_id, "state": found.state.value},
                )
                if found.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
                    unresolved += 1
        if unresolved == 0 and not recovered:
            self.repos.flags.set(self.repos.flags.UNKNOWN_ORDER_BLOCK, "false")
        return unresolved, tuple(recovered)

    def cancel_stale_entry_orders(
        self, max_age_s: int
    ) -> tuple[int, tuple[RecoveredExecution, ...]]:
        """Cancel stale entries and return executions requiring accounting."""
        cancelled = 0
        recovered: list[RecoveredExecution] = []
        now = self.clock.now()
        for row in self.repos.orders.non_terminal_orders(self.mode):
            if row["purpose"] != "entry" or row["side"] != Side.BUY.value:
                continue
            if row["state"] not in (
                OrderState.ACKNOWLEDGED.value,
                OrderState.PARTIALLY_FILLED.value,
            ):
                continue
            from trading_bot.core.models import parse_iso

            age = (now - parse_iso(row["created_at"])).total_seconds()
            if age > max_age_s:
                try:
                    latest = self.adapter.query_order(row["symbol"], row["client_order_id"])
                except ExchangeUnavailable as exc:
                    log.warning(
                        "query before stale cancel of %s failed: %s", row["client_order_id"], exc
                    )
                    continue
                if latest is not None:
                    self._record_response(row["id"], latest)
                    if latest.state in (
                        OrderState.FILLED,
                        OrderState.CANCELLED,
                        OrderState.REJECTED,
                    ):
                        if latest.executed_qty > 0:
                            recovered.append(RecoveredExecution(row, latest))
                        continue
                try:
                    resp = self.adapter.cancel_order(row["symbol"], row["client_order_id"])
                except (OrderRejectedError, ExchangeUnavailable) as exc:
                    log.warning("cancel of %s failed: %s", row["client_order_id"], exc)
                    continue
                self._record_response(row["id"], resp)
                if resp.executed_qty > 0:
                    recovered.append(RecoveredExecution(row, resp))
                self.audit.append(
                    "gateway.stale_cancel", {"client_order_id": row["client_order_id"]}
                )
                cancelled += 1
        return cancelled, tuple(recovered)
