"""The trade state machine (CLAUDE.md §6).

    IDLE --tap--> ENTERING --filled--> WAITING --repriced / wait timeout--> EXITING_PASSIVE
                    |                       |                                     |
                    `--no fill--> IDLE      `-- deadline / FLATTEN / opposite --> EXITING_FORCED
                                                tap / stop / feed down                |
                                                                          flat --> IDLE

Everything that can change state is an Event, handled one at a time by `handle()`:
taps, FLATTEN, book updates, fills on resting orders, and timers. Timers carry a
generation number, so a timer scheduled for an earlier phase is ignored once the
engine has moved on. Exchange calls are awaited inside `handle`; events arriving
meanwhile wait in the queue. Time and timers come from an injected Scheduler, so
tests drive the clock by hand.

Prices inside the engine are cents in the held outcome's own terms (a NO position
at 41 means we paid 41c for NO). Quantities are Decimal contracts.

Position accounting: IOC fills come from the order response. Fills on the resting
passive order come from FillEvents (user WS), capped per order. When the passive
order is cancelled, the cancel response says how much was still resting; the rest
filled at our limit price, so we book it right away and cap any late WS fills.
After every trade, a timer re-checks the position via REST, and any mismatch is
force-exited.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from courtside.book import Top
from courtside.exchange import Exchange, Execution, FillEvent, OrderRequest, own_price
from courtside.fees import trade_fee
from courtside.kalshi.rest import KalshiError
from courtside.kalshi.sides import Action, Outcome
from courtside.params import TradeParams
from courtside.store import EventLog

log = logging.getLogger(__name__)

_ZERO = Decimal(0)
# Errors meaning the market can't trade any more: hold to settlement.
_MARKET_CLOSED_CODES = {"market_inactive", "market_closed", "market_settled"}


class State(StrEnum):
    IDLE = "IDLE"
    ENTERING = "ENTERING"
    WAITING = "WAITING"
    EXITING_PASSIVE = "EXITING_PASSIVE"
    EXITING_FORCED = "EXITING_FORCED"


class Direction(StrEnum):
    UP = "up"  # good for Team A -> buy YES on "Team A wins"
    DOWN = "down"  # bad for Team A  -> buy NO

    @property
    def outcome(self) -> Outcome:
        return Outcome.YES if self is Direction.UP else Outcome.NO


# ---- events ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tap:
    direction: Direction
    t_ms: int  # server receive time
    client_ts_ms: int | None = None  # phone clock at tap, for latency logs
    tap_id: str | None = None


@dataclass(frozen=True, slots=True)
class Flatten:
    t_ms: int


@dataclass(frozen=True, slots=True)
class BookUpdate:
    top: Top


@dataclass(frozen=True, slots=True)
class Fill:
    fill: FillEvent


@dataclass(frozen=True, slots=True)
class Timer:
    kind: str  # wait_done | deadline | requote | retry_exit | flat_alert | check_flat
    gen: int


Event = Tap | Flatten | BookUpdate | Fill | Timer


class Scheduler(Protocol):
    def now_ms(self) -> int: ...

    def call_at(self, t_ms: int, event: Event) -> None: ...


class MarketView(Protocol):
    """The engine's read-only view of the live book for its market."""

    @property
    def healthy(self) -> bool: ...

    def at(self, t_ms: int) -> Top | None: ...

    def latest(self) -> Top | None: ...


Listener = Any  # callable(kind: str, payload: dict) -> None; wired to the phone in M3


# ---- position bookkeeping ------------------------------------------------------


@dataclass
class Passive:
    order_id: str
    price: int
    count: Decimal
    filled: Decimal = _ZERO
    final: Decimal | None = None  # total filled, known once cancelled


@dataclass
class Position:
    outcome: Outcome
    qty: Decimal
    t_fill: int
    entry_qty: Decimal = _ZERO
    entry_cost: Decimal = _ZERO  # sum of price * qty, cents
    exit_qty: Decimal = _ZERO
    exit_value: Decimal = _ZERO  # cents
    fees: Decimal = _ZERO  # dollars
    exits: list[dict[str, Any]] = field(default_factory=list)

    @property
    def avg_entry(self) -> Decimal:
        return self.entry_cost / self.entry_qty if self.entry_qty else _ZERO

    def gross_dollars(self) -> Decimal:
        return (self.exit_value - self.avg_entry * self.exit_qty) / 100


# ---- engine ----------------------------------------------------------------------


