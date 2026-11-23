from decimal import Decimal

import pytest

from courtside.fees import trade_fee


def test_taker_fee_at_50c() -> None:
    # 0.07 * 10 * 0.5 * 0.5 = 0.175
    assert trade_fee(50, 10, is_maker=False) == Decimal("0.175000")


def test_fee_symmetric_across_legs() -> None:
    assert trade_fee(30, 7, is_maker=False) == trade_fee(70, 7, is_maker=False)


def test_fee_rounds_up_to_micro_dollars() -> None:
    # 0.07 * 1 * 0.01 * 0.99 = 0.000693 exactly; 0.07*3*0.33*0.67 = 0.046431
    assert trade_fee(1, 1, is_maker=False) == Decimal("0.000693")
    assert trade_fee(33, 3, is_maker=False) == Decimal("0.046431")


def test_maker_fees_by_type() -> None:
    assert trade_fee(50, 10, is_maker=True, fee_type="quadratic") == 0
    assert trade_fee(50, 10, is_maker=True, fee_type="quadratic_with_maker_fees") == Decimal(
        "0.043750"
    )
    assert trade_fee(50, 10, is_maker=True, fee_type="quadratic_with_combo_maker_fees") == Decimal(
        "0.087500"
    )


def test_multiplier() -> None:
    assert trade_fee(50, 10, is_maker=False, fee_multiplier=2) == Decimal("0.350000")


def test_flat_not_modeled() -> None:
    with pytest.raises(NotImplementedError):
        trade_fee(50, 1, is_maker=False, fee_type="flat")
