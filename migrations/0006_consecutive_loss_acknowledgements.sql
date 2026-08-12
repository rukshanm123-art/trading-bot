-- Operator acknowledgements for the latched consecutive-loss brake.
--
-- The position watermark preserves the losing history while allowing the
-- next streak to begin only after a documented human review.  Records are
-- append-only and mode-scoped. A UNIQUE watermark makes retries idempotent.

CREATE TABLE IF NOT EXISTS consecutive_loss_acknowledgements (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'testnet', 'live')),
    watermark_position_id TEXT NOT NULL REFERENCES positions(id) ON DELETE RESTRICT,
    watermark_closed_at TEXT NOT NULL,
    streak_count INTEGER NOT NULL CHECK (streak_count > 0),
    acknowledged_at TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (TRIM(actor) <> ''),
    note TEXT NOT NULL CHECK (TRIM(note) <> ''),
    UNIQUE (mode, watermark_position_id)
);

CREATE INDEX IF NOT EXISTS idx_loss_ack_mode_time
    ON consecutive_loss_acknowledgements (mode, acknowledged_at);

-- A shared qualification database can contain paper and live records. The
-- recovery gate must never accept a reconciliation performed in another mode.
ALTER TABLE reconciliation_results ADD COLUMN mode TEXT;

CREATE INDEX IF NOT EXISTS idx_reconciliation_mode_time
    ON reconciliation_results (mode, ts);
