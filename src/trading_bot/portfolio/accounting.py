"""Portfolio accounting: positions, equity, realized P&L. Decimal-exact.

Idempotency contract: exchange responses report CUMULATIVE executed
quantity/quote/fees for an order. Every booking below therefore applies only
the DELTA between the response and the cumulative amounts already accounted
for that order (persisted on the orders row), so restarts, reconciliation and
repeated polling can process the same response any number of times without
double-counting. A DB uniqueness constraint on realizations is the final
backstop.

When a response carries no fill details (Binance order queries may omit
them), fees are ASSUMED at the configured taker rate — worst-case for the
sellable quantity, so the error side is always safe.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from trading_bot.core.enums import TERMINAL_ORDER_STATES, Mode
from trading_bot.core.models import AssetBalance, OrderResponse, PositionState, SymbolRules
from trading_bot.core.types import BPS_DENOM, ZERO
from trading_bot.storage.repositories import Repositories

log = logging.getLogger(__name__)


def equity_in_quote(
    balances: dict[str, AssetBalance], rules: SymbolRules, price: Decimal
) -> Decimal:
    total = ZERO
    for asset, bal in balances.items():
        if asset == rules.quote_asset:
            total += bal.total
        elif asset == rules.base_asset:
            total += bal.total * price
    return total


class PortfolioService:
    def __init__(
        self,
        repos: Repositories,
        mode: Mode,
        rules: SymbolRules,
        fee_bps: Decimal = Decimal("10"),
    ) -> None:
        self.repos = repos
        self.mode = mode
        self.rules = rules
        self.fee_bps = fee_bps

    # ------------------------------------------------------------------
    def open_position(self) -> PositionState | None:
        return self.repos.positions.open_position(self.mode)

    def _persist_response(self, response: OrderResponse) -> None:
        """Persist the response and raw fills inside the accounting transaction.

        Production and testnet responses must always have a persist-before-submit
        order intent. Direct PAPER accounting tests may omit that intent; their
        client order id is used as the non-FK fill grouping id in that case.
        """
        row = self.repos.orders.get_by_client_id(response.client_order_id)
        if row is None and self.mode != Mode.PAPER:
            raise RuntimeError(
                f"order intent {response.client_order_id} missing before fill accounting; "
                "reconciliation required"
            )
        if row is not None:
            self.repos.orders.update_state(response.client_order_id, response.state, response)
        if not response.fills:
            return
        order_id = row["id"] if row is not None else response.client_order_id
        self.repos.orders.add_fills(order_id, response.client_order_id, response.fills)

    def _warn_third_asset_fees(self, response: OrderResponse) -> None:
        third = response.third_asset_fees
        if third:
            log.warning(
                "order %s charged fees in third asset(s) %s — these are NOT "
                "valued in quote P&L. Disable BNB-fee payment for this account "
                "(docs/API_KEY_SETUP.md).",
                response.client_order_id,
                {k: str(v) for k, v in third.items()},
            )

    def _cumulative_entry_view(self, response: OrderResponse) -> tuple[Decimal, Decimal]:
        """(net_base_qty_cum, entry_fee_quote_cum) for a BUY response."""
        self._warn_third_asset_fees(response)
        if response.fills:
            base_fee = sum(
                (f.fee for f in response.fills if f.fee_asset == self.rules.base_asset), ZERO
            )
            base_fee_quote = sum(
                (f.fee * f.price for f in response.fills if f.fee_asset == self.rules.base_asset),
                ZERO,
            )
            quote_fee = sum(
                (f.fee for f in response.fills if f.fee_asset == self.rules.quote_asset), ZERO
            )
            return response.executed_qty - base_fee, quote_fee + base_fee_quote
        # No fill details: assume the whole commission was charged in base at
        # the configured taker rate (worst case for sellable quantity).
        assumed_base_fee = response.executed_qty * self.fee_bps / BPS_DENOM
        avg = response.avg_fill_price
        log.warning(
            "entry response %s carries no fills; assuming worst-case base fee %s",
            response.client_order_id,
            assumed_base_fee,
        )
        return response.executed_qty - assumed_base_fee, assumed_base_fee * avg

    def record_entry(self, response: OrderResponse, stop_price: Decimal) -> str | None:
        """Atomically persist all accounting effects of cumulative entry fills."""
        with self.repos.db.transaction():
            return self._record_entry(response, stop_price)

    def _record_entry(self, response: OrderResponse, stop_price: Decimal) -> str | None:
        """Create or update the position from CUMULATIVE entry fills.
        Idempotent: reprocessing a response with no new quantity is a no-op."""
        if response.executed_qty <= ZERO:
            return None
        self._persist_response(response)
        existing = self.repos.positions.open_by_entry_order(self.mode, response.client_order_id)
        acc_qty, _, _ = self.repos.orders.accounted_totals(response.client_order_id)
        if response.executed_qty <= acc_qty:
            return existing.position_id if existing else None

        avg = response.avg_fill_price
        net_qty, entry_fee_quote = self._cumulative_entry_view(response)
        if net_qty <= ZERO:
            return existing.position_id if existing else None

        if existing is not None:
            self.repos.positions.update_open_entry(
                existing.position_id, net_qty, avg, entry_fee_quote, stop_price
            )
            pid = existing.position_id
            log.info(
                "position updated from entry fills: %s %s @ %s (stop %s, fee %s)",
                net_qty,
                response.symbol,
                avg,
                stop_price,
                entry_fee_quote,
            )
        else:
            pid = self.repos.positions.insert_open(
                mode=self.mode,
                symbol=response.symbol,
                qty=net_qty,
                avg_entry_price=avg,
                stop_price=stop_price,
                entry_order_id=response.client_order_id,
                entry_fee=entry_fee_quote,
            )
            log.info(
                "position opened: %s %s @ %s (stop %s, fee %s)",
                net_qty,
                response.symbol,
                avg,
                stop_price,
                entry_fee_quote,
            )
        self.repos.orders.set_accounted_totals(
            response.client_order_id,
            response.executed_qty,
            response.cumulative_quote,
            entry_fee_quote,
        )
        return pid

    def record_exit(self, position: PositionState, response: OrderResponse, reason: str) -> Decimal:
        """Atomically persist all accounting effects of cumulative exit fills."""
        with self.repos.db.transaction():
            return self._record_exit(position, response, reason)

    def _record_exit(
        self, position: PositionState, response: OrderResponse, reason: str
    ) -> Decimal:
        """Apply an exit response, booking ONLY the not-yet-accounted delta.
        Returns the realized P&L of that delta (ZERO when nothing new)."""
        executed = response.executed_qty
        if executed <= ZERO:
            return ZERO
        self._persist_response(response)
        coid = response.client_order_id
        acc_qty, acc_quote, acc_fee = self.repos.orders.accounted_totals(coid)
        delta_qty = executed - acc_qty
        if delta_qty <= ZERO:
            log.info("exit response %s already fully accounted (cum %s)", coid, executed)
            return ZERO
        if delta_qty > position.qty:
            raise RuntimeError(
                f"exit delta {delta_qty} exceeds open position {position.qty}; "
                "reconciliation required"
            )

        self._warn_third_asset_fees(response)
        if response.fills:
            cum_fee_quote = response.total_fees_quote_equiv
        else:
            cum_fee_quote = response.cumulative_quote * self.fee_bps / BPS_DENOM
            log.warning(
                "exit response %s carries no fills; assuming taker fee %s",
                coid,
                cum_fee_quote,
            )
        delta_quote = response.cumulative_quote - acc_quote
        delta_fee = cum_fee_quote - acc_fee
        if delta_quote < ZERO:
            raise RuntimeError(f"cumulative quote regressed for {coid}; reconciliation required")
        if delta_fee < ZERO:
            delta_fee = ZERO
        delta_price = delta_quote / delta_qty

        proceeds_net = delta_quote - delta_fee
        entry_fee_share = position.entry_fee * delta_qty / position.qty
        cost_basis = position.avg_entry_price * delta_qty + entry_fee_share
        realized = proceeds_net - cost_basis
        inserted = self.repos.positions.add_realization(
            position_id=position.position_id,
            mode=self.mode,
            symbol=position.symbol,
            exit_order_id=coid,
            qty=delta_qty,
            avg_entry_price=position.avg_entry_price,
            exit_price=delta_price,
            entry_fee_allocated=entry_fee_share,
            exit_fee=delta_fee,
            realized_pnl=realized,
            exit_reason=reason,
            ts=response.ts,
            cum_qty_after=executed,
        )
        if not inserted:
            log.warning("realization for %s at cum %s already recorded; skipping", coid, executed)
            return ZERO
        self.repos.orders.set_accounted_totals(
            coid, executed, response.cumulative_quote, cum_fee_quote
        )

        remaining_qty = position.qty - delta_qty
        remaining_entry_fee = position.entry_fee - entry_fee_share
        cumulative_pnl, cumulative_exit_fee = self.repos.positions.realized_totals(
            position.position_id
        )
        if remaining_qty == ZERO:
            self.repos.positions.close(
                position_id=position.position_id,
                exit_order_id=coid,
                exit_fee=cumulative_exit_fee,
                realized_pnl=cumulative_pnl,
                exit_reason=reason,
                ts=response.ts,
            )
        else:
            dust_value = remaining_qty * delta_price
            if remaining_qty < self.rules.min_qty or dust_value < self.rules.min_notional:
                self.repos.positions.mark_dust(
                    position.position_id,
                    remaining_qty,
                    remaining_entry_fee,
                    coid,
                    cumulative_exit_fee,
                    cumulative_pnl,
                    f"{reason}:dust_below_exchange_minimum",
                    response.ts,
                )
            else:
                self.repos.positions.update_open_after_exit(
                    position.position_id,
                    remaining_qty,
                    remaining_entry_fee,
                    cumulative_exit_fee,
                    cumulative_pnl,
                )
            if response.state in TERMINAL_ORDER_STATES and position.protective_order_id == coid:
                self.repos.positions.set_protective_order(position.position_id, None)
        log.info(
            "position exit applied (%s): sold %s @ ~%s, realized pnl %s, remaining %s",
            reason,
            delta_qty,
            delta_price,
            realized,
            remaining_qty,
        )
        return realized

    # ------------------------------------------------------------------
    def record_dust_sweep(self, dust_rows: list[dict], response: OrderResponse) -> Decimal:
        """Atomically persist all accounting effects of a cumulative dust sweep."""
        with self.repos.db.transaction():
            return self._record_dust_sweep(dust_rows, response)

    def _record_dust_sweep(self, dust_rows: list[dict], response: OrderResponse) -> Decimal:
        """Book only the newly filled part of an aggregate dust-sweep sell.

        Dust retains its original cost basis until sold.  A partial sweep must
        therefore reduce residues cumulatively rather than closing every row.
        """
        from trading_bot.core.types import dec

        executed = response.executed_qty
        if executed <= ZERO or not dust_rows:
            return ZERO
        self._persist_response(response)
        coid = response.client_order_id
        acc_qty, acc_quote, acc_fee = self.repos.orders.accounted_totals(coid)
        if executed <= acc_qty:
            return ZERO
        delta_qty = executed - acc_qty
        available = sum((dec(r["qty"]) for r in dust_rows), ZERO)
        if delta_qty > available:
            raise RuntimeError(
                f"dust sweep delta {delta_qty} exceeds tracked residue {available}; "
                "reconciliation required"
            )
        self._warn_third_asset_fees(response)
        cum_fee_quote = (
            response.total_fees_quote_equiv
            if response.fills
            else response.cumulative_quote * self.fee_bps / BPS_DENOM
        )
        delta_quote = response.cumulative_quote - acc_quote
        delta_fee = cum_fee_quote - acc_fee
        if delta_quote < ZERO or delta_fee < ZERO:
            raise RuntimeError(f"dust sweep cumulative totals regressed for {coid}")
        delta_price = delta_quote / delta_qty
        proceeds_net = delta_quote - delta_fee
        remaining = delta_qty
        consumed = ZERO
        for row in dust_rows:
            if remaining <= ZERO:
                break
            row_qty = dec(row["qty"])
            sold_qty = min(row_qty, remaining)
            share = sold_qty / delta_qty
            entry_fee = dec(row["entry_fee"])
            entry_fee_allocated = entry_fee * sold_qty / row_qty
            exit_fee = delta_fee * share
            realized = (
                sold_qty * delta_price
                - exit_fee
                - (sold_qty * dec(row["avg_entry_price"]) + entry_fee_allocated)
            )
            consumed += sold_qty
            inserted = self.repos.positions.add_realization(
                position_id=row["id"],
                mode=self.mode,
                symbol=row["symbol"],
                exit_order_id=coid,
                qty=sold_qty,
                avg_entry_price=dec(row["avg_entry_price"]),
                exit_price=delta_price,
                entry_fee_allocated=entry_fee_allocated,
                exit_fee=exit_fee,
                realized_pnl=realized,
                exit_reason="dust_sweep",
                ts=response.ts,
                cum_qty_after=acc_qty + consumed,
            )
            if not inserted:
                raise RuntimeError(
                    f"duplicate dust realization for {coid}; reconciliation required"
                )
            self.repos.positions.apply_dust_sale(
                row["id"],
                sold_qty=sold_qty,
                remaining_entry_fee=entry_fee - entry_fee_allocated,
                add_realized=realized,
                add_exit_fee=exit_fee,
                exit_order_id=coid,
                ts=response.ts,
            )
            remaining -= sold_qty
        if remaining != ZERO:
            raise RuntimeError(f"unallocated dust sweep quantity {remaining} for {coid}")
        self.repos.orders.set_accounted_totals(
            coid, executed, response.cumulative_quote, cum_fee_quote
        )
        log.info(
            "dust sweep: booked delta %s for net %s across %s residues",
            delta_qty,
            proceeds_net,
            len(dust_rows),
        )
        return proceeds_net

    def snapshot(self, balances: dict[str, AssetBalance], price: Decimal) -> Decimal:
        equity = equity_in_quote(balances, self.rules, price)
        self.repos.balances.snapshot(
            self.mode,
            {a: {"free": str(b.free), "locked": str(b.locked)} for a, b in balances.items()},
            equity,
            price,
        )
        return equity
