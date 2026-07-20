# Architecture

## Design goals

Capital protection first; deterministic execution; auditability; testability;
fail closed on any doubt. Profit optimisation is explicitly subordinate.

## The pipeline (one evaluation per closed candle)

```
                ┌──────────────────────────────────────────────────────┐
 market data ──▶ 1 validation (staleness, gaps, jumps, spread, symbol) │
                │ 2 strategy signal (pure function, closed candles)    │
                │ 3 risk engine (all gates + sizing; single-use token) │
                │ 4 exchange-rule validation (filters, sizing floors)  │
                │ 5 order preview (persisted intent + audit record)    │
                │ 6 execution gateway  ── the ONLY adapter.create_order│
                │ 7 post-trade: fills → position → snapshots → recon   │
                └──────────────────────────────────────────────────────┘
```

The protective-exit monitor (stop breach check against the live bid) runs
every cycle, independent of candle cadence, and keeps running under an active
kill switch.

## Modules

| Module | Responsibility |
|---|---|
| `config` | pydantic-validated config; hard safety caps in `constants.py` that config can only tighten |
| `core` | Decimal money types (floats refused), domain models, enums |
| `security` | secret redaction, env secret provider, live-mode gate |
| `exchange` | `ExchangeAdapter` interface; Binance Spot REST adapter; deterministic paper simulator |
| `market_data` | fetch + validation service; CSV fixture source for offline replay |
| `strategies` | EMA trend (edge-triggered), buy-and-hold + no-trade benchmarks |
| `risk` | the final authority: gates, Decimal sizing, HMAC approval tokens, risk state from DB |
| `execution` | order state machine, execution gateway, client order ids, exit monitor |
| `portfolio` | accounting (positions, equity, realized P&L), reconciliation |
| `storage` | SQLite/Postgres behind one API, SQL migrations, hash-chained audit log |
| `control` | kill switches, circuit breaker, AUTO_CONTINUE / DAILY_APPROVAL |
| `engine` | wiring + main loop + instance lock + interval scheduling |
| `backtest` | the SAME engine replayed over fixtures; walk-forward evaluation |
| `reporting` | daily report, performance metrics, benchmarks |
| `notifications` | console / email / telegram adapters (secrets from env only) |
| `monitoring` | component health, metrics, read-only localhost HTTP endpoint |
| `ai` | advisory-only narrator (deterministic templates); no trading authority |
| `cli` | operator commands; the only control surface |

## Key structural safety decisions

**Risk approval tokens.** `RiskEngine.evaluate_entry/exit` returns an
HMAC-signed, single-use token bound to the exact order parameters, keyed by a
per-process random key. `ExecutionGateway.submit` refuses any order without a
valid token. Consequences: a rejected evaluation *cannot* reach the exchange;
a restart invalidates all previous approvals; a stored intent can never be
blindly resubmitted.

**Persist-then-submit.** The order intent is written to the DB *before* the
network call; the response (or UNKNOWN state) immediately after. A timeout
marks the order UNKNOWN, blocks new entries, and reconciliation resolves it
by client-order-id lookup. Intents not found on the exchange after
`max_order_age_s` are abandoned (state REJECTED), never retried.

**Mode separation.** The gateway verifies `adapter.kind == mode`; the Binance
adapter refuses any (mode, base URL, credential-source) mismatch; paper mode
cannot construct a signed adapter at all; testnet and live credentials use
different environment variables. Endpoint class is explicit:
`fixture`, `live_public`, `testnet`, or `live`. TESTNET market data and signed
orders both use the Spot Testnet endpoint; PAPER fixture mode constructs no
HTTP client; LIVE remains locked.

**Partial fills.** Every observed fill is persisted independently and
idempotently. Entry responses update cumulative quantity, weighted average
entry price, fees and protective quantity. Exit fills realize P&L only for the
filled quantity, allocate entry fees proportionally, preserve the remaining
position, and mark only exchange-dust residue as explicit `dust`.

**Atomic accounting.** Raw fill rows, position changes, realization-ledger and
cumulative order-accounting writes for one observed response commit as one
database transaction. The gateway persists order state but deliberately hands
fills to `PortfolioService`; an interrupted accounting write rolls back as a
unit. Any unexpected cycle failure marks runtime/database state uncertain,
persists the reconciliation block where possible and prevents new entries
until health and reconciliation recover.

**Dynamic exchange rules.** `exchangeInfo` drives LOT_SIZE,
MARKET_LOT_SIZE, price grid, notional minimum/maximum and current one-minute
request-weight limits. MARKET quantities are placed on a grid satisfying both
general and market-specific filters. Documented unknown execution outcomes are
never retried as new orders; they enter UNKNOWN and reconcile by client id.
The HTTP transport retains response headers and 418/429 retries wait at least
the exchange-provided `Retry-After` duration.

**One writer.** A DB instance lock (heartbeat row) prevents two engines from
trading the same database; scheduled jobs run inside the single engine loop,
so strategy evaluations cannot overlap.

**Time injection.** All persistence timestamps flow through
`core.models.utcnow()`, which follows the fixture clock during replays —
cooldowns, daily limits and exposure accounting behave identically in
backtests and production.

## Data flow & storage

Every evaluation writes a decision record (correlation id, config hash,
strategy version, market snapshot, signal, risk inputs/result, execution
result, explanation). Critical actions additionally append to the
hash-chained `audit_log`. Balances snapshot every cycle; daily equity rows
anchor the daily-loss limit.

Reconciliation exceptions persist a failure result, set the reconciliation
block flag, degrade health and block new entries. A later market-data success
cannot clear that block; only a successful reconciliation can.

## Deliberate omissions

No leverage, margin, futures, options, borrowing, short selling, martingale,
grid multiplication, averaging down, automatic withdrawals or cross-exchange
transfers. The withdrawal capability does not exist in the type system, and a
security test (`tests/security/test_code_hygiene.py`) enforces its absence.
