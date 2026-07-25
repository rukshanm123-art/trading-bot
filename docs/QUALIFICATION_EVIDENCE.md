# Qualification Evidence

Live qualification uses a dedicated append-only JSONL ledger at:

```text
var/quality/qualification_evidence.jsonl
```

Each record is hash-chained to the previous record. The live gate verifies the
chain before counting evidence. Tampering, missing commit/configuration/
strategy metadata, invalid wall-clock timestamps, or `git_state=no_repo`
rejects the evidence.

Only records with:

- `source_mode: "paper"`
- `data_source_class: "live_market"`
- `database_backend: "postgres"`
- real wall-clock start/end timestamps
- a Git commit
- configuration hash
- strategy version
- eligible paper decision count

can count toward paper qualification. Historical backtests, offline fixtures,
synthetic simulations and testnet records count as zero live-market paper days.
Repeating the same calendar day does not create duplicate day credit.

## When records are written

The engine flushes a record every `EVIDENCE_FLUSH_INTERVAL_S` (30 minutes)
from the run loop, and once more on clean shutdown. A 24/7 deployment must
never have to stop to prove that it ran — evidence written only at shutdown
would leave a continuously running bot at `0/30` days forever.

Each flush covers the window since the previous flush and then advances the
mark, so consecutive records are **contiguous and non-overlapping**. This
matters: day credit is the SUM of per-day coverage across records, so
overlapping windows would manufacture wall-clock time. A day is credited only
when its records cover ≥ `QUALIFICATION_MIN_DAY_COVERAGE_S` (12 h) *and* the
database shows ≥ `QUALIFICATION_MIN_DECISIONS_PER_DAY` decisions that day, so
flushing more often can never buy a day.

A hard kill (SIGKILL, OOM, power loss) loses at most the current unflushed
window — up to 30 minutes, against a 12-hour daily floor.

## Provenance: which code produced the evidence

Every record names the commit it came from; records that cannot be attributed
are rejected. Two sources are accepted:

| state | source | typical host |
| --- | --- | --- |
| `repo` | a real `.git` checkout | running from source |
| `image` | `.build_info.json`, written from `ARG GIT_COMMIT` at build time | Docker |

A container image deliberately ships no `.git`, so **Docker deployments must
be built with the commit stamp** — use `scripts/deploy_update.sh`, not a bare
`docker compose up -d --build`. Without it the engine refuses to write
evidence at all (rather than writing records the gate will throw away) and
logs `NON-QUALIFYING PAPER RUN` at startup plus an error at every flush.

SQLite live-market paper sessions are also deliberately non-qualifying. They
remain useful for evaluation, but the engine records no qualification evidence
for them and emits a startup/health warning. Start the real qualification period
on the same long-lived PostgreSQL deployment the LIVE gate will later inspect:

```bash
export DATABASE_URL='postgresql://bot:YOUR_PASSWORD@localhost:5432/trading_bot'
PYTHONPATH=src .venv/bin/python -m trading_bot --config config/paper.yaml paper run
```

Supply credentials outside Git. The evidence HMAC key, paper decisions and
reports live in this database; switching to a different database does not carry
qualification forward. A tested PostgreSQL dump/restore does, because it
preserves those records and the evidence key. Back up both PostgreSQL and the
append-only evidence ledger.

Passing qualification does not enable live trading. It only allows the next
manual live gate to be considered; live funds remain locked until every gate,
the unlock ceremony, environment switch and API-key permission check pass.
