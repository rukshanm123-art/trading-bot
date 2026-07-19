"""Financial primitives.

All money and quantity arithmetic in this codebase uses ``decimal.Decimal``.
Binary floats are rejected at the boundary: constructing a Decimal from a
float raises, so a float can never silently contaminate a financial value.
"""

from __future__ import annotations

import json
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

DecimalLike = Decimal | int | str

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
BPS_DENOM = Decimal("10000")


class MoneyError(ValueError):
    """Raised when a value cannot be safely converted to Decimal."""


def dec(value: DecimalLike) -> Decimal:
    """Convert to Decimal, refusing binary floats outright."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise MoneyError(f"Cannot convert bool to Decimal: {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):  # pragma: no cover - guarded by type hints too
        raise MoneyError(
            f"Refusing to build a financial Decimal from float {value!r}; pass a string."
        )
    if isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation as exc:
            raise MoneyError(f"Invalid decimal literal: {value!r}") from exc
        if not d.is_finite():
            raise MoneyError(f"Non-finite decimal not allowed: {value!r}")
        return d
    raise MoneyError(f"Unsupported type for Decimal conversion: {type(value).__name__}")


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    """Floor ``value`` to the nearest multiple of ``step`` (exchange step-size rule).

    Never rounds up — an order quantity/price must not exceed what risk sizing allowed.
    """
    if step <= ZERO:
        raise MoneyError(f"Step must be positive, got {step}")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def is_multiple_of(value: Decimal, step: Decimal) -> bool:
    if step <= ZERO:
        return False
    return (value % step) == ZERO


def pct(value: Decimal, percent: Decimal) -> Decimal:
    """``percent``% of ``value``."""
    return value * percent / HUNDRED


def bps(value: Decimal, basis_points: Decimal) -> Decimal:
    """``basis_points`` bps of ``value``."""
    return value * basis_points / BPS_DENOM


def pct_change(new: Decimal, old: Decimal) -> Decimal:
    if old == ZERO:
        return ZERO
    return (new - old) / old * HUNDRED


def round_display(value: Decimal, places: int = 8) -> Decimal:
    q = Decimal(1).scaleb(-places)
    return value.quantize(q, rounding=ROUND_HALF_EVEN)


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that serialises Decimals as strings (lossless round-trip)."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, cls=DecimalEncoder, sort_keys=True, default=str)
