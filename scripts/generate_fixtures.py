#!/usr/bin/env python3
"""Generate the deterministic synthetic BTCUSDT fixture used by paper runs,
backtests and tests.

Usage: python scripts/generate_fixtures.py [--n 4320] [--seed 7] [--out data/fixtures/btcusdt_1h.csv]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_bot.backtest.synth import generate_rows, write_csv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4320, help="number of hourly candles (180 days)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="data/fixtures/btcusdt_1h.csv")
    args = ap.parse_args()

    rows = generate_rows(n=args.n, seed=args.seed)
    write_csv(rows, args.out)
    print(f"wrote {len(rows)} candles to {args.out} (seed={args.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
