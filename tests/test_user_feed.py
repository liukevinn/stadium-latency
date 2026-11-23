from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519

from courtside.exchange import FillEvent
from courtside.kalshi.auth import Signer
from courtside.kalshi.ws import UserFeed, parse_fill
from courtside.store import EventLog, realized_today

# Shape from https://docs.kalshi.com/websockets/user-fills (checked 2026-11-23)
FILL = {
    "trade_id": "t1",
    "order_id": "o1",
    "market_ticker": "T",
    "is_taker": False,
    "side": "yes",
    "yes_price_dollars": "0.4800",
    "count_fp": "6.00",
    "fee_cost": "0.000000",
    "ts_ms": 1795461317047,
}


def test_parse_fill() -> None:
    f = parse_fill(FILL)
    assert f == FillEvent("t1", "o1", Decimal(6), Decimal(48), Decimal(0), False, 1795461317047)


def test_user_feed_delivers_fills_and_counts_gaps() -> None:
    got: list[FillEvent] = []
    feed = UserFeed(
        "wss://x/trade-api/ws/v2", Signer("k", ed25519.Ed25519PrivateKey.generate()), got.append
    )
    feed.handle(0, {"type": "subscribed", "msg": {"channel": "fill", "sid": 9}})
    assert feed.connected.is_set()
    feed.handle(1, {"type": "fill", "sid": 9, "seq": 1, "msg": FILL})
    feed.handle(2, {"type": "fill", "sid": 9, "seq": 3, "msg": {**FILL, "trade_id": "t2"}})
    assert [f.trade_id for f in got] == ["t1", "t2"]  # a gap is logged, fills still delivered
    assert feed.stats["gaps"] == 1


def test_realized_today_sums_closed_trades(tmp_path: Path) -> None:
    path = tmp_path / "day.jsonl"
    log = EventLog(path)
    log.write("trade_closed", net="0.50", settled=False)
    log.write("trade_closed", net="-0.20", settled=False)
    log.write("trade_closed", net="9.99", settled=True)  # settles later; not counted
    log.write("trade_closed", net="1.00", settled=False, adopted="startup")  # unknown entry
    log.write("alert", alert="x")
    log.close()
    assert realized_today(path) == Decimal("0.30")
    assert realized_today(tmp_path / "missing.jsonl") == 0
