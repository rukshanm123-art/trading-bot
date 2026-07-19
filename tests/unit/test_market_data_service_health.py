"""Candle and quote feed health are independent."""

from datetime import timedelta

from tests.helpers import T0, FakeQuoteSource, make_config, make_quote
from trading_bot.exchange.errors import ExchangeUnavailable
from trading_bot.exchange.interface import FrozenClock
from trading_bot.market_data.service import MarketDataService

NOW = T0 + timedelta(hours=40)


class FlakySource(FakeQuoteSource):
    def __init__(self):
        super().__init__(price="100", ts=T0)
        self.fail_candles = False
        self.fail_quotes = False

    def get_candles(self, symbol: str, interval: str, limit: int = 200):
        if self.fail_candles:
            raise ExchangeUnavailable("candles down")
        return super().get_candles(symbol, interval, limit)

    def get_price(self, symbol: str):
        if self.fail_quotes:
            raise ExchangeUnavailable("quotes down")
        return make_quote("100", ts=NOW, symbol=symbol)


def service(source: FlakySource) -> MarketDataService:
    clock = FrozenClock(NOW)
    cfg = make_config(risk={"market_data_recovery_successes": 2})
    return MarketDataService(source, cfg, clock, min_candles=30)


def test_candle_failures_do_not_clear_on_quote_success():
    src = FlakySource()
    svc = service(src)
    src.fail_candles = True
    for _ in range(3):
        svc.closed_candles()
        svc.quote()
    assert svc.candle_health.consecutive_failures == 3
    assert svc.candle_health.circuit_open
    assert svc.quote_health.consecutive_failures == 0
    assert not svc.quote_health.circuit_open


def test_quote_failures_do_not_clear_on_candle_success():
    src = FlakySource()
    svc = service(src)
    src.fail_quotes = True
    for _ in range(3):
        svc.closed_candles()
        svc.quote()
    assert svc.quote_health.consecutive_failures == 3
    assert svc.quote_health.circuit_open
    assert svc.candle_health.consecutive_failures == 0
    assert not svc.candle_health.circuit_open


def test_candle_recovery_requires_configured_success_count():
    src = FlakySource()
    svc = service(src)
    src.fail_candles = True
    for _ in range(3):
        svc.closed_candles()
    assert svc.candle_health.circuit_open

    src.fail_candles = False
    svc.closed_candles()
    assert svc.candle_health.circuit_open
    svc.closed_candles()
    assert not svc.candle_health.circuit_open
    assert svc.candle_health.consecutive_failures == 0
