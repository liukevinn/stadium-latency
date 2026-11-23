from typing import Any

from cryptography.hazmat.primitives.asymmetric import ed25519

from courtside.kalshi.auth import Signer
from courtside.kalshi.ws import MarketFeed


def make_feed() -> tuple[MarketFeed, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []
    feed = MarketFeed(
        "wss://x/trade-api/ws/v2",
        Signer("k", ed25519.Ed25519PrivateKey.generate()),
        ["A", "B"],
        lambda t, m: seen.append(m),
    )
    return feed, seen


def snap(seq: int, ticker: str, bid: str, nobid: str) -> dict[str, Any]:
    return {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [[bid, "10.00"]],
            "no_dollars_fp": [[nobid, "10.00"]],
        },
    }


def delta(seq: int, ticker: str, price: str, d: str) -> dict[str, Any]:
    return {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": seq,
        "msg": {"market_ticker": ticker, "price_dollars": price, "delta_fp": d, "side": "yes"},
    }


def test_snapshots_then_deltas_build_history() -> None:
    feed, seen = make_feed()
    feed.handle(0, {"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}})
    feed.handle(10, snap(1, "A", "0.4000", "0.5800"))
    assert not feed.connected.is_set()  # B has no snapshot yet
    feed.handle(11, snap(2, "B", "0.5800", "0.4000"))
    assert feed.connected.is_set()
    feed.handle(20, delta(3, "A", "0.4100", "5.00"))
    top = feed.history["A"].latest()
    assert top is not None and (top.yes_bid, top.yes_ask, top.t_ms) == (41, 42, 20)
    assert feed.history["A"].at(15) is not None and feed.history["A"].at(15).yes_bid == 40  # type: ignore[union-attr]
    assert len(seen) == 4  # every raw message reaches the consumer


def test_seq_gap_invalidates_and_blanks_history() -> None:
    feed, _ = make_feed()
    feed.handle(0, {"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}})
    feed.handle(10, snap(1, "A", "0.4000", "0.5800"))
    feed.handle(11, snap(2, "B", "0.5800", "0.4000"))
    feed.handle(30, delta(5, "A", "0.4100", "5.00"))  # expected 3: gap
    assert feed.stats["gaps"] == 1
    assert not feed.books["A"].valid and not feed.connected.is_set()
    blank = feed.history["A"].at(30)
    assert blank is not None and blank.yes_bid is None  # no stale anchor during an outage
    assert feed.history["A"].at(29).yes_bid == 40  # type: ignore[union-attr]
    feed.handle(40, delta(6, "A", "0.4200", "1.00"))  # ignored until a snapshot
    assert feed.history["A"].latest().yes_bid is None  # type: ignore[union-attr]
    feed.handle(50, snap(7, "A", "0.4500", "0.5300"))
    feed.handle(51, snap(8, "B", "0.5300", "0.4500"))
    assert feed.connected.is_set()
    assert feed.history["A"].latest().yes_bid == 45  # type: ignore[union-attr]


def test_trade_channel_gap_does_not_touch_books() -> None:
    feed, _ = make_feed()
    feed.handle(0, {"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}})
    feed.handle(0, {"type": "subscribed", "msg": {"channel": "trade", "sid": 2}})
    feed.handle(10, snap(1, "A", "0.4000", "0.5800"))
    feed.handle(20, {"type": "trade", "sid": 2, "seq": 1, "msg": {"market_ticker": "A"}})
    feed.handle(21, {"type": "trade", "sid": 2, "seq": 3, "msg": {"market_ticker": "A"}})
    assert feed.stats["gaps"] == 1
    assert feed.books["A"].valid
