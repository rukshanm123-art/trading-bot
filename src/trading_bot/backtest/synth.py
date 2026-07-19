"""Deterministic synthetic OHLCV generation for fixtures and tests.

Regime-switching random walk (trend up / trend down / chop) with bounded
per-candle moves so generated data passes the market-data validators.
Synthetic data is for MECHANICS testing — it says nothing about real-market
profitability (docs/BACKTESTING.md).
"""

from __future__ import annotations

import csv
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path


def generate_rows(
    n: int = 4320,
    seed: int = 7,
    start: datetime | None = None,
    interval_seconds: int = 3600,
    start_price: float = 60_000.0,
) -> list[dict[str, str]]:
    rng = random.Random(seed)
    t = start or datetime(2025, 1, 1, tzinfo=UTC)
    price = start_price
    rows: list[dict[str, str]] = []
    regime_left = 0
    drift, vol = 0.0, 0.004

    for _ in range(n):
        if regime_left <= 0:
            regime = rng.choice(["up", "down", "chop", "chop"])
            regime_left = rng.randint(72, 240)
            if regime == "up":
                drift, vol = 0.0008, 0.005
            elif regime == "down":
                drift, vol = -0.0007, 0.006
            else:
                drift, vol = 0.0, 0.003
        regime_left -= 1

        ret = max(-0.08, min(0.08, rng.gauss(drift, vol)))
        open_p = price
        close_p = max(100.0, open_p * (1 + ret))
        hi = max(open_p, close_p) * (1 + abs(rng.gauss(0, 0.0012)))
        lo = min(open_p, close_p) * (1 - abs(rng.gauss(0, 0.0012)))
        volume = abs(rng.gauss(120, 40)) + 1

        rows.append(
            {
                "open_time": t.isoformat(),
                "open": f"{open_p:.2f}",
                "high": f"{hi:.2f}",
                "low": f"{lo:.2f}",
                "close": f"{close_p:.2f}",
                "volume": f"{volume:.4f}",
            }
        )
        price = close_p
        t += timedelta(seconds=interval_seconds)
    return rows


def write_csv(rows: list[dict[str, str]], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["open_time", "open", "high", "low", "close", "volume"]
        )
        writer.writeheader()
        writer.writerows(rows)
