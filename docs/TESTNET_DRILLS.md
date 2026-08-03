# Testnet drills (Stage 4)

Run these on the testnet VM over 2–4 weeks. The goal is to see the safety
machinery *actually fire* against a live exchange — something the ~30 USDT
paper account almost never triggers. Record each outcome on
`docs/LIVE_TRADING_CHECKLIST.md`.

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

- [ ] **Entries actually happen** — `report performance` shows opened trades.
- [ ] **Stop-loss exits work** — the resting `STOP_LOSS_LIMIT` fills, or the
      software monitor escalates to a market exit on a gap-through. Confirm a
      position closes on a downward move.
- [ ] **Daily-loss brake** (`max_daily_loss_pct: 2`) — after enough intraday
      loss, new entries stop for the day.
- [ ] **Consecutive-loss pause** (`pause_after_consecutive_losses: 3`) — three
      losing trades → trading pauses; you get a critical alert.
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
