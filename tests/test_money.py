from __future__ import annotations

import pytest

from moneyhub.money import from_cents, to_cents


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12.34", 1234),
        ("-12.34", -1234),
        ("0", 0),
        ("0.005", 1),  # half-up rounding
        ("2.675", 268),
        ("$1,234.56", 123456),
        ("(45.10)", -4510),  # accounting-style negative
        ("  19.99 ", 1999),
        (12.34, 1234),
        (5315.72, 531572),
        (100, 10000),
    ],
)
def test_to_cents(value, expected):
    assert to_cents(value) == expected


@pytest.mark.parametrize("bad", ["", "abc", "12.3.4", None, "$"])
def test_to_cents_rejects_garbage(bad):
    with pytest.raises((ValueError, TypeError)):
        to_cents(bad)


@pytest.mark.parametrize(
    ("cents", "expected"),
    [
        (1234, "12.34"),
        (-1234, "-12.34"),
        (0, "0.00"),
        (5, "0.05"),
        (-5, "-0.05"),
        (100000, "1000.00"),
    ],
)
def test_from_cents(cents, expected):
    assert from_cents(cents) == expected


@pytest.mark.parametrize("cents", [0, 1, -1, 99, -12345, 531572])
def test_round_trip(cents):
    assert to_cents(from_cents(cents)) == cents
