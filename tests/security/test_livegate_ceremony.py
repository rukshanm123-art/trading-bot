"""Live-gate unlock ceremony + risk summary + binance public-data paths."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import make_config
from trading_bot.config import constants as C
from trading_bot.core.types import dec
from trading_bot.security.livegate import LiveGate
from trading_bot.security.secrets import StaticSecretProvider


def _gate(repos, tmp_path, secrets=None):
    return LiveGate(
        repos,
        make_config(),
        StaticSecretProvider(secrets or {}),
        config_path=None,
        project_root=tmp_path,
    )


def test_unlock_ceremony_full_flow(repos, tmp_path):
    gate = _gate(repos, tmp_path)
    unlock_id, phrase = gate.start_unlock()
    assert len(phrase.split()) == C.LIVE_CONFIRMATION_WORDS
    assert not gate.is_unlocked()
    # wrong phrase rejected
    assert not gate.confirm(unlock_id, "totally wrong phrase words here now")
    assert not gate.is_unlocked()
    # correct phrase accepted
    assert gate.confirm(unlock_id, phrase)
    assert gate.is_unlocked()


def test_confirm_unknown_and_expired_unlock(repos, tmp_path):
    gate = _gate(repos, tmp_path)
    assert not gate.confirm("nonexistent-id", "whatever phrase text here now")
    # expire an unlock by backdating the row
    unlock_id, phrase = gate.start_unlock()
    repos.db.execute(
        "UPDATE live_unlock SET expires_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), unlock_id),
    )
    assert not gate.confirm(unlock_id, phrase)


def test_risk_summary_and_phrase_randomness(repos, tmp_path):
    gate = _gate(repos, tmp_path)
    summary = gate.risk_summary()
    assert summary["symbol"] == "BTCUSDT"
    assert summary["max_daily_loss_pct"] == "2"
    assert summary["continuation_mode"] in ("auto_continue", "daily_approval")
    assert LiveGate.generate_phrase() != LiveGate.generate_phrase()


def test_assert_live_start_allowed_raises_when_locked(repos, tmp_path):
    gate = _gate(repos, tmp_path)
    with pytest.raises(PermissionError, match="LIVE mode locked"):
        gate.assert_live_start_allowed()


def test_quality_gate_prerequisite_reads_record(repos, tmp_path):
    # write a genuine-looking (but incomplete) quality record and ensure the
    # prerequisite surfaces its problems rather than crashing
    qd = tmp_path / "var" / "quality"
    qd.mkdir(parents=True)
    (qd / C.QUALITY_GATE_FILE.split("/")[-1]).write_text(
        json.dumps({"passed": True, "ran_at": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )
    gate = _gate(repos, tmp_path)
    prereqs = {p.name: p for p in gate.prerequisites()}
    assert "test_suite" in prereqs
    assert not prereqs["test_suite"].ok  # incomplete record


# ------------------------------------------------- binance public data paths
def test_binance_public_data_price_and_candles():
    from trading_bot.core.enums import EndpointEnvironment
    from trading_bot.exchange.binance import BinancePublicData

    def transport(method, url, headers, params, timeout):
        if url.endswith("/api/v3/ticker/bookTicker"):
            return 200, {"bidPrice": "99.90", "askPrice": "100.10"}
        if url.endswith("/api/v3/klines"):
            return 200, [
                [
                    1750000000000,
                    "100",
                    "101",
                    "99",
                    "100.5",
                    "12.0",
                    1750003599999,
                    "0",
                    0,
                    "0",
                    "0",
                    "0",
                ]
            ]
        if url.endswith("/api/v3/time"):
            return 200, {"serverTime": 1750000000000}
        return 200, {}

    public = BinancePublicData(EndpointEnvironment.LIVE_PUBLIC, transport=transport)
    quote = public.get_price("BTCUSDT")
    assert quote.bid == dec("99.90") and quote.ask == dec("100.10")
    candles = public.get_candles("BTCUSDT", "1h", limit=1)
    assert candles[0].close == dec("100.5")
    assert public.server_time().year >= 2025


def test_binance_public_data_retries_then_fails():
    from trading_bot.core.enums import EndpointEnvironment
    from trading_bot.exchange.binance import BinancePublicData
    from trading_bot.exchange.errors import ExchangeUnavailable

    calls = {"n": 0}

    def transport(method, url, headers, params, timeout):
        calls["n"] += 1
        return 503, {"msg": "unavailable"}

    errors = []
    public = BinancePublicData(
        EndpointEnvironment.LIVE_PUBLIC,
        transport=transport,
        on_api_error=lambda path, err: errors.append((path, err)),
    )
    with pytest.raises(ExchangeUnavailable):
        public.get_price("BTCUSDT")
    assert calls["n"] > 1  # retried
    assert errors  # error callback fired


def test_endpoint_environment_validation():
    from trading_bot.core.enums import EndpointEnvironment
    from trading_bot.exchange.binance import validate_endpoint
    from trading_bot.exchange.errors import EndpointMismatchError

    assert validate_endpoint(EndpointEnvironment.TESTNET) == C.BINANCE_TESTNET_BASE_URL
    assert validate_endpoint(EndpointEnvironment.LIVE) == C.BINANCE_LIVE_BASE_URL
    with pytest.raises(EndpointMismatchError):
        validate_endpoint(EndpointEnvironment.TESTNET, "https://api.binance.com")
    with pytest.raises(EndpointMismatchError):
        validate_endpoint(EndpointEnvironment.LIVE, "https://evil.example.com/path")
