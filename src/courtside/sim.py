"""A fake exchange, market view and clock for testing the engine without Kalshi.

FakeExchange matches IOC orders against a real OrderBook (same YES-bid/NO-bid
layout as Kalshi), keeps a signed YES position, enforces client_order_id
uniqueness and reduce_only, and lets tests inject failures. Resting orders don't
match automatically: a test fills them with `fill_resting()`, which returns the
FillEvent the user WS would deliver. That lets a test deliver it late or never.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from courtside.book import BookHistory, OrderBook, Top
from courtside.engine import BookUpdate, Event, Scheduler
from courtside.exchange import Execution, FillEvent, OrderRequest, own_price
from courtside.fees import trade_fee
from courtside.kalshi.rest import KalshiError
from courtside.kalshi.sides import BookSide, flip, to_wire

_ZERO = Decimal(0)


@dataclass
class Resting:
    req: OrderRequest
    side: BookSide
    yes_price: int
    remaining: Decimal


class FakeExchange:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.book = OrderBook(ticker)
        self.book.valid = True
        self.position_yes = _ZERO  # signed: + YES, - NO
        self.resting: dict[str, Resting] = {}
        self.placed: list[OrderRequest] = []
        self.cancel_all_calls = 0
        self.fail: dict[str, list[Exception]] = {}  # method name -> errors to raise next
        self.paused = False
        self._coids: set[str] = set()
        self._ids = itertools.count(1)
        self.clock: Callable[[], int] = lambda: 0

    # ---- test helpers ----------------------------------------------------------

    def set_book(self, yes_bid: int | None, yes_ask: int | None, size: int = 100) -> None:
        """Replace the book with one level per side (YES bid, and NO bid = 100 - YES ask)."""
        self.book.yes = {yes_bid: Decimal(size)} if yes_bid is not None else {}
        self.book.no = {flip(yes_ask): Decimal(size)} if yes_ask is not None else {}

    def inject(self, method: str, *errors: Exception) -> None:
        self.fail.setdefault(method, []).extend(errors)

    def fill_resting(self, order_id: str, qty: int | Decimal | None = None) -> FillEvent:
        """Fill a resting order (as if someone traded against it). Returns the WS event."""
        r = self.resting[order_id]
        q = r.remaining if qty is None else min(Decimal(qty), r.remaining)
        r.remaining -= q
        if r.remaining == 0:
            del self.resting[order_id]
        self.position_yes += q if r.side is BookSide.BID else -q
        trade_id = f"t{next(self._ids)}"
        return FillEvent(trade_id, order_id, q, Decimal(r.yes_price), _ZERO, False, self.clock())

    # ---- Exchange protocol -------------------------------------------------------

    async def place(self, req: OrderRequest) -> Execution:
        self._maybe_fail("place")
        self.placed.append(req)
        if req.client_order_id in self._coids:
            raise KalshiError(409, "order_already_exists", "order already exists", "")
        self._coids.add(req.client_order_id)
        if self.paused:
            raise KalshiError(409, "trading_is_paused", "trading is paused", "")
        wire = to_wire(req.outcome, req.action, req.price)
        count = req.count
        if req.reduce_only:
            reducing = (wire.book_side is BookSide.BID and self.position_yes < 0) or (
                wire.book_side is BookSide.ASK and self.position_yes > 0
            )
            count = min(count, abs(self.position_yes)) if reducing else _ZERO
        if req.post_only and self._crosses(wire.book_side, wire.yes_price):
            raise KalshiError(400, "post_only_cross", "post only order would cross", "")

        filled, cost, fee = self._match(wire.book_side, wire.yes_price, count)
        self.position_yes += filled if wire.book_side is BookSide.BID else -filled
        order_id = f"o{next(self._ids)}"
        remaining = count - filled
        if not req.ioc and remaining > 0:
            self.resting[order_id] = Resting(req, wire.book_side, wire.yes_price, remaining)
        avg = own_price(req.outcome, cost / filled) if filled else None
        return Execution(order_id, filled, remaining if not req.ioc else _ZERO, avg, fee, 0)

    async def cancel(self, order_id: str, ticker: str) -> Decimal:
        self._maybe_fail("cancel")
        r = self.resting.pop(order_id, None)
        if r is None:
            raise KalshiError(404, "not_found", "not found", "")
        return r.remaining

    async def cancel_all(self) -> None:
        self._maybe_fail("cancel_all")
        self.cancel_all_calls += 1
        self.resting.clear()

    async def position(self, ticker: str) -> Decimal:
        self._maybe_fail("position")
        return self.position_yes

    async def quote(self, ticker: str, t_ms: int) -> Top:
        self._maybe_fail("quote")
        return self.book.top(t_ms)

    # ---- internals -------------------------------------------------------------

    def _maybe_fail(self, method: str) -> None:
        errs = self.fail.get(method)
        if errs:
            raise errs.pop(0)

    def _crosses(self, side: BookSide, yes_price: int) -> bool:
        top = self.book.top(0)
        if side is BookSide.BID:
            return top.yes_ask is not None and yes_price >= top.yes_ask
        return top.yes_bid is not None and yes_price <= top.yes_bid

    def _match(
        self, side: BookSide, yes_limit: int, count: Decimal
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Take liquidity up to the limit. Returns (filled, YES-cents cost, fee dollars)."""
        filled = cost = fee = _ZERO
        if side is BookSide.BID:  # buy YES: lift NO bids, cheapest YES ask first
            levels = self.book.no
            prices = sorted(levels, reverse=True)  # high NO bid = low YES ask
            yes_px = flip
        else:  # sell YES: hit YES bids, highest first
            levels = self.book.yes
            prices = sorted(levels, reverse=True)
            yes_px = _same
        for p in prices:
            px = yes_px(p)
            if (side is BookSide.BID and px > yes_limit) or (
                side is BookSide.ASK and px < yes_limit
            ):
                break
            q = min(count - filled, levels[p])
            if q <= 0:
                break
            levels[p] -= q
            if levels[p] == 0:
                del levels[p]
            filled += q
            cost += q * px
            fee += trade_fee(px, q, is_maker=False)
        return filled, cost, fee


class FakeMarket:
    """MarketView backed by a BookHistory that tests write to directly."""

    def __init__(self, history_ms: int = 120_000) -> None:
        self.history = BookHistory(history_ms)
        self.healthy = True

    def at(self, t_ms: int) -> Top | None:
        return self.history.at(t_ms)

    def latest(self) -> Top | None:
        return self.history.latest()

    def push(self, top: Top) -> BookUpdate:
        self.history.push(top)
        return BookUpdate(top)


class FakeScheduler(Scheduler):
    """Manual clock. `due(until)` advances time and returns timer events in order."""

    def __init__(self, start_ms: int = 1_000_000) -> None:
        self.t = start_ms
        self._timers: list[tuple[int, int, Event]] = []
        self._seq = itertools.count()

    def now_ms(self) -> int:
        return self.t

    def call_at(self, t_ms: int, event: Event) -> None:
        self._timers.append((t_ms, next(self._seq), event))
        self._timers.sort(key=lambda x: (x[0], x[1]))

    def pop_due(self, until_ms: int) -> tuple[int, Event] | None:
        if self._timers and self._timers[0][0] <= until_ms:
            t, _, ev = self._timers.pop(0)
            return t, ev
        return None


def _same(price: int) -> int:
    return price
