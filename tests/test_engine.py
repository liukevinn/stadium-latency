"""State machine tests: every transition and every edge case in CLAUDE.md §6.

World wires the Engine to FakeExchange/FakeMarket/FakeScheduler. Time only moves
when a test calls `advance()`, which also fires any timers that come due.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from courtside.book import Top
from courtside.engine import Direction, Engine, Fill, Flatten, State, Tap
from courtside.exchange import FillEvent
from courtside.fees import trade_fee
from courtside.kalshi.rest import KalshiError
from courtside.kalshi.sides import Action, Outcome
from courtside.params import TradeParams
from courtside.sim import FakeExchange, FakeMarket, FakeScheduler
from courtside.store import EventLog

UP, DOWN = Direction.UP, Direction.DOWN
D = Decimal


class World:
    def __init__(self, **overrides: Any) -> None:
        params = dict(
            size=10, chase_cents=5, lookback_ms=1_500, min_move_cents=4, deadline_ms=10_000,
            stop_cents=0, max_position=30, requote_ms=250, forced_slip_cents=5,
        )  # fmt: skip
        params.update(overrides)
        self.sched = FakeScheduler()
        self.fx = FakeExchange("T")
        self.fx.clock = self.sched.now_ms
        self.mkt = FakeMarket()
        self.log = EventLog(keep=True)
        self.alerts: list[str] = []
        self.engine = Engine(
            ticker="T",
            exchange=self.fx,
            market=self.mkt,
            scheduler=self.sched,
            params=TradeParams(**params),
            log_=self.log,
            listener=lambda kind, p: self.alerts.append(p["alert"]) if kind == "alert" else None,
        )

    @property
    def state(self) -> State:
        return self.engine.state

    async def start(self, bid: int | None = 40, ask: int | None = 42, size: int = 100) -> None:
        await self.engine.start()
        await self.quote(bid, ask, size)
        await self.advance(2_000)  # give the history a pre-play quote

    async def start_without_tap_history(self) -> None:
        await self.engine.handle(self.mkt.push(self.fx.book.top(self.sched.now_ms())))
        await self.engine.start()

    async def quote(self, bid: int | None, ask: int | None, size: int = 100) -> None:
        self.fx.set_book(bid, ask, size)
        s = D(size)
        top = Top(self.sched.now_ms(), bid, s if bid else D(0), ask, s if ask else D(0))
        await self.engine.handle(self.mkt.push(top))

    async def advance(self, ms: int) -> None:
        target = self.sched.t + ms
        while (due := self.sched.pop_due(target)) is not None:
            self.sched.t = due[0]
            await self.engine.handle(due[1])
        self.sched.t = target

    async def tap(self, d: Direction) -> None:
        await self.engine.handle(Tap(d, self.sched.now_ms()))

    async def deliver(self, fill: FillEvent) -> None:
        await self.engine.handle(Fill(fill))

    def only_resting_id(self) -> str:
        assert len(self.fx.resting) == 1, self.fx.resting
        return next(iter(self.fx.resting))

    def closed(self) -> dict[str, Any]:
        trades = self.log.of("trade_closed")
        assert len(trades) == 1, trades
        return trades[0]


# ---- happy path -----------------------------------------------------------------


async def test_up_tap_reprice_passive_exit_fills() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    assert w.state is State.WAITING
    assert w.fx.position_yes == 10
    entry = w.fx.placed[0]
    assert (entry.action, entry.price, entry.ioc) == (Action.BUY, 47, True)  # 42 + chase 5

    await w.quote(47, 49)  # bid 47 >= 42 + 4 -> caught up
    assert w.state is State.EXITING_PASSIVE
    oid = w.only_resting_id()
    assert w.fx.resting[oid].req.price == 48  # max(ask-1, bid+1) = max(48, 48)
    assert w.fx.resting[oid].req.post_only

    await w.deliver(w.fx.fill_resting(oid))
    assert w.state is State.IDLE and w.fx.position_yes == 0
    t = w.closed()
    assert t["path"] == ["repriced"]
    assert D(t["gross"]) == D("0.60")  # (48 - 42) * 10 contracts
    assert D(t["fees"]) == trade_fee(42, 10, is_maker=False)  # passive exit: no maker fee
    assert D(t["net"]) == D("0.60") - trade_fee(42, 10, is_maker=False)

    await w.advance(2_000)  # post-trade REST check finds us flat
    assert "position_mismatch" not in w.alerts


async def test_down_tap_buys_no_and_exits_on_no_leg() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(DOWN)  # NO ask = 100 - 40 = 60 -> limit 65
    assert w.fx.position_yes == -10
    assert w.fx.placed[0].outcome is Outcome.NO and w.fx.placed[0].price == 65
    assert w.engine.pos is not None and w.engine.pos.avg_entry == 60

    await w.quote(30, 33)  # NO bid = 67 >= 64
    assert w.state is State.EXITING_PASSIVE
    oid = w.only_resting_id()
    assert w.fx.resting[oid].req.price == 69  # NO ask 70 - 1 = 69 > NO bid 67 + 1
    await w.deliver(w.fx.fill_resting(oid))
    assert w.fx.position_yes == 0
    assert D(w.closed()["gross"]) == D("0.90")  # (69 - 60) * 10


# ---- entry edge cases -------------------------------------------------------------


async def test_market_already_caught_up_no_fill() -> None:
    w = World()
    await w.start(40, 42)
    await w.quote(55, 57)  # moved just now; anchor 1.5 s ago is still 42
    await w.tap(UP)  # limit 47 < 57 -> nothing fills
    assert w.state is State.IDLE and w.fx.position_yes == 0
    assert len(w.log.of("trade_missed")) == 1
    assert w.fx.resting == {}  # IOC leaves nothing behind


async def test_limit_is_capped_at_max_price() -> None:
    w = World()
    await w.start(93, 95)
    await w.tap(UP)
    assert w.fx.placed[0].price == 97  # min(95 + 5, 97)


async def test_partial_fill_manages_only_filled_amount() -> None:
    w = World()
    await w.start(40, 42, size=4)
    await w.tap(UP)
    assert w.engine.pos is not None and w.engine.pos.qty == 4
    await w.quote(47, 49)
    assert w.fx.resting[w.only_resting_id()].remaining == 4


async def test_double_tap_while_entering_is_ignored() -> None:
    w = World()
    await w.start()
    t = w.sched.now_ms()
    await w.engine.handle(Tap(UP, t))
    await w.engine.handle(Tap(UP, t))  # arrived while the first was entering
    assert len(w.fx.placed) == 1
    assert len(w.log.of("tap_ignored")) == 1
    assert "extensions" not in w.engine.trade


async def test_entry_reject_4xx_goes_idle() -> None:
    w = World()
    await w.start()
    w.fx.inject("place", KalshiError(400, "insufficient_balance", "no money", ""))
    await w.tap(UP)
    assert w.state is State.IDLE
    assert w.log.of("entry_rejected")[0]["code"] == "insufficient_balance"


async def test_entry_timeout_that_did_fill_is_adopted() -> None:
    w = World()
    await w.start()
    w.fx.inject("place", TimeoutError("read timeout"))
    w.fx.position_yes = D(10)  # the order reached the exchange and filled
    await w.tap(UP)
    assert w.state is State.WAITING
    assert w.engine.pos is not None and w.engine.pos.qty == 10
    assert w.engine.pos.avg_entry == 47  # assume the worst allowed price
    assert "entry_uncertain" in w.alerts


async def test_entry_timeout_without_fill_goes_idle() -> None:
    w = World()
    await w.start()
    w.fx.inject("place", TimeoutError("read timeout"))
    await w.tap(UP)
    assert w.state is State.IDLE and w.fx.position_yes == 0


@pytest.mark.parametrize(
    "setup,reason",
    [("no_history", "no_anchor"), ("feed_down", "feed_down"), ("loss_limit", "daily_loss_limit")],
)
async def test_tap_rejections(setup: str, reason: str) -> None:
    w = World()
    if setup == "no_history":
        await w.engine.start()
    else:
        await w.start()
    if setup == "feed_down":
        w.mkt.healthy = False
    if setup == "loss_limit":
        w.engine.realized_today = D(-20)
    await w.tap(UP)
    assert w.state is State.IDLE and w.fx.placed == []
    assert w.log.of("tap_rejected")[0]["reason"] == reason


async def test_taps_rejected_before_start() -> None:
    w = World()
    await w.tap(UP)
    assert w.log.of("tap_rejected")[0]["reason"] == "not_ready"


# ---- waiting / passive / forced ----------------------------------------------------


async def test_market_never_moves_wait_timeout_then_deadline() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.advance(4_999)
    assert w.state is State.WAITING
    await w.advance(1)  # wait_ms = deadline / 2 = 5 s
    assert w.state is State.EXITING_PASSIVE
    assert w.fx.resting[w.only_resting_id()].req.price == 41  # max(42-1, 40+1)
    await w.advance(5_000)  # deadline 10 s after the fill
    assert w.state is State.IDLE and w.fx.position_yes == 0
    assert w.fx.resting == {}
    forced = w.fx.placed[-1]
    assert forced.reduce_only and forced.ioc and forced.price == 35  # bid 40 - slip 5
    t = w.closed()
    assert t["path"] == ["wait_timeout", "deadline"]
    assert D(t["gross"]) == D("-0.20")  # bought 42, sold at the 40 bid


async def test_requotes_are_throttled_and_follow_the_book() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.quote(47, 50)
    first = w.only_resting_id()
    assert w.fx.resting[first].req.price == 49
    await w.quote(48, 52)  # wants 51, but within 250 ms of the last quote
    assert first in w.fx.resting
    await w.advance(250)
    second = w.only_resting_id()
    assert second != first and w.fx.resting[second].req.price == 51


async def test_overshoot_then_revert_requotes_lower() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.quote(55, 58)
    assert w.fx.resting[w.only_resting_id()].req.price == 57
    await w.advance(300)
    await w.quote(50, 53)
    assert w.fx.resting[w.only_resting_id()].req.price == 52


async def test_same_direction_tap_extends_deadline() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.advance(6_000)  # past wait_ms: now passive
    assert w.state is State.EXITING_PASSIVE
    await w.tap(UP)  # it got better: keep holding
    assert w.state is State.WAITING and w.fx.resting == {}
    await w.advance(4_500)  # the original deadline (10 s) has passed
    assert w.state is State.WAITING
    await w.advance(500)  # new wait timeout, 5 s after the extension
    assert w.state is State.EXITING_PASSIVE
    await w.advance(5_000)  # new deadline
    assert w.state is State.IDLE
    assert len(w.fx.placed) == 4  # entry, passive, passive, forced (no added size)


@pytest.mark.parametrize("how", ["opposite_tap", "flatten"])
async def test_opposite_tap_or_flatten_exits_now(how: str) -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.advance(100)
    if how == "flatten":
        await w.engine.handle(Flatten(w.sched.now_ms()))
    else:
        await w.tap(DOWN)
    assert w.state is State.IDLE and w.fx.position_yes == 0
    assert w.closed()["path"] == [how]


async def test_flatten_queued_during_entry_exits_after_fill() -> None:
    w = World()
    await w.start()
    t = w.sched.now_ms()
    await w.engine.handle(Tap(UP, t))
    await w.engine.handle(Flatten(t))  # was queued behind the entry
    assert w.state is State.IDLE and w.fx.position_yes == 0


async def test_auto_stop_on_reversal() -> None:
    w = World(stop_cents=3)
    await w.start(40, 42)
    await w.tap(UP)
    await w.quote(39, 41)  # 39 is not < 42 - 3
    assert w.state is State.WAITING
    await w.quote(38, 40)  # flag on the play: bid 38 < 39
    assert w.state is State.IDLE
    assert w.closed()["path"] == ["stop"]


async def test_stop_off_by_default() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.quote(20, 22)
    assert w.state is State.WAITING


# ---- failures during the exit -------------------------------------------------------


async def test_feed_down_while_holding_forces_exit_using_rest_quote() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    w.mkt.healthy = False
    w.fx.set_book(41, 43)  # REST still works
    await w.engine.handle(w.mkt.push(Top(w.sched.now_ms(), None, D(0), None, D(0))))
    assert w.state is State.IDLE and w.fx.position_yes == 0
    assert w.closed()["path"] == ["feed_down"]
    assert w.fx.placed[-1].price == 36  # REST bid 41 - 5


async def test_thin_book_retries_until_flat_and_alerts() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    w.fx.set_book(40, 42, size=3)
    await w.engine.handle(Flatten(w.sched.now_ms()))
    assert w.state is State.EXITING_FORCED and w.fx.position_yes == 7
    await w.quote(None, 42)  # bid side empty now
    await w.advance(3_000)
    assert "no_bid" in w.alerts and "not_flat" in w.alerts
    assert w.state is State.EXITING_FORCED
    await w.quote(39, 42)
    await w.advance(1_000)
    assert w.state is State.IDLE and w.fx.position_yes == 0


async def test_paused_market_keeps_retrying_with_backoff() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    w.fx.paused = True
    await w.engine.handle(Flatten(w.sched.now_ms()))
    await w.advance(2_000)
    rejected = [a for a in w.alerts if a == "forced_exit_rejected"]
    assert 3 <= len(rejected) <= 5  # 200, 400, 800, 1000 ms backoff
    w.fx.paused = False
    await w.advance(1_000)
    assert w.state is State.IDLE and w.fx.position_yes == 0


async def test_game_ends_while_holding_is_logged_as_settlement() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    w.fx.inject("place", KalshiError(409, "market_inactive", "closed", ""))
    await w.engine.handle(Flatten(w.sched.now_ms()))
    assert w.state is State.IDLE
    t = w.closed()
    assert t["settled"] and t["path"] == ["flatten", "settlement"]
    assert w.engine.realized_today == 0  # P&L arrives at settlement, not now


async def test_forced_exit_timeout_resyncs_quantity() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    w.fx.inject("place", TimeoutError())  # forced sell outcome unknown...
    await w.engine.handle(Flatten(w.sched.now_ms()))
    w.fx.position_yes = D(0)  # ...it actually filled
    await w.advance(300)
    assert w.state is State.IDLE
    assert w.log.of("qty_resynced")


# ---- passive-fill accounting races --------------------------------------------------


async def test_partial_passive_fill_then_deadline() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.quote(47, 49)
    oid = w.only_resting_id()
    await w.deliver(w.fx.fill_resting(oid, 6))
    assert w.engine.pos is not None and w.engine.pos.qty == 4
    await w.advance(10_000)
    assert w.state is State.IDLE and w.fx.position_yes == 0
    t = w.closed()
    assert D(t["gross"]) == (6 * 6 + 4 * (47 - 42)) / D(100)


async def test_fill_before_cancel_with_late_ws_message_is_not_double_counted() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.quote(47, 49)
    oid = w.only_resting_id()
    late = w.fx.fill_resting(oid, 4)  # filled at the exchange; WS message delayed
    await w.engine.handle(Flatten(w.sched.now_ms()))  # cancel reports 6 still resting
    assert w.state is State.IDLE and w.fx.position_yes == 0
    await w.deliver(late)  # arrives after the trade closed: must be ignored
    t = w.closed()
    assert D(t["gross"]) == (4 * 6 + 6 * (47 - 42)) / D(100)
    await w.advance(2_000)
    assert "position_mismatch" not in w.alerts


async def test_ws_fill_then_cancel_overlap_is_capped() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.quote(47, 49)
    oid = w.only_resting_id()
    ev = w.fx.fill_resting(oid, 4)
    await w.deliver(ev)
    await w.deliver(ev)  # duplicate delivery
    assert w.engine.pos is not None and w.engine.pos.qty == 6


async def test_passive_fully_filled_before_cancel_404() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.quote(47, 49)
    oid = w.only_resting_id()
    w.fx.fill_resting(oid)  # fully filled; WS message lost; cancel will 404
    await w.engine.handle(Flatten(w.sched.now_ms()))
    assert w.state is State.IDLE and w.fx.position_yes == 0
    assert w.log.of("cancel_failed") and w.log.of("qty_resynced")


# ---- reconciliation -------------------------------------------------------------------


async def test_startup_cancels_orders_and_flattens_leftover_position() -> None:
    w = World()
    w.fx.position_yes = D(-5)  # leftover NO position from before a restart
    w.fx.set_book(40, 42)
    await w.start_without_tap_history()
    assert w.fx.cancel_all_calls >= 1
    assert w.state is State.IDLE and w.fx.position_yes == 0
    assert w.log.of("position_adopted")[0]["why"] == "startup"


async def test_post_trade_check_catches_phantom_position() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.engine.handle(Flatten(w.sched.now_ms()))
    w.fx.position_yes = D(2)  # exchange disagrees with what we booked
    await w.advance(1_500)
    assert "position_mismatch" in w.alerts
    assert w.fx.position_yes == 0


async def test_stale_timers_from_previous_trade_are_ignored() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    await w.engine.handle(Flatten(w.sched.now_ms()))  # trade 1 closes at once
    await w.advance(1_000)
    await w.tap(UP)  # trade 2 starts; trade 1's wait/deadline timers are still queued
    await w.advance(4_500)  # trade 1's 5 s wait timer fires here: must be ignored
    assert w.state is State.WAITING


async def test_engine_error_while_holding_forces_exit() -> None:
    w = World()
    await w.start(40, 42)
    await w.tap(UP)
    w.fx.inject("cancel", RuntimeError("boom"))
    w.mkt.healthy = True
    await w.quote(47, 49)  # passive quote placed
    await w.engine.handle(Flatten(w.sched.now_ms()))  # cancel raises -> handled
    await w.advance(1_000)
    assert w.state is State.IDLE and w.fx.position_yes == 0
