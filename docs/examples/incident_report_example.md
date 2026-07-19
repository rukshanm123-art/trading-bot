# Incident report — UNKNOWN order after network timeout (EXAMPLE)

*This is a worked example of the template in docs/INCIDENT_RESPONSE.md,
based on a simulated testnet drill.*

| Field | Value |
|---|---|
| Incident id | INC-2026-07-03-01 |
| Severity | 2 |
| Mode | testnet |
| Detected | 2026-07-03 14:22 NZST (alert: "order UNKNOWN — entries blocked") |
| Resolved | 2026-07-03 14:41 NZST |
| Operator | rukshan |

## Timeline (NZST)

- **14:21:58** Entry `tb-en-4f9c…` approved and submitted (0.00010 BTC ≈ 6.1 USDT).
- **14:22:04** Socket timeout mid-submission → gateway marked the order
  UNKNOWN, set the entry block, sent a critical alert. No retry attempted
  (by design — the order might exist on the exchange).
- **14:22:04→14:35** Engine kept cycling safely: exits/stop monitoring
  active, all further entries rejected `UNKNOWN_ORDER_PENDING`.
- **14:35:12** Wi-Fi dropout on the host identified and fixed.
- **14:36:40** Scheduled reconciliation queried by client order id: the
  exchange HAD accepted the order (FILLED). State updated
  UNKNOWN → FILLED, position recorded with its protective stop, entry
  block cleared. Audit chain verified.
- **14:41** `status` clean; monitoring green; no operator reset needed.

## Impact

None financial. One entry executed exactly once (no duplicate); entries
paused for 14 minutes; position carried its stop throughout.

## What worked

Persist-then-submit + UNKNOWN state + client-order-id reconciliation did
exactly what they exist for; the kill chain was never needed.

## Follow-ups

- [x] Add host network watchdog (done 2026-07-04)
- [x] Re-run the drill quarterly (added to checklist Stage 3)
