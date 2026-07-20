"""Typed repositories over the Database. All writes go through these classes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from trading_bot.core.enums import TERMINAL_ORDER_STATES, Mode, OrderState
from trading_bot.core.models import (
    DecisionRecord,
    Fill,
    OrderResponse,
    PositionState,
    SizedOrder,
    iso,
    parse_iso,
    utcnow,
)
from trading_bot.core.types import ZERO, dec, json_dumps
from trading_bot.storage.db import Database, uid


class FlagsRepo:
    """control_flags: kill switch, pause, approval window, blocking conditions."""

    KILL_SWITCH = "kill_switch"
    PAUSED = "paused"
    APPROVAL_UNTIL = "approval_until"
    UNKNOWN_ORDER_BLOCK = "unknown_order_block"
    RECONCILIATION_BLOCK = "reconciliation_block"

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str) -> str | None:
        row = self.db.query_one("SELECT value FROM control_flags WHERE key = ?", (key,))
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        now = iso(utcnow())
        if self.db.query_one("SELECT key FROM control_flags WHERE key = ?", (key,)):
            self.db.execute(
                "UPDATE control_flags SET value = ?, updated_at = ? WHERE key = ?",
                (value, now, key),
            )
        else:
            self.db.execute(
                "INSERT INTO control_flags (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM control_flags WHERE key = ?", (key,))

    def is_true(self, key: str) -> bool:
        return (self.get(key) or "").lower() in ("true", "1", "yes")


class OrdersRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert_intent(
        self,
        sized: SizedOrder,
        mode: Mode,
        correlation_id: str,
        purpose: str,
        state: OrderState = OrderState.RISK_APPROVED,
    ) -> str:
        """Persist intent BEFORE any external submission. Returns internal order id."""
        order_id = uid()
        now = iso(utcnow())
        self.db.execute(
            "INSERT INTO orders (id, client_order_id, exchange_order_id, correlation_id, mode, "
            "symbol, side, order_type, qty, limit_price, stop_price, state, purpose, "
            "intent_json, response_json, created_at, updated_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                order_id,
                sized.client_order_id,
                correlation_id,
                mode.value,
                sized.symbol,
                sized.side.value,
                sized.order_type.value,
                str(sized.qty),
                str(sized.limit_price) if sized.limit_price is not None else None,
                str(sized.stop_price),
                state.value,
                purpose,
                json_dumps(sized.as_dict()),
                now,
                now,
            ),
        )
        return order_id

    def update_state(
        self,
        client_order_id: str,
        state: OrderState,
        response: OrderResponse | None = None,
        note: str = "",
    ) -> None:
        response_json: str | None = None
        exchange_order_id: str | None = None
        if response is not None:
            exchange_order_id = response.exchange_order_id
            response_json = json_dumps(
                {
                    "state": response.state.value,
                    "raw_status": response.raw_status,
                    "executed_qty": str(response.executed_qty),
                    "cumulative_quote": str(response.cumulative_quote),
                    "ts": iso(response.ts),
                    "note": note,
                }
            )
        self.db.execute(
            "UPDATE orders SET state = ?, "
            "response_json = COALESCE(?, response_json), "
            "exchange_order_id = COALESCE(?, exchange_order_id), "
            "updated_at = ? WHERE client_order_id = ?",
            (state.value, response_json, exchange_order_id, iso(utcnow()), client_order_id),
        )

    def add_fills(self, order_id: str, client_order_id: str, fills: tuple[Fill, ...]) -> None:
        """Idempotent fill persistence. Fills with a stable exchange trade id
        dedupe on (client_order_id, trade_id); anonymous fills fall back to
        count-prefix dedupe (exchange responses report cumulative lists)."""
        known_ids = {
            r["trade_id"]
            for r in self.db.query(
                "SELECT trade_id FROM fills WHERE client_order_id = ? AND trade_id IS NOT NULL",
                (client_order_id,),
            )
            if r["trade_id"]
        }
        existing = self.db.query_one(
            "SELECT COUNT(*) AS n FROM fills WHERE client_order_id = ? "
            "AND (trade_id IS NULL OR trade_id = '')",
            (client_order_id,),
        )
        anon_seen = int(existing["n"]) if existing else 0
        anon_index = 0
        for f in fills:
            if f.trade_id:
                if f.trade_id in known_ids:
                    continue
            else:
                anon_index += 1
                if anon_index <= anon_seen:
                    continue
            self.db.execute(
                "INSERT INTO fills (id, order_id, client_order_id, price, qty, fee, fee_asset, "
                "trade_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uid(),
                    order_id,
                    client_order_id,
                    str(f.price),
                    str(f.qty),
                    str(f.fee),
                    f.fee_asset,
                    f.trade_id,
                    iso(utcnow()),
                ),
            )

    def get_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        )

    def non_terminal_orders(self, mode: Mode) -> list[dict[str, Any]]:
        terminal = tuple(s.value for s in TERMINAL_ORDER_STATES)
        placeholders = ",".join("?" for _ in terminal)
        return self.db.query(
            f"SELECT * FROM orders WHERE mode = ? AND state NOT IN ({placeholders}) "  # noqa: S608 # nosec B608 - placeholders are generated ?s, values bound
            "ORDER BY created_at",
            (mode.value, *terminal),
        )

    def unknown_orders(self, mode: Mode) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM orders WHERE mode = ? AND state IN (?, ?) ORDER BY created_at",
            (mode.value, OrderState.UNKNOWN.value, OrderState.RECONCILIATION_REQUIRED.value),
        )

    def active_entry_orders(self, mode: Mode) -> list[dict[str, Any]]:
        terminal = tuple(s.value for s in TERMINAL_ORDER_STATES)
        placeholders = ",".join("?" for _ in terminal)
        return self.db.query(
            f"SELECT * FROM orders WHERE mode = ? AND purpose = 'entry' "  # noqa: S608 # nosec B608 - generated placeholders only
            f"AND state NOT IN ({placeholders}) ORDER BY created_at",
            (mode.value, *terminal),
        )

    def active_exit_orders(self, mode: Mode) -> list[dict[str, Any]]:
        terminal = tuple(s.value for s in TERMINAL_ORDER_STATES)
        placeholders = ",".join("?" for _ in terminal)
        return self.db.query(
            f"SELECT * FROM orders WHERE mode = ? AND purpose = 'exit' "  # noqa: S608 # nosec B608 - generated placeholders only
            f"AND state NOT IN ({placeholders}) ORDER BY created_at",
            (mode.value, *terminal),
        )

    def accounted_totals(self, client_order_id: str) -> tuple[Decimal, Decimal, Decimal]:
        """Cumulative (qty, quote, fee_quote) already booked into positions."""
        row = self.db.query_one(
            "SELECT qty, quote, fee_quote FROM order_accounting WHERE client_order_id = ?",
            (client_order_id,),
        )
        if not row:
            return ZERO, ZERO, ZERO
        return dec(row["qty"]), dec(row["quote"]), dec(row["fee_quote"])

    def set_accounted_totals(
        self, client_order_id: str, qty: Decimal, quote: Decimal, fee_quote: Decimal
    ) -> None:
        self.db.execute(
            "INSERT INTO order_accounting (client_order_id, qty, quote, fee_quote, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(client_order_id) DO UPDATE SET "
            "qty = excluded.qty, quote = excluded.quote, fee_quote = excluded.fee_quote, "
            "updated_at = excluded.updated_at",
            (client_order_id, str(qty), str(quote), str(fee_quote), iso(utcnow())),
        )

    def entries_on_day(self, mode: Mode, day: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM orders WHERE mode = ? AND purpose = 'entry' "
            "AND side = 'BUY' AND created_at >= ? AND created_at < ? "
            "AND state NOT IN (?, ?)",
            (
                mode.value,
                f"{day}T00:00:00+00:00",
                _next_day(day),
                OrderState.RISK_REJECTED.value,
                OrderState.REJECTED.value,
            ),
        )
        return int(row["n"]) if row else 0


def _next_day(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    return d.strftime("%Y-%m-%dT00:00:00+00:00")


class DecisionsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(self, rec: DecisionRecord) -> None:
        sig = rec.signal
        risk = rec.risk
        execution = rec.execution
        self.db.execute(
            "INSERT INTO decisions (id, correlation_id, ts, mode, strategy, strategy_version, "
            "config_hash, symbol, market_data_ts, candle_open_time, signal_action, signal_reason, "
            "indicators_json, proposed_order_json, risk_approved, risk_codes_json, "
            "risk_inputs_json, execution_state, execution_json, explanation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.decision_id,
                rec.correlation_id,
                iso(rec.ts),
                rec.mode.value,
                rec.strategy,
                rec.strategy_version,
                rec.config_hash,
                rec.symbol,
                iso(rec.market_data_ts) if rec.market_data_ts else None,
                iso(sig.candle_open_time) if sig else None,
                sig.action.value if sig else None,
                sig.reason if sig else None,
                json_dumps(sig.indicators) if sig else None,
                json_dumps(risk.order.as_dict()) if risk and risk.order else None,
                (1 if risk.approved else 0) if risk else None,
                json_dumps([c.value for c in risk.codes]) if risk else None,
                json_dumps(risk.inputs) if risk else None,
                execution.state.value if execution else None,
                json_dumps({"submitted": execution.submitted, "error": execution.error})
                if execution
                else None,
                rec.explanation,
            ),
        )

    def count(self, mode: Mode) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM decisions WHERE mode = ?", (mode.value,))
        return int(row["n"]) if row else 0

    def first_ts(self, mode: Mode) -> datetime | None:
        row = self.db.query_one("SELECT MIN(ts) AS t FROM decisions WHERE mode = ?", (mode.value,))
        return parse_iso(row["t"]) if row and row["t"] else None

    def last_decision(self, mode: Mode) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM decisions WHERE mode = ? ORDER BY ts DESC LIMIT 1", (mode.value,)
        )

    def on_day(self, mode: Mode, day: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM decisions WHERE mode = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (mode.value, f"{day}T00:00:00+00:00", _next_day(day)),
        )

    def between(self, mode: Mode, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM decisions WHERE mode = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (mode.value, iso(start), iso(end)),
        )

    def days_histogram(self, mode: Mode) -> dict[str, int]:
        """UTC day -> decision count. Used to cross-check qualification
        evidence against what actually happened in this database."""
        rows = self.db.query(
            "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n FROM decisions "
            "WHERE mode = ? GROUP BY substr(ts, 1, 10)",
            (mode.value,),
        )
        return {r["day"]: int(r["n"]) for r in rows}

    def last_processed_candle(self, mode: Mode, strategy: str) -> datetime | None:
        row = self.db.query_one(
            "SELECT MAX(candle_open_time) AS t FROM decisions WHERE mode = ? AND strategy = ?",
            (mode.value, strategy),
        )
        return parse_iso(row["t"]) if row and row["t"] else None


class PositionsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def open_position(self, mode: Mode) -> PositionState | None:
        row = self.db.query_one(
            "SELECT * FROM positions WHERE mode = ? AND status = 'open' "
            "ORDER BY opened_at DESC LIMIT 1",
            (mode.value,),
        )
        if not row:
            return None
        return PositionState(
            position_id=row["id"],
            symbol=row["symbol"],
            qty=dec(row["qty"]),
            avg_entry_price=dec(row["avg_entry_price"]),
            stop_price=dec(row["stop_price"]),
            opened_at=parse_iso(row["opened_at"]),
            entry_fee=dec(row["entry_fee"]),
            entry_order_id=row["entry_order_id"],
            protective_order_id=row.get("protective_order_id"),
        )

    def open_by_entry_order(self, mode: Mode, entry_order_id: str) -> PositionState | None:
        row = self.db.query_one(
            "SELECT * FROM positions WHERE mode = ? AND entry_order_id = ? AND status = 'open' "
            "ORDER BY opened_at DESC LIMIT 1",
            (mode.value, entry_order_id),
        )
        if not row:
            return None
        return PositionState(
            position_id=row["id"],
            symbol=row["symbol"],
            qty=dec(row["qty"]),
            avg_entry_price=dec(row["avg_entry_price"]),
            stop_price=dec(row["stop_price"]),
            opened_at=parse_iso(row["opened_at"]),
            entry_fee=dec(row["entry_fee"]),
            entry_order_id=row["entry_order_id"],
            protective_order_id=row.get("protective_order_id"),
        )

    def insert_open(
        self,
        mode: Mode,
        symbol: str,
        qty: Decimal,
        avg_entry_price: Decimal,
        stop_price: Decimal,
        entry_order_id: str,
        entry_fee: Decimal,
        ts: datetime | None = None,
    ) -> str:
        pid = uid()
        self.db.execute(
            "INSERT INTO positions (id, mode, symbol, qty, avg_entry_price, stop_price, "
            "opened_at, closed_at, status, entry_order_id, exit_order_id, entry_fee, exit_fee, "
            "realized_pnl, exit_reason) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'open', ?, NULL, ?, "
            "NULL, NULL, NULL)",
            (
                pid,
                mode.value,
                symbol,
                str(qty),
                str(avg_entry_price),
                str(stop_price),
                iso(ts or utcnow()),
                entry_order_id,
                str(entry_fee),
            ),
        )
        return pid

    def set_protective_order(self, position_id: str, client_order_id: str | None) -> None:
        self.db.execute(
            "UPDATE positions SET protective_order_id = ? WHERE id = ?",
            (client_order_id, position_id),
        )

    def update_stop(self, position_id: str, stop_price: Decimal) -> None:
        self.db.execute(
            "UPDATE positions SET stop_price = ? WHERE id = ?", (str(stop_price), position_id)
        )

    def update_open_entry(
        self,
        position_id: str,
        qty: Decimal,
        avg_entry_price: Decimal,
        entry_fee: Decimal,
        stop_price: Decimal,
    ) -> None:
        self.db.execute(
            "UPDATE positions SET qty = ?, avg_entry_price = ?, entry_fee = ?, stop_price = ? "
            "WHERE id = ? AND status = 'open'",
            (str(qty), str(avg_entry_price), str(entry_fee), str(stop_price), position_id),
        )

    def update_open_after_exit(
        self,
        position_id: str,
        remaining_qty: Decimal,
        remaining_entry_fee: Decimal,
        cumulative_exit_fee: Decimal,
        cumulative_realized_pnl: Decimal,
    ) -> None:
        self.db.execute(
            "UPDATE positions SET qty = ?, entry_fee = ?, exit_fee = ?, realized_pnl = ? "
            "WHERE id = ? AND status = 'open'",
            (
                str(remaining_qty),
                str(remaining_entry_fee),
                str(cumulative_exit_fee),
                str(cumulative_realized_pnl),
                position_id,
            ),
        )

    def mark_dust(
        self,
        position_id: str,
        remaining_qty: Decimal,
        remaining_entry_fee: Decimal,
        exit_order_id: str,
        cumulative_exit_fee: Decimal,
        cumulative_realized_pnl: Decimal,
        exit_reason: str,
        ts: datetime | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE positions SET status = 'dust', qty = ?, entry_fee = ?, closed_at = ?, "
            "exit_order_id = ?, exit_fee = ?, realized_pnl = ?, exit_reason = ? WHERE id = ?",
            (
                str(remaining_qty),
                str(remaining_entry_fee),
                iso(ts or utcnow()),
                exit_order_id,
                str(cumulative_exit_fee),
                str(cumulative_realized_pnl),
                exit_reason,
                position_id,
            ),
        )

    def close(
        self,
        position_id: str,
        exit_order_id: str,
        exit_fee: Decimal,
        realized_pnl: Decimal,
        exit_reason: str,
        ts: datetime | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE positions SET status = 'closed', closed_at = ?, exit_order_id = ?, "
            "exit_fee = ?, realized_pnl = ?, exit_reason = ? WHERE id = ?",
            (
                iso(ts or utcnow()),
                exit_order_id,
                str(exit_fee),
                str(realized_pnl),
                exit_reason,
                position_id,
            ),
        )

    def add_realization(
        self,
        position_id: str,
        mode: Mode,
        symbol: str,
        exit_order_id: str,
        qty: Decimal,
        avg_entry_price: Decimal,
        exit_price: Decimal,
        entry_fee_allocated: Decimal,
        exit_fee: Decimal,
        realized_pnl: Decimal,
        exit_reason: str,
        ts: datetime | None = None,
        cum_qty_after: Decimal | None = None,
    ) -> bool:
        """Returns False when the DB idempotency constraint says this exact
        realization (same exit order, same cumulative point) already exists."""
        inserted = self.db.execute_rowcount(
            "INSERT INTO position_realizations (id, position_id, mode, symbol, "
            "exit_order_id, qty, avg_entry_price, exit_price, entry_fee_allocated, "
            "exit_fee, realized_pnl, exit_reason, ts, cum_qty_after) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (
                uid(),
                position_id,
                mode.value,
                symbol,
                exit_order_id,
                str(qty),
                str(avg_entry_price),
                str(exit_price),
                str(entry_fee_allocated),
                str(exit_fee),
                str(realized_pnl),
                exit_reason,
                iso(ts or utcnow()),
                str(cum_qty_after) if cum_qty_after is not None else None,
            ),
        )
        return inserted == 1

    def realized_totals(self, position_id: str) -> tuple[Decimal, Decimal]:
        rows = self.db.query(
            "SELECT realized_pnl, exit_fee FROM position_realizations WHERE position_id = ?",
            (position_id,),
        )
        pnl = sum((dec(r["realized_pnl"]) for r in rows), ZERO)
        fees = sum((dec(r["exit_fee"]) for r in rows), ZERO)
        return pnl, fees

    def dust_positions(self, mode: Mode) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM positions WHERE mode = ? AND status = 'dust' ORDER BY closed_at",
            (mode.value,),
        )

    def apply_dust_sale(
        self,
        position_id: str,
        sold_qty: Decimal,
        remaining_entry_fee: Decimal,
        add_realized: Decimal,
        add_exit_fee: Decimal,
        exit_order_id: str,
        ts: datetime,
        exit_reason: str = "dust_sweep",
    ) -> None:
        """Apply one allocated dust-sweep fill without losing unsold residue."""
        row = self.db.query_one("SELECT * FROM positions WHERE id = ?", (position_id,))
        if not row or row["status"] != "dust":
            raise RuntimeError(f"dust position {position_id} is unavailable")
        current_qty = dec(row["qty"])
        if sold_qty <= ZERO or sold_qty > current_qty:
            raise RuntimeError(
                f"dust sale {sold_qty} exceeds tracked residue {current_qty} for {position_id}"
            )
        realized = (dec(row["realized_pnl"]) if row.get("realized_pnl") else ZERO) + add_realized
        exit_fee = (dec(row["exit_fee"]) if row.get("exit_fee") else ZERO) + add_exit_fee
        remaining_qty = current_qty - sold_qty
        if remaining_qty == ZERO:
            self.db.execute(
                "UPDATE positions SET status = 'closed', qty = '0', entry_fee = '0', "
                "closed_at = ?, exit_order_id = ?, exit_fee = ?, realized_pnl = ?, "
                "exit_reason = ? WHERE id = ?",
                (
                    iso(ts),
                    exit_order_id,
                    str(exit_fee),
                    str(realized),
                    exit_reason,
                    position_id,
                ),
            )
        else:
            self.db.execute(
                "UPDATE positions SET qty = ?, entry_fee = ?, exit_order_id = ?, "
                "exit_fee = ?, realized_pnl = ?, exit_reason = ? "
                "WHERE id = ? AND status = 'dust'",
                (
                    str(remaining_qty),
                    str(remaining_entry_fee),
                    exit_order_id,
                    str(exit_fee),
                    str(realized),
                    exit_reason,
                    position_id,
                ),
            )

    def closed_positions(self, mode: Mode, limit: int = 200) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM positions WHERE mode = ? AND status IN ('closed', 'dust') "
            "ORDER BY closed_at DESC LIMIT ?",
            (mode.value, limit),
        )

    def closed_on_day(self, mode: Mode, day: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM positions WHERE mode = ? AND status IN ('closed', 'dust') "
            "AND closed_at >= ? AND closed_at < ? ORDER BY closed_at",
            (mode.value, f"{day}T00:00:00+00:00", _next_day(day)),
        )

    def closed_between(self, mode: Mode, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM positions WHERE mode = ? AND status IN ('closed', 'dust') "
            "AND closed_at >= ? AND closed_at < ? ORDER BY closed_at",
            (mode.value, iso(start), iso(end)),
        )

    def opened_on_day(self, mode: Mode, day: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM positions WHERE mode = ? AND opened_at >= ? AND opened_at < ? "
            "ORDER BY opened_at",
            (mode.value, f"{day}T00:00:00+00:00", _next_day(day)),
        )

    def opened_between(self, mode: Mode, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM positions WHERE mode = ? AND opened_at >= ? AND opened_at < ? "
            "ORDER BY opened_at",
            (mode.value, iso(start), iso(end)),
        )

    def consecutive_losses(self, mode: Mode) -> int:
        rows = self.closed_positions(mode, limit=50)
        count = 0
        for row in rows:
            pnl = dec(row["realized_pnl"]) if row["realized_pnl"] is not None else ZERO
            if pnl < ZERO:
                count += 1
            else:
                break
        return count

    def last_losing_close_time(self, mode: Mode) -> datetime | None:
        rows = self.closed_positions(mode, limit=50)
        for row in rows:
            pnl = dec(row["realized_pnl"]) if row["realized_pnl"] is not None else ZERO
            if pnl < ZERO:
                return parse_iso(row["closed_at"])
        return None

    def realized_pnl_between(self, mode: Mode, start: datetime, end: datetime) -> Decimal:
        rows = self.db.query(
            "SELECT realized_pnl FROM position_realizations WHERE mode = ? "
            "AND ts >= ? AND ts < ?",
            (mode.value, iso(start), iso(end)),
        )
        total = ZERO
        for row in rows:
            if row["realized_pnl"] is not None:
                total += dec(row["realized_pnl"])
        if rows:
            return total
        legacy = self.db.query(
            "SELECT realized_pnl FROM positions WHERE mode = ? AND status IN ('closed', 'dust') "
            "AND closed_at >= ? AND closed_at < ?",
            (mode.value, iso(start), iso(end)),
        )
        for row in legacy:
            if row["realized_pnl"] is not None:
                total += dec(row["realized_pnl"])
        return total

    def realizations_between(
        self, mode: Mode, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM position_realizations WHERE mode = ? AND ts >= ? AND ts < ? "
            "ORDER BY ts",
            (mode.value, iso(start), iso(end)),
        )


class BalanceRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def snapshot(
        self,
        mode: Mode,
        balances: dict[str, dict[str, str]],
        equity: Decimal,
        quote_price: Decimal | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO balance_snapshots (id, ts, mode, balances_json, equity, quote_price) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                uid(),
                iso(utcnow()),
                mode.value,
                json_dumps(balances),
                str(equity),
                str(quote_price) if quote_price is not None else None,
            ),
        )

    def latest(self, mode: Mode) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM balance_snapshots WHERE mode = ? ORDER BY ts DESC LIMIT 1",
            (mode.value,),
        )

    def latest_at_or_before(self, mode: Mode, ts: datetime) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM balance_snapshots WHERE mode = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (mode.value, iso(ts)),
        )

    def first(self, mode: Mode) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM balance_snapshots WHERE mode = ? ORDER BY ts ASC LIMIT 1",
            (mode.value,),
        )

    def equity_at_or_before(self, mode: Mode, ts: datetime) -> Decimal | None:
        row = self.db.query_one(
            "SELECT equity FROM balance_snapshots WHERE mode = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (mode.value, iso(ts)),
        )
        return dec(row["equity"]) if row else None

    def peak_equity(self, mode: Mode) -> Decimal | None:
        rows = self.db.query("SELECT equity FROM balance_snapshots WHERE mode = ?", (mode.value,))
        if not rows:
            return None
        return max(dec(r["equity"]) for r in rows)


class DailyEquityRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def ensure_day_start(self, day: str, mode: Mode, equity: Decimal) -> Decimal:
        """Record start-of-day equity once; returns the stored value."""
        row = self.db.query_one(
            "SELECT start_equity FROM daily_equity WHERE day = ? AND mode = ?",
            (day, mode.value),
        )
        if row:
            return dec(row["start_equity"])
        self.db.execute(
            "INSERT INTO daily_equity (day, mode, start_equity, end_equity, realized_pnl, fees, "
            "entries_count, updated_at) VALUES (?, ?, ?, NULL, NULL, NULL, 0, ?)",
            (day, mode.value, str(equity), iso(utcnow())),
        )
        return equity

    def set_day_end(
        self,
        day: str,
        mode: Mode,
        equity: Decimal,
        realized_pnl: Decimal,
        fees: Decimal,
        entries: int,
    ) -> None:
        self.db.execute(
            "UPDATE daily_equity SET end_equity = ?, realized_pnl = ?, fees = ?, "
            "entries_count = ?, updated_at = ? WHERE day = ? AND mode = ?",
            (str(equity), str(realized_pnl), str(fees), entries, iso(utcnow()), day, mode.value),
        )

    def get(self, day: str, mode: Mode) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM daily_equity WHERE day = ? AND mode = ?", (day, mode.value)
        )

    def recent(self, mode: Mode, limit: int = 30) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM daily_equity WHERE mode = ? ORDER BY day DESC LIMIT ?",
            (mode.value, limit),
        )


class ReportsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def insert(
        self, day: str, kind: str, mode: Mode, content_md: str, content: dict[str, Any]
    ) -> str:
        rid = uid()
        self.db.execute(
            "INSERT INTO reports (id, day, kind, mode, content_md, content_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, day, kind, mode.value, content_md, json_dumps(content), iso(utcnow())),
        )
        return rid

    def get(self, day: str, kind: str, mode: Mode) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM reports WHERE day = ? AND kind = ? AND mode = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (day, kind, mode.value),
        )

    def count(self, kind: str, mode: Mode) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM reports WHERE kind = ? AND mode = ?",
            (kind, mode.value),
        )
        return int(row["n"]) if row else 0


class EventsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def killswitch(self, source: str, active: bool, reason: str) -> None:
        self.db.execute(
            "INSERT INTO killswitch_events (id, ts, source, active, reason) VALUES (?, ?, ?, ?, ?)",
            (uid(), iso(utcnow()), source, 1 if active else 0, reason),
        )

    def approval(self, action: str, hours: int | None, actor: str, note: str = "") -> None:
        self.db.execute(
            "INSERT INTO approval_events (id, ts, action, hours, actor, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid(), iso(utcnow()), action, hours, actor, note),
        )

    def reconciliation(self, ok: bool, details: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO reconciliation_results (id, ts, ok, details_json) VALUES (?, ?, ?, ?)",
            (uid(), iso(utcnow()), 1 if ok else 0, json_dumps(details)),
        )

    def last_reconciliation(self) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM reconciliation_results ORDER BY ts DESC LIMIT 1")

    def api_error(self, endpoint: str, message: str) -> None:
        self.db.execute(
            "INSERT INTO api_errors (id, ts, endpoint, message) VALUES (?, ?, ?, ?)",
            (uid(), iso(utcnow()), endpoint, message[:500]),
        )

    def api_errors_last_hour(self, now: datetime | None = None) -> int:
        cutoff = (now or utcnow()) - timedelta(hours=1)
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM api_errors WHERE ts >= ?", (iso(cutoff),)
        )
        return int(row["n"]) if row else 0

    def alert(self, severity: str, kind: str, message: str, delivered: dict[str, bool]) -> None:
        self.db.execute(
            "INSERT INTO alerts (id, ts, severity, kind, message, delivered_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid(), iso(utcnow()), severity, kind, message, json_dumps(delivered)),
        )

    def alerts_on_day(self, day: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM alerts WHERE ts >= ? AND ts < ? ORDER BY ts",
            (f"{day}T00:00:00+00:00", _next_day(day)),
        )

    def alerts_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM alerts WHERE ts >= ? AND ts < ? ORDER BY ts", (iso(start), iso(end))
        )

    def api_errors_on_day(self, day: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM api_errors WHERE ts >= ? AND ts < ? ORDER BY ts",
            (f"{day}T00:00:00+00:00", _next_day(day)),
        )

    def api_errors_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM api_errors WHERE ts >= ? AND ts < ? ORDER BY ts",
            (iso(start), iso(end)),
        )


class ConfigVersionsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, cfg_hash: str, content_json: str) -> None:
        if not self.db.query_one("SELECT hash FROM config_versions WHERE hash = ?", (cfg_hash,)):
            self.db.execute(
                "INSERT INTO config_versions (hash, first_seen, content_json) VALUES (?, ?, ?)",
                (cfg_hash, iso(utcnow()), content_json),
            )


class SimStateRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT value_json FROM sim_state WHERE key = ?", (key,))
        return json.loads(row["value_json"]) if row else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        payload = json_dumps(value)
        now = iso(utcnow())
        if self.db.query_one("SELECT key FROM sim_state WHERE key = ?", (key,)):
            self.db.execute(
                "UPDATE sim_state SET value_json = ?, updated_at = ? WHERE key = ?",
                (payload, now, key),
            )
        else:
            self.db.execute(
                "INSERT INTO sim_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                (key, payload, now),
            )


class LiveUnlockRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, phrase_hash: str, expires_at: datetime, risk_summary: dict[str, Any]) -> str:
        unlock_id = uid()
        self.db.execute(
            "INSERT INTO live_unlock (id, ts, phrase_hash, expires_at, confirmed, "
            "risk_summary_json) VALUES (?, ?, ?, ?, 0, ?)",
            (unlock_id, iso(utcnow()), phrase_hash, iso(expires_at), json_dumps(risk_summary)),
        )
        return unlock_id

    def confirm(self, unlock_id: str) -> None:
        self.db.execute("UPDATE live_unlock SET confirmed = 1 WHERE id = ?", (unlock_id,))

    def get(self, unlock_id: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM live_unlock WHERE id = ?", (unlock_id,))

    def has_valid_unlock(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        rows = self.db.query(
            "SELECT * FROM live_unlock WHERE confirmed = 1 ORDER BY ts DESC LIMIT 1"
        )
        if not rows:
            return False
        return parse_iso(rows[0]["expires_at"]) > now


class Repositories:
    """Bundle of all repositories over a single Database connection."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.flags = FlagsRepo(db)
        self.orders = OrdersRepo(db)
        self.decisions = DecisionsRepo(db)
        self.positions = PositionsRepo(db)
        self.balances = BalanceRepo(db)
        self.daily_equity = DailyEquityRepo(db)
        self.reports = ReportsRepo(db)
        self.events = EventsRepo(db)
        self.config_versions = ConfigVersionsRepo(db)
        self.sim_state = SimStateRepo(db)
        self.live_unlock = LiveUnlockRepo(db)
