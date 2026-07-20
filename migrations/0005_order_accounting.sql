-- Dedicated ledger of cumulative amounts already booked per order.
-- Independent of the orders table so accounting deltas can never silently
-- no-op when a row is missing (UPSERT semantics, primary-key enforced).

CREATE TABLE IF NOT EXISTS order_accounting (
    client_order_id TEXT PRIMARY KEY,
    qty TEXT NOT NULL,
    quote TEXT NOT NULL,
    fee_quote TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
