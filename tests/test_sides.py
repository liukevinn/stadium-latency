import pytest

from courtside.kalshi.sides import (
    Action,
    BookSide,
    Outcome,
    flip,
    outcome_from_position,
    to_wire,
)


# Mirrors the table at https://docs.kalshi.com/getting_started/order_direction
@pytest.mark.parametrize(
    "outcome,action,price,side,yes_price",
    [
        (Outcome.YES, Action.BUY, 62, BookSide.BID, 62),
        (Outcome.NO, Action.SELL, 38, BookSide.BID, 62),
        (Outcome.NO, Action.BUY, 38, BookSide.ASK, 62),
        (Outcome.YES, Action.SELL, 62, BookSide.ASK, 62),
    ],
)
def test_to_wire(
    outcome: Outcome, action: Action, price: int, side: BookSide, yes_price: int
) -> None:
    w = to_wire(outcome, action, price)
    assert (w.book_side, w.yes_price) == (side, yes_price)


def test_flip() -> None:
    assert flip(62) == 38
    assert flip(flip(17)) == 17
    assert Outcome.YES.other is Outcome.NO


def test_outcome_from_position() -> None:
    assert outcome_from_position(3) is Outcome.YES
    assert outcome_from_position(-0.5) is Outcome.NO
    assert outcome_from_position(0) is None
