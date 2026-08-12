# Operations guide

## Local development

```bash
make setup            # venv + pinned deps + editable install
make fixtures         # regenerate deterministic candle fixtures
make test             # full suite
make quality          # lint + types + bandit + pip-audit + quality record
make paper-sim        # offline deterministic paper run (500 cycles)
make backtest-sample  # 180-day fixture backtest
```

If the editable install misbehaves, prefix commands with `PYTHONPATH=src`.

## Production deployment (docker compose)

```bash
cp .env.example .env          # set POSTGRES_PASSWORD (+ notifier secrets)
./scripts/deploy_update.sh    # commit-stamped build + up; --no-pull to skip git pull
docker compose logs -f bot
```

Use the script rather than `docker compose up -d --build` directly: it passes
`GIT_COMMIT`, which is the only way a container (which ships no `.git`) can
identify its own code. Unidentifiable builds record no qualification evidence
— see docs/QUALIFICATION_EVIDENCE.md.

The container migrates the DB then starts **paper** mode. Testnet/live
require editing the command/config deliberately — a restart or redeploy can
never begin live trading by itself (the live gate re-checks everything at
every startup).

The default local `config/paper.yaml` uses SQLite for convenience and is
non-qualifying. For the 30-day live-market qualification period, set
`DATABASE_URL` to the long-lived PostgreSQL deployment before day one. The
engine records qualification evidence only when its active backend is
PostgreSQL; never store the connection password in YAML or Git.

### Startup sequence (every start, all modes)

1. migrations → 2. config validation vs hard caps → 3. mode/endpoint/
credential checks (+ key permission check in live) → 4. LIVE PostgreSQL and
read-only external-alert connectivity checks → 5. instance lock →
6. reconciliation (balances, open orders, stale intents, partial fills) →
7. only then the trading loop. A reconciliation mismatch or reconciliation
exception blocks entries before the first evaluation. TESTNET/LIVE startup
fails closed; PAPER may keep running for diagnostics but cannot open entries
while the block is active.

An unexpected cycle exception is also treated as state uncertainty: health is
degraded, the database/reconciliation gate blocks new entries, and an external
critical alert is attempted. This is deliberate because an exception can occur
after the exchange accepted an order but before local accounting completed.

## Scheduling

Internal: candle-cadence evaluation, exit monitoring every cycle,
reconciliation every 30 min, DB integrity hourly, daily report at
`reporting.daily_time_local`. The DB instance lock prevents overlapping
engines; a stale lock (heartbeat >120 s) is stolen on restart. External cron
is optional — e.g. nightly `scripts/backup_db.sh`.

## Monitoring

- `GET 127.0.0.1:9754/health` — component breakdown: application, exchange,
  market_data, database, risk engine, trading_permitted, kill switch, last
  cycle/reconciliation. **A live HTTP process does not mean trading is
  safe — read the components.**
- `/health/live`, `/health/ready` — probes; `/metrics` — Prometheus text
  (equity, drawdown, cycles, decisions, entries/exits, errors).
- Alerts (console/email/telegram): risk-limit events, kill switch,
  reconciliation mismatch, exit failures, consecutive-loss pause.
- A consecutive-loss pause is not a cooldown and never clears with time.
  `resume` may clear the ordinary pause/kill switch but cannot clear this
  brake. After review, use `risk acknowledge-loss-pause --note '<decision>'`;
  the command still refuses unless the account has no open position, order
  state is certain, and a same-mode reconciliation passed within the last hour.
- LIVE configuration must enable email or Telegram. Before unlock/startup, the
  gate performs a read-only SMTP or Telegram-chat connectivity probe; a failed
  probe refuses LIVE. A separately received test alert remains an operator
  checklist requirement because connectivity alone cannot prove delivery.

## Backup & restore

- SQLite: `./scripts/backup_db.sh` (WAL checkpoint + copy, keeps 30).
- Postgres: `DATABASE_URL=... ./scripts/backup_db.sh` (pg_dump custom format).
- Restore: stop the bot, restore the file / `pg_restore`, start, verify
  `status` + `audit verify`, confirm reconciliation is OK before resuming.

## Log rotation

`var/logs/bot.jsonl` grows unbounded by default. Use logrotate (size 50M,
keep 10, copytruncate) or a container log driver with rotation.

## Upgrade / rollback

1. `python -m trading_bot pause` (positions stay stop-monitored)
2. backup DB; note current image/commit
3. deploy new version → migrations apply automatically (append-only; no
   destructive migrations policy)
4. `status` + `audit verify` + reconciliation OK → `resume`
5. Rollback = redeploy previous image + restore the pre-upgrade backup if a
   new migration was applied. Never run two versions against one DB (the
   instance lock will refuse anyway).

## Disaster recovery

Host lost: provision new host → restore latest backup → start in PAPER mode →
verify reconciliation against the exchange (it will flag any position/balance
drift) → only then consider re-enabling the previous mode. Live mode will
demand a fresh quality record and unlock ceremony — that is intentional.
If in doubt at any point: `TRADING_KILL_SWITCH=true` or create `STOP_TRADING`.

## Routine checks (daily, 2 minutes)

Read the daily report: recommendation, rejected-entry reasons, drawdown,
consecutive losses, benchmark drift, API errors, "trading permitted". In
DAILY_APPROVAL mode, `approve --hours 24` is your deliberate go/no-go.

Daily reports use the configured local timezone by converting local start/end
of day to UTC query boundaries. UTC timestamps remain the storage format.

## Supervised 24/7 operation

- `./scripts/run_supervised.sh config/paper.yaml` — restart loop with
  exponential backoff; a clean exit ends supervision, crashes restart it.
- macOS: `docs/examples/com.trading-bot.paper.plist` is a launchd template
  (survives logout; adjust WorkingDirectory, then `launchctl load`).
- Docker: `restart: unless-stopped` in compose provides the same property.
- The supervisor inherits `DATABASE_URL`. Without that override, the default
  SQLite run is explicitly non-qualifying and contributes zero LIVE paper days.
- With native stops enabled, a crashed process leaves the protective
  STOP_LOSS_LIMIT resting ON the exchange; on restart, reconciliation adopts
  whatever happened while the bot was down (including a stop that filled).
