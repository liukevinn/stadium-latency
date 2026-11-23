from decimal import Decimal

from courtside.book import BookHistory, OrderBook, Top
from courtside.kalshi.sides import Outcome

SNAP = {
    "market_ticker": "T",
    "yes_dollars_fp": [["0.3400", "490.19"], ["0.3600", "469.24"]],
    "no_dollars_fp": [["0.6100", "427.35"], ["0.6300", "381.45"]],
}


def delta(side: str, price: str, d: str) -> dict[str, str]:
    return {"market_ticker": "T", "side": side, "price_dollars": price, "delta_fp": d}


def test_snapshot_top_of_book() -> None:
    b = OrderBook("T")
    assert not b.valid
    b.apply_snapshot(SNAP)
    top = b.top(1000)
    assert b.valid
    assert (top.yes_bid, top.yes_ask) == (36, 37)  # ask = 100 - best NO bid 63
    assert top.yes_bid_size == Decimal("469.24")
    assert top.yes_ask_size == Decimal("381.45")


def test_outcome_views() -> None:
    top = Top(0, 36, Decimal(1), 37, Decimal(1))
    assert (top.bid(Outcome.YES), top.ask(Outcome.YES)) == (36, 37)
    assert (top.bid(Outcome.NO), top.ask(Outcome.NO)) == (63, 64)
    empty = Top(0, None, Decimal(0), None, Decimal(0))
    assert empty.bid(Outcome.NO) is None and empty.ask(Outcome.NO) is None


def test_deltas_add_and_remove_levels() -> None:
    b = OrderBook("T")
    b.apply_snapshot(SNAP)
    b.apply_delta(delta("yes", "0.3700", "5.00"))  # new best bid
    assert b.top(0).yes_bid == 37
    b.apply_delta(delta("yes", "0.3700", "-5.00"))  # removed again
    assert b.top(0).yes_bid == 36
    b.apply_delta(delta("no", "0.6300", "-381.45"))  # best NO bid gone -> ask widens
    assert b.top(0).yes_ask == 39
    b.apply_delta(delta("no", "0.6300", "-1.00"))  # never goes negative
    assert 63 not in b.no


def test_empty_sides() -> None:
    b = OrderBook("T")
    b.apply_snapshot({"market_ticker": "T"})
    top = b.top(0)
    assert top.yes_bid is None and top.yes_ask is None


def q(t: int, bid: int, ask: int) -> Top:
    return Top(t, bid, Decimal(1), ask, Decimal(1))


def test_history_at() -> None:
    h = BookHistory(keep_ms=60_000)
    assert h.at(5) is None
    h.push(q(100, 40, 42))
    h.push(q(200, 40, 42))  # unchanged quote: not recorded
    h.push(q(300, 45, 47))
    assert len(h) == 2
    assert h.at(99) is None
    assert h.at(100) == q(100, 40, 42)
    assert h.at(299) == q(100, 40, 42)
    assert h.at(300) == q(300, 45, 47)
    assert h.latest() == q(300, 45, 47)


def test_history_prunes_but_keeps_live_quote_at_cutoff() -> None:
    h = BookHistory(keep_ms=1_000)
    for i in range(100):
        h.push(q(i * 100, 10 + i % 2, 20))
    assert len(h) < 100
    cutoff = 99 * 100 - 1_000
    assert h.at(cutoff) is not None  # the quote live at the cutoff is still known
