# Testnet drills (Stage 4)

Run these on the testnet VM over 2–4 weeks. The goal is to see the safety
machinery *actually fire* against a live exchange — something the ~30 USDT
paper account almost never triggers. Record each outcome on
`docs/LIVE_TRADING_CHECKLIST.md`.

The final uninterrupted two-week evidence clock restarts after any change to
the trading loop, risk engine, execution/gateway, exit management, or
accounting. Research-only, documentation, CLI wording, watchdog, and test-only
changes do not restart it. Record the deployed commit and UTC start time rather
than adjudicating changes after the fact. The consecutive-loss recovery change
is a risk-engine change, so its testnet deployment restarts this clock.

Set an alias first so the commands are short:
```bash
cd ~/trading-bot
C="sudo docker compose -f docker-compose.testnet.yml"
CFG="--config config/testnet.yaml"
```

## 0. One-time (and post-reset) account prep — start flat on cash

The Binance testnet faucet funds each account with a basket including **~1 BTC**.
The engine (correctly) fail-closes at startup on base-asset holdings it did not
buy — a live account is funded with clean quote only. So sell the faucet BTC to
USDT once, so the testnet account starts flat like live will:

```bash
# run on the HOST (a fresh 1-BTC faucet balance makes the container fail-closed
# and crash-loop, so you can't `exec` into it — the script reads keys from ./.env)
python3 scripts/testnet_reset_to_cash.py
$C up -d bot          # start / restart the bot once the account is cash
```
> This is an **operator** step — the engine never sells holdings it didn't buy.
> A tiny sub-minNotional BTC remainder (< ~5 USDT) can't be sold; testnet's
> `max_reconciliation_mismatch_quote` (2 USDT) tolerates that untradeable dust.
> **Binance testnet wipes and re-funds balances roughly monthly** — after each
> reset, re-run the command above, then `$C up -d bot`.

---

## A. Failure drills (Stage 4)

### A1 — Clean restart / crash recovery
```bash
$C restart bot
$C logs bot --tail 40 | grep -iE "reconcil|instance lock|startup"
```
**Expect:** startup reconciliation runs, the instance lock is reacquired
(immediately on a graceful stop; within ~120 s if it was hard-killed), no
duplicate orders, position/balance state is unchanged. Run this **with an open
position** too — confirm the position and its resting stop survive the restart.

### A2 — Kill switch via CLI / DB flag
```bash
$C exec bot python -m trading_bot $CFG stop --reason "drill A2"
$C exec bot python -m trading_bot $CFG status   | grep -i kill     # -> active
```
**Expect:** new entries are refused; the protective-stop monitor and exits keep
running (a kill switch must never strand an open position). Then clear it:
```bash
$C exec bot python -m trading_bot $CFG resume
```

### A3 — Kill switch via environment
Add `TRADING_KILL_SWITCH=true` to `.env`, then:
```bash
$C up -d bot
$C exec bot python -m trading_bot $CFG status | grep -i kill        # -> active
```
Revert the line and `up -d bot` again to clear.

### A4 — Exchange unavailable / network loss
```bash
$C exec bot sh -c 'ls'   # confirm reachable, then simulate loss:
sudo docker network disconnect $(docker network ls --filter name=testnet -q | head -1) trading-bot-testnet-bot-1 2>/dev/null || true
```
**Expect:** cycles log `exchange unavailable`, health goes DEGRADED, **entries
are refused fail-closed, and the process does not crash**. Reconnect and confirm
it recovers:
```bash
sudo docker network connect $(docker network ls --filter name=testnet -q | head -1) trading-bot-testnet-bot-1 2>/dev/null || $C up -d bot
```
(If the network names differ, simplest equivalent: `$C stop bot` for a few
minutes, then `$C start bot`, and confirm clean recovery + reconciliation.)

### A5 — Unknown-order / reconciliation integrity
No safe way to force a lost order response on testnet, but verify the guard is
present and reconciliation is clean:
```bash
$C exec bot python -m trading_bot $CFG status | grep -iE "unknown|reconcil"
```
**Expect:** `Unknown orders: 0`, last reconciliation `OK`. If an UNKNOWN order
ever appears, entries must block until it is resolved (that is the designed
behaviour).

---

## B. Behavioural evidence (Stage 2) — needs real trades

