"""Portfolio accounting: positions, equity, realized P&L. Decimal-exact."""

from __future__ import annotations

import logging
from decimal import Decimal

from trading_bot.core.enums import Mode
from trading_bot.core.models import AssetBalance, OrderResponse, PositionState, SymbolRules
from trading_bot.core.types import ZERO
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
    def __init__(self, repos: Repositories, mode: Mode, rules: SymbolRules) -> None:
        self.repos = repos
        self.mode = mode
        self.rules = rules

    # ------------------------------------------------------------------
    def open_position(self) -> PositionState | None:
        return self.repos.positions.open_position(self.mode)

    def record_entry(self, response: OrderResponse, stop_price: Decimal) -> str | None:
        """Create or update the position from cumulative entry fills."""
        if response.executed_qty <= ZERO:
            return None
        avg = response.avg_fill_price
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
        net_qty = response.executed_qty - base_fee
        entry_fee_quote = quote_fee + base_fee_quote
        existing = self.repos.positions.open_by_entry_order(self.mode, response.client_order_id)
        if existing is not None:
            self.repos.positions.update_open_entry(
                existing.position_id,
                net_qty,
                avg,
                entry_fee_quote,
                stop_price,
            )
            log.info(
                "position updated from entry fills: %s %s @ %s (stop %s, fee %s)",
                net_qty,
                response.symbol,
                avg,
                stop_price,
                entry_fee_quote,
            )
            return existing.position_id
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
        return pid

    def record_exit(self, position: PositionState, response: OrderResponse, reason: str) -> Decimal:
        """Apply an exit fill. Partial exits reduce the open position."""
        exit_qty = response.executed_qty
        if exit_qty <= ZERO:
            return ZERO
        if exit_qty > position.qty:
            raise RuntimeError(
                f"exit fill {exit_qty} exceeds open position {position.qty}; reconciliation required"
            )
        exit_fee_quote = response.total_fees_quote_equiv
        proceeds_net = response.cumulative_quote - exit_fee_quote
        entry_fee_share = position.entry_fee * exit_qty / position.qty
        cost_basis = position.avg_entry_price * exit_qty + entry_fee_share
        realized = proceeds_net - cost_basis
        remaining_qty = position.qty - exit_qty
        remaining_entry_fee = position.entry_fee - entry_fee_share
        self.repos.positions.add_realization(
            position_id=position.position_id,
            mode=self.mode,
            symbol=position.symbol,
            exit_order_id=response.client_order_id,
            qty=exit_qty,
            avg_entry_price=position.avg_entry_price,
            exit_price=response.avg_fill_price,
            entry_fee_allocated=entry_fee_share,
            exit_fee=exit_fee_quote,
            realized_pnl=realized,
            exit_reason=reason,
            ts=response.ts,
        )
        cumulative_pnl, cumulative_exit_fee = self.repos.positions.realized_totals(
            position.position_id
        )
        if remaining_qty == ZERO:
            self.repos.positions.close(
                position_id=position.position_id,
                exit_order_id=response.client_order_id,
                exit_fee=cumulative_exit_fee,
                realized_pnl=cumulative_pnl,
                exit_reason=reason,
            )
        else:
            dust_value = remaining_qty * response.avg_fill_price
            if remaining_qty < self.rules.min_qty or dust_value < self.rules.min_notional:
                self.repos.positions.mark_dust(
                    position.position_id,
                    remaining_qty,
                    remaining_entry_fee,
                    response.client_order_id,
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
        log.info(
            "position exit applied (%s): sold %s @ ~%s, realized pnl %s, remaining %s",
            reason,
            exit_qty,
            response.avg_fill_price,
            realized,
            remaining_qty,
        )
        return realized

    def snapshot(self, balances: dict[str, AssetBalance], price: Decimal) -> Decimal:
        equity = equity_in_quote(balances, self.rules, price)
        self.repos.balances.snapshot(
            self.mode,
            {a: {"free": str(b.free), "locked": str(b.locked)} for a, b in balances.items()},
            equity,
            price,
        )
        return equity
