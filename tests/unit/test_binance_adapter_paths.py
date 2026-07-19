"""Additional Binance adapter paths with mocked transports only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_bot.config import constants as C
from trading_bot.core.enums import EndpointEnvironment, Mode, OrderState, OrderType, Side
from trading_bot.core.models import AssetBalance, OrderRequest
from trading_bot.core.types import dec
from trading_bot.exchange.binance import BinanceAdapter, BinancePublicData, estimate_equity
from trading_bot.exchange.errors import (
    EndpointMismatchError,
    ExchangeAuthError,
    ExchangeUnavailable,
)
from trading_bot.security.secrets import StaticSecretProvider

TESTNET_CREDS = {
    C.ENV_TESTNET_KEY: "testnet-key-0123456789abcdef",
    C.ENV_TESTNET_SECRET: "testnet-secret-0123456789abcdef",
}
LIVE_CREDS = {
    C.ENV_LIVE_KEY: "live-key-0123456789abcdef",
    C.ENV_LIVE_SECRET: "live-secret-0123456789abcdef",
}


def _exchange_info(symbol: str = "BTCUSDT") -> dict:
    return {
        "symbols": [
            {
                "symbol": symbol,
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "orderTypes": ["MARKET", "LIMIT"],
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.00001", "stepSize": "0.00001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "NOTIONAL", "minNotional": "5"},
                ],
            }
        ]
    }


def _filled_order(client_id: str = "cid") -> dict:
    return {
        "clientOrderId": client_id,
        "orderId": 123,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "status": "FILLED",
        "origQty": "0.1",
        "executedQty": "0.1",
        "cummulativeQuoteQty": "10",
        "transactTime": 1_750_000_000_000,
        "fills": [{"price": "100", "qty": "0.1", "commission": "0.0001", "commissionAsset": "BTC"}],
    }


def test_public_data_rejects_fixture_environment() -> None:
    with pytest.raises(EndpointMismatchError):
        BinancePublicData(EndpointEnvironment.FIXTURE)


def test_public_data_parses_time_rules_quote_and_candles(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        [
            1_750_000_000_000,
            "100",
            "102",
            "99",
            "101",
            "12",
            1_750_003_599_000,
        ]
    ]

    def transport(method, url, headers, params, timeout):
        if url.endswith("/api/v3/time"):
            return 200, {"serverTime": 1_750_000_000_000}
        if url.endswith("/api/v3/exchangeInfo"):
            return 200, _exchange_info()
        if url.endswith("/api/v3/ticker/bookTicker"):
            return 200, {"bidPrice": "99", "askPrice": "101"}
        if url.endswith("/api/v3/klines"):
            return 200, rows
        raise AssertionError(url)

    client = BinancePublicData(EndpointEnvironment.TESTNET, transport=transport)

    assert client.server_time() == datetime.fromtimestamp(1_750_000_000_000 / 1000, tz=UTC)
    rules = client.get_rules("BTCUSDT")
    assert rules.min_notional == dec("5")
    assert client.get_price("BTCUSDT").mid == dec("100")
    candle = client.get_candles("BTCUSDT", "1h", 1)[0]
    assert candle.open == dec("100")
    assert candle.close == dec("101")
    assert candle.is_closed is True


def test_public_data_retries_transient_errors_and_reports_final_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("trading_bot.exchange.binance.time.sleep", lambda _: None)
    calls = 0
    errors: list[tuple[str, str]] = []

    def transient(method, url, headers, params, timeout):
        nonlocal calls
        calls += 1
        return (
            (429, {"code": -1003, "msg": "rate limit"}) if calls == 1 else (200, {"serverTime": 1})
        )

    client = BinancePublicData(
        EndpointEnvironment.TESTNET,
        transport=transient,
        on_api_error=lambda path, err: errors.append((path, err)),
    )
    assert client.server_time() == datetime.fromtimestamp(0.001, tz=UTC)
    assert calls == 2
    assert errors == []

    def bad_request(method, url, headers, params, timeout):
        return 400, {"code": -1121, "msg": "bad symbol"}

    failing = BinancePublicData(
        EndpointEnvironment.TESTNET,
        transport=bad_request,
        on_api_error=lambda path, err: errors.append((path, err)),
    )
    with pytest.raises(ExchangeUnavailable):
        failing.get_price("BAD")
    assert errors[-1] == ("/api/v3/ticker/bookTicker", "http_400")


def test_public_data_missing_symbol_is_order_rejection() -> None:
    def transport(method, url, headers, params, timeout):
        return 200, {"symbols": []}

    client = BinancePublicData(EndpointEnvironment.TESTNET, transport=transport)
    with pytest.raises(Exception, match="Symbol BTCUSDT not found"):
        client.get_rules("BTCUSDT")


def test_signed_adapter_parses_balances_create_query_and_cancel() -> None:
    seen: list[tuple[str, str, dict]] = []

    def transport(method, url, headers, params, timeout):
        seen.append((method, url, dict(params)))
        if url.endswith("/api/v3/account"):
            return 200, {
                "balances": [
                    {"asset": "USDT", "free": "30", "locked": "0"},
                    {"asset": "BTC", "free": "0", "locked": "0"},
                ]
            }
        if url.endswith("/api/v3/order") and method == "POST":
            return 200, _filled_order(params["newClientOrderId"])
        if url.endswith("/api/v3/order") and method == "GET":
            return 200, _filled_order(params["origClientOrderId"])
        if url.endswith("/api/v3/order") and method == "DELETE":
            body = _filled_order(params["origClientOrderId"])
            body["status"] = "CANCELED"
            return 200, body
        raise AssertionError((method, url))

    adapter = BinanceAdapter(Mode.TESTNET, StaticSecretProvider(TESTNET_CREDS), transport=transport)

    balances = adapter.get_balances()
    assert set(balances) == {"USDT"}
    assert balances["USDT"].free == dec("30")

    req = OrderRequest(
        symbol="BTCUSDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=dec("0.1"),
        client_order_id="cid-1",
    )
    created = adapter.create_order(req)
    assert created.state == OrderState.FILLED
    assert created.total_fees_quote_equiv == dec("0.01")
    assert adapter.query_order("BTCUSDT", "cid-1").executed_qty == dec("0.1")
    assert adapter.cancel_order("BTCUSDT", "cid-1").state == OrderState.CANCELLED
    assert all("signature" in params for _, _, params in seen)


def test_limit_order_requires_price_and_sends_time_in_force() -> None:
    captured: dict = {}

    def transport(method, url, headers, params, timeout):
        captured.update(params)
        body = _filled_order(params["newClientOrderId"])
        body["type"] = "LIMIT"
        return 200, body

    adapter = BinanceAdapter(Mode.TESTNET, StaticSecretProvider(TESTNET_CREDS), transport=transport)
    with pytest.raises(Exception, match="LIMIT order requires a price"):
        adapter.create_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                qty=dec("0.1"),
                client_order_id="limit-missing",
            )
        )

    adapter.create_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=dec("0.1"),
            price=dec("100"),
            client_order_id="limit-ok",
        )
    )
    assert captured["price"] == "100"
    assert captured["timeInForce"] == "GTC"


def test_live_key_permission_checks_refuse_withdrawal_or_missing_spot() -> None:
    def withdrawal(method, url, headers, params, timeout):
        return 200, {"enableWithdrawals": True, "enableSpotAndMarginTrading": True}

    adapter = BinanceAdapter(Mode.LIVE, StaticSecretProvider(LIVE_CREDS), transport=withdrawal)
    with pytest.raises(ExchangeAuthError, match="WITHDRAWALS ENABLED"):
        adapter.verify_key_permissions()

    def no_spot(method, url, headers, params, timeout):
        return 200, {"enableWithdrawals": False, "enableSpotAndMarginTrading": False}

    adapter = BinanceAdapter(Mode.LIVE, StaticSecretProvider(LIVE_CREDS), transport=no_spot)
    with pytest.raises(ExchangeAuthError, match="spot trading"):
        adapter.verify_key_permissions()


def test_testnet_key_permission_check_is_noop() -> None:
    def transport(method, url, headers, params, timeout):
        raise AssertionError("testnet permission check should not call sapi")

    adapter = BinanceAdapter(Mode.TESTNET, StaticSecretProvider(TESTNET_CREDS), transport=transport)
    assert adapter.api_restrictions() is None
    adapter.verify_key_permissions()


def test_estimate_equity_values_quote_and_base_assets_only() -> None:
    balances = {
        "USDT": AssetBalance("USDT", dec("30"), dec("2")),
        "BTC": AssetBalance("BTC", dec("0.1"), dec("0.02")),
        "BNB": AssetBalance("BNB", dec("99"), dec("1")),
    }

    assert estimate_equity(balances, "BTC", "USDT", dec("100")) == dec("44")
