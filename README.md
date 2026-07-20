# trading-bot

A safety-first, paper-first automated **spot** trading system for a single
crypto pair (default `BTCUSDT` on Binance), built for educational
experimentation with a very small starting balance (~NZD 50 / ~30 USDT).

> ## ⚠️ Read this first
>
> - **Trading can lose all deposited capital.**
> - **Small accounts are dominated by fees, spread and minimum-order limits.**
>   With ~30 USDT and the default risk limits, many signals will be correctly
>   rejected (`MIN_NOTIONAL_EXCEEDS_RISK`) — expect few trades. At ~20 USDT the
>   bot will refuse to trade at all rather than exceed its risk budget. That
>   is designed behaviour, not a bug.
> - **Backtest and paper results do not guarantee future results.**
> - **Nothing in this repository is financial advice.**
> - You remain responsible for exchange rules, taxes and legal compliance.

## Core safety principle

**No AI/LLM component has authority to trade.** Every live decision passes
through deterministic, tested code:

```
market-data validation → strategy signal → risk approval (signed single-use
token) → exchange-rule validation → order preview (persisted intent) →
execution gateway (the ONLY path to the exchange) → post-trade reconciliation
```

AI is used only for generating human-readable report text
(`src/trading_bot/ai/analyst.py`, deterministic templates by default). Its
output is stored as text and never parsed into actions.

## Operating modes

| Mode | Default | Credentials | Endpoint |
|---|---|---|---|
| **paper** | ✅ yes | none | public market data + local simulator |
| **testnet** | opt-in | `BINANCE_TESTNET_*` | `https://testnet.binance.vision` only |
| **live** | **locked** | `BINANCE_LIVE_*` | `https://api.binance.com` only |

Live mode is locked until *all* of: ≥30 calendar days **and** ≥300 recorded
paper decisions, paper daily reports + a backtest report exist, the full test
suite passed within 72h (≥100 tests, coverage + named safety tests verified),
an interactive unlock ceremony (typed random phrase), `LIVE_TRADING_ENABLED=true`,
PostgreSQL supplied through `DATABASE_URL`, an operational external alert
channel, and an API key with **withdrawals
disabled** (verified via the exchange API).
There is no automatic promotion between modes. See
[docs/LIVE_TRADING_CHECKLIST.md](docs/LIVE_TRADING_CHECKLIST.md).
The current release-readiness audit and remaining operator-owned blockers are
tracked in [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md).

## Hard risk limits (defaults; config can only tighten them)

| Limit | Default | Hard cap |
|---|---|---|
| Open positions | 1 | 1 |
| Position allocation | 20% of equity | 25% |
| Cash reserve | 50% of equity | ≥30% |
| Risk per trade | 0.5% of equity | 1% |
| Daily realised loss | 2% | 3% |
| Rolling 7-day loss | 5% | 6% |
| Max drawdown | 8% | 10% |
| Entries per day | 2 | 4 |
| Cooldown after a loss | 12 h | ≥1 h |
| Pause after consecutive losses | 3 | — |

Every rejection carries a structured reason code
(`DAILY_LOSS_LIMIT`, `STALE_MARKET_DATA`, `MIN_NOTIONAL_EXCEEDS_RISK`, …).
Full model: [docs/RISK_MODEL.md](docs/RISK_MODEL.md).

## Kill switches (any one blocks new entries; manual reset required)

1. CLI — `python -m trading_bot stop`
2. Environment — `TRADING_KILL_SWITCH=true`
3. Database flag
4. Emergency file — create `STOP_TRADING` in the project root
5. Automatic circuit breaker (API errors, unknown orders, reconciliation
   mismatch, drawdown) — latches after persistent hard trips

An existing position is **not** blindly liquidated; the configurable
emergency policy (`hold_and_monitor` default) keeps the protective stop
monitor running. Exits always remain allowed.

## Quick start (paper, no credentials)

