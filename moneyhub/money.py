"""Money amounts are integer cents everywhere. These are the only converters."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_cents(value: str | int | float | Decimal) -> int:
    """Convert a dollar amount to signed integer cents.

    Accepts bank-export formatting: "$1,234.56", "-12.34", "(12.34)" for
    negatives, plain numbers. Raises ValueError on anything non-numeric.
    """
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        value = cleaned
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a money amount: {value!r}") from exc
    return int(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def from_cents(cents: int) -> str:
    """1234 -> '12.34'; -1234 -> '-12.34'."""
    sign = "-" if cents < 0 else ""
    magnitude = abs(cents)
    return f"{sign}{magnitude // 100}.{magnitude % 100:02d}"
