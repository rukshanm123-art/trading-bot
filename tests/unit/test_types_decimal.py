"""Decimal money primitives: floats are refused, rounding never rounds up."""

from decimal import Decimal

import pytest

from trading_bot.core.types import (
    MoneyError,
    bps,
    dec,
    is_multiple_of,
    json_dumps,
    pct,
    quantize_down,
)


def test_dec_accepts_strings_and_ints():
    assert dec("1.23") == Decimal("1.23")
    assert dec(7) == Decimal(7)
    assert dec(Decimal("0.1")) == Decimal("0.1")


def test_dec_refuses_floats():
    with pytest.raises(MoneyError):
        dec(0.1)  # type: ignore[arg-type]


def test_dec_refuses_bool_and_garbage():
    with pytest.raises(MoneyError):
        dec(True)  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        dec("not-a-number")
    with pytest.raises(MoneyError):
        dec("NaN")
    with pytest.raises(MoneyError):
        dec("Infinity")


def test_quantize_down_floors_to_step():
    assert quantize_down(Decimal("1.23456789"), Decimal("0.0001")) == Decimal("1.2345")
    assert quantize_down(Decimal("0.999999"), Decimal("0.00001")) == Decimal("0.99999")
    # exact multiples unchanged
    assert quantize_down(Decimal("5"), Decimal("0.5")) == Decimal("5")


def test_quantize_down_never_rounds_up():
    v = Decimal("0.0000199")
    assert quantize_down(v, Decimal("0.00001")) == Decimal("0.00001")
    assert quantize_down(v, Decimal("0.00001")) <= v


def test_quantize_down_rejects_bad_step():
    with pytest.raises(MoneyError):
        quantize_down(Decimal("1"), Decimal("0"))


def test_is_multiple_of():
    assert is_multiple_of(Decimal("0.00015"), Decimal("0.00005"))
    assert not is_multiple_of(Decimal("0.000151"), Decimal("0.00005"))


def test_pct_and_bps():
    assert pct(Decimal("200"), Decimal("2")) == Decimal("4")
    assert bps(Decimal("10000"), Decimal("10")) == Decimal("10")


def test_json_dumps_decimal_lossless():
    assert '"0.10000000000000001"' in json_dumps({"x": Decimal("0.10000000000000001")})
