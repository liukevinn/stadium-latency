"""Find games and record their order books to disk (no trading).

    .venv/bin/python scripts/record.py games                 # list open games
    .venv/bin/python scripts/record.py games duke             # search by team/title words
    .venv/bin/python scripts/record.py record duke clemson    # record the one matching game
    .venv/bin/python scripts/record.py record KXNBAGAME-26NOV23OKCIND --minutes 30

Recording writes every raw WS message, stamped with local receive time, to
data/recordings/<env>/<event>-<utc start>.jsonl, gzipped on exit. A status line
prints every 5 s. Stop with Ctrl-C; the file is closed cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import statistics
from datetime import UTC, datetime
from pathlib import Path

from courtside.clock import ntp_offset_ms
from courtside.config import load_settings
from courtside.discovery import Game, list_games, search
from courtside.kalshi.auth import Signer
from courtside.kalshi.rest import KalshiREST
from courtside.kalshi.ws import MarketFeed, Message, now_ms
from courtside.recording import RecordingWriter


def show_games(games: list[Game]) -> None:
    for g in sorted(games, key=lambda g: g.event_ticker):
        teams = "  |  ".join(f"{m.team} {m.yes_bid}/{m.yes_ask}" for m in g.markets)
        print(f"{g.event_ticker:<36} {g.title[:44]:<44} {teams}")


async def resolve(rest: KalshiREST, query: list[str]) -> Game:
    games = await list_games(rest)
    q = " ".join(query)
    exact = [g for g in games if g.event_ticker.lower() == q.lower()]
    found = exact or search(games, q)
    if len(found) != 1:
        show_games(found)
        raise SystemExit(f"{len(found)} games match {q!r}; narrow the query")
    return found[0]


async def record(
    feed: MarketFeed, writer: RecordingWriter, minutes: float, clock_offset_ms: float
) -> None:
    lat: list[float] = []  # exchange ts_ms -> local receive, clock-corrected

    def on_message(recv_ms: int, msg: Message) -> None:
        writer.write(recv_ms, msg)
        ts = (msg.get("msg") or {}).get("ts_ms")
        if isinstance(ts, int):
            lat.append(recv_ms + clock_offset_ms - ts)

    feed.on_message = on_message
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop.set)
    loop.add_signal_handler(signal.SIGTERM, stop.set)

    task = asyncio.create_task(feed.run())
    deadline = loop.time() + minutes * 60 if minutes else None
    try:
        while not stop.is_set() and (deadline is None or loop.time() < deadline):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)
            writer.flush()
            parts = []
            for t, hist in feed.history.items():
                top = hist.latest()
                quote = f"{top.yes_bid}/{top.yes_ask}" if top else "-"
                parts.append(f"{t.rsplit('-', 1)[-1]} {quote}")
            p50 = statistics.median(lat) if lat else float("nan")
            print(
                f"{datetime.now().strftime('%H:%M:%S')}  {'  '.join(parts)}  "
                f"msgs={feed.stats['messages']} gaps={feed.stats['gaps']} "
                f"reconnects={feed.stats['reconnects']} lag_p50={p50:.0f}ms (n={len(lat)})"
            )
            lat.clear()
    finally:
        await feed.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        path = writer.close()
        print(f"saved {path} ({writer.lines} lines)")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("games", help="list/search open games")
    g.add_argument("query", nargs="*")
    r = sub.add_parser("record", help="record one game's books")
    r.add_argument("query", nargs="+", help="search words or an exact event ticker")
    r.add_argument(
        "--minutes", type=float, default=0, help="stop after N minutes (0 = until Ctrl-C)"
    )
    r.add_argument("--out", type=Path, default=Path("data/recordings"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = load_settings()
    signer = Signer.from_pem_file(settings.key_id, settings.key_path)
    async with KalshiREST(settings.endpoints.rest, signer) as rest:
        if args.cmd == "games":
            games = await list_games(rest)
            show_games(search(games, " ".join(args.query)) if args.query else games)
            return
        game = await resolve(rest, args.query)

    tickers = [m.ticker for m in game.markets]
    print(
        f"recording {game.event_ticker}: {game.title}  [{', '.join(tickers)}]  env={settings.env}"
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = args.out / settings.env / f"{game.event_ticker}-{stamp}.jsonl"
    offset = await ntp_offset_ms()
    print(f"local clock offset vs NTP: {offset} ms (true time = local + offset)")
    meta = {
        "env": settings.env,
        "clock_offset_ms": offset,
        "event_ticker": game.event_ticker,
        "title": game.title,
        "markets": [{"ticker": m.ticker, "team": m.team} for m in game.markets],
        "started_utc": datetime.now(UTC).isoformat(),
    }
    writer = RecordingWriter(path, meta, now_ms=now_ms())
    feed = MarketFeed(settings.endpoints.ws, signer, tickers)
    await record(feed, writer, args.minutes, offset or 0.0)


if __name__ == "__main__":
    asyncio.run(main())
