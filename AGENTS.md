# AGENTS.md

## Project Purpose

This repository is a safety-first cryptocurrency spot-trading system for offline
fixture testing, historical backtesting, and live-market paper trading. It is
not a profitability guarantee and is not financial advice.

## Safety Invariants

- LIVE trading must remain locked and disabled by default.
- Never request, store, print, or fabricate real exchange credentials.
- Never place live orders from tests, CI, documentation examples, or default
  commands.
- Spot only: no leverage, futures, margin, borrowing, shorting, withdrawals, or
  transfers in the runtime path.
- Use `Decimal` for money, prices, quantities, fees, and risk calculations.
- A rejected risk decision must never reach order submission.
- No new entry may be accepted unless a conservative protective exit at the stop
  is representable inside exchange minimum-notional rules.
- Partial fills must be persisted and reconciled cumulatively.
- Unknown order state, reconciliation failure, exchange uncertainty, database
  uncertainty, or unsafe market data must fail closed and block new entries.
- A no-trade result is valid and preferable to an unsafe trade.

## Verification Commands

```bash
make lint
make type
make test
make scan
make audit
make secret-scan
make archive-check
python -m trading_bot quality run
python -m trading_bot quality verify
```

## Formatting And Typing

```bash
make fmt
make lint
make type
```

## Safe Runtime Commands

```bash
PYTHONPATH=src .venv/bin/python -m trading_bot --config config/paper.fixture.yaml paper run --cycles 500
PYTHONPATH=src .venv/bin/python -m trading_bot --config config/paper.fixture.yaml backtest run --data data/fixtures/btcusdt_1h.csv
PYTHONPATH=src .venv/bin/python -m trading_bot --config config/paper.fixture.yaml report daily
```

Do not add commands, tests, CI jobs, docs, or automations that enable live mode
or submit real orders. Safety-critical failures must fail closed.
