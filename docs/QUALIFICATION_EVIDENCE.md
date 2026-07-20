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
