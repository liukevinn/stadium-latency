"""Runs one game for real: feeds, always-on recorder, MARKs, and (optionally) the engine.

    trader = Trader(settings, signer, game, team_a_ticker, params, trading=True)
    await trader.start()      # connect; if trading, reconcile and start the engine loop
    trader.mark(Direction.UP) # paper mode: log what the book looked like, no order
    trader.tap(Direction.UP)  # live mode (trading=True only)
    trader.flatten()
    await trader.stop()

trading=False needs only market data (a read-only key is enough). It records the
game and accepts MARKs, which is the evidence-gathering mode of CLAUDE.md §8/§9.

Everything the engine does runs on one asyncio loop. tap() and flatten() stamp
the server receive time and queue the event; the engine handles events in order.
The market feed also records every raw message of the game to disk (CLAUDE.md §9),
so the book from -30 s to +60 s around every tap is always available.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from courtside.book import Top
from courtside.clock import ntp_offset_ms
from courtside.config import Settings
from courtside.discovery import Game
from courtside.engine import (
    BookUpdate,
    Direction,
    Engine,
    Event,
    Fill,
    Flatten,
    Listener,
    MarketView,
    State,
    Tap,
)
from courtside.exchange import FillEvent, KalshiExchange
from courtside.kalshi.auth import Signer
from courtside.kalshi.rest import KalshiREST
from courtside.kalshi.ws import MarketFeed, Message, UserFeed, now_ms
from courtside.params import TradeParams
from courtside.recording import RecordingWriter
from courtside.store import EventLog, realized_today

log = logging.getLogger(__name__)


class AsyncioScheduler:
    """Real clock; timers put their event on the engine queue."""

    def __init__(self, queue: asyncio.Queue[Event]) -> None:
        self.queue = queue

    def now_ms(self) -> int:
        return now_ms()

    def call_at(self, t_ms: int, event: Event) -> None:
        delay = max(0, t_ms - now_ms()) / 1000
        asyncio.get_running_loop().call_later(delay, self.queue.put_nowait, event)


class FeedView:
    """The engine's MarketView over a MarketFeed, for one ticker."""

    def __init__(self, feed: MarketFeed, ticker: str) -> None:
        self.feed = feed
        self.ticker = ticker

    @property
    def healthy(self) -> bool:
        return self.feed.books[self.ticker].valid

    def at(self, t_ms: int) -> Top | None:
        return self.feed.history[self.ticker].at(t_ms)

    def latest(self) -> Top | None:
        return self.feed.history[self.ticker].latest()