```bash
make setup                       # venv + pinned deps
cp .env.example .env             # optional; no secrets needed for paper

# offline, deterministic (fixture data):
PYTHONPATH=src .venv/bin/python -m trading_bot --config config/paper.fixture.yaml paper run --cycles 500

# live public market data (still simulated money):
PYTHONPATH=src .venv/bin/python -m trading_bot --config config/paper.yaml paper run
```

Useful commands:

```bash
python -m trading_bot status                     # full status
python -m trading_bot stop                       # kill switch ON
python -m trading_bot resume --note "why"        # manual reset
python -m trading_bot pause / approve --hours 24 # daily continuation control
python -m trading_bot report daily               # markdown daily report
python -m trading_bot report performance         # stats + benchmarks
python -m trading_bot close-position-preview     # preview only, no order
python -m trading_bot backtest run --data data/fixtures/btcusdt_1h.csv [--walk-forward]
python -m trading_bot live status | live unlock  # gated live ceremony
python -m trading_bot db migrate | db backup
python -m trading_bot audit verify               # hash-chained audit log
```

## Quality gate (verified, not self-declared)

`make record-tests` runs the whole suite and writes
`var/quality/latest_test_run.json` with: tests collected/passed, coverage %,
presence of named safety tests, recomputed artifact hashes, source/dependency
hashes, tool status and git state. Verify it with:

```bash
python -m trading_bot quality run
python -m trading_bot quality verify
```

The live gate rejects zero-test, stale, tampered, no-repository, dirty, missing
hash, missing regression-test, or low-coverage evidence. Qualification evidence
is separate: backtests and offline fixtures never count as live-market paper
days.

## Project layout

```
src/trading_bot/    config core security exchange market_data strategies risk
                    execution portfolio storage reporting notifications
                    monitoring control engine backtest ai cli
tests/              unit / integration / security / property (hypothesis)
docs/               architecture, risk model, security, threat model, ops,
                    live checklist, incident response, backtesting, strategy,
                    API key setup + example reports
config/             paper.yaml (default) · paper.fixture.yaml · testnet.yaml ·
                    live.locked.yaml
migrations/         plain SQL, applied in order, recorded in schema_migrations
```

Docs index: [ARCHITECTURE](docs/ARCHITECTURE.md) ·
[RISK_MODEL](docs/RISK_MODEL.md) · [SECURITY](docs/SECURITY.md) ·
[THREAT_MODEL](docs/THREAT_MODEL.md) · [OPERATIONS](docs/OPERATIONS.md) ·
[LIVE_TRADING_CHECKLIST](docs/LIVE_TRADING_CHECKLIST.md) ·
[INCIDENT_RESPONSE](docs/INCIDENT_RESPONSE.md) ·
[BACKTESTING](docs/BACKTESTING.md) · [STRATEGY](docs/STRATEGY.md) ·
[API_KEY_SETUP](docs/API_KEY_SETUP.md) · [PRODUCTION_READINESS](docs/PRODUCTION_READINESS.md) · [TESTING](docs/TESTING.md) ·
[QUALIFICATION_EVIDENCE](docs/QUALIFICATION_EVIDENCE.md)

## Deployment

`Dockerfile` (non-root, read-only fs, no capabilities) and
`docker-compose.yml` (bot + PostgreSQL) are provided; the container starts in
**paper** mode and cannot start live trading by deployment alone. See
[docs/OPERATIONS.md](docs/OPERATIONS.md) for backup/restore, upgrades,
rollback and disaster recovery.

## Honest limitations

- The baseline EMA strategy is a transparent benchmark, not an edge; on the
  bundled 180-day synthetic bear fixture it lost 2.3% while buy-and-hold lost
  31% — capital protection working, profit not promised.
- Software stop-losses cannot guarantee an exit price (see RISK_MODEL).
- Tiny accounts may correctly produce no trades because every entry must also
  prove that the stop-level exit remains above exchange minimum notional after
  fees, slippage and a conservative buffer.
- Partial entries/exits are cumulative: every fill is persisted, average entry
  price and fees update after later fills, and partial exits leave residual
  exposure open unless the remainder is explicit exchange dust.
- Paper fills are a model (spread + bounded slippage + fees); real fills
  differ.
- One symbol, spot only, long/flat only — by design.
