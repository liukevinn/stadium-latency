from decimal import Decimal

import pytest

from courtside.kalshi.units import (
    ceil_to,
    format_count,
    format_price,
    parse_count,
    parse_price,
)


@pytest.mark.parametrize("wire,cents", [("0.5600", 56), ("0.01", 1), ("0.99", 99), ("1.0000", 100)])
def test_parse_price(wire: str, cents: int) -> None:
    assert parse_price(wire) == cents


def test_parse_price_rejects_sub_cent() -> None:
    with pytest.raises(ValueError, match="sub-cent"):
        parse_price("0.5650")


@pytest.mark.parametrize("cents,wire", [(56, "0.5600"), (1, "0.0100"), (99, "0.9900")])
def test_format_price(cents: int, wire: str) -> None:
    assert format_price(cents) == wire


@pytest.mark.parametrize("bad", [0, 100, -5])
def test_format_price_range(bad: int) -> None:
    with pytest.raises(ValueError):
        format_price(bad)


def test_price_round_trip() -> None:
    for c in range(1, 100):
        assert parse_price(format_price(c)) == c


def test_counts() -> None:
    assert format_count(10) == "10.00"
    assert format_count(Decimal("0.5")) == "0.50"
    assert parse_count("278.00") == Decimal(278)
    for bad in (0, -1, Decimal("0.001")):
        with pytest.raises(ValueError):
            format_count(bad)


def test_ceil_to() -> None:
    assert ceil_to(Decimal("0.0000001"), 6) == Decimal("0.000001")
    assert ceil_to(Decimal("0.1225"), 2) == Decimal("0.13")