class Engine:
    def __init__(
        self,
        *,
        ticker: str,
        exchange: Exchange,
        market: MarketView,
        scheduler: Scheduler,
        params: TradeParams,
        log_: EventLog,
        realized_today: Decimal = _ZERO,
        fee_type: str = "quadratic",
        fee_multiplier: float = 1.0,
        listener: Listener | None = None,
    ) -> None:
        params.validate()
        self.ticker = ticker
        self.ex = exchange
        self.market = market
        self.sched = scheduler
        self.params = params
        self.events = log_
        self.realized_today = realized_today
        self.fee_type = fee_type
        self.fee_multiplier = fee_multiplier
        self.listener = listener

        self.state = State.IDLE
        self.ready = False  # set by start() after reconciling with the exchange
        self.pos: Position | None = None
        self.trade: dict[str, Any] = {}
        self.passive: Passive | None = None
        self._passives: dict[str, Passive] = {}
        self._ioc_ids: set[str] = set()
        self._seen_trade_ids: set[str] = set()
        self._gen = 0
        self._deadline_at = 0
        self._last_quote_ms = -(10**12)
        self._requote_pending = False
        self._entry_acked_ms = 0  # taps received before this were sent during ENTERING
        self._forced_attempts = 0
        self._resync_qty = False  # a forced-exit order had an unknown outcome
        self._cancel_all_needed = False  # a resting order may exist that we don't track

    # ---- public API ----------------------------------------------------------

    async def start(self) -> None:
        """Reconcile with the exchange before accepting taps (CLAUDE.md §6, server restart)."""
        await self.ex.cancel_all()
        signed = await self.ex.position(self.ticker)
        self.ready = True
        if signed != 0:
            self._adopt(signed, "startup")
            await self._go_forced("startup_position")
        self._notify()

    async def handle(self, ev: Event) -> None:
        try:
            if isinstance(ev, Tap):
                await self._on_tap(ev)
            elif isinstance(ev, Flatten):
                await self._on_flatten()
            elif isinstance(ev, BookUpdate):
                await self._on_book(ev.top)
            elif isinstance(ev, Fill):
                await self._on_fill(ev.fill)
            elif isinstance(ev, Timer):
                await self._on_timer(ev)
        except Exception as e:  # never let one event kill the engine
            log.exception("engine error handling %s", ev)
            self._alert("engine_error", error=f"{type(e).__name__}: {e}")
            if self.pos is None and self.state is not State.IDLE:
                self._to_idle()
            elif self.pos is not None and self.state is not State.EXITING_FORCED:
                await self._go_forced("engine_error")

    def snapshot(self) -> dict[str, Any]:
        now = self.sched.now_ms()
        p = self.pos
        return {
            "state": self.state.value,
            "ready": self.ready,
            "ticker": self.ticker,
            "outcome": p.outcome.value if p else None,
            "qty": str(p.qty) if p else "0",
            "avg_entry": str(round(p.avg_entry, 2)) if p else None,
            "deadline_in_ms": max(0, self._deadline_at - now) if p else None,
            "realized_today": str(self.realized_today),
            "last_trade": self.trade.get("latency"),
        }

    # ---- taps & flatten ------------------------------------------------------

    async def _on_tap(self, tap: Tap) -> None:
        base = {"direction": tap.direction.value, "t_ms": tap.t_ms, "tap_id": tap.tap_id}
        if not self.ready:
            return self._reject(base, "not_ready")
        # Events are handled one at a time, so a tap that arrived while the entry
        # order was in flight is only seen now. It was sent during ENTERING: ignore it.
        if self.state is State.EXITING_FORCED or (
            self.state is not State.IDLE and tap.t_ms <= self._entry_acked_ms
        ):
            return self._log("tap_ignored", **base, state=self.state.value)
        if self.state is State.IDLE:
            return await self._enter(tap, base)
        assert self.pos is not None
        if tap.direction.outcome is self.pos.outcome:
            await self._extend(tap)
        else:
            await self._go_forced("opposite_tap")

    async def _on_flatten(self) -> None:
        # A FLATTEN sent during ENTERING is handled once the entry finishes (it was
        # queued), so here the state is never ENTERING.
        self._log("flatten", state=self.state.value)
        if self.state in (State.WAITING, State.EXITING_PASSIVE):
            await self._go_forced("flatten")
        elif self.state is State.IDLE:
            await self.ex.cancel_all()  # belt and braces: nothing should be resting

    # ---- step 1: ENTERING ----------------------------------------------------

    async def _enter(self, tap: Tap, base: dict[str, Any]) -> None:
        p = self.params
        if self.realized_today <= -p.daily_loss_limit:
            self._alert("daily_loss_limit", realized_today=str(self.realized_today))
            return self._reject(base, "daily_loss_limit")
        if not self.market.healthy:
            return self._reject(base, "feed_down")
        outcome = tap.direction.outcome
        anchor = self.market.at(tap.t_ms - p.lookback_ms)
        anchor_ask = anchor.ask(outcome) if anchor else None
        if anchor_ask is None:
            return self._reject(base, "no_anchor")
        limit = min(anchor_ask + p.chase_cents, p.max_price)
        count = Decimal(min(p.size, p.max_position))
        now_top = self.market.latest()

        self.state = State.ENTERING
        self.trade = {
            "trade_id": uuid.uuid4().hex[:12],
            **base,
            "client_ts_ms": tap.client_ts_ms,
            "outcome": outcome.value,
            "anchor_t_ms": anchor.t_ms if anchor else None,
            "anchor_ask": anchor_ask,
            "ask_at_tap": now_top.ask(outcome) if now_top else None,
            "limit": limit,
            "count": str(count),
            "path": [],
        }
        self._notify()
        req = OrderRequest(self.ticker, outcome, Action.BUY, limit, count, _coid("in"), ioc=True)
        t_sent = self.sched.now_ms()
        try:
            ex = await self.ex.place(req)
        except Exception as e:
            resolved = await self._resolve_uncertain_entry(req, e)
            if resolved is None:
                return
            ex = resolved
        t_ack = self.sched.now_ms()
        self._entry_acked_ms = t_ack
        self._ioc_ids.add(ex.order_id)
        self.trade["latency"] = {
            "phone_to_server_ms": tap.t_ms - tap.client_ts_ms if tap.client_ts_ms else None,
            "server_to_send_ms": t_sent - tap.t_ms,
            "order_rtt_ms": t_ack - t_sent,
        }
        self.trade["entry"] = _exec_dict(ex)
        if ex.filled == 0:
            self._log("trade_missed", **self.trade)
            self._to_idle()
            return
        assert ex.avg_price is not None
        self.pos = Position(outcome=outcome, qty=ex.filled, t_fill=t_ack)
        self.pos.entry_qty = ex.filled
        self.pos.entry_cost = ex.avg_price * ex.filled
        self.pos.fees += ex.fee
        self._log("entry_filled", trade_id=self.trade["trade_id"], **_exec_dict(ex))
        self._start_waiting(t_ack)
        top = self.market.latest()
        if top is not None:
            await self._on_book(top)  # the book may already have moved

    async def _resolve_uncertain_entry(self, req: OrderRequest, e: Exception) -> Execution | None:
        """The buy failed. A 4xx means it definitely didn't happen. Anything else
        (timeout, 5xx) might have filled, so ask the exchange for the position."""
        if isinstance(e, KalshiError) and 400 <= e.status < 500:
            self._log("entry_rejected", trade_id=self.trade["trade_id"], code=e.code, msg=e.message)
            self._to_idle()
            return None
        self._alert("entry_uncertain", error=f"{type(e).__name__}: {e}")
        try:
            signed = await self.ex.position(self.ticker)
        except Exception:
            signed = _ZERO
            self._alert("position_check_failed")
        held = signed if req.outcome is Outcome.YES else -signed
        if held <= 0:
            self._log("trade_missed", **self.trade, uncertain=True)
            self._to_idle()
            return None
        # Assume the worst price we allowed; the post-trade REST check will catch errors.
        return Execution("unknown", held, _ZERO, Decimal(req.price), _ZERO, self.sched.now_ms())

    # ---- step 2: WAITING -----------------------------------------------------

    def _start_waiting(self, now: int) -> None:
        self.state = State.WAITING
        self._gen += 1
        self._deadline_at = now + self.params.deadline_ms
        self.sched.call_at(now + self.params.wait_ms, Timer("wait_done", self._gen))
        self.sched.call_at(self._deadline_at, Timer("deadline", self._gen))
        self._notify()

    async def _extend(self, tap: Tap) -> None:
        """Same-direction tap while holding: it got better, keep holding (v1: no adding)."""
        assert self.pos is not None
        if self.passive is not None:
            await self._cancel_passive()
        if self.pos.qty == 0:
            return self._finish()
        self.trade.setdefault("extensions", []).append(tap.t_ms)
        self._log("extend", trade_id=self.trade["trade_id"], t_ms=tap.t_ms)
        self._start_waiting(self.sched.now_ms())

    async def _on_book(self, top: Top) -> None:
        if self.pos is None or self.state not in (State.WAITING, State.EXITING_PASSIVE):
            return
        if not self.market.healthy:
            return await self._go_forced("feed_down")
        bid = top.bid(self.pos.outcome)
        p = self.params
        if bid is not None and p.stop_cents > 0 and bid < self.pos.avg_entry - p.stop_cents:
            return await self._go_forced("stop")
        if self.state is State.WAITING:
            if bid is not None and bid >= self.pos.avg_entry + p.min_move_cents:
                await self._go_passive("repriced")
        else:
            await self._requote()

    # ---- step 3: EXITING_PASSIVE ---------------------------------------------

    async def _go_passive(self, reason: str) -> None:
        self.state = State.EXITING_PASSIVE
        self.trade["path"].append(reason)
        self._log("exit_passive", trade_id=self.trade["trade_id"], reason=reason)
        self._notify()
        await self._requote()

    def _passive_price(self) -> int | None:
        assert self.pos is not None
        top = self.market.latest() if self.market.healthy else None
        if top is None:
            return None
        bid, ask = top.bid(self.pos.outcome), top.ask(self.pos.outcome)
        if bid is None and ask is None:
            return None
        price = max(ask - 1 if ask is not None else 0, bid + 1 if bid is not None else 0)
        return min(max(price, 1), 99)

    async def _requote(self) -> None:
        assert self.pos is not None
        desired = self._passive_price()
        if desired is None or (self.passive is not None and self.passive.price == desired):
            return
        now = self.sched.now_ms()
        wait = self._last_quote_ms + self.params.requote_ms - now
        if wait > 0:
            if not self._requote_pending:
                self._requote_pending = True
                self.sched.call_at(now + wait, Timer("requote", self._gen))
            return
        if self.passive is not None:
            await self._cancel_passive()
            if self.pos.qty == 0:
                return self._finish()
        self._last_quote_ms = now
        req = OrderRequest(
            self.ticker, self.pos.outcome, Action.SELL, desired, self.pos.qty, _coid("px"),
            ioc=False, post_only=True,
        )  # fmt: skip
        try:
            ex = await self.ex.place(req)
        except Exception as e:
            if not (isinstance(e, KalshiError) and 400 <= e.status < 500):
                self._cancel_all_needed = True  # it may be resting without our knowledge
            self._log("passive_rejected", error=str(e), price=desired)
            self._requote_pending = True
            self.sched.call_at(now + self.params.requote_ms, Timer("requote", self._gen))
            return
        self.passive = Passive(ex.order_id, desired, req.count)
        self._passives[ex.order_id] = self.passive
        self._log("passive_quote", order_id=ex.order_id, price=desired, count=str(req.count))
        if ex.filled > 0 and ex.avg_price is not None:  # crossed despite post_only
            self.passive.filled = ex.filled
            self._apply_exit(ex.filled, ex.avg_price, ex.fee, "passive")
            if self.pos.qty == 0:
                self._finish()

    async def _cancel_passive(self) -> None:
        """Cancel the resting exit order and book whatever filled before the cancel."""
        p, self.passive = self.passive, None
        if p is None:
            return
        try:
            resting = await self.ex.cancel(p.order_id, self.ticker)
        except Exception as e:
            # 404 = already gone (filled or cancelled). Either way, the fill stream
            # or the REST position check fills in what we can't see here.
            self._log("cancel_failed", order_id=p.order_id, error=str(e))
            self._cancel_all_needed = True
            self._resync_qty = True
            return
        p.final = p.count - resting
        missing = p.final - p.filled
        if missing > 0:
            fee = trade_fee(
                p.price, missing, is_maker=True,
                fee_type=self.fee_type, fee_multiplier=self.fee_multiplier,
            )  # fmt: skip
            p.filled = p.final
            self._apply_exit(missing, Decimal(p.price), fee, "passive")

    async def _on_fill(self, f: FillEvent) -> None:
        if f.trade_id in self._seen_trade_ids:
            return  # duplicate delivery
        self._seen_trade_ids.add(f.trade_id)
        p = self._passives.get(f.order_id)
        if p is None:
            # IOC fills are already booked from the order response; a late fill on a
            # closed trade's order is caught by the post-trade position check.
            if f.order_id not in self._ioc_ids:
                self._log("fill_untracked_order", order_id=f.order_id, count=str(f.count))
            return
        cap = p.final if p.final is not None else p.count
        q = min(f.count, cap - p.filled)
        if q <= 0 or self.pos is None:
            return
        p.filled += q
        fee = f.fee * q / f.count if f.count else _ZERO
        self._apply_exit(q, own_price(self.pos.outcome, f.yes_price), fee, "passive")
        if self.pos.qty == 0:
            if self.passive is p:
                self.passive = None
            self._finish()

    # ---- step 4: EXITING_FORCED ----------------------------------------------

    async def _go_forced(self, reason: str) -> None:
        if self.state is State.EXITING_FORCED or self.pos is None:
            return
        self.state = State.EXITING_FORCED
        self._gen += 1
        self._forced_attempts = 0
        self.trade.setdefault("path", []).append(reason)
        self._log("exit_forced", trade_id=self.trade.get("trade_id"), reason=reason)
        self._notify()
        if self.passive is not None:
            await self._cancel_passive()
        if self.pos.qty == 0:
            return self._finish()
        now = self.sched.now_ms()
        self.sched.call_at(now + self.params.flat_alert_ms, Timer("flat_alert", self._gen))
        await self._forced_attempt()

    async def _forced_attempt(self) -> None:
        assert self.pos is not None
        pos = self.pos
        self._forced_attempts += 1
        if self._cancel_all_needed:
            try:
                await self.ex.cancel_all()
                self._cancel_all_needed = False
            except Exception as e:
                self._log("cancel_all_failed", error=str(e))
        if self._resync_qty:
            await self._resync_from_exchange()
            if pos.qty == 0:
                return self._finish()

        bid = await self._current_bid()
        if bid is None:
            self._alert("no_bid", qty=str(pos.qty))
            return self._schedule_retry()
        limit = max(1, bid - self.params.forced_slip_cents)
        req = OrderRequest(
            self.ticker, pos.outcome, Action.SELL, limit, pos.qty, _coid("fx"),
            ioc=True, reduce_only=True,
        )  # fmt: skip
        try:
            ex = await self.ex.place(req)
        except KalshiError as e:
            if e.code in _MARKET_CLOSED_CODES:
                self._log("held_to_settlement", trade_id=self.trade.get("trade_id"), code=e.code)
                self.trade["path"].append("settlement")
                return self._finish(settled=True)
            if e.status >= 500:
                self._resync_qty = True
            self._alert("forced_exit_rejected", code=e.code, attempt=self._forced_attempts)
            return self._schedule_retry()
        except Exception as e:
            self._resync_qty = True  # it may have filled
            self._alert("forced_exit_error", error=str(e), attempt=self._forced_attempts)
            return self._schedule_retry()
        self._ioc_ids.add(ex.order_id)
        if ex.filled > 0 and ex.avg_price is not None:
            self._apply_exit(ex.filled, ex.avg_price, ex.fee, "forced")
        if pos.qty <= 0:
            self._finish()
        else:
            self._schedule_retry()

    async def _current_bid(self) -> int | None:
        assert self.pos is not None
        top = self.market.latest() if self.market.healthy else None
        if top is None:
            try:
                top = await self.ex.quote(self.ticker, self.sched.now_ms())
            except Exception as e:
                self._log("quote_failed", error=str(e))
                return None
        return top.bid(self.pos.outcome)

    async def _resync_from_exchange(self) -> None:
        assert self.pos is not None
        try:
            signed = await self.ex.position(self.ticker)
        except Exception as e:
            self._log("position_check_failed", error=str(e))
            return
        held = signed if self.pos.outcome is Outcome.YES else -signed
        held = max(held, _ZERO)
        if held != self.pos.qty:
            self._log("qty_resynced", engine=str(self.pos.qty), exchange=str(held))
            gone = self.pos.qty - held
            if gone > 0:  # sold at an unknown price; book at the current bid estimate
                top = self.market.latest()
                px = top.bid(self.pos.outcome) if top else None
                self._apply_exit(gone, Decimal(px or 0), _ZERO, "resync")
            else:
                self.pos.qty = held
        self._resync_qty = False

    def _schedule_retry(self) -> None:
        delay = min(self.params.forced_retry_ms * 2 ** (self._forced_attempts - 1), 1_000)
        self.sched.call_at(self.sched.now_ms() + delay, Timer("retry_exit", self._gen))

    # ---- timers --------------------------------------------------------------

    async def _on_timer(self, t: Timer) -> None:
        if t.kind == "check_flat":
            return await self._check_flat()
        if t.gen != self._gen:
            return  # stale: scheduled for an earlier phase
        if t.kind == "wait_done" and self.state is State.WAITING:
            await self._go_passive("wait_timeout")
        elif t.kind == "deadline" and self.state in (State.WAITING, State.EXITING_PASSIVE):
            await self._go_forced("deadline")
        elif t.kind == "requote" and self.state is State.EXITING_PASSIVE:
            self._requote_pending = False
            await self._requote()
        elif t.kind == "retry_exit" and self.state is State.EXITING_FORCED:
            await self._forced_attempt()
        elif t.kind == "flat_alert" and self.state is State.EXITING_FORCED and self.pos:
            self._alert("not_flat", qty=str(self.pos.qty), after_ms=self.params.flat_alert_ms)

    async def _check_flat(self) -> None:
        """After a trade, confirm with the exchange that we really are flat."""
        if self.state is not State.IDLE:
            return
        signed = await self.ex.position(self.ticker)
        if signed != 0:
            self._alert("position_mismatch", exchange=str(signed))
            self._adopt(signed, "post_trade_check")
            await self._go_forced("position_mismatch")

    # ---- bookkeeping ---------------------------------------------------------

    def _apply_exit(self, qty: Decimal, price: Decimal, fee: Decimal, how: str) -> None:
        assert self.pos is not None
        qty = min(qty, self.pos.qty)
        self.pos.qty -= qty
        self.pos.exit_qty += qty
        self.pos.exit_value += price * qty
        self.pos.fees += fee
        self.pos.exits.append(
            {"t_ms": self.sched.now_ms(), "how": how, "qty": str(qty), "price": str(price)}
        )
        self._notify()

    def _adopt(self, signed: Decimal, why: str) -> None:
        """Take over a position we didn't open (or lost track of). Entry price unknown,
        so P&L for it is not meaningful; it's flagged in the log."""
        outcome = Outcome.YES if signed > 0 else Outcome.NO
        qty = abs(signed)
        top = self.market.latest()
        px = Decimal((top.bid(outcome) if top else None) or 0)
        self.pos = Position(outcome, qty, self.sched.now_ms(), qty, px * qty)
        self.trade = {"trade_id": uuid.uuid4().hex[:12], "adopted": why, "path": []}
        self._log("position_adopted", why=why, signed=str(signed))

    def _finish(self, *, settled: bool = False) -> None:
        pos = self.pos
        assert pos is not None
        gross = pos.gross_dollars()
        net = gross - pos.fees
        if not settled and not self.trade.get("adopted"):
            self.realized_today += net
        record = {
            **self.trade,
            "avg_entry": str(pos.avg_entry),
            "entry_qty": str(pos.entry_qty),
            "exits": pos.exits,
            "gross": str(gross),
            "fees": str(pos.fees),
            "net": str(net),
            "settled": settled,
            "held_ms": self.sched.now_ms() - pos.t_fill,
            "realized_today": str(self.realized_today),
        }
        self._log("trade_closed", **record)
        if self.realized_today <= -self.params.daily_loss_limit:
            self._alert("daily_loss_limit", realized_today=str(self.realized_today))
        self._to_idle()
        self.sched.call_at(self.sched.now_ms() + 1_500, Timer("check_flat", self._gen))

    def _to_idle(self) -> None:
        self.state = State.IDLE
        self.pos = None
        self.passive = None
        self._passives.clear()
        self._gen += 1
        self._requote_pending = False
        self._notify()

    def _reject(self, base: dict[str, Any], reason: str) -> None:
        self._log("tap_rejected", **base, reason=reason)
        self._emit("rejected", {"reason": reason})

    def _log(self, kind: str, **fields: Any) -> None:
        self.events.write(kind, at_ms=self.sched.now_ms(), **fields)

    def _alert(self, kind: str, **fields: Any) -> None:
        self._log("alert", alert=kind, **fields)
        self._emit("alert", {"alert": kind, **fields})

    def _notify(self) -> None:
        self._emit("state", self.snapshot())

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.listener is not None:
            self.listener(kind, payload)


def _coid(prefix: str) -> str:
    return f"cs-{prefix}-{uuid.uuid4().hex[:24]}"


def _exec_dict(ex: Execution) -> dict[str, Any]:
    return {
        "order_id": ex.order_id,
        "filled": str(ex.filled),
        "avg_price": str(ex.avg_price) if ex.avg_price is not None else None,
        "fee": str(ex.fee),
        "exchange_ts_ms": ex.exchange_ts_ms,
    }