Testnet accounts are pre-funded with large fake balances, so the strategy
**will** place orders (unlike the paper account). Watch for these over the
drill period:

- [x] **Entries actually happen** — `report performance` shows opened trades.
- [ ] **Stop-loss exits work** — the resting `STOP_LOSS_LIMIT` fills, or the
      software monitor escalates to a market exit on a gap-through. Confirm a
      position closes on a downward move.
- [ ] **Daily-loss brake** (`max_daily_loss_pct: 2`) — after enough intraday
      loss, new entries stop for the day.
- [x] **Consecutive-loss pause engages** (`pause_after_consecutive_losses: 3`)
      — three losing trades latched the brake and a critical alert was emitted.
- [ ] **Consecutive-loss recovery drill** — after deploying the dedicated
      acknowledgement path, prove early/open-position/unknown-order refusals,
      successful review with dust present, idempotent retry, restart survival,
      and backup/restore survival. **Partial operational pass on 2026-08-13:**
      normal acknowledgement, idempotency, restart, and backup/restore passed;
      Testnet had no dust/open/unknown state to exercise those refusal paths
      against the exchange-backed database (the automated safety tests cover
      them).
- [ ] **Drawdown brake** (`max_drawdown_pct: 8`) — entries stop past the
      drawdown ceiling.
- [ ] **Entries rejected for the RIGHT reasons** — review the logs/report for
      reason codes (min-notional, spread, slippage, cash reserve, etc.).
- [ ] **Daily reports reconcile** and balances survive a restart (A1).

> Brakes only fire when losses occur. Over 2–4 weeks of live testnet price
> action you should see some; if the market is quiet, you can shorten the
> `interval` (e.g. `5m`) in a throwaway testnet config to generate more trades
> and exercise the exits faster. Never carry such a tweak into paper/live.

---

## C. Alerts

Confirm testnet alerts reach you (tagged `[testnet]` so they don't get
confused with the paper daily report):
```bash
$C exec bot python -m trading_bot $CFG notify test
```

---

When every box above is checked and recorded on `LIVE_TRADING_CHECKLIST.md`,
**and** the paper 30-day gate has cleared, you have the behavioural evidence
the live decision needs. Testnet passing is necessary, not sufficient — the
full live gate and unlock ceremony still stand between here and real funds.


---

## Drill run 1 — 2026-08-08 (testnet 161.33.237.78)

Baseline before: equity 74097.01788450, flat, unknown orders 0, reconciliation
OK, 5 trades / 1 win. Every drill's state change was reverted afterwards.

| Drill | Result | Evidence |
|---|---|---|
| A1 restart / crash recovery | PASS | lock reacquired 23:51:17, startup reconciliation OK, equity byte-identical |
| A2 kill switch (CLI) | PASS + 2 defects found | halt active `cli:drill A2`, engine stayed healthy |
| A2 resume | PASS | halt cleared, engine resumed, equity unchanged |
| A3 kill switch (env) | PASS | `env:TRADING_KILL_SWITCH=true`; **`resume` correctly REFUSED to clear it** and reported the blocker |
| A4 exchange unavailable | PASS | `cycle skipped, exchange unavailable (ConnectionError)`, process did NOT crash, decisions resumed 00:45:23 after reconnect, breaker did not false-trip on ~100 s |
| A5 reconciliation / unknown orders | PASS | `Unknown orders: 0`, reconciliation OK |
| C alert channel | PASS | `[SENT] telegram` |

### Defects found (both fixed, both with regression tests)

1. **False backstop claim.** `stop` printed "a STOP_TRADING file was also
   created as an independent backstop" when the read-only rootfs had blocked
   the write — `activate()` swallowed the OSError into a log line the operator
   never sees. An operator would believe two independent halts existed when
   there was one. `activate()` now returns whether the file was written and the
   CLI warns, pointing at the `TRADING_KILL_SWITCH` env alternative.

2. **Silent halt (more serious).** The kill-switch alert sat INSIDE the
   `position is not None` branch, so halting while flat notified nobody. Not
   hypothetical: the 2026-08-05 testnet reset tripped the circuit breaker while
   the bot was flat and it sat halted ~11 h, discovered only by looking. The
   alert now fires on any halt, with a separate flag so the emergency position
   policy still applies once per episode.

