"""Binance adapter error taxonomy is explicit and sanitized."""

import pytest
import requests

from trading_bot.config import constants as C
from trading_bot.core.enums import EndpointEnvironment, Mode, OrderType, Side
from trading_bot.core.models import OrderRequest
from trading_bot.core.types import dec
from trading_bot.exchange.binance import BinanceAdapter, validate_endpoint
from trading_bot.exchange.errors import (
    EndpointMismatchError,
    ExchangeAuthError,
    ExchangeUnavailable,
    OrderStateUnknownError,
)
from trading_bot.security.secrets import StaticSecretProvider

CREDS = {
    C.ENV_TESTNET_KEY: "testnet-key-0123456789abcdef",
    C.ENV_TESTNET_SECRET: "testnet-secret-0123456789abcdef",
}


def adapter_with(transport):
    return BinanceAdapter(Mode.TESTNET, StaticSecretProvider(CREDS), transport=transport)


def test_binance_2013_query_order_returns_none():
    def transport(method, url, headers, params, timeout):
        return 400, {"code": -2013, "msg": "Order does not exist."}

    adapter = adapter_with(transport)
    assert adapter.query_order("BTCUSDT", "missing-client-id") is None


def test_5xx_response_is_exchange_unavailable():
    def transport(method, url, headers, params, timeout):
        return 503, {"code": -1000, "msg": "busy"}

    adapter = adapter_with(transport)
    with pytest.raises(ExchangeUnavailable):
        adapter.query_order("BTCUSDT", "client-id")


def test_timeout_on_order_submission_is_unknown_state():
    def transport(method, url, headers, params, timeout):
        raise requests.Timeout("socket timed out")

    adapter = adapter_with(transport)
    req = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=dec("0.001"),
        client_order_id="tb-en-timeout",
    )
    with pytest.raises(OrderStateUnknownError):
        adapter.create_order(req)


def test_auth_failure_remains_distinct_and_sanitized():
    def transport(method, url, headers, params, timeout):
        return 401, {"code": -2015, "msg": "Invalid API-key"}

    adapter = adapter_with(transport)
    with pytest.raises(ExchangeAuthError) as exc:
        adapter.get_balances()
    text = str(exc.value)
    assert "auth failure" in text
    assert CREDS[C.ENV_TESTNET_KEY] not in text
    assert CREDS[C.ENV_TESTNET_SECRET] not in text
    assert "signature=" not in text


def test_endpoint_url_formatting_cannot_bypass_environment():
    assert (
        validate_endpoint(EndpointEnvironment.TESTNET, C.BINANCE_TESTNET_BASE_URL + "/")
        == C.BINANCE_TESTNET_BASE_URL
    )
    with pytest.raises(EndpointMismatchError):
        validate_endpoint(EndpointEnvironment.TESTNET, C.BINANCE_TESTNET_BASE_URL + "/api/v3")
    with pytest.raises(EndpointMismatchError):
        validate_endpoint(EndpointEnvironment.TESTNET, C.BINANCE_LIVE_BASE_URL + "/")
