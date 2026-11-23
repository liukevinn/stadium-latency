"""Find game-winner markets. Kalshi has no text search, so we fetch and filter here.

A game is one Kalshi event (e.g. KXNBAGAME-26NOV23OKCIND) with one market per
team. Each market's YES side is "this team wins"; `yes_sub_title` holds the team
name. Choosing "Team A" means choosing which of those markets to trade.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from courtside.kalshi.rest import KalshiREST

# Game-winner series for the leagues we care about. Every Kalshi sports series
# whose ticker matches GAME_SERIES_RE is also a game-winner series; use
# all_game_series() to list them.
DEFAULT_SERIES = (
    "KXNFLGAME",
    "KXNCAAFGAME",
    "KXNBAGAME",
    "KXNCAAMBGAME",
    "KXWNBAGAME",
    "KXNHLGAME",
    "KXMLBGAME",
)
GAME_SERIES_RE = re.compile(r"^KX[A-Z0-9]+GAME$")


@dataclass(frozen=True, slots=True)
class GameMarket:
    ticker: str
    team: str
    yes_bid: str  # dollars, as reported by REST (display only)
    yes_ask: str
    volume: str


@dataclass(frozen=True, slots=True)
class Game:
    event_ticker: str
    series_ticker: str
    title: str
    markets: tuple[GameMarket, ...]

    def matches(self, query: str) -> bool:
        """Every word of the query must appear in the title or a team name."""
        hay = " ".join([self.title, self.event_ticker, *(m.team for m in self.markets)]).lower()
        return all(word in hay for word in query.lower().split())


async def all_game_series(rest: KalshiREST) -> list[str]:
    data = await rest.request("GET", "/series", params={"category": "Sports"})
    return sorted(s["ticker"] for s in data.get("series", []) if GAME_SERIES_RE.match(s["ticker"]))


async def list_games(rest: KalshiREST, series: tuple[str, ...] = DEFAULT_SERIES) -> list[Game]:
    """All open games in the given series, each with its team markets."""
    per_series = await asyncio.gather(*(_series_games(rest, s) for s in series))
    return [g for games in per_series for g in games]


def search(games: list[Game], query: str) -> list[Game]:
    return [g for g in games if g.matches(query)]


async def _series_games(rest: KalshiREST, series_ticker: str) -> list[Game]:
    games: list[Game] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "status": "open",
            "with_nested_markets": "true",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        data = await rest.events(**params)
        for ev in data.get("events", []):
            markets = tuple(
                GameMarket(
                    ticker=m["ticker"],
                    team=m.get("yes_sub_title") or m["ticker"],
                    yes_bid=m.get("yes_bid_dollars", ""),
                    yes_ask=m.get("yes_ask_dollars", ""),
                    volume=m.get("volume_fp", ""),
                )
                for m in ev.get("markets", [])
            )
            games.append(Game(ev["event_ticker"], ev["series_ticker"], ev["title"], markets))
        cursor = data.get("cursor")
        if not cursor:
            return games
