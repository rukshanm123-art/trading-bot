# Incident response

## First move, always

```bash
python -m trading_bot stop --reason "incident: <one line>"
```

This sets the DB kill flag AND writes the `STOP_TRADING` file. If the CLI is
unavailable: `export TRADING_KILL_SWITCH=true` (restart-proof) or
`touch STOP_TRADING` in the project root. The open position is NOT blindly
liquidated (`hold_and_monitor`); the protective stop keeps being monitored.

## Severity ladder

| Sev | Examples | Response |
|---|---|---|
| 1 | suspected key compromise; unexplained balance change; orders you didn't place | kill switch → **revoke the API key at the exchange immediately** → assess position manually on the exchange UI |
| 2 | UNKNOWN order stuck; reconciliation mismatch; audit chain broken | kill switch → investigate before any reset |
| 3 | repeated API errors; stale data; notifier failures | usually self-paused already (circuit breaker); investigate at leisure |

## Playbooks

### Suspected credential compromise
1. Kill switch. 2. Revoke key at the exchange (don't just disable the bot).
3. Check exchange trade/withdrawal history. 4. Rotate: new key, withdrawals
disabled, IP allowlist, update env. 5. Search logs for the leak vector
(remember logs are redacted — absence of the secret in logs is expected).
6. Post-incident notes; only then reset.

### Unknown / stuck order
1. `trading-bot status` (unknown count). 2. The engine reconciles by client
order id automatically; to force it, restart — startup reconciliation runs
before trading. 3. If the exchange shows the order but the bot can't see it:
cancel manually on the exchange UI, then restart. 4. Entries stay blocked
until unknowns are resolved — never override by editing the DB.

### Reconciliation mismatch
1. Compare `status` balances vs exchange UI. 2. Common benign cause: manual
trading on the same account — don't do that; use a dedicated sub-account.
3. If genuinely unexplained → treat as Sev 1. 4. Only after understanding
the cause: `trading-bot resume --note "<explanation>"`.

If reconciliation itself raises an exception, treat it the same as a mismatch:
the bot records a failed reconciliation, sets the persistent reconciliation
block, marks trading as not permitted and blocks all new entries. A later
market-data success does not clear that block; only a successful reconciliation
does.

### Runaway losses / market crash
The daily-loss, 7-day and drawdown breakers should have halted entries
already. Confirm via `status`, decide about the open position with
`close-position-preview`, and remember: exits remain allowed under the kill
switch if you choose to close manually via policy or the exchange UI.

### Database corruption / host loss
Kill switch (env/file — DB may be unusable) → restore per
docs/OPERATIONS.md → start in PAPER → verify reconciliation → post-incident.

## Reset discipline

A kill switch reset is always manual and logged
(`resume --note`), the env switch can only be cleared in the environment,
and DAILY_APPROVAL still requires a fresh `approve`. Write the incident up
(see docs/examples/incident_report_example.md) before resuming live.
