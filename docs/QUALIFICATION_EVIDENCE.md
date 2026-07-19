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
- real wall-clock start/end timestamps
- a Git commit
- configuration hash
- strategy version
- eligible paper decision count

can count toward paper qualification. Historical backtests, offline fixtures,
synthetic simulations and testnet records count as zero live-market paper days.
Repeating the same calendar day does not create duplicate day credit.

Passing qualification does not enable live trading. It only allows the next
manual live gate to be considered; live funds remain locked until every gate,
the unlock ceremony, environment switch and API-key permission check pass.
