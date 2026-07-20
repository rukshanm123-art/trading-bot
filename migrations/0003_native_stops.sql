-- Exchange-native protective stop orders: link the resting STOP_LOSS_LIMIT
-- order to its position so restarts and reconciliation can recover it.

ALTER TABLE positions ADD COLUMN protective_order_id TEXT