### Not yet covered

- [ ] **A1 with an OPEN POSITION** — needs a live position; confirm the
      position and its resting stop survive a restart.
- [ ] **Remaining Section B brake engagement** — daily-loss and drawdown
      brakes still need losing trades to accumulate.

---

## Drill run 2 — 2026-08-12 (testnet observation)

Across 30 entry signals, 5 were approved and 20 were refused by
`CONSECUTIVE_LOSS_PAUSE`; cooldown and max-entries-per-day refusals accounted
for the remaining blocked signals. This is valid live-condition evidence that
the consecutive-loss brake, cooldown, and daily-entry cap engage and that
rejected entries do not reach submission.

The inspection also found that the original streak was derived from all closed
position history and had no supported recovery path: entries were needed to
produce a winning close, but the pause blocked every entry. The corrected
design is a latched manual-review brake with an append-only position watermark
and dedicated acknowledgement command. It intentionally does not time-decay.

The corrected build has not yet been deployed in this record. Deployment must
start a new two-week evidence clock and the acknowledgement recovery drill is
the first required exercise. Remaining Section B evidence is daily-loss and
max-drawdown engagement. A1 with an open position and a protective-stop exit
also remain outstanding.

---

## Drill run 3 — 2026-08-13 (loss-pause recovery deployment)

The corrected risk-engine build was deployed to the Testnet VM from clean
`main` at commit `c72479eaf48797f12d7a041cd26940cc6071b5d1`. The container
stamp records build time `2026-08-13T04:57:46Z`. GitHub Actions run
`31638763314` passed for that exact commit, including PostgreSQL 16 and all
quality/security jobs. A pre-deployment database backup was written to
`var/backups/pre-loss-pause-c72479e.db` before the migration.

Pre-review state was deliberately safe: the bot was flat, with no pending or
unknown orders, current Testnet reconciliation `OK`, and the new brake active
at effective/raw streak `3/3`. The generic `resume` command returned non-zero
and left that latch active, proving it cannot bypass the dedicated review path.

| Check | Result | Evidence |
|---|---|---|
| Fixed build deployed | PASS | image commit `c72479eaf48797f12d7a041cd26940cc6071b5d1`; startup reconciliation OK |
| Generic `resume` cannot clear latch | PASS | command returned 1; effective/raw streak stayed `3/3` |
| Dedicated acknowledgement | PASS | review recorded at `2026-08-13T05:02:56.275573Z`; effective streak became 0 while raw history stayed 3 |
| Idempotent retry | PASS | repeating the identical command left exactly 1 acknowledgement row and 1 matching audit event |
| Audit integrity | PASS | `audit verify` returned `audit chain OK` before and after restart |
| Backup/restore survival | PASS | `post-loss-pause-c72479e.db` contains the single acknowledgement and audit event; audit verification and `PRAGMA integrity_check` both passed |
| Restart survival | PASS | after the conservative stale-heartbeat takeover, container healthy, reconciliation OK at `2026-08-13T05:10:15.433840Z`, effective/raw streak still `0/3`, acknowledgement/audit counts still `1/1` |

The restart exposed an operational characteristic rather than a bypass: Docker
restart did not release the database instance lease immediately, so repeated
starts correctly refused ownership until the approximately 120-second stale
heartbeat window expired. The replacement instance acquired the lock at
`2026-08-13T05:10:15Z`; no lock was deleted or force-cleared.

The authoritative Testnet database had zero dust positions and no open or
unknown-order state during this review. Therefore, this run does **not** claim
exchange-backed proof of acknowledgement with dust or of the unsafe-state
refusals. The clean test suite proves those paths, including acknowledgement
with dust, early/open-position/unknown-order refusal, transactional rollback,
idempotency, and backup restoration. A future naturally occurring suitable
Testnet state can close the remaining operational-evidence wording without
manufacturing exchange state.

The new fixed-build evidence clock starts at
`2026-08-13T05:10:15Z` and reaches two weeks at
`2026-08-27T05:10:15Z`, provided no trading-loop, risk, execution/gateway,
exit-management, or accounting change is deployed. Documentation-only changes
do not restart it. Still outstanding: A1 with an open position, a real
protective-stop exit, daily-loss engagement, and max-drawdown engagement.
LIVE remains **NO-GO**.
