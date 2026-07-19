-- Initial schema. Portable SQL: TEXT ids (app-generated UUIDs), ISO-8601 UTC
-- timestamps as TEXT, Decimal values as TEXT (lossless), booleans as INTEGER 0/1.

CREATE TABLE IF NOT EXISTS control_flags (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_lock (
    name TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market_data_ts TEXT,
    candle_open_time TEXT,
    signal_action TEXT,
    signal_reason TEXT,
    indicators_json TEXT,
    proposed_order_json TEXT,
    risk_approved INTEGER,
    risk_codes_json TEXT,
    risk_inputs_json TEXT,
    execution_state TEXT,
    execution_json TEXT,
    explanation TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions (ts);
CREATE INDEX IF NOT EXISTS idx_decisions_mode ON decisions (mode);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT,
    correlation_id TEXT,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    qty TEXT NOT NULL,
    limit_price TEXT,
    stop_price TEXT,
    state TEXT NOT NULL,
    purpose TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_state ON orders (state);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders (created_at);

CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    fee TEXT NOT NULL,
    fee_asset TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty TEXT NOT NULL,
    avg_entry_price TEXT NOT NULL,
    stop_price TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL,
    entry_order_id TEXT NOT NULL,
    exit_order_id TEXT,
    entry_fee TEXT NOT NULL,
    exit_fee TEXT,
    realized_pnl TEXT,
    exit_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    balances_json TEXT NOT NULL,
    equity TEXT NOT NULL,
    quote_price TEXT
);

CREATE INDEX IF NOT EXISTS idx_balance_snapshots_ts ON balance_snapshots (ts);

CREATE TABLE IF NOT EXISTS daily_equity (
    day TEXT NOT NULL,
    mode TEXT NOT NULL,
    start_equity TEXT NOT NULL,
    end_equity TEXT,
    realized_pnl TEXT,
    fees TEXT,
    entries_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (day, mode)
);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    mode TEXT NOT NULL,
    content_md TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_day ON reports (day, kind);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    severity TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    delivered_json TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    hash TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    content_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_events (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    hours INTEGER,
    actor TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS killswitch_events (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    active INTEGER NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS reconciliation_results (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_unlock (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    phrase_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    risk_summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_errors (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_errors_ts ON api_errors (ts)
