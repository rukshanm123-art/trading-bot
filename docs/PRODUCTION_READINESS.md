# Production readiness and real-trading NO-GO gates

Last reviewed: 2026-07-20.

This document records engineering readiness; it is not financial advice, a
profitability claim, or authorization to trade. LIVE remains locked by default.

## Code controls implemented

- Spot-only, long/flat execution with no withdrawal, transfer, margin, futures,
  borrowing, leverage or short-selling capability.
- Persist-before-submit order intents, unique client order ids and UNKNOWN
  outcome reconciliation. Binance `-1000`, `-1006` and `-1007` order outcomes
  fail closed instead of becoming retryable rejections.
- Atomic entry, exit and dust accounting; cumulative partial fills and replay
  idempotency; explicit tracked dust.
- Exchange-native protective stop-limit orders with software escalation, plus
  conservative stop-level minimum-notional qualification before entry.
- Dynamic LOT_SIZE and MARKET_LOT_SIZE grids, notional bounds, account fee
  lookup, clock-offset compensation, request-weight tracking and Retry-After
  backoff.
- Startup and periodic reconciliation, one-writer lock, deterministic risk
  caps, kill switches, circuit breaker, health/metrics and hash-chained audit.
- LIVE gate requiring qualification evidence, clean/fresh quality evidence,
  explicit risk config, PostgreSQL, external alerts, credentials, environment
  opt-in and an interactive unlock ceremony. Key permissions are checked at
  startup and withdrawals-enabled keys are refused.

The exchange integration is aligned with the current official Binance Spot
[general REST contract](https://developers.binance.com/en/docs/products/spot/rest-api),
[filters](https://developers.binance.com/en/docs/products/spot/filters), and
[error semantics](https://developers.binance.com/en/docs/products/spot/errors).

## Mandatory NO-GO conditions

Do not use real funds while any item below is incomplete:

- [ ] At least 30 distinct days and 300 decisions of signed qualification
      evidence from PAPER using live public market data—not fixtures/backtests.
- [ ] Two to four weeks on Spot Testnet, including every restart, partial-fill,
      network-loss, clock-skew, stale-order and kill-switch drill in
      `LIVE_TRADING_CHECKLIST.md`.
- [ ] A clean reviewed Git commit and fresh quality evidence produced from that
      exact clean tree. Evidence from a dirty development tree cannot unlock
      LIVE.
- [ ] PostgreSQL production deployment tested under restart; automated backup,
      restore and audit-chain verification demonstrated on a disposable copy.
- [ ] Telegram or email enabled, secrets supplied outside the repository, and
      a real critical test alert received on a separate device.
- [ ] Dedicated IP-allowlisted key with spot trading enabled, withdrawals
      disabled, no shared testnet/live value and third-asset fee payment
      disabled or explicitly accounted for.
- [ ] Host time synchronization, log rotation, disk-space monitoring, TLS
      trust-store updates, process supervision and monitoring-token handling
      verified on the actual host.
- [ ] Exchange availability and legal eligibility confirmed for the operator's
      jurisdiction; tax and recordkeeping obligations understood.
- [ ] First-live-session runbook reviewed by a second person, including the
      kill command, exchange-side order cancellation and credential revocation.

## Known residual risks

- Stop-limit orders can gap through and remain unfilled. The software escalates
  to a market exit, but no execution price is guaranteed.
- REST polling is the authoritative recovery path. Binance user-data streams
  can reduce notification latency, but they cannot replace REST reconciliation;
  this release does not depend on a WebSocket stream for safety.
- Exchange insolvency, account takeover from a compromised trusted host,
  plausible-but-wrong exchange data, network partitions and severe market gaps
  cannot be eliminated by local software.
- The EMA strategy is a benchmark and has no demonstrated durable edge.

The operational decision is therefore **NO-GO** until every mandatory item is
independently evidenced and `live status` reports all gates passing. Do not
weaken a gate to make the status green.
