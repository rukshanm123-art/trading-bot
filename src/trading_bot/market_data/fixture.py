"""Fixture (CSV) market data source for offline paper runs, tests and backtests.

CSV columns: open_time_iso,open,high,low,close,volume
The cursor makes the fixture behave like a live feed: get_candles returns
history up to the cursor; advance() moves time forward one candle.
"""

from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path

from trading_bot.config.models import VALID_INTERVALS
from trading_bot.core.models import Candle, PriceQuote, parse_iso
from trading_bot.core.types import BPS_DENOM, dec


class FixtureDataSource:
    def __init__(
        self,
        path: str | Path,
        symbol: str,
        interval: str,
        spread_bps: str = "5",
        start_index: int | None = None,
    ) -> None:
        self.symbol = symbol
        self.interval = interval
        self.spread_bps = dec(spread_bps)
        self.candles: list[Candle] = []
        interval_s = VALID_INTERVALS[interval]
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                open_time = parse_iso(row["open_time"])
                self.candles.append(
                    Candle(
                        symbol=symbol,
                        interval=interval,
                        open_time=open_time,
                        close_time=open_time + timedelta(seconds=interval_s - 1),
                        open=dec(row["open"]),
                        high=dec(row["high"]),
                        low=dec(row["low"]),
                        close=dec(row["close"]),
                        volume=dec(row["volume"]),
                        is_closed=True,
                    )
                )
        if not self.candles:
            raise ValueError(f"fixture {path} contains no candles")
        # cursor = index of the latest candle considered "closed now"
        self.cursor = len(self.candles) - 1 if start_index is None else start_index

    # ------------------------------------------------------------------
    @property
    def current(self) -> Candle:
        return self.candles[self.cursor]

    def advance(self) -> bool:
        if self.cursor + 1 >= len(self.candles):
            return False
        self.cursor += 1
        return True

    def now(self):
        """Simulated wall-clock: just after the current candle closes."""
        return self.current.close_time + timedelta(seconds=1)

    # ------------------------------------------------- MarketDataSource
    def get_price(self, symbol: str) -> PriceQuote:
        c = self.current
        # Replays expose an execution quote for the NEXT candle's open. The
        # strategy only receives candles before this one, preventing same-bar
        # fills in backtests and fixture paper runs.
        ref = c.open
        half = ref * self.spread_bps / 2 / BPS_DENOM
        return PriceQuote(symbol=symbol, bid=ref - half, ask=ref + half, last=ref, ts=self.now())

    def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]:
        start = max(0, self.cursor + 1 - limit)
        return self.candles[start : self.cursor + 1]
