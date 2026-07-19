-- Partial-fill and partial-exit accounting.
-- Every exit fill that realizes P&L is recorded here independently of the
-- current open/closed state of the parent position.

CREATE TABLE IF NOT EXISTS position_realizations (
    id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exit_order_id TEXT NOT NULL,
    qty TEXT NOT NULL,
    avg_entry_price TEXT NOT NULL,
    exit_price TEXT NOT NULL,
    entry_fee_allocated TEXT NOT NULL,
    exit_fee TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    exit_reason TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_position_realizations_ts
    ON position_realizations (mode, ts);
