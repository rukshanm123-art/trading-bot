"""Exchange adapter interface + clock abstraction.

Every interaction with any exchange goes through this interface. There is
deliberately no withdraw/transfer method anywhere in the type — the capability
does not exist in this codebase.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Protocol

from trading_bot.core.models import (
    AssetBalance,
    Candle,
    OrderRequest,
    OrderResponse,
    PriceQuote,
    SymbolRules,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test/backtest clock."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        self._now = ts

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)


class ExchangeAdapter(ABC):
    """Order + account operations. Implementations: BinanceAdapter, PaperExchange."""

    kind: str  # "paper" | "testnet" | "live"

    @abstractmethod
    def server_time(self) -> datetime: ...

    @abstractmethod
    def get_rules(self, symbol: str) -> SymbolRules: ...

    @abstractmethod
    def get_balances(self) -> dict[str, AssetBalance]: ...

    @abstractmethod
    def get_price(self, symbol: str) -> PriceQuote: ...

    @abstractmethod
    def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]: ...

    @abstractmethod
    def create_order(self, request: OrderRequest) -> OrderResponse: ...

    @abstractmethod
    def query_order(self, symbol: str, client_order_id: str) -> OrderResponse | None: ...

    @abstractmethod
    def cancel_order(self, symbol: str, client_order_id: str) -> OrderResponse: ...


class MarketDataSource(Protocol):
    """Read-only market data (public endpoints or fixtures)."""

    def get_price(self, symbol: str) -> PriceQuote: ...

    def get_candles(self, symbol: str, interval: str, limit: int = 200) -> list[Candle]: ...
