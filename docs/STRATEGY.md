# Strategies

## Design stance

The baseline strategy is deliberately simple and transparent. This project
does not pretend AI predicts markets; the engineering value is in the risk
engine, execution safety and auditability around whatever signal you use.

## Strategy A — EMA trend filter (`ema_trend`, default)

- Completed candles only (the in-progress candle is always dropped).
- Signal: fast EMA (12) vs slow EMA (26), **edge-triggered** — enter long
  only on the candle where fast crosses ABOVE slow; a persisting condition
  never re-signals, so duplicate entries are impossible at the strategy
  level (and blocked again by `POSITION_ALREADY_OPEN` and per-candle
  deduplication at the engine level).
- Exit: fast crosses back below slow; a safety net exits if holding while
  the trend is negative (covers a missed cross across restarts).
- The protective stop (default 2%) is managed by the exit monitor, not the
  strategy.
- Parameters (`fast`, `slow`, `stop_loss_pct`) are config-validated
  (1 < fast < slow ≤ 500; stop within [0.2%, 10%]).

Known behaviour: chop produces whipsaw losses (the cooldown and
consecutive-loss pause exist for exactly this); trends are entered late by
construction. It is a benchmark baseline, not an edge.

## Strategy B — buy and hold (`buy_and_hold`)

Benchmark only: enters once, never exits. Reports also compute the
buy-and-hold comparison analytically from first/last prices, so you never
need to run it to see the comparison.

## Strategy C — no-trade (`no_trade`)

Benchmark only: stays in quote currency; the 0% line every report compares
against.

## Bias controls

- No look-ahead: strategies receive `candles[:k]` and cannot see beyond the
  last closed candle (tested in `tests/unit/test_ema_strategy.py`).
- No trading on stale data: validation gates run before the strategy.
- One decision per candle: engine deduplicates by candle open time,
  persisted across restarts.
- Churn limits: entries/day cap, cooldowns, consecutive-loss pause.
- Parameter honesty: use `backtest run --walk-forward`; only the
  out-of-sample segment is reportable (docs/BACKTESTING.md).

## Adding a strategy

Implement `Strategy.evaluate(candles, has_position) -> SignalDecision`,
register it in `strategies/registry.py` (closed registry — no dynamic
loading), add the name to the config validator, version it (`version` is
recorded on every decision), and ship tests. A strategy can only ever emit
ENTER_LONG / EXIT_LONG / HOLD — sizing and every safety decision stay in the
risk engine.
