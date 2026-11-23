from decimal import Decimal

import pytest

from courtside.fees import trade_fee


def test_taker_fee_at_50c() -> None:
    # 0.07 * 10 * 0.5 * 0.5 = 0.175
    assert trade_fee(50, 10, is_maker=False) == Decimal("0.1750")


def test_matches_demo_fills() -> None:
    # Observed on demo 2026-11-23 (CLAUDE.md §11); the six fills sum to fees_paid 0.1626.
    fills = [
        (37, 1, "0.0164"),
        (36, 1, "0.0162"),
        (36, 1, "0.0162"),
        (37, 1, "0.0164"),
        (37, 3, "0.0490"),
        (36, 3, "0.0484"),
    ]
    for px, n, charged in fills:
        assert trade_fee(px, n, is_maker=False) == Decimal(charged)
    assert sum(trade_fee(px, n, is_maker=False) for px, n, _ in fills) == Decimal("0.1626")


def test_fee_symmetric_across_legs() -> None:
    assert trade_fee(30, 7, is_maker=False) == trade_fee(70, 7, is_maker=False)


def test_fee_rounding() -> None:
    # 0.07 * 1 * 0.01 * 0.99 = 0.000693 -> charged 0.0007; model precision is 6 dp
    assert trade_fee(1, 1, is_maker=False) == Decimal("0.0007")
    assert trade_fee(1, 1, is_maker=False, balance_places=6) == Decimal("0.000693")
    assert trade_fee(1, 1, is_maker=False, balance_places=2) == Decimal("0.01")


def test_maker_fees_by_type() -> None:
    assert trade_fee(50, 10, is_maker=True, fee_type="quadratic") == 0
    assert trade_fee(50, 10, is_maker=True, fee_type="quadratic_with_maker_fees") == Decimal(
        "0.0438"
    )
    assert trade_fee(50, 10, is_maker=True, fee_type="quadratic_with_combo_maker_fees") == Decimal(
        "0.087500"
    )


def test_multiplier() -> None:
    assert trade_fee(50, 10, is_maker=False, fee_multiplier=2) == Decimal("0.3500")


def test_flat_not_modeled() -> None:
    with pytest.raises(NotImplementedError):
        trade_fee(50, 1, is_maker=False, fee_type="flat")
