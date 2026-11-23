"""The one place that converts between Kalshi wire values and our internal units.

Internal units:
    price   int cents, 1..99           wire: dollar string, e.g. "0.5600"
    count   Decimal contracts           wire: fixed-point string, e.g. "10.00"
    money   Decimal dollars             wire: dollar string, up to 6 decimals

Prices are whole cents because game-winner markets trade on a 1-cent grid. If a
market ever quotes a sub-cent level, parse_price raises instead of rounding
silently (see CLAUDE.md §11).

Counts are Decimal because Kalshi supports fractional contracts (0.01 minimum). Our
own orders are whole contracts, but a fill against someone's fractional resting
order could come back fractional.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

_CENT = Decimal("0.01")


def parse_price(wire: str) -> int:
    """Convert "0.5600" to 56. Raises ValueError on a sub-cent price."""
    cents = Decimal(wire) * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"sub-cent price {wire!r}; this market is not on a 1-cent grid")
    return int(cents)


def format_price(cents: int) -> str:
    """Convert 56 to "0.5600". Only 1..99 are valid order prices."""
    if not 1 <= cents <= 99:
        raise ValueError(f"order price must be 1..99 cents, got {cents}")
    return f"{Decimal(cents) / 100:.4f}"


def parse_count(wire: str) -> Decimal:
    """Convert "10.00" to Decimal("10.00")."""
    return Decimal(wire)


def format_count(count: int | Decimal) -> str:
    """Convert 10 to "10.00". Must be positive and a multiple of 0.01."""
    d = Decimal(count)
    if d <= 0 or d != d.quantize(_CENT):
        raise ValueError(f"count must be a positive multiple of 0.01, got {count}")
    return f"{d:.2f}"


def parse_dollars(wire: str) -> Decimal:
    """Convert a money string such as "0.012345" to Decimal."""
    return Decimal(wire)


def ceil_to(value: Decimal, places: int) -> Decimal:
    """Round up to a fixed number of decimal places."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_CEILING)
