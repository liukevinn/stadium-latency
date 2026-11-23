"""What the engine needs from an exchange, plus the real Kalshi implementation.

The engine thinks only in its own outcome's terms ("sell NO at 41c"). This layer
does the YES-leg translation through kalshi.sides, so prices the engine sees are
always for the outcome it holds.

Fills:
  - IOC orders: the fill comes back in the order response (Execution).
  - Resting (passive) orders: fills arrive later, from the user `fill` WebSocket,
    as FillEvents passed to the engine. A cancel response reports how many
    contracts were still resting, which lets the engine account for fills whose
    WS message hasn't arrived yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from courtside.book import Top
from courtside.kalshi.rest import KalshiREST
from courtside.kalshi.sides import Action, Outcome, to_wire
from courtside.kalshi.units import parse_count, parse_price

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class OrderRequest:
    ticker: str
    outcome: Outcome
    action: Action
    price: int  # cents, in `outcome`'s own terms
    count: Decimal
    client_order_id: str
    ioc: bool  # True = immediate_or_cancel; False = good_till_canceled
    reduce_only: bool = False
    post_only: bool = False


@dataclass(frozen=True, slots=True)
class Execution:
    """Result of placing an order. Prices are in the order's own outcome terms."""

    order_id: str
    filled: Decimal
    resting: Decimal
    avg_price: Decimal | None  # cents, own outcome
    fee: Decimal  # dollars, total for the immediate fill
    exchange_ts_ms: int


@dataclass(frozen=True, slots=True)
class FillEvent:
    """A fill on one of our orders, reported by the exchange after the fact."""

    trade_id: str  # unique per fill; used to drop duplicate deliveries
    order_id: str
    count: Decimal
    yes_price: Decimal  # cents, YES leg (converted to the engine's outcome by the engine)
    fee: Decimal  # dollars
    is_taker: bool
    exchange_ts_ms: int


class Exchange(Protocol):
    async def place(self, req: OrderRequest) -> Execution: ...

    async def cancel(self, order_id: str, ticker: str) -> Decimal:
        """Cancel one order. Returns how many contracts were still resting."""
        ...

    async def cancel_all(self) -> None: ...

    async def position(self, ticker: str) -> Decimal:
        """Signed YES position: + = long YES, - = long NO."""
        ...

    async def quote(self, ticker: str, t_ms: int) -> Top:
        """Top of book via REST, for when the market feed is down."""
        ...


def own_price(outcome: Outcome, yes_price: Decimal) -> Decimal:
    """Convert a YES-leg price (cents) into `outcome`'s own terms."""
    return yes_price if outcome is Outcome.YES else 100 - yes_price


class KalshiExchange:
    """Exchange backed by Kalshi REST. Fills of resting orders come from UserFeed."""

    def __init__(self, rest: KalshiREST) -> None:
        self.rest = rest

    async def place(self, req: OrderRequest) -> Execution:
        wire = to_wire(req.outcome, req.action, req.price)
        ack = await self.rest.create_order(
            ticker=req.ticker,
            side=wire.book_side,
            yes_price=wire.yes_price,
            count=req.count,
            time_in_force="immediate_or_cancel" if req.ioc else "good_till_canceled",
            client_order_id=req.client_order_id,
            reduce_only=req.reduce_only,
            post_only=req.post_only,
        )
        avg = None
        if ack.avg_fill_price is not None:
            avg = own_price(req.outcome, ack.avg_fill_price * 100)
        fee = (ack.avg_fee or _ZERO) * ack.fill_count
        return Execution(ack.order_id, ack.fill_count, ack.remaining_count, avg, fee, ack.ts_ms)

    async def cancel(self, order_id: str, ticker: str) -> Decimal:
        res = await self.rest.cancel_order(order_id, ticker)
        return parse_count(res["reduced_by"])

    async def cancel_all(self) -> None:
        await self.rest.cancel_all()

    async def position(self, ticker: str) -> Decimal:
        rows = await self.rest.positions(ticker)
        return sum((parse_count(r["position_fp"]) for r in rows if r["ticker"] == ticker), _ZERO)

    async def quote(self, ticker: str, t_ms: int) -> Top:
        m = await self.rest.market(ticker)
        bid, ask = parse_price(m["yes_bid_dollars"]), parse_price(m["yes_ask_dollars"])
        return Top(
            t_ms=t_ms,
            yes_bid=bid if bid > 0 else None,
            yes_bid_size=parse_count(m.get("yes_bid_size_fp") or "0"),
            yes_ask=ask if ask < 100 else None,
            yes_ask_size=parse_count(m.get("yes_ask_size_fp") or "0"),
        )
