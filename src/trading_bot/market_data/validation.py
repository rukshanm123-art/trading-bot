"""Market-data quality gates. Trading pauses when any check fails."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from trading_bot.core.models import Candle, PriceQuote
from trading_bot.core.types import HUNDRED, ZERO


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    issues: tuple[str, ...] = field(default_factory=tuple)

    @staticmethod
    def failure(*issues: str) -> ValidationResult:
        return ValidationResult(ok=False, issues=tuple(issues))

    @staticmethod
    def success() -> ValidationResult:
        return ValidationResult(ok=True)


def validate_candles(
    candles: list[Candle],
    symbol: str,
    interval_seconds: int,
    now: datetime,
    max_gap_pct: Decimal,
    grace_seconds: int,
    min_candles: int,
    max_future_seconds: int = 30,
) -> ValidationResult:
    issues: list[str] = []
    if len(candles) < min_candles:
        return ValidationResult.failure(
            f"insufficient candles: {len(candles)} < required {min_candles}"
        )

    interval = timedelta(seconds=interval_seconds)
    prev: Candle | None = None
    for i, c in enumerate(candles):
        if c.symbol != symbol:
            issues.append(f"candle[{i}] wrong symbol {c.symbol} != {symbol}")
            break
        if not c.is_closed:
            issues.append(f"candle[{i}] is not closed (incomplete candles must be dropped)")
        if c.open <= ZERO or c.high <= ZERO or c.low <= ZERO or c.close <= ZERO:
            issues.append(f"candle[{i}] non-positive price")
        if c.volume < ZERO:
            issues.append(f"candle[{i}] negative volume")
        if c.high < c.open:
            issues.append(f"candle[{i}] open above high")
        if c.high < c.close:
            issues.append(f"candle[{i}] close above high")
        if c.high < c.low:
            issues.append(f"candle[{i}] high < low")
        if c.low > c.open:
            issues.append(f"candle[{i}] open below low")
        if c.low > c.close:
            issues.append(f"candle[{i}] close below low")
        if c.open_time > c.close_time:
            issues.append(f"candle[{i}] open_time after close_time")
        if c.open_time > now + timedelta(seconds=max_future_seconds):
            issues.append(f"candle[{i}] future open_time beyond clock skew")
        if c.close_time > now + timedelta(seconds=max_future_seconds):
            issues.append(f"candle[{i}] future close_time beyond clock skew")
        if prev is not None:
            delta = c.open_time - prev.open_time
            if delta != interval:
                if delta <= timedelta(0):
                    issues.append(f"candle[{i}] non-monotonic/duplicate open_time")
                else:
                    issues.append(
                        f"candle[{i}] gap: {delta.total_seconds():.0f}s != {interval_seconds}s"
                    )
            if prev.close > ZERO:
                jump = abs((c.close - prev.close) / prev.close * HUNDRED)
                if jump > max_gap_pct:
                    issues.append(
                        f"candle[{i}] abnormal jump {jump:.2f}% > {max_gap_pct}% "
                        "(possible bad tick or market gap — trading paused)"
                    )
        prev = c
        if len(issues) >= 5:
            break

    last = candles[-1]
    staleness = now - last.close_time
    if staleness < -timedelta(seconds=max_future_seconds):
        issues.append("future candles: last close is beyond maximum clock skew")
    if staleness > timedelta(seconds=interval_seconds + grace_seconds):
        issues.append(
            f"stale candles: last close {staleness.total_seconds():.0f}s ago "
            f"(max {interval_seconds + grace_seconds}s)"
        )

    return ValidationResult(ok=not issues, issues=tuple(issues))


def validate_quote(
    quote: PriceQuote,
    symbol: str,
    now: datetime,
    max_age_seconds: int,
    max_spread_bps: Decimal,
    max_future_seconds: int = 30,
) -> ValidationResult:
    issues: list[str] = []
    if quote.symbol != symbol:
        issues.append(f"quote wrong symbol {quote.symbol} != {symbol}")
    if quote.bid <= ZERO or quote.ask <= ZERO or quote.last <= ZERO:
        issues.append("non-positive bid/ask/last")
    elif quote.ask < quote.bid:
        issues.append("crossed book (ask < bid)")
    else:
        if quote.spread_bps > max_spread_bps:
            issues.append(f"spread {quote.spread_bps:.1f}bps > max {max_spread_bps}bps")
    age = (now - quote.ts).total_seconds()
    if age < -max_future_seconds:
        issues.append(f"future quote: {-age:.0f}s ahead (max skew {max_future_seconds}s)")
    if age > max_age_seconds:
        issues.append(f"stale quote: {age:.0f}s old (max {max_age_seconds}s)")
    return ValidationResult(ok=not issues, issues=tuple(issues))
