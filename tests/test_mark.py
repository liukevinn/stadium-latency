from decimal import Decimal

from courtside.book import Top
from courtside.engine import Direction
from courtside.sim import FakeMarket
from courtside.trader import mark_record


def test_mark_records_live_and_anchor_quotes_in_tapped_terms() -> None:
    m = FakeMarket()
    one = Decimal(1)
    m.push(Top(1_000, 40, one, 42, one))  # pre-play
    m.push(Top(2_900, 47, one, 49, one))  # already moved when the tap lands
    rec = mark_record(Direction.DOWN, 3_000, m, 1_500, client_ts_ms=2_950, mark_id="m1", ticker="T")
    assert (rec["yes_bid"], rec["yes_ask"]) == (47, 49)
    assert (rec["anchor_yes_bid"], rec["anchor_yes_ask"]) == (40, 42)
    assert rec["anchor_ask"] == 60  # NO ask before the play = 100 - 40
    assert rec["ask_now"] == 53
    assert rec["direction"] == "down" and rec["mark_id"] == "m1" and rec["healthy"]
