# Backtesting

## The one-engine principle

`trading_bot.backtest.engine.run_backtest` does not reimplement trading: it
constructs the SAME `TradingEngine` (strategy, risk engine, sizing, fees,
filters, gateway, accounting) over a CSV fixture, an in-memory database, an
isolated project root and a fixture-driven clock. Whatever the backtest does,
paper and live do — including rejections, cooldowns and circuit breakers.

Execution realism: candle `N` closes, the strategy sees only candles through
`N`, then the replay advances to candle `N+1` and fills against a simulated
quote at `N+1` open with spread + bounded pseudo-random slippage + taker fees
+ simulated rejections/partial fills (all deterministic under the configured
seed). The signal candle close is never reused as the fill price. If the next
candle gaps through a stop, the conservative software-stop model exits at the
subsequent executable quote; this is still a model and cannot guarantee a real
execution price.

## Commands

```bash
python -m trading_bot --config config/paper.fixture.yaml backtest run \
    --data data/fixtures/btcusdt_1h.csv

python -m trading_bot backtest run --data data/fixtures/btcusdt_1h.csv --walk-forward
```

Reports save to `var/reports/backtest-*.json` and register in the DB (the
live gate requires at least one).

## Metrics produced

total/annualised return, buy-and-hold and no-trade benchmarks, max drawdown,
Sharpe, Sortino, Calmar, trade count, win rate, profit factor, expectancy,
average win/loss, fees, exposure time, turnover, decision count. Money is
Decimal end-to-end; the ratio statistics are float (analytics, not
accounting).

## Walk-forward (bias control)

`--walk-forward` splits data 60/20/20:

1. grid over EMA pairs on TRAIN,
2. top-2 by train return judged on VALIDATION,
3. the winner evaluated ONCE on the untouched TEST segment.

Only `out_of_sample_test` is an unbiased estimate; the report says so
explicitly. Never quote train/validation numbers as expected performance —
that is curve fitting.

## Fixtures

`data/fixtures/btcusdt_1h.csv`: 4320 hourly candles (180 days), seeded
regime-switching random walk (`scripts/generate_fixtures.py --seed 7`).
Synthetic data validates MECHANICS (does the stop fire? do breakers engage?)
— it says nothing about real-market edge. For real-data backtests, export
Binance klines to the same CSV schema
(`open_time,open,high,low,close,volume`) and pass that file.

## Sample result (bundled bear fixture, honest numbers)

| metric | value |
|---|---|
| market (buy & hold) | −31.10% |
| strategy | −2.33% |
| trades | 8 (3W/5L), then consecutive-loss pause engaged |
| max drawdown | 6.49% (cap 8%) |
| exposure | 7.8% of the period |

Capital protection worked; nobody got rich. **Backtested results do not
guarantee future performance.**
