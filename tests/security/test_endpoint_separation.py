"""Mode / endpoint / credential separation. A config or env mistake must be a
hard startup failure, never a silent fallback."""

import pytest

from tests.conftest import MIGRATIONS
from tests.helpers import make_config, make_trend_rows, write_rows_csv
from trading_bot.config import constants as C
from trading_bot.core.enums import Mode
from trading_bot.engine.trader import TradingEngine
from trading_bot.exchange.binance import BinanceAdapter
from trading_bot.exchange.errors import EndpointMismatchError, ExchangeAuthError
from trading_bot.security.secrets import StaticSecretProvider

TESTNET_CREDS = {
    C.ENV_TESTNET_KEY: "testnet-key-0123456789abcdef",
    C.ENV_TESTNET_SECRET: "testnet-secret-0123456789abcdef",
}
LIVE_CREDS = {
    C.ENV_LIVE_KEY: "live-key-0123456789abcdef",
    C.ENV_LIVE_SECRET: "live-secret-0123456789abcdef",
}


def test_testnet_adapter_refuses_live_url():
    with pytest.raises(EndpointMismatchError):
        BinanceAdapter(
            Mode.TESTNET,
            StaticSecretProvider(TESTNET_CREDS),
            base_url=C.BINANCE_LIVE_BASE_URL,
        )


def test_live_adapter_refuses_testnet_url():
    with pytest.raises(EndpointMismatchError):
        BinanceAdapter(
            Mode.LIVE,
            StaticSecretProvider(LIVE_CREDS),
            base_url=C.BINANCE_TESTNET_BASE_URL,
        )


def test_paper_mode_cannot_construct_signed_adapter():
    with pytest.raises(EndpointMismatchError):
        BinanceAdapter(Mode.PAPER, StaticSecretProvider(TESTNET_CREDS))


def test_missing_credentials_fail_closed():
    with pytest.raises(ExchangeAuthError):
        BinanceAdapter(Mode.TESTNET, StaticSecretProvider({}))


def test_testnet_mode_never_reads_live_keys():
    """Only live vars set -> testnet has no credentials -> refuse to start."""
    with pytest.raises(ExchangeAuthError):
        BinanceAdapter(Mode.TESTNET, StaticSecretProvider(LIVE_CREDS))


def test_same_key_in_both_slots_refused():
    shared = {
        **TESTNET_CREDS,
        C.ENV_LIVE_KEY: TESTNET_CREDS[C.ENV_TESTNET_KEY],
        C.ENV_LIVE_SECRET: "other-secret-0123456789",
    }
    with pytest.raises(EndpointMismatchError):
        BinanceAdapter(Mode.TESTNET, StaticSecretProvider(shared))


def test_all_testnet_requests_hit_testnet_host_only():
    urls: list[str] = []

    def transport(method, url, headers, params, timeout):
        urls.append(url)
        if url.endswith("/api/v3/time"):
            return 200, {"serverTime": 1750000000000}
        if url.endswith("/api/v3/account"):
            return 200, {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}]}
        return 200, {}

    adapter = BinanceAdapter(Mode.TESTNET, StaticSecretProvider(TESTNET_CREDS), transport=transport)
    adapter.sync_clock()
    adapter.get_balances()
    assert urls, "no requests captured"
    for url in urls:
        assert url.startswith(C.BINANCE_TESTNET_BASE_URL), url


def test_signed_request_uses_header_auth_not_url(monkeypatch):
    captured: dict = {}

    def transport(method, url, headers, params, timeout):
        captured["headers"] = dict(headers)
        captured["url"] = url
        return 200, {"balances": []}

    adapter = BinanceAdapter(Mode.TESTNET, StaticSecretProvider(TESTNET_CREDS), transport=transport)
    adapter.get_balances()
    # api key travels in the X-MBX-APIKEY header; the URL path carries no key
    assert captured["headers"]["X-MBX-APIKEY"] == TESTNET_CREDS[C.ENV_TESTNET_KEY]
    assert TESTNET_CREDS[C.ENV_TESTNET_KEY] not in captured["url"]
    assert TESTNET_CREDS[C.ENV_TESTNET_SECRET] not in str(captured)


def test_testnet_engine_never_constructs_live_public_data_client(tmp_path):
    urls: list[str] = []

    def transport(method, url, headers, params, timeout):
        urls.append(url)
        if url.endswith("/api/v3/exchangeInfo"):
            return 200, {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "orderTypes": ["MARKET", "LIMIT"],
                        "filters": [
                            {"filterType": "LOT_SIZE", "minQty": "0.00001", "stepSize": "0.00001"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                        ],
                    }
                ]
            }
        return 200, {"serverTime": 1750000000000}

    cfg = make_config(
        mode="testnet",
        db={"url": f"sqlite:///{tmp_path}/testnet.db"},
        data={"source": "exchange"},
        monitoring={"enabled": False},
    )
    engine = TradingEngine(
        cfg,
        secrets=StaticSecretProvider(TESTNET_CREDS),
        transport=transport,
        migrations_dir=MIGRATIONS,
        project_root=tmp_path,
        close_db_on_shutdown=False,
    )
    assert urls
    assert all(url.startswith(C.BINANCE_TESTNET_BASE_URL) for url in urls)
    engine.db.close()


def test_paper_fixture_mode_performs_no_external_http_calls(tmp_path):
    fixture = write_rows_csv(make_trend_rows([(40, 0.0)], 100.0), tmp_path / "fixture.csv")

    def transport(method, url, headers, params, timeout):
        raise AssertionError(f"unexpected HTTP call to {url}")

    cfg = make_config(
        db={"url": f"sqlite:///{tmp_path}/fixture.db"},
        data={"source": "fixture", "fixture_path": fixture},
    )
    engine = TradingEngine(
        cfg,
        transport=transport,
        migrations_dir=MIGRATIONS,
        project_root=tmp_path,
        close_db_on_shutdown=False,
    )
    assert engine.fixture is not None
    engine.db.close()
