"""The one place that maps our trade intent to Kalshi's V2 order direction.

V2 orders (POST /portfolio/events/orders) are always quoted on the YES leg:
    bid = buy YES  (equivalently: sell NO)
    ask = sell YES (equivalently: buy NO)
Source: https://docs.kalshi.com/getting_started/order_direction

The engine thinks in terms of the outcome it is long ("buy NO at 38c"). This module
turns that into (book_side, yes_price). A NO price of p cents is a YES price of
100 - p cents.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def other(self) -> Outcome:
        return Outcome.NO if self is Outcome.YES else Outcome.YES


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"


class BookSide(StrEnum):
    BID = "bid"
    ASK = "ask"


@dataclass(frozen=True, slots=True)
class WireDirection:
    book_side: BookSide
    yes_price: int  # cents


def flip(price: int) -> int:
    """Convert a price from one leg to the other (YES 62c is NO 38c)."""
    return 100 - price


def to_wire(outcome: Outcome, action: Action, price: int) -> WireDirection:
    """Map "action `outcome` at `price` (in that outcome's own cents)" to a V2 direction.

    to_wire(YES, BUY, 62)  -> bid @ 62
    to_wire(NO,  BUY, 38)  -> ask @ 62
    to_wire(YES, SELL, 65) -> ask @ 65
    to_wire(NO,  SELL, 35) -> bid @ 65
    """
    gains_yes = (outcome is Outcome.YES) == (action is Action.BUY)
    yes_price = price if outcome is Outcome.YES else flip(price)
    return WireDirection(BookSide.BID if gains_yes else BookSide.ASK, yes_price)


def outcome_from_position(position: float | int) -> Outcome | None:
    """Kalshi's signed position_fp: positive = long YES, negative = long NO, zero = flat."""
    if position > 0:
        return Outcome.YES
    if position < 0:
        return Outcome.NO
    return None
