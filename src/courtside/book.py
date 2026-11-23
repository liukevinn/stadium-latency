"""Order book state and the rolling top-of-book history used to find pre-play prices.

Kalshi books are bids only (CLAUDE.md §11):
    yes levels: bids for YES, in YES cents
    no levels:  bids for NO,  in NO cents (we subscribe with use_yes_price=false)
so best YES ask = 100 - best NO bid, and for the NO outcome everything mirrors.

All times are local wall-clock milliseconds at the moment a message was received.
That is the clock the engine compares tap times against.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from courtside.kalshi.sides import Outcome, flip
from courtside.kalshi.units import parse_count, parse_price

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Top:
    """Best bid/ask on the YES leg at one moment. Prices are cents; None = empty side."""

    t_ms: int
    yes_bid: int | None
    yes_bid_size: Decimal
    yes_ask: int | None
    yes_ask_size: Decimal

    def bid(self, outcome: Outcome) -> int | None:
        """Best price someone will pay us for `outcome` right now."""
        if outcome is Outcome.YES:
            return self.yes_bid
        return None if self.yes_ask is None else flip(self.yes_ask)

    def ask(self, outcome: Outcome) -> int | None:
        """Best price we can buy `outcome` at right now."""
        if outcome is Outcome.YES:
            return self.yes_ask
        return None if self.yes_bid is None else flip(self.yes_bid)

    def same_quote(self, other: Top) -> bool:
        return (
            self.yes_bid == other.yes_bid
            and self.yes_ask == other.yes_ask
            and self.yes_bid_size == other.yes_bid_size
            and self.yes_ask_size == other.yes_ask_size
        )


class OrderBook:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.yes: dict[int, Decimal] = {}  # YES bids: cents -> size
        self.no: dict[int, Decimal] = {}  # NO bids:  cents -> size
        self.valid = False  # False until a snapshot arrives (and again after a seq gap)

    def apply_snapshot(self, msg: dict[str, Any]) -> None:
        self.yes = {parse_price(p): parse_count(q) for p, q in msg.get("yes_dollars_fp") or []}
        self.no = {parse_price(p): parse_count(q) for p, q in msg.get("no_dollars_fp") or []}
        self.valid = True

    def apply_delta(self, msg: dict[str, Any]) -> None:
        levels = self.yes if msg["side"] == "yes" else self.no
        price = parse_price(msg["price_dollars"])
        size = levels.get(price, _ZERO) + parse_count(msg["delta_fp"])
        if size > 0:
            levels[price] = size
        else:
            levels.pop(price, None)

    def top(self, t_ms: int) -> Top:
        yb = max(self.yes) if self.yes else None
        nb = max(self.no) if self.no else None
        return Top(
            t_ms=t_ms,
            yes_bid=yb,
            yes_bid_size=self.yes[yb] if yb is not None else _ZERO,
            yes_ask=flip(nb) if nb is not None else None,
            yes_ask_size=self.no[nb] if nb is not None else _ZERO,
        )


class BookHistory:
    """Top-of-book changes for the last `keep_ms`, queryable by time.

    `at(t)` returns the quote that was live at time t: the last change at or
    before t. It answers "what was the ask 1500 ms before the tap?"
    """

    def __init__(self, keep_ms: int = 120_000) -> None:
        self.keep_ms = keep_ms
        self._times: list[int] = []
        self._tops: list[Top] = []

    def __len__(self) -> int:
        return len(self._tops)

    def push(self, top: Top) -> bool:
        """Record `top` if the quote changed. Returns True if it was recorded."""
        if self._tops and self._tops[-1].same_quote(top):
            return False
        self._times.append(top.t_ms)
        self._tops.append(top)
        self._prune(top.t_ms - self.keep_ms)
        return True

    def latest(self) -> Top | None:
        return self._tops[-1] if self._tops else None

    def at(self, t_ms: int) -> Top | None:
        i = bisect.bisect_right(self._times, t_ms)
        return self._tops[i - 1] if i else None

    def clear(self) -> None:
        self._times.clear()
        self._tops.clear()

    def _prune(self, cutoff_ms: int) -> None:
        # Keep the last entry before the cutoff: it is still the live quote at the cutoff.
        i = bisect.bisect_right(self._times, cutoff_ms) - 1
        if i > 0 and i > len(self._times) // 2:  # trim in batches, not every push
            del self._times[:i]
            del self._tops[:i]
