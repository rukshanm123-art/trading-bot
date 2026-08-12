# Live trading checklist

Live mode is LOCKED. Work through every stage in order. There is no shortcut
and no automatic promotion; the engine re-verifies gates at every startup.

## Stage 0 — accept the premises

- [ ] I can afford to lose 100% of the live balance.
- [ ] I understand fees/spread/minimums dominate a ~30 USDT account and that
      few or zero trades is correct behaviour.
- [ ] I understand a software stop cannot guarantee an exit price.
- [ ] I understand paper/backtest results do not predict live results.

## Stage 1 — engineering gates (verified by `live status`)

- [ ] Before day one, `DATABASE_URL` selects the long-lived PostgreSQL
      qualification database. SQLite paper sessions count as zero days.
- [ ] ≥ 30 calendar days of live-market paper trading on that PostgreSQL
      database (`LIVE_MIN_PAPER_DAYS`)
- [ ] ≥ 300 recorded paper decisions (`LIVE_MIN_PAPER_DECISIONS`)
- [ ] Qualification evidence verifies as live-market paper operation. Backtest,
      historical fixture and offline simulation records count as zero days.
- [ ] ≥ 1 paper daily report and ≥ 1 backtest report in the DB
- [ ] Quality record fresh (<72 h): real Git repository, source/dependency/
      config/test/coverage hashes verified, ≥100 tests collected, 0 failures,
      configured coverage threshold met, all named safety tests present
      (`python -m trading_bot quality run && python -m trading_bot quality verify`)
- [ ] Risk limits explicitly present in the live config file
- [ ] `DATABASE_URL` still selects the tested qualification PostgreSQL
      deployment; its backup and restoration have been demonstrated
- [ ] At least one external alert channel (Telegram or email) is enabled and
      has all required environment secrets; console-only alerting is rejected
      by configuration validation and the live gate
- [ ] The read-only external-channel connectivity probe passes, then a real
      critical test notification is received on a separate device

## Stage 2 — behavioural evidence from the paper month

- [ ] The period included at least one meaningful market decline, and the
      daily-loss / consecutive-loss / drawdown brakes engaged correctly
- [ ] Orders were rejected for the RIGHT reasons (review rejected-entry codes)
- [ ] Daily reports reconciled; balances survived at least one restart
- [ ] Stop-loss monitoring exited positions as designed
- [ ] The bot stayed inactive whenever data was unhealthy

## Stage 3 — testnet (2–4 weeks, `config/testnet.yaml`)

- [ ] Testnet keys created at https://testnet.binance.vision, set as
      `BINANCE_TESTNET_*`
- [ ] Final uninterrupted 2-week clock is tied to one deployed commit. Restart
      it after trading-loop, risk, execution/gateway, exit-management, or
      accounting changes; research/docs/wording/watchdog/test-only changes do
      not reset it.
- [x] Consecutive-loss, cooldown, and daily-entry-cap brakes engaged and
      blocked entries for the recorded reason codes.
- [ ] Dedicated consecutive-loss acknowledgement recovery drill passed,
      including dust, idempotency, restart, and database restore.
- [ ] Daily-loss and max-drawdown brakes engaged and blocked new entries.
- [ ] Normal operation for 2+ weeks, then deliberate failure drills:
  - [ ] disconnect the network mid-session (expect EXCHANGE_UNAVAILABLE,
        no crash, entries pause)
  - [ ] kill the process during an order; restart (expect UNKNOWN →
        reconcile → no duplicate)
  - [ ] restart with a pending order (expect client-order-id recovery)
  - [ ] trip every kill switch (CLI, env, DB flag, STOP_TRADING file,
        circuit breaker) and reset each
  - [ ] start with invalid credentials (expect refusal, no fallback)
  - [ ] skew the host clock ±30 s (expect offset compensation)
  - [ ] let the DAILY_APPROVAL window expire (expect entries stop)

## Stage 4 — live key hygiene (docs/API_KEY_SETUP.md)

- [ ] Dedicated API key: spot trading ENABLED, withdrawals DISABLED,
      IP allowlisted to the bot host
- [ ] Key set as `BINANCE_LIVE_*` (never in files); old keys revoked
- [ ] `live status` shows every prerequisite PASS
- [ ] Send a real test notification and verify it arrives on a separate device

## Stage 5 — unlock ceremony

- [ ] `python -m trading_bot live unlock` from an interactive terminal;
      read the displayed risk summary; type the random phrase
- [ ] `export LIVE_TRADING_ENABLED=true` (deliberate, per-shell/service)
- [ ] Start with `config/live.locked.yaml` (mode: live). The engine verifies
      key permissions (refuses if withdrawals are enabled) and every gate
      again.

## Stage 6 — first live weeks

- [ ] Smallest balance you can fully afford to lose
- [ ] DAILY_APPROVAL stays on: review the report and approve every day
- [ ] Manually verify every order against the exchange UI for the first week
- [ ] Any anomaly → `trading-bot stop` first, investigate second
      (docs/INCIDENT_RESPONSE.md)

The checklist is an operator process, not a promise that the software is
profitable or risk-free. See `docs/PRODUCTION_READINESS.md` for the current
code audit and explicit NO-GO conditions.
