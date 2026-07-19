# API key setup

## Principles

Least privilege, dedicated keys, mode-separated variables, no secrets in
files. The bot REFUSES a live key that can withdraw.

## Testnet (Stage 3 of the checklist)

1. Sign in at https://testnet.binance.vision (GitHub login).
2. Generate an HMAC key pair.
3. Set in the environment (or `.env` for compose):
   ```
   BINANCE_TESTNET_API_KEY=...
   BINANCE_TESTNET_API_SECRET=...
   ```
4. `python -m trading_bot --config config/testnet.yaml run`

Testnet funds are fake and reset periodically. Testnet variables are never
read in live mode and vice versa; using the same key in both slots is a
startup error.

TESTNET mode uses the Spot Testnet endpoint for both signed account/order API
and public market data. PAPER fixture mode constructs no Binance HTTP client.
PAPER exchange-data mode uses public live-market data with simulated balances.

## Live (only after the full checklist)

On the exchange, create a NEW key used by nothing else:

- ✅ Enable Reading
- ✅ Enable Spot & Margin Trading (spot is used; the bot never sends margin
  parameters — enforced by tests)
- ❌ Enable Withdrawals — **must stay OFF.** The bot calls
  `GET /sapi/v1/account/apiRestrictions` at startup and refuses to run if
  withdrawals are enabled.
- ❌ Futures — off.
- 🔒 Restrict access to trusted IPs only → your bot host's static IP.

Set:

```
BINANCE_LIVE_API_KEY=...
BINANCE_LIVE_API_SECRET=...
LIVE_TRADING_ENABLED=true        # the separate, deliberate switch
```

## Handling rules

- Environment only. Never in YAML, code, logs (redacted anyway), tickets or
  chat. `.env` stays untracked.
- Prefer a dedicated exchange sub-account so reconciliation is meaningful
  (manual trades on the same account trigger mismatch blocks by design).
- Rotate quarterly and immediately on any suspicion: create new key →
  update env → restart → revoke old key → verify `status`.
- The key name/label on the exchange should say what it is
  (`trading-bot live, no-withdrawal, IP-locked`).