class Trader:
    def __init__(
        self,
        settings: Settings,
        signer: Signer,
        game: Game,
        ticker: str,
        params: TradeParams,
        *,
        trading: bool = False,
        data_dir: Path = Path("data"),
        listener: Listener | None = None,
    ) -> None:
        if ticker not in {m.ticker for m in game.markets}:
            raise ValueError(f"{ticker} is not a market of {game.event_ticker}")
        self.settings = settings
        self.signer = signer
        self.game = game
        self.ticker = ticker
        self.params = params
        self.trading = trading
        self.data_dir = data_dir
        self.listener = listener
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.rest = KalshiREST(settings.endpoints.rest, signer)
        self.recorder: RecordingWriter | None = None
        self.events: EventLog | None = None
        self.engine: Engine | None = None
        self.user_feed: UserFeed | None = None
        self._tasks: list[asyncio.Task[Any]] = []

    # ---- lifecycle -------------------------------------------------------------

    async def start(self, *, feed_timeout_s: float = 15) -> None:
        env = self.settings.env
        day = datetime.now().strftime("%Y-%m-%d")
        log_path = self.data_dir / "logs" / env / f"{day}.jsonl"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        rec_path = self.data_dir / "recordings" / env / f"{self.game.event_ticker}-{stamp}.jsonl"

        fee_type, fee_mult = await self._fee_model()
        offset = await ntp_offset_ms()
        self.recorder = RecordingWriter(
            rec_path,
            {
                "env": env,
                "event_ticker": self.game.event_ticker,
                "title": self.game.title,
                "markets": [{"ticker": m.ticker, "team": m.team} for m in self.game.markets],
                "team_a_ticker": self.ticker,
                "clock_offset_ms": offset,
                "started_utc": datetime.now(UTC).isoformat(),
            },
            now_ms=now_ms(),
        )
        self.events = EventLog(log_path)
        self.events.write(
            "session_start", at_ms=now_ms(), ticker=self.ticker, env=env, trading=self.trading,
            params=_params_dict(self.params), fee_type=fee_type, fee_multiplier=fee_mult,
            clock_offset_ms=offset, recording=str(rec_path),
        )  # fmt: skip

        tickers = [m.ticker for m in self.game.markets]
        self.market_feed = MarketFeed(
            self.settings.endpoints.ws, self.signer, tickers,
            on_message=self._record, on_top=self._on_top,
        )  # fmt: skip
        self.view = FeedView(self.market_feed, self.ticker)
        self._tasks += [
            asyncio.create_task(self.market_feed.run()),
            asyncio.create_task(self._flush_loop()),
        ]
        await asyncio.wait_for(self.market_feed.connected.wait(), feed_timeout_s)
        if not self.trading:
            return

        self.user_feed = UserFeed(
            self.settings.endpoints.user_ws, self.signer, self._on_fill, on_message=self._record
        )
        self._tasks.append(asyncio.create_task(self.user_feed.run()))
        await asyncio.wait_for(self.user_feed.connected.wait(), feed_timeout_s)

        self.engine = Engine(
            ticker=self.ticker,
            exchange=KalshiExchange(self.rest),
            market=self.view,
            scheduler=AsyncioScheduler(self.queue),
            params=self.params,
            log_=self.events,
            realized_today=realized_today(log_path),
            fee_type=fee_type,
            fee_multiplier=fee_mult,
            listener=self.listener,
        )
        await self.engine.start()  # reconcile before accepting taps
        self._tasks.append(asyncio.create_task(self._engine_loop()))

    async def stop(self) -> None:
        await self.market_feed.stop()
        if self.user_feed is not None:
            await self.user_feed.stop()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if self.recorder is not None:
            self.recorder.close()
        if self.events is not None:
            self.events.close()
        await self.rest.aclose()

    # ---- inputs ----------------------------------------------------------------

    def tap(
        self,
        direction: Direction,
        client_ts_ms: int | None = None,
        tap_id: str | None = None,
        t_ms: int | None = None,
    ) -> None:
        if self.engine is None:
            raise RuntimeError("trading is off for this session (paper mode only)")
        self.queue.put_nowait(Tap(direction, t_ms or now_ms(), client_ts_ms, tap_id))

    def flatten(self) -> None:
        if self.engine is None:
            raise RuntimeError("trading is off for this session")
        self.queue.put_nowait(Flatten(now_ms()))

    def mark(
        self,
        direction: Direction,
        client_ts_ms: int | None = None,
        mark_id: str | None = None,
        t_ms: int | None = None,
    ) -> dict[str, Any]:
        """Paper mode: log the tap and the book around it, without trading."""
        rec = mark_record(
            direction, t_ms or now_ms(), self.view, self.params.lookback_ms,
            client_ts_ms=client_ts_ms, mark_id=mark_id, ticker=self.ticker,
        )  # fmt: skip
        assert self.events is not None
        self.events.write("mark", at_ms=now_ms(), **rec)
        return rec

    def set_params(self, params: TradeParams) -> None:
        """Change parameters. Only allowed while flat, so a trade never changes rules midway."""
        params.validate()
        if self.engine is not None:
            if self.engine.state is not State.IDLE:
                raise RuntimeError("can't change settings while a trade is open")
            self.engine.params = params
        self.params = params
        if self.events is not None:
            self.events.write("params_changed", at_ms=now_ms(), params=_params_dict(params))

    def quote(self) -> Top | None:
        return self.view.latest()

    def quotes(self) -> dict[str, dict[str, Any]]:
        """Latest quote for every market in the game (both teams), for the phone."""
        out = {}
        for ticker, hist in self.market_feed.history.items():
            healthy = self.market_feed.books[ticker].valid
            out[ticker] = quote_dict(hist.latest(), healthy=healthy, ticker=ticker)
        return out

    # ---- internals -------------------------------------------------------------

    async def _engine_loop(self) -> None:
        assert self.engine is not None
        while True:
            ev = await self.queue.get()
            await self.engine.handle(ev)

    def _on_top(self, ticker: str, top: Top) -> None:
        if ticker == self.ticker and self.engine is not None:
            self.queue.put_nowait(BookUpdate(top))
        if self.listener is not None:
            healthy = self.market_feed.books[ticker].valid
            self.listener("quote", quote_dict(top, healthy=healthy, ticker=ticker))

    def _on_fill(self, fill: FillEvent) -> None:
        self.queue.put_nowait(Fill(fill))

    def _record(self, recv_ms: int, msg: Message) -> None:
        if self.recorder is not None:
            self.recorder.write(recv_ms, msg)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self.recorder is not None:
                self.recorder.flush()

    async def _fee_model(self) -> tuple[str, float]:
        """Series fee settings, with any event-level override."""
        ev = (await self.rest.event(self.game.event_ticker))["event"]
        series = await self.rest.series(ev["series_ticker"])
        fee_type = ev.get("fee_type_override") or series["fee_type"]
        mult = float(ev.get("fee_multiplier_override") or series["fee_multiplier"])
        return fee_type, mult


def _params_dict(p: TradeParams) -> dict[str, Any]:
    return {f: str(getattr(p, f)) for f in p.__dataclass_fields__}


def quote_dict(top: Top | None, *, healthy: bool, ticker: str | None = None) -> dict[str, Any]:
    if top is None:
        return {
            "ticker": ticker,
            "healthy": healthy,
            "yes_bid": None,
            "yes_ask": None,
            "t_ms": None,
        }
    return {
        "ticker": ticker,
        "healthy": healthy,
        "yes_bid": top.yes_bid,
        "yes_ask": top.yes_ask,
        "yes_bid_size": str(top.yes_bid_size),
        "yes_ask_size": str(top.yes_ask_size),
        "t_ms": top.t_ms,
    }


def mark_record(
    direction: Direction,
    t_ms: int,
    view: MarketView,
    lookback_ms: int,
    *,
    client_ts_ms: int | None,
    mark_id: str | None,
    ticker: str,
) -> dict[str, Any]:
    """What a MARK records: the tap, the live quote, and the pre-play anchor quote.
    The full book around it is in the game recording (Milestone 4 replays it)."""
    outcome = direction.outcome
    top, anchor = view.latest(), view.at(t_ms - lookback_ms)
    return {
        "mark_id": mark_id,
        "direction": direction.value,
        "ticker": ticker,
        "t_ms": t_ms,
        "client_ts_ms": client_ts_ms,
        "healthy": view.healthy,
        "yes_bid": top.yes_bid if top else None,
        "yes_ask": top.yes_ask if top else None,
        "anchor_yes_bid": anchor.yes_bid if anchor else None,
        "anchor_yes_ask": anchor.yes_ask if anchor else None,
        "anchor_ask": anchor.ask(outcome) if anchor else None,  # in the tapped outcome's terms
        "ask_now": top.ask(outcome) if top else None,
    }
