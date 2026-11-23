"""The one place that computes Kalshi trading fees.

STATUS: taker formula verified on demo on 2026-11-23 (CLAUDE.md §11). Six fills
reconciled exactly with `fees_paid_dollars`. The maker share is still
unverified, but sports game-winner series charge no maker fees. Kalshi's reported
fee is still the source of truth for P&L; use this function for estimates.

What the docs say (CLAUDE.md §11):
  - Series.fee_type is one of quadratic | quadratic_with_maker_fees |
    quadratic_with_combo_maker_fees | flat. Series.fee_multiplier scales the fee.
    Events can override both (fee_type_override, fee_multiplier_override).
  - "combo" maker fees use a 0.5 maker multiplier instead of 0.25.
  - The fee is computed per fill and rounded UP to $0.000001. The amount charged
    is then rounded UP to the account's balance precision: $0.0001 for our
    account, verified. Non-direct members are aligned to $0.01.
  - Canceling an order costs nothing. Maker fees are charged only when a resting
    order fills.

Model:
  - taker fee = 0.07 * fee_multiplier * C * P * (1 - P)          (verified)
  - maker fee = taker fee * maker_share (0.25, or 0.5 for combo)  (unverified)
"""

from __future__ import annotations

from decimal import Decimal

from courtside.kalshi.units import ceil_to

TAKER_COEFF = Decimal("0.07")
BALANCE_PLACES = 4  # our account balance precision ($0.0001), verified on demo

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
    balance_places: int = BALANCE_PLACES,
) -> Decimal:
    """Fee in dollars charged for one fill of `count` contracts at `price_cents` (either leg)."""
    if fee_type not in _MAKER_SHARE:
        raise NotImplementedError(f"fee_type {fee_type!r} is not modeled")
    p = Decimal(price_cents) / 100
    fee = TAKER_COEFF * Decimal(str(fee_multiplier)) * Decimal(count) * p * (1 - p)
    if is_maker:
        fee *= _MAKER_SHARE[fee_type]
    return ceil_to(ceil_to(fee, 6), balance_places)
