"""Drive the engine from the terminal (demo only until Milestone 5).

    .venv/bin/python scripts/trade.py okc indiana --team indiana --size 1 --deadline 4000
    then type:  u = ▲ (Team A good)   d = ▼ (Team A bad)   f = FLATTEN   s = status   q = quit

    .venv/bin/python scripts/trade.py okc indiana --team indiana --size 1 --deadline 4000 --auto
    runs a scripted check: ▲ held to the deadline, then ▼ followed by FLATTEN.

State changes and alerts print as they happen. Events go to data/logs/<env>/<day>.jsonl,
and the raw book to data/recordings/<env>/.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Any

from courtside.config import load_settings
from courtside.discovery import Game, list_games, search
from courtside.engine import Direction, State
from courtside.kalshi.auth import Signer
from courtside.kalshi.rest import KalshiREST
from courtside.params import TradeParams
from courtside.trader import Trader


def pick_team(game: Game, team: str) -> str:
    hits = [
        m
        for m in game.markets
        if team.lower() in m.team.lower() or m.ticker.lower().endswith(team.lower())
    ]
    if len(hits) != 1:
        raise SystemExit(
            f"--team {team!r} must match exactly one of {[m.team for m in game.markets]}"
        )
    return hits[0].ticker


def printer(kind: str, payload: dict[str, Any]) -> None:
    if kind == "state":
        keys = ("state", "outcome", "qty", "avg_entry", "deadline_in_ms", "realized_today")
        print("  state", {k: payload[k] for k in keys})
    else:
        print(f"  {kind.upper()}", payload)


async def wait_idle(trader: Trader, timeout_s: float) -> None:
    assert trader.engine is not None
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while loop.time() < end:
        await asyncio.sleep(0.1)
        if trader.engine.state is State.IDLE and trader.queue.empty():
            return
    raise TimeoutError("engine did not return to IDLE")


async def auto(trader: Trader) -> None:
    deadline_s = trader.params.deadline_ms / 1000
    await asyncio.sleep(trader.params.lookback_ms / 1000 + 0.5)  # need a pre-tap quote
    print("auto: ▲ tap, hold to deadline")
    trader.tap(Direction.UP)
    await wait_idle(trader, deadline_s + 10)
    await asyncio.sleep(2)  # let the post-trade position check run
    print("auto: ▼ tap, then FLATTEN after 1 s")
    trader.tap(Direction.DOWN)
    await asyncio.sleep(1)
    trader.flatten()
    await wait_idle(trader, 10)
    await asyncio.sleep(2)


async def interactive(trader: Trader) -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = (await loop.run_in_executor(None, sys.stdin.readline)).strip().lower()
        if line in ("q", ""):
            return
        if line == "u":
            trader.tap(Direction.UP)
        elif line == "d":
            trader.tap(Direction.DOWN)
        elif line == "f":
            trader.flatten()
        elif line == "s" and trader.engine:
            print(json.dumps(trader.engine.snapshot(), indent=1))


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("query", nargs="+", help="game search words or event ticker")
    ap.add_argument("--team", required=True, help="Team A: team name or ticker suffix")
    ap.add_argument("--size", type=int, default=1)
    ap.add_argument("--deadline", type=int, help="deadline_ms (default: sport preset or 20000)")
    ap.add_argument("--sport", choices=["football", "baseball", "basketball"])
    ap.add_argument("--stop", type=int, default=0, help="stop_cents (0 = off)")
    ap.add_argument("--auto", action="store_true", help="run the scripted check and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

    settings = load_settings()
    if settings.env != "demo":
        raise SystemExit("trading runs on demo only until Milestone 5")
    signer = Signer.from_pem_file(settings.key_id, settings.key_path)

    params = TradeParams(
        size=args.size, max_position=max(3 * args.size, args.size), stop_cents=args.stop
    )
    if args.sport:
        params = params.for_sport(args.sport)
    if args.deadline:
        params = dataclasses.replace(params, deadline_ms=args.deadline, wait_max_ms=None)

    async with KalshiREST(settings.endpoints.rest, signer) as rest:
        games = await list_games(rest)
    q = " ".join(args.query)
    found = [g for g in games if g.event_ticker.lower() == q.lower()] or search(games, q)
    if len(found) != 1:
        raise SystemExit(f"{len(found)} games match {q!r}")
    game = found[0]
    ticker = pick_team(game, args.team)
    print(f"{game.title}: Team A = {ticker}  params={params}")

    trader = Trader(settings, signer, game, ticker, params, data_dir=Path("data"), listener=printer)
    await trader.start()
    print("ready. u/d/f/s/q" if not args.auto else "ready.")
    try:
        await (auto(trader) if args.auto else interactive(trader))
    finally:
        await trader.stop()


if __name__ == "__main__":
    asyncio.run(main())
