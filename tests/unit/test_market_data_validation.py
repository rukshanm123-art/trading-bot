"""Market-data quality gates: anything doubtful pauses trading."""

from dataclasses import replace
from datetime import timedelta

from tests.helpers import T0, make_candles, make_quote
from trading_bot.core.types import dec
from trading_bot.market_data.validation import validate_candles, validate_quote

NOW = T0 + timedelta(hours=40)  # just after the last candle of a 40-candle series


def good_candles():
    return make_candles(["100"] * 40)


def test_good_series_passes():
    r = validate_candles(good_candles(), "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert r.ok, r.issues


def test_insufficient_history_fails():
    r = validate_candles(good_candles()[:10], "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert "insufficient" in r.issues[0]


def test_stale_candles_fail():
    r = validate_candles(
        good_candles(), "BTCUSDT", 3600, NOW + timedelta(hours=3), dec("10"), 90, 30
    )
    assert not r.ok
    assert any("stale" in i for i in r.issues)


def test_missing_interval_fails():
    candles = good_candles()
    candles.pop(20)  # hole in the series
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("gap" in i for i in r.issues)


def test_duplicate_candle_fails():
    candles = good_candles()
    candles[21] = candles[20]
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("non-monotonic" in i or "duplicate" in i for i in r.issues)


def test_non_positive_price_fails():
    candles = good_candles()
    candles[5] = replace(candles[5], close=dec("0"))
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("non-positive" in i for i in r.issues)


def test_abnormal_jump_fails():
    closes = ["100"] * 39 + ["115"]  # +15% in one hourly candle
    r = validate_candles(make_candles(closes), "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("jump" in i for i in r.issues)


def test_wrong_symbol_fails():
    r = validate_candles(good_candles(), "ETHUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("wrong symbol" in i for i in r.issues)


def test_incomplete_candle_flagged():
    candles = good_candles()
    candles[-1] = replace(candles[-1], is_closed=False)
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("not closed" in i for i in r.issues)


def test_high_below_low_fails():
    candles = good_candles()
    candles[7] = replace(candles[7], high=dec("90"), low=dec("110"))
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok


def test_close_above_high_fails():
    candles = good_candles()
    candles[7] = replace(candles[7], high=dec("100"), close=dec("101"))
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("close above high" in i for i in r.issues)


def test_open_above_high_fails():
    candles = good_candles()
    candles[7] = replace(candles[7], high=dec("100"), open=dec("101"))
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("open above high" in i for i in r.issues)


def test_close_below_low_fails():
    candles = good_candles()
    candles[7] = replace(candles[7], low=dec("100"), close=dec("99"))
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("close below low" in i for i in r.issues)


def test_open_below_low_fails():
    candles = good_candles()
    candles[7] = replace(candles[7], low=dec("100"), open=dec("99"))
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("open below low" in i for i in r.issues)


def test_negative_volume_fails():
    candles = good_candles()
    candles[7] = replace(candles[7], volume=dec("-1"))
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("negative volume" in i for i in r.issues)


def test_future_candle_fails():
    candles = good_candles()
    future = NOW + timedelta(minutes=5)
    candles[-1] = replace(
        candles[-1],
        open_time=future,
        close_time=future + timedelta(minutes=59),
    )
    r = validate_candles(candles, "BTCUSDT", 3600, NOW, dec("10"), 90, 30)
    assert not r.ok
    assert any("future" in i for i in r.issues)


# ------------------------------------------------------------- quotes
def test_good_quote_passes():
    r = validate_quote(make_quote("100", ts=NOW), "BTCUSDT", NOW, 120, dec("20"))
    assert r.ok


def test_wide_spread_fails():
    r = validate_quote(make_quote("100", spread_bps="40", ts=NOW), "BTCUSDT", NOW, 120, dec("20"))
    assert not r.ok
    assert any("spread" in i for i in r.issues)


def test_stale_quote_fails():
    r = validate_quote(
        make_quote("100", ts=NOW - timedelta(seconds=300)), "BTCUSDT", NOW, 120, dec("20")
    )
    assert not r.ok
    assert any("stale" in i for i in r.issues)


def test_future_quote_fails():
    r = validate_quote(
        make_quote("100", ts=NOW + timedelta(minutes=5)), "BTCUSDT", NOW, 120, dec("20")
    )
    assert not r.ok
    assert any("future quote" in i for i in r.issues)


def test_zero_or_negative_quote_price_fails():
    q = make_quote("100", ts=NOW)
    bad = replace(q, last=dec("0"))
    r = validate_quote(bad, "BTCUSDT", NOW, 120, dec("20"))
    assert not r.ok
    assert any("non-positive" in i for i in r.issues)


def test_crossed_book_fails():
    q = make_quote("100", ts=NOW)
    crossed = replace(q, bid=dec("101"), ask=dec("99"))
    r = validate_quote(crossed, "BTCUSDT", NOW, 120, dec("20"))
    assert not r.ok
    assert any("crossed" in i for i in r.issues)


def test_quote_wrong_symbol_fails():
    r = validate_quote(make_quote("100", ts=NOW), "ETHUSDT", NOW, 120, dec("20"))
    assert not r.ok
