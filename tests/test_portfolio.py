from decimal import Decimal as D

from courtside.portfolio import build_portfolio

POSITIONS = [
    {  # long 10 YES, cost $4.20 (avg 42c)
        "ticker": "KXNBAGAME-X-IND", "position_fp": "10.00", "market_exposure_dollars": "4.200000",
        "realized_pnl_dollars": "0.500000", "fees_paid_dollars": "0.170000",
    },
    {  # long 5 NO, cost $3.00 (avg 60c NO)
        "ticker": "KXNFLGAME-Y-DAL", "position_fp": "-5.00", "market_exposure_dollars": "3.000000",
        "realized_pnl_dollars": "0", "fees_paid_dollars": "0.08",
    },
    {"ticker": "CLOSED", "position_fp": "0.00", "market_exposure_dollars": "0"},
    {  # long 2 YES with nobody bidding
        "ticker": "KXNHLGAME-Z-BOS", "position_fp": "2.00", "market_exposure_dollars": "1.00",
    },
]  # fmt: skip
MARKETS = {
    "KXNBAGAME-X-IND": {"event_ticker": "KXNBAGAME-X", "yes_sub_title": "Indiana",
                        "yes_bid_dollars": "0.4500", "yes_ask_dollars": "0.4600"},
    "KXNFLGAME-Y-DAL": {"event_ticker": "KXNFLGAME-Y", "yes_sub_title": "Dallas",
                        "yes_bid_dollars": "0.3500", "yes_ask_dollars": "0.3700"},
    "KXNHLGAME-Z-BOS": {"event_ticker": "KXNHLGAME-Z", "yes_sub_title": "Boston",
                        "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0500"},
}  # fmt: skip


def test_build_portfolio() -> None:
    pf = build_portfolio(POSITIONS, MARKETS, {"balance_dollars": "100.5000"}, t_ms=7)
    assert pf.cash == D("100.5") and pf.t_ms == 7
    by = {h.ticker: h for h in pf.holdings}
    assert set(by) == {"KXNBAGAME-X-IND", "KXNFLGAME-Y-DAL", "KXNHLGAME-Z-BOS"}  # flat one dropped

    ind = by["KXNBAGAME-X-IND"]
    assert (ind.outcome, ind.qty, ind.avg_cents, ind.mark_cents) == ("yes", 10, D("42.0"), 45)
    assert ind.unrealized == D("0.30") and ind.team == "Indiana"
    assert ind.realized == D("0.5") and ind.fees == D("0.17")

    dal = by["KXNFLGAME-Y-DAL"]
    assert (dal.outcome, dal.qty, dal.avg_cents) == ("no", 5, D("60.0"))
    assert dal.mark_cents == 63  # NO bid = 100 - YES ask 37
    assert dal.unrealized == D("0.15")

    bos = by["KXNHLGAME-Z-BOS"]
    assert bos.mark_cents is None and bos.unrealized is None  # nobody bidding

    assert pf.unrealized == D("0.45")
    d = pf.to_dict()
    assert d["cash"] == "100.5000" and d["holdings"][0]["qty"] == "10.00"
