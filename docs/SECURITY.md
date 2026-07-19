# Security

## Secrets

- Secrets live ONLY in the process environment (`.env` is a local
  convenience; never committed — `.gitignore` + gitleaks in CI).
- `EnvSecretProvider` registers every fetched value with the global redactor;
  log lines, JSON logs and exception text are scrubbed (value-based +
  pattern-based: `X-MBX-APIKEY`, `signature=`, `password=`, tokens).
- API keys travel in headers, never URLs. Signatures are HMAC-SHA256 over the
  query string, computed in one place (`exchange/binance.py`).
- No secret is ever written to the database, a report, or an audit record.
- Rotation: create the new key on the exchange, update env, restart, revoke
  the old key. See docs/API_KEY_SETUP.md.

## LLM containment

The only AI-adjacent module (`ai/analyst.py`) is a deterministic template
renderer by default. The documented contract for any future LLM provider:
input is structured and already redacted; output is stored/displayed as
text; output is NEVER parsed into actions, config, or orders. News, social
media and model-generated sentiment are never trusted executable data. The
pipeline has no code path from text to order — enforced structurally by the
risk-token + gateway design and tested in `tests/security/`.

## Input validation

- Config: strict pydantic schemas, unknown keys rejected, floats for money
  rejected, hard-cap enforcement, optional sha256 sidecar (tamper check).
- Market data: schema, symbol echo, monotonic timestamps, positivity, jump
  and staleness bounds (`market_data/validation.py`); anything doubtful
  pauses entries.
- Exchange responses: parsed through typed constructors; malformed bodies
  surface as errors, never as silent zeros.

## No dynamic execution

No `eval`, no `exec`, no shell construction from data, no dynamically loaded
strategy code (strategies are a closed registry), no `subprocess` in runtime
code. Enforced by `tests/security/test_code_hygiene.py` and bandit in CI.

## Network surface

- Outbound: exchange REST endpoints only (plus optional SMTP/Telegram if the
  operator enables notifications). Compose notes recommend egress
  restriction at the network layer.
- Inbound: ONE read-only HTTP endpoint (health/metrics), bound to
  `127.0.0.1` by default, optional bearer token (`MONITORING_TOKEN`); POST
  returns 405. There are no browser-based control actions, hence no CSRF
  surface: all control is local CLI.
- Never expose the monitoring port to the public internet; if you must
  scrape it remotely, front it with an authenticated reverse proxy.

## Containers

Non-root user, `read_only: true`, `cap_drop: ALL`,
`no-new-privileges`, tmpfs for `/tmp`, state confined to a named volume.
CI builds the image and runs a paper-mode smoke command only.

## Supply chain

Pinned requirements; `pip-audit --strict` and `bandit` in CI; dependabot-style
updates should bump pins deliberately. The runtime dependency surface is
deliberately small: requests, pydantic, PyYAML, rich.

## Order-flow integrity

- Client order ids are generated at approval time, persisted before
  submission, unique, and reused for queries after restarts.
- Single-use HMAC approval tokens (process-scoped) make risk-bypass and
  replay structurally impossible; attempts are audited
  (`gateway.token_rejected`).
- Paper/backtest fill and slippage randomness is deterministic seeded
  pseudo-randomness for simulation reproducibility only. Authentication,
  approval tokens, signatures and live security decisions use cryptographic
  randomness.
- The append-only audit log is hash-chained; `trading-bot audit verify`
  detects edits or deletions. This is tamper-EVIDENT, not tamper-proof —
  see THREAT_MODEL.md for the DB-admin caveat.

## Live-mode defence in depth

Prerequisites (30 days / 300 decisions / reports / verified quality record) →
interactive typed-phrase ceremony (TTY required) → `LIVE_TRADING_ENABLED=true`
→ endpoint/credential separation checks → key permission verification
(withdrawals must be DISABLED) → DAILY_APPROVAL continuation by default →
five kill switches at runtime.
