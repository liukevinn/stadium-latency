"""The one place that computes Kalshi trading fees.

STATUS: the formula shape is from Kalshi's docs, but the coefficients are UNVERIFIED.
The fee schedule PDF could not be fetched on 2026-11-23. scripts/probe.py compares
this function against the fees Kalshi actually charges (`average_fee_paid` on each
fill). Treat the exchange-reported fee as the source of truth; use this only for
estimates before an order is sent.

What the docs say (CLAUDE.md §11):
  - Series.fee_type is one of quadratic | quadratic_with_maker_fees |
    quadratic_with_combo_maker_fees | flat. Series.fee_multiplier scales the fee.
    Events can override both (fee_type_override, fee_multiplier_override).
  - "combo" maker fees use a 0.5 maker multiplier instead of 0.25.
  - The fee is computed per fill and rounded UP to $0.000001.
  - Canceling an order costs nothing. Maker fees are charged only when a resting
    order fills.

Assumed (to be checked by the probe):
  - taker fee = 0.07 * fee_multiplier * C * P * (1 - P)
  - maker fee = taker fee * maker_share (0.25, or 0.5 for combo)
"""

from __future__ import annotations

from decimal import Decimal

from courtside.kalshi.units import ceil_to

TAKER_COEFF = Decimal("0.07")  # UNVERIFIED

_MAKER_SHARE: dict[str, Decimal] = {
    "quadratic": Decimal(0),
    "quadratic_with_maker_fees": Decimal("0.25"),
    "quadratic_with_combo_maker_fees": Decimal("0.5"),
}


def trade_fee(
    price_cents: int,
    count: int | Decimal,
    *,
    is_maker: bool,
    fee_type: str = "quadratic",
    fee_multiplier: float | Decimal = 1,
) -> Decimal:
    """Fee in dollars for one fill of `count` contracts at `price_cents` (either leg)."""
    if fee_type not in _MAKER_SHARE:
        raise NotImplementedError(f"fee_type {fee_type!r} is not modeled")
    p = Decimal(price_cents) / 100
    fee = TAKER_COEFF * Decimal(str(fee_multiplier)) * Decimal(count) * p * (1 - p)
    if is_maker:
        fee *= _MAKER_SHARE[fee_type]
    return ceil_to(fee, 6)
