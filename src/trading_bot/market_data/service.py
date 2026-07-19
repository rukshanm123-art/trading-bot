"""Market data service: fetch + validate, fail closed on any doubt."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from trading_bot.config.models import AppConfig
from trading_bot.core.models import Candle, PriceQuote
from trading_bot.exchange.errors import DataUnavailableError, ExchangeUnavailable
from trading_bot.exchange.interface import Clock, MarketDataSource
from trading_bot.market_data.validation import (
    ValidationResult,
    validate_candles,
    validate_quote,
)

log = logging.getLogger(__name__)


@dataclass
class FeedHealth:
    consecutive_failures: int = 0
    recovery_successes: int = 0
    last_success_ts: datetime | None = None
    last_failure_ts: datetime | None = None
    last_error_category: str = ""
    circuit_open: bool = False


class MarketDataService:
    def __init__(
        self,
        source: MarketDataSource,
        cfg: AppConfig,
        clock: Clock,
        min_candles: int,
    ) -> None:
        self.source = source
        self.cfg = cfg
        self.clock = clock
        self.min_candles = min_candles
        self.last_ok_quote: PriceQuote | None = None
        self.candle_health = FeedHealth()
        self.quote_health = FeedHealth()

    @property
    def consecutive_failures(self) -> int:
        return max(
            self.candle_health.consecutive_failures,
            self.quote_health.consecutive_failures,
        )

    def required_feeds_healthy(self) -> bool:
        return not self.candle_health.circuit_open and not self.quote_health.circuit_open

    def _success(self, health: FeedHealth) -> None:
        now = self.clock.now()
        health.last_success_ts = now
        health.recovery_successes += 1
        if health.recovery_successes >= self.cfg.risk.market_data_recovery_successes:
            health.consecutive_failures = 0
            health.last_error_category = ""
            health.circuit_open = False

    def _failure(self, health: FeedHealth, category: str) -> None:
        health.consecutive_failures += 1
        health.recovery_successes = 0
        health.last_failure_ts = self.clock.now()
        health.last_error_category = category
        if health.consecutive_failures >= 3:
            health.circuit_open = True

    def closed_candles(self, limit: int = 300) -> tuple[list[Candle], ValidationResult]:
        try:
            raw = self.source.get_candles(self.cfg.symbol, self.cfg.interval, limit=limit)
        except ExchangeUnavailable as exc:
            self._failure(self.candle_health, "fetch_failed")
            return [], ValidationResult.failure(f"candle fetch failed: {exc}")
        candles = [c for c in raw if c.is_closed]  # NEVER act on an incomplete candle
        result = validate_candles(
            candles,
            symbol=self.cfg.symbol,
            interval_seconds=self.cfg.interval_seconds,
            now=self.clock.now(),
            max_gap_pct=self.cfg.risk.max_gap_pct,
            grace_seconds=self.cfg.risk.candle_grace_s,
            min_candles=self.min_candles,
            max_future_seconds=self.cfg.risk.max_clock_skew_s,
        )
        if result.ok:
            self._success(self.candle_health)
        else:
            category = "validation_failed"
            if any("stale" in i for i in result.issues):
                category = "stale"
            elif any("future" in i for i in result.issues):
                category = "future_timestamp"
            self._failure(self.candle_health, category)
            log.warning("candle validation failed: %s", "; ".join(result.issues))
        return candles, result

    def quote(self) -> tuple[PriceQuote | None, ValidationResult]:
        try:
            q = self.source.get_price(self.cfg.symbol)
        except ExchangeUnavailable as exc:
            self._failure(self.quote_health, "fetch_failed")
            return None, ValidationResult.failure(f"quote fetch failed: {exc}")
        result = validate_quote(
            q,
            symbol=self.cfg.symbol,
            now=self.clock.now(),
            max_age_seconds=self.cfg.risk.max_quote_age_s,
            max_spread_bps=self.cfg.risk.max_spread_bps,
            max_future_seconds=self.cfg.risk.max_clock_skew_s,
        )
        if result.ok:
            self.last_ok_quote = q
            self._success(self.quote_health)
        else:
            category = "validation_failed"
            if any("stale" in i for i in result.issues):
                category = "stale"
            elif any("future" in i for i in result.issues):
                category = "future_timestamp"
            elif any("spread" in i for i in result.issues):
                category = "spread"
            self._failure(self.quote_health, category)
            log.warning("quote validation failed: %s", "; ".join(result.issues))
        return q, result

    def require_quote(self) -> PriceQuote:
        q, result = self.quote()
        if q is None or not result.ok:
            raise DataUnavailableError("; ".join(result.issues) or "no quote")
        return q
