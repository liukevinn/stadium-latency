"""What the Kalshi account actually holds, for the phone's portfolio view.

This is Kalshi's view, not the engine's. It shows every open position in the
account, including ones the engine didn't open, so the phone always shows the
truth even in paper mode.

Per position:
    qty, outcome   from position_fp (+ = long YES, - = long NO)
    cost           market_exposure_dollars (cost basis of what's still held, no fees)
    mark           what we could sell for now: YES bid, or NO bid = 1 - YES ask
    unrealized     mark * qty - cost (before exit fees)
    realized       realized_pnl_dollars; fees: fees_paid_dollars
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from courtside.kalshi.rest import KalshiREST
from courtside.kalshi.units import parse_count, parse_dollars

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Holding:
    ticker: str
    event_ticker: str
    team: str
    outcome: str  # "yes" | "no"
    qty: Decimal
    avg_cents: Decimal
    mark_cents: Decimal | None  # None when nobody is bidding
    cost: Decimal  # dollars
    value: Decimal | None
    unrealized: Decimal | None
    realized: Decimal
    fees: Decimal


@dataclass(frozen=True, slots=True)
class Portfolio:
    cash: Decimal
    holdings: tuple[Holding, ...]
    open_value: Decimal
    unrealized: Decimal
    t_ms: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["holdings"] = [asdict(h) for h in self.holdings]
        out: dict[str, Any] = _stringify(d)
        return out


def build_portfolio(
    positions: list[dict[str, Any]],
    markets: dict[str, dict[str, Any]],
    balance: dict[str, Any],
    t_ms: int,
) -> Portfolio:
    holdings = []
    for p in positions:
        signed = parse_count(p.get("position_fp") or "0")
        if signed == 0:
            continue
        m = markets.get(p["ticker"], {})
        qty = abs(signed)
        outcome = "yes" if signed > 0 else "no"
        cost = parse_dollars(p.get("market_exposure_dollars") or "0")
        mark = _mark_cents(m, outcome)
        value = mark * qty / 100 if mark is not None else None
        holdings.append(
            Holding(
                ticker=p["ticker"],
                event_ticker=m.get("event_ticker", ""),
                team=m.get("yes_sub_title") or p["ticker"],
                outcome=outcome,
                qty=qty,
                avg_cents=(cost / qty * 100).quantize(Decimal("0.1")),
                mark_cents=mark,
                cost=cost,
                value=value,
                unrealized=value - cost if value is not None else None,
                realized=parse_dollars(p.get("realized_pnl_dollars") or "0"),
                fees=parse_dollars(p.get("fees_paid_dollars") or "0"),
            )
        )
    holdings.sort(key=lambda h: h.ticker)
    return Portfolio(
        cash=parse_dollars(balance.get("balance_dollars") or "0"),
        holdings=tuple(holdings),
        open_value=sum((h.value or _ZERO for h in holdings), _ZERO),
        unrealized=sum((h.unrealized or _ZERO for h in holdings), _ZERO),
        t_ms=t_ms,
    )


async def fetch_portfolio(rest: KalshiREST, t_ms: int) -> Portfolio:
    """Three reads: positions, the markets they're in (one call), and cash."""
    positions = await rest.positions()
    tickers = sorted({p["ticker"] for p in positions if parse_count(p["position_fp"]) != 0})
    markets: dict[str, dict[str, Any]] = {}
    if tickers:
        data = await rest.markets(tickers=",".join(tickers), limit=len(tickers))
        markets = {m["ticker"]: m for m in data.get("markets", [])}
    return build_portfolio(positions, markets, await rest.balance(), t_ms)


def _mark_cents(market: dict[str, Any], outcome: str) -> Decimal | None:
    """Best bid for the held outcome, in cents. None if that side has no bid."""
    if outcome == "yes":
        bid = parse_dollars(market.get("yes_bid_dollars") or "0") * 100
        return bid if bid > 0 else None
    ask = parse_dollars(market.get("yes_ask_dollars") or "1") * 100
    return 100 - ask if ask < 100 else None


def _stringify(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _stringify(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_stringify(v) for v in x]
    if isinstance(x, Decimal):
        return str(x)
    return x
