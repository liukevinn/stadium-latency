"""Kalshi WebSocket feeds.

MarketFeed: order books and trades for a few markets (market-data host).
UserFeed:   fills on our own orders (the lower-latency user host).

Both share one connect / resubscribe / reconnect-with-backoff loop (_Socket).

MarketFeed:
  - connects with signed headers and subscribes to `orderbook_delta` and `trade`;
  - stamps each message with local receive time before doing anything else;
  - keeps an OrderBook and a BookHistory per market;
  - checks `seq` per subscription (sid). seq is shared across all markets in one
    subscription. On a gap it marks the books invalid and asks for fresh snapshots
    (update_subscription / get_snapshot), as Kalshi's docs require;
  - reconnects with backoff. Subscriptions don't survive a reconnect, so it
    resubscribes and waits for new snapshots.

Consumers get every raw message through `on_message(recv_ms, msg)`. The recorder
writes them to disk. The engine gets `on_top(ticker, top)` whenever a quote changes
(including the empty quote pushed when a book becomes invalid), and reads
`feed.history`. Synthetic lifecycle messages have types starting with "_"
(e.g. "_connected").

UserFeed turns `fill` messages into FillEvents. Fills can't be replayed after a
gap or disconnect. The engine covers that with cancel responses and its post-trade
REST position check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import websockets

from courtside.book import BookHistory, OrderBook, Top
from courtside.exchange import FillEvent
from courtside.kalshi.auth import Signer
from courtside.kalshi.units import parse_count, parse_dollars

log = logging.getLogger(__name__)

Message = dict[str, Any]
OnMessage = Callable[[int, Message], None]
OnTop = Callable[[str, Top], None]
OnFill = Callable[[FillEvent], None]

_BACKOFF_S = (0.25, 0.5, 1, 2, 4, 8)
_ZERO = Decimal(0)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class _Socket:
    """Signed connect, subscribe, read loop, reconnect with backoff."""

    name = "socket"

    def __init__(self, url: str, signer: Signer, on_message: OnMessage | None) -> None:
        self.url = url
        self.signer = signer
        self.on_message = on_message
        self.connected = asyncio.Event()
        self.stats = {"messages": 0, "gaps": 0, "reconnects": 0}
        self._ws: Any = None
        self._cmd_id = 0
        self._stopping = False
        self._next_seq: dict[int, int] = {}  # sid -> expected seq

    async def run(self) -> None:
        """Connect and process messages until stop(), reconnecting on failure."""
        attempt = 0
        while not self._stopping:
            try:
                headers = self.signer.headers("GET", urlsplit(self.url).path)
                async with websockets.connect(self.url, additional_headers=headers) as ws:
                    self._ws = ws
                    attempt = 0
                    await self._subscribe()
                    self._emit({"type": "_connected"})
                    async for raw in ws:
                        self.handle(now_ms(), json.loads(raw))
            except (TimeoutError, OSError, websockets.WebSocketException) as e:
                if self._stopping:
                    break
                log.warning("%s disconnected: %s", self.name, e)
            finally:
                self._ws = None
                self._on_disconnect(now_ms())
            if self._stopping:
                break
            self.stats["reconnects"] += 1
            self._emit({"type": "_disconnected"})
            await asyncio.sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
            attempt += 1

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            await self._ws.close()

    def handle(self, recv_ms: int, msg: Message) -> None:
        raise NotImplementedError

    async def _subscribe(self) -> None:
        raise NotImplementedError

    def _on_disconnect(self, t_ms: int) -> None:
        self.connected.clear()

    def _emit(self, msg: Message) -> None:
        if self.on_message is not None:
            self.on_message(now_ms(), msg)

    async def _send(self, cmd: str, params: dict[str, Any]) -> None:
        self._cmd_id += 1
        await self._ws.send(json.dumps({"id": self._cmd_id, "cmd": cmd, "params": params}))

    def _check_seq(self, msg: Message) -> bool:
        """Track seq per sid. Returns False on a gap (the message after the gap)."""
        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is None or seq is None:
            return True
        expected = self._next_seq.get(sid)
        self._next_seq[sid] = seq + 1
        if expected is not None and seq != expected and msg.get("type") != "orderbook_snapshot":
            self.stats["gaps"] += 1
            log.warning("%s seq gap on sid %s: expected %s got %s", self.name, sid, expected, seq)
            return False
        return True


class MarketFeed(_Socket):
    name = "market feed"

    def __init__(
        self,
        url: str,
        signer: Signer,
        tickers: list[str],
        on_message: OnMessage | None = None,
        *,
        on_top: OnTop | None = None,
        history_ms: int = 120_000,
    ) -> None:
        super().__init__(url, signer, on_message)
        self.tickers = list(tickers)
        self.on_top = on_top
        self.books = {t: OrderBook(t) for t in self.tickers}
        self.history = {t: BookHistory(history_ms) for t in self.tickers}
        self._sid_channel: dict[int, str] = {}

    # ---- message handling (pure apart from the resync send; unit-tested) -------

    def handle(self, recv_ms: int, msg: Message) -> None:
        self.stats["messages"] += 1
        if self.on_message is not None:
            self.on_message(recv_ms, msg)

        mtype = msg.get("type")
        if mtype == "subscribed":
            self._sid_channel[msg["msg"]["sid"]] = msg["msg"]["channel"]
            return
        if mtype == "error":
            log.error("market feed error: %s", msg.get("msg"))
            return

        if not self._check_seq(msg):
            sid = msg["sid"]
            if self._sid_channel.get(sid) == "orderbook_delta":
                self._invalidate_all(recv_ms)
                self._request_snapshots(sid)
            return  # don't apply a delta on top of an unknown state

        body = msg.get("msg", {})
        book = self.books.get(body.get("market_ticker", ""))
        if book is None:
            return
        if mtype == "orderbook_snapshot":
            book.apply_snapshot(body)
        elif mtype == "orderbook_delta":
            if not book.valid:
                return
            book.apply_delta(body)
        else:
            return
        if book.valid:
            self._push(book.top(recv_ms), book.ticker)
            if all(b.valid for b in self.books.values()):
                self.connected.set()

    # ---- internals -----------------------------------------------------------

    def _push(self, top: Top, ticker: str) -> None:
        if self.history[ticker].push(top) and self.on_top is not None:
            self.on_top(ticker, top)

    def _on_disconnect(self, t_ms: int) -> None:
        self._invalidate_all(t_ms)

    def _invalidate_all(self, t_ms: int) -> None:
        """Mark every book unknown. Pushes an empty quote into each history so that
        history.at(t) for t inside the outage returns "no price", not a stale one."""
        for b in self.books.values():
            if b.valid:
                b.valid = False
                self._push(Top(t_ms, None, _ZERO, None, _ZERO), b.ticker)
        self.connected.clear()

    async def _subscribe(self) -> None:
        self._next_seq.clear()
        self._sid_channel.clear()
        await self._send(
            "subscribe",
            {
                "channels": ["orderbook_delta"],
                "market_tickers": self.tickers,
                "use_yes_price": False,  # explicit: Kalshi plans to flip the default
            },
        )
        await self._send("subscribe", {"channels": ["trade"], "market_tickers": self.tickers})

    def _request_snapshots(self, sid: int) -> None:
        if self._ws is None:
            return
        params = {"sids": [sid], "market_tickers": self.tickers, "action": "get_snapshot"}
        asyncio.get_running_loop().create_task(self._send("update_subscription", params))


class UserFeed(_Socket):
    """Our own fills, from the `fill` channel on the user WebSocket host."""

    name = "user feed"

    def __init__(
        self,
        url: str,
        signer: Signer,
        on_fill: OnFill,
        on_message: OnMessage | None = None,
    ) -> None:
        super().__init__(url, signer, on_message)
        self.on_fill = on_fill

    def handle(self, recv_ms: int, msg: Message) -> None:
        self.stats["messages"] += 1
        if self.on_message is not None:
            self.on_message(recv_ms, msg)
        mtype = msg.get("type")
        if mtype == "subscribed":
            self.connected.set()
            return
        if mtype == "error":
            log.error("user feed error: %s", msg.get("msg"))
            return
        self._check_seq(msg)  # a gap can't be replayed; logged and counted only
        if mtype == "fill":
            self.on_fill(parse_fill(msg["msg"]))

    async def _subscribe(self) -> None:
        self._next_seq.clear()
        await self._send("subscribe", {"channels": ["fill"]})


def parse_fill(m: Message) -> FillEvent:
    return FillEvent(
        trade_id=m["trade_id"],
        order_id=m["order_id"],
        count=parse_count(m["count_fp"]),
        yes_price=parse_dollars(m["yes_price_dollars"]) * 100,
        fee=parse_dollars(m.get("fee_cost") or "0"),
        is_taker=bool(m.get("is_taker")),
        exchange_ts_ms=int(m.get("ts_ms") or 0),
    )
