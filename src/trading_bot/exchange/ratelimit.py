"""Exchange request-weight tracking (Binance X-MBX-USED-WEIGHT-1M header).

The default HTTP transport reports the used weight after every response; the
clients consult ``suggested_delay()`` before each request and sleep briefly
when we approach the exchange limit — backing off BEFORE Binance starts
returning 429/418 (which would trip the API-error circuit breaker).

Custom transports (tests, fixtures) simply never update the tracker, so the
delay stays zero and behaviour is unchanged offline.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

from trading_bot.config import constants as C


class UsedWeightTracker:
    def __init__(
        self,
        limit_per_minute: int = C.RATE_LIMIT_WEIGHT_PER_MINUTE,
        soft_ratio: float = C.RATE_LIMIT_SOFT_RATIO,
        hard_ratio: float = C.RATE_LIMIT_HARD_RATIO,
    ) -> None:
        self.limit = limit_per_minute
        self.soft = int(limit_per_minute * soft_ratio)
        self.hard = int(limit_per_minute * hard_ratio)
        self._lock = threading.Lock()
        self._used = 0
        self._observed_at = 0.0
        self._retry_after_until = 0.0

    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        value = None
        for key, raw in headers.items():
            if key.lower() == "x-mbx-used-weight-1m":
                value = raw
                break
        retry_after = next(
            (raw for key, raw in headers.items() if key.lower() == "retry-after"), None
        )
        with self._lock:
            if value is not None:
                try:
                    self._used = int(str(value).strip())
                    self._observed_at = time.monotonic()
                except ValueError:
                    pass
            if retry_after is not None:
                try:
                    seconds = max(0.0, float(str(retry_after).strip()))
                    self._retry_after_until = max(
                        self._retry_after_until, time.monotonic() + seconds
                    )
                except ValueError:
                    pass

    def update_exchange_limits(self, rate_limits: list[dict[str, Any]]) -> None:
        """Use exchangeInfo's current one-minute REQUEST_WEIGHT limit."""
        for item in rate_limits:
            try:
                is_minute_limit = (
                    item.get("rateLimitType") == "REQUEST_WEIGHT"
                    and item.get("interval") == "MINUTE"
                    and int(item.get("intervalNum", 0)) == 1
                )
            except (TypeError, ValueError):
                continue
            if is_minute_limit:
                try:
                    limit = int(item["limit"])
                except (KeyError, TypeError, ValueError):
                    return
                if limit <= 0:
                    return
                with self._lock:
                    self.limit = limit
                    self.soft = int(limit * C.RATE_LIMIT_SOFT_RATIO)
                    self.hard = int(limit * C.RATE_LIMIT_HARD_RATIO)
                return

    def used_weight(self) -> int:
        with self._lock:
            # The exchange window resets every minute; stale observations
            # must not keep throttling forever.
            if time.monotonic() - self._observed_at > 60:
                return 0
            return self._used

    def suggested_delay(self) -> float:
        used = self.used_weight()
        with self._lock:
            retry_delay = max(0.0, self._retry_after_until - time.monotonic())
        if used >= self.hard:
            return max(C.RATE_LIMIT_HARD_DELAY_S, retry_delay)
        if used >= self.soft:
            return max(C.RATE_LIMIT_SOFT_DELAY_S, retry_delay)
        return retry_delay


TRACKER = UsedWeightTracker()
