-- Idempotent fill accounting.
-- Orders carry the cumulative quantities/fees ALREADY booked into positions,
-- so reprocessing a cumulative exchange response applies only the delta.
-- Fills carry the exchange trade id where known. Realizations carry the
-- cumulative executed quantity they brought the order to, with a uniqueness
-- constraint so a replayed response cannot create a second row.
-- (NOTE for future migrations: no semicolons inside comments — the migration
-- runner splits statements on them.)

ALTER TABLE orders ADD COLUMN accounted_qty TEXT;

ALTER TABLE orders ADD COLUMN accounted_quote TEXT;

ALTER TABLE orders ADD COLUMN accounted_fee_quote TEXT;

ALTER TABLE fills ADD COLUMN trade_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_trade_unique
    ON fills (client_order_id, trade_id)
    WHERE trade_id IS NOT NULL AND trade_id != '';

ALTER TABLE position_realizations ADD COLUMN cum_qty_after TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_realizations_idempotent
    ON position_realizations (exit_order_id, cum_qty_after)
    WHERE cum_qty_after IS NOT NULL
