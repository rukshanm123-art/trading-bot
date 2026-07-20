# Risk model

The risk engine (`src/trading_bot/risk/engine.py`) is the final authority.
Every entry evaluation runs ALL gates and returns structured reason codes;
approval yields a single-use signed token without which the execution gateway
refuses to submit.

## Hard caps vs configuration

`config/constants.py` defines absolute caps; `config/models.py` rejects any
YAML that tries to exceed them (fail closed at startup). Defaults are the
conservative values; you may only tighten further.

| Control | Default | Hard cap | Reason code |
|---|---|---|---|
| Open positions | 1 | 1 | `POSITION_ALREADY_OPEN` |
| Position allocation | 20% equity | 25% | `ALLOCATION_EXCEEDED` (via sizing) |
| Quote cash reserve | 50% equity | ≥30% | `CASH_RESERVE_BREACH` |
| Risk per trade | 0.5% equity | 1% | `RISK_BUDGET_EXCEEDED` |
| Daily realised loss | 2% of start-of-day equity | 3% | `DAILY_LOSS_LIMIT` |
| Rolling 7-day loss | 5% | 6% | `WEEKLY_LOSS_LIMIT` |
| Max drawdown (peak-to-now) | 8% | 10% | `MAX_DRAWDOWN` |
| Entries per UTC day | 2 | 4 | `MAX_ENTRIES_PER_DAY` |
| Cooldown after losing trade | 12 h | ≥1 h | `COOLDOWN_ACTIVE` |
| Consecutive-loss pause | 3 losses | — | `CONSECUTIVE_LOSS_PAUSE` |
| Max spread | 20 bps | 100 bps | `SPREAD_TOO_WIDE` |
| Max quote age | 120 s | — | `STALE_MARKET_DATA` |
| Single-candle jump | 10% | — | `GAP_TOLERANCE_EXCEEDED` |
| API errors / hour | 10 | — | `API_ERROR_THRESHOLD` |
| Reconciliation mismatch | 0.05 USDT | — | `RECONCILIATION_MISMATCH` |

Plus control gates: `KILL_SWITCH_ACTIVE`, `CIRCUIT_BREAKER_OPEN`,
`TRADING_NOT_APPROVED`, `UNKNOWN_ORDER_PENDING`, `DUPLICATE_SIGNAL`,
`SYMBOL_NOT_TRADING`, `EXCHANGE_UNAVAILABLE`.

## Position sizing (`risk/sizing.py`)

```
stop_price  = entry × (1 − stop_loss_pct)   quantised DOWN to tick
risk_budget = equity × risk_per_trade
qty = min( risk_budget / stop_distance,          # risk cap
           alloc_cap_quote / entry,              # allocation cap
           (quote_free − reserve) / (entry×fee)) # reserve + fee cap
qty = floor(qty, step_size)                      # NEVER round up
reject if qty < minQty or qty×entry < minNotional
reject if qty×stop×(1 − exit_buffer_bps/10000) < minNotional
```

`exit_buffer_bps = taker_fee_bps + max_slippage_bps +
protective_exit_buffer_bps`. The default `protective_exit_buffer_bps` is
100 bps and the hard cap is 500 bps. This extra stop-exit check prevents the
trapped-position failure where the entry satisfies the exchange minimum but the
protective sell at the stop would be rejected as too small. Quantity is never
rounded upward to satisfy the exit minimum; the entry is rejected with
`PROTECTIVE_EXIT_NOT_REPRESENTABLE`.

**Small-account arithmetic (the honest picture).** With 30 USDT equity:
allocation cap = 6 USDT ≥ Binance's 5 USDT minNotional → the bot can trade,
barely, risking ≈0.12–0.15 USDT per trade. With 20 USDT: cap = 4 USDT < 5 →
every entry is rejected with `MIN_NOTIONAL_EXCEEDS_RISK`. The bot reports
this clearly and will never round an order upward to satisfy the exchange.

## Protective exits — two layers

**Layer 1: exchange-native stop (default, `execution.use_native_stops`).**
After every entry fill the engine places a STOP_LOSS_LIMIT sell ON the
exchange: trigger = the invalidation level, limit =
`protective_limit_offset_bps` (default 100) below it. The order rests on the
book, so a dead process or dead host no longer means an unprotected
position. The sizing check guarantees before entry that this order will be
representable using the NET sellable quantity (gross buy − worst-case
base-asset fee, floored to the step size) at a conservatively discounted
stop price — otherwise the entry is rejected with
`PROTECTIVE_EXIT_NOT_REPRESENTABLE`.

**Layer 2: software monitor (escalation backstop).** Every cycle
(`stop_monitor_interval_s`, default 20 s) the engine checks the live bid.
When the stop is breached and the native stop is resting, it waits up to
`protective_escalation_cycles` for the exchange fill; if the market gapped
through the stop's limit price (the one case a stop-limit cannot fill) or
patience runs out, it cancels the native stop (fill-aware: cancellation may
reveal it already filled) and market-sells. Any other exit path cancels the
native stop FIRST, so a double-sell is structurally impossible; a second
concurrent exit is additionally rejected with `EXIT_ORDER_ACTIVE`.

**Still no guarantees.** A stop-limit can go unfilled through a gap and a
market escalation fills at whatever the market offers. Mitigations: gap
detection pauses entries; the daily-loss, 7-day and drawdown breakers cap
cumulative damage; `hold_and_monitor` keeps both layers running under a kill
switch. Residual risk remains and is accepted and documented here.

## Consecutive-loss and cooldown behaviour

After each losing close: a 12 h per-symbol cooldown starts. After 3
consecutive losses: entries stop until a winning state change or manual
review — on the bundled 180-day bear fixture this engaged after 8 trades and
kept the account at −2.3% while the market fell 31%.

## Dust

Exits floor to the exchange step size. Partial exits allocate entry fees
proportionally, record exit fees per fill, and reduce the open quantity. A
remaining residue below `minQty` or below `minNotional` at the exit price is
marked as explicit `dust`, not silently discarded. Reconciliation tolerates
only that documented dust threshold; holdings at or above minNotional without
a recorded open position block trading as unexplained.

## Changing this model

Any change to `constants.py` caps is a code change: it must ship with updated
tests (`tests/unit/test_config.py`, `tests/unit/test_risk_engine.py`) and a
fresh quality record, or the live gate rejects the build.
