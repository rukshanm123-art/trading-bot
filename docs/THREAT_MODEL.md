# Threat model

Scope: single-operator deployment, one exchange account, small balance.
Assets: exchange API credentials, account funds, database integrity,
decision/audit history.

| # | Threat | Vector | Mitigations | Residual risk |
|---|---|---|---|---|
| 1 | Stolen API credentials | leaked .env, logs, repo | env-only secrets; redaction of logs/exceptions; gitleaks in CI; key restricted to spot trade, withdrawals disabled, IP allowlist | attacker with the key + allowlisted IP can still place trades; revoke immediately (INCIDENT_RESPONSE) |
| 2 | Prompt injection → trading action | malicious text reaching an LLM | no LLM in the decision path at all; narrator output never parsed; token+gateway make text→order impossible | negligible by construction |
| 3 | Malicious/corrupt market data | exchange bug, MITM, bad tick | TLS; symbol echo checks; positivity/monotonic/jump/staleness validation; entries pause on any doubt | a plausible-but-wrong price inside bounds could still trigger a bad (size-capped) trade |
| 4 | Compromised dependency | PyPI supply chain | pinned versions; pip-audit strict; tiny dependency set; non-root read-only container | zero-day in a pinned dep until bumped |
| 5 | Log leakage of secrets | exception traces, debug logs | value+pattern redaction on every handler; headers never logged; urllib3 quieted | novel formats could evade patterns; value-registration covers known secrets |
| 6 | Database tampering | direct DB edit | hash-chained audit log (`audit verify`); reconciliation vs exchange truth; alerts on mismatch | a DB admin can rewrite the whole chain; use DB permissions + offsite backups |
| 7 | Replay / duplicate orders | retries, restarts, races | single-use tokens; unique client ids; persist-then-submit; UNKNOWN state blocks entries; idempotent paper/exchange lookup by client id; instance lock | none known for self-inflicted duplicates |
| 8 | Wrong exchange endpoint | config/env mix-up | hard (mode, URL, credential-var) binding; refusal on any mismatch; distinct env var names; same-key detection | — |
| 9 | Unauthenticated control | network access to host | no network control surface exists; monitoring endpoint read-only, loopback, optional bearer token | host compromise = game over (as with any bot) |
| 10 | Container escape | runtime vuln | non-root, cap_drop ALL, no-new-privileges, read-only fs | shared-kernel risks as usual |
| 11 | Accidental live activation | deploy mistake, config typo | live locked behind 8 independent conditions incl. interactive TTY ceremony and env flag; containers ship with `LIVE_TRADING_ENABLED=false`; no auto-promotion code path | deliberate multi-step operator error |
| 12 | Clock drift → signature rejects | host clock wrong | server-time sync with offset compensation; drift logged; recvWindow bounded | extreme drift degrades to EXCHANGE_UNAVAILABLE (fail closed) |
| 13 | Runaway loop / bug spam | logic error | entries/day cap, order-age cancels, API-error circuit breaker, kill-switch latch | capped blast radius |
| 14 | Operator coercion/phishing ("approve this trade") | social | bot never asks for approvals via email/chat; approval is local CLI only; docs state no unauthenticated message is ever approval | — |

Assumed trusted: the host OS, the Python interpreter, the exchange itself.
Out of scope: nation-state attackers, exchange insolvency, market
manipulation.
