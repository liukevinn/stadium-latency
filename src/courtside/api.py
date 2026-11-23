"""Phone-facing server: the PWA, one WebSocket per phone, and an HTTP fallback.

Protocol (JSON). Phone -> server:
    {"type": "auth", "token": "..."}                      first WS message; else closed (4401)
    {"type": "tap", "dir": "up"|"down", "id", "client_ts"} live mode: trade; paper mode: MARK
    {"type": "flatten", "id", "client_ts"}
    {"type": "mode", "mode": "paper"|"live", "id"}
    {"type": "search", "q", "id"}                          -> {"type": "games", ...}
    {"type": "select", "event_ticker", "team_a", "sport", "id"}
    {"type": "params", "params": {...}, "id"}
    {"type": "portfolio", "id"}                            refresh the account view now
    {"type": "ping", "client_ts"}                          -> {"type": "pong", ...} (clock sync)
Server -> phone:
    {"type": "ack", "id", "ok", "t_server", "error"?, ...}  for every command
    {"type": "snapshot", ...}   full state on connect and after every change
    {"type": "state" | "quote" | "alert" | "rejected", ...}  pushed as they happen
    {"type": "portfolio", ...}  Kalshi positions + cash, every 3 s while a phone is connected

`client_ts` is the phone's clock corrected to the server clock (the phone estimates
the offset from ping/pong), so the server can log phone→server latency.

Auth: one long random token (COURTSIDE_TOKEN), compared in constant time. On the
WebSocket it goes in the first message, so it never appears in URLs or access logs.
The phone keeps it in localStorage. Static files hold no secrets and are public.
Serve over HTTPS in production: phones need a secure context for Wake Lock and
service workers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from courtside.config import Settings
from courtside.discovery import Game, list_games, search
from courtside.engine import Direction
from courtside.kalshi.auth import Signer
from courtside.kalshi.rest import KalshiREST
from courtside.kalshi.ws import now_ms
from courtside.params import SPORT_DEADLINE_MS, TradeParams, params_from_dict, params_to_dict
from courtside.portfolio import fetch_portfolio
from courtside.trader import Trader, quote_dict

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
_GAMES_TTL_S = 60
_CLIENT_QUEUE_MAX = 200
_SLOW_COMMANDS = {"search", "select", "portfolio"}
_PORTFOLIO_EVERY_S = 3  # while a phone is connected
_PORTFOLIO_IDLE_EVERY_S = 30


# ---- fan-out to connected phones -------------------------------------------------


class Hub:
    """Pushes messages to every connected phone. Quotes are coalesced to <= 10/s."""

    def __init__(self) -> None:
        self.clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._quote: dict[str, Any] | None = None
        self._quote_scheduled = False

    def publish(self, msg: dict[str, Any]) -> None:
        for q in list(self.clients):
            if q.qsize() >= _CLIENT_QUEUE_MAX:  # a stuck phone must not grow memory
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(msg)

    def publish_quote(self, quote: dict[str, Any]) -> None:
        self._quote = quote
        if not self._quote_scheduled:
            self._quote_scheduled = True
            asyncio.get_running_loop().call_later(0.1, self._flush_quote)

    def _flush_quote(self) -> None:
        self._quote_scheduled = False
        if self._quote is not None:
            self.publish({"type": "quote", **self._quote})


# ---- server-side session: one selected game, paper or live -------------------------


class Session:
    def __init__(
        self,
        settings: Settings,
        signer: Signer,
        *,
        trading_allowed: bool,
        data_dir: Path = Path("data"),
    ) -> None:
        self.settings = settings
        self.signer = signer
        self.trading_allowed = trading_allowed
        self.data_dir = data_dir
        self.hub = Hub()
        self.rest = KalshiREST(settings.endpoints.rest, signer)
        self.trader: Trader | None = None
        self.sport: str | None = None
        self.mode = "paper"
        self.state_path = data_dir / "server_state.json"
        self.params = TradeParams()
        self._games: tuple[float, list[Game]] | None = None
        self.portfolio: dict[str, Any] | None = None
        self._portfolio_task: asyncio.Task[None] | None = None
        self._select_lock = asyncio.Lock()
        self._load_state()

    # ---- lifecycle ----------------------------------------------------------

    async def resume(self) -> None:
        """Start background work, and after a restart reopen the last selected game
        (the engine reconciles)."""
        self._portfolio_task = asyncio.create_task(self._portfolio_loop())
        saved = self._read_state().get("selection")
        if not saved:
            return
        try:
            await self.select(saved["event_ticker"], saved["team_a"], saved.get("sport"))
            log.info("resumed %s", saved)
        except Exception as e:
            log.warning("could not resume %s: %s", saved, e)

    async def close(self) -> None:
        if self._portfolio_task is not None:
            self._portfolio_task.cancel()
        if self.trader is not None:
            await self.trader.stop()
        await self.rest.aclose()

    # ---- commands -----------------------------------------------------------

    async def handle(self, msg: dict[str, Any], t_recv: int) -> dict[str, Any]:
        """Run one phone command. Always returns an ack (ok or error)."""
        kind = msg.get("type")
        ack: dict[str, Any] = {"type": "ack", "id": msg.get("id"), "cmd": kind, "t_server": t_recv}
        client_ts = msg.get("client_ts")
        if isinstance(client_ts, int | float):
            ack["phone_to_server_ms"] = t_recv - int(client_ts)
        try:
            if kind == "ping":
                return {"type": "pong", "client_ts": client_ts, "server_ts": now_ms()}
            if kind == "tap":
                ack.update(self._tap(msg, t_recv))
            elif kind == "flatten":
                self._require_trader().flatten()
            elif kind == "mode":
                self._set_mode(msg.get("mode"))
            elif kind == "search":
                ack["games"] = [_game_dict(g) for g in await self.search(str(msg.get("q", "")))]
            elif kind == "select":
                await self.select(msg["event_ticker"], msg["team_a"], msg.get("sport"))
            elif kind == "params":
                self._set_params(msg.get("params") or {})
            elif kind == "portfolio":
                await self.refresh_portfolio()
            else:
                raise ValueError(f"unknown command {kind!r}")
            ack["ok"] = True
        except Exception as e:
            ack.update(ok=False, error=str(e))
        if kind in ("mode", "select", "params"):
            self.hub.publish(self.snapshot())
        return ack

    def _tap(self, msg: dict[str, Any], t_recv: int) -> dict[str, Any]:
        trader = self._require_trader()
        direction = Direction(msg["dir"])
        client_ts = int(msg["client_ts"]) if msg.get("client_ts") is not None else None
        if self.mode == "live":
            trader.tap(direction, client_ts, msg.get("id"), t_ms=t_recv)
            return {"traded": True}
        rec = trader.mark(direction, client_ts, msg.get("id"), t_ms=t_recv)
        return {"traded": False, "mark": rec}

    def _set_mode(self, mode: Any) -> None:
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        if mode == "live" and not (self.trader and self.trader.engine):
            raise RuntimeError("live trading is not enabled on this server")
        self.mode = mode
        log.info("mode -> %s", mode)

    def _set_params(self, changes: dict[str, Any]) -> None:
        params = params_from_dict({**params_to_dict(self.params), **changes})
        if self.trader is not None:
            self.trader.set_params(params)  # raises if a trade is open
        self.params = params
        self._save_state()

    async def search(self, q: str) -> list[Game]:
        now = time.monotonic()
        if self._games is None or now - self._games[0] > _GAMES_TTL_S:
            self._games = (now, await list_games(self.rest))
        games = self._games[1]
        return sorted(search(games, q) if q else games, key=lambda g: g.event_ticker)[:50]

    async def select(self, event_ticker: str, team_a: str, sport: str | None) -> None:
        async with self._select_lock:
            if self.trader and self.trader.engine and self.trader.engine.pos is not None:
                raise RuntimeError("flatten before switching games")
            games = await self.search("")
            game = next((g for g in games if g.event_ticker == event_ticker), None)
            if game is None:
                raise ValueError(f"no open game {event_ticker}")
            if sport is not None:
                if sport not in SPORT_DEADLINE_MS:
                    raise ValueError(f"unknown sport {sport!r}")
                self.params = self.params.for_sport(sport)
            if self.trader is not None:
                await self.trader.stop()
                self.trader = None
            trader = Trader(
                self.settings, self.signer, game, team_a, self.params,
                trading=self.trading_allowed, data_dir=self.data_dir, listener=self._listener,
            )  # fmt: skip
            await trader.start()
            self.trader = trader
            self.sport = sport
            self.mode = "paper"  # every new game starts in paper mode
            self._save_state(
                selection={"event_ticker": event_ticker, "team_a": team_a, "sport": sport}
            )

    async def refresh_portfolio(self) -> None:
        self.portfolio = (await fetch_portfolio(self.rest, now_ms())).to_dict()
        self.hub.publish({"type": "portfolio", **self.portfolio})

    async def _portfolio_loop(self) -> None:
        """Refresh every 3 s while a phone is connected, every 30 s otherwise. Checks
        once a second, so a phone that just connected gets fresh data within ~1 s."""
        last = -1e9
        while True:
            every = _PORTFOLIO_EVERY_S if self.hub.clients else _PORTFOLIO_IDLE_EVERY_S
            if time.monotonic() - last >= every:
                last = time.monotonic()
                try:
                    await self.refresh_portfolio()
                except Exception as e:  # keep polling; the phone shows the last good copy
                    log.warning("portfolio refresh failed: %s", e)
            await asyncio.sleep(1)

    def snapshot(self) -> dict[str, Any]:
        t = self.trader
        market = None
        if t is not None:
            market = {
                "event_ticker": t.game.event_ticker,
                "title": t.game.title,
                "team_a": t.ticker,
                "teams": {m.ticker: m.team for m in t.game.markets},
            }
        return {
            "type": "snapshot",
            "env": self.settings.env,
            "trading_allowed": bool(t and t.engine),
            "mode": self.mode,
            "sport": self.sport,
            "market": market,
            "engine": t.engine.snapshot() if t and t.engine else None,
            "quote": quote_dict(t.quote(), healthy=t.view.healthy) if t else None,
            "params": params_to_dict(self.params),
            "portfolio": self.portfolio,
        }

    # ---- internals ------------------------------------------------------------

    def _require_trader(self) -> Trader:
        if self.trader is None:
            raise RuntimeError("no game selected")
        return self.trader

    def _listener(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "quote":
            self.hub.publish_quote(payload)
        else:
            self.hub.publish({"type": kind, **payload})

    def _read_state(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = json.loads(self.state_path.read_text())
            return data
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _load_state(self) -> None:
        saved = self._read_state()
        if "params" in saved:
            try:
                self.params = params_from_dict(saved["params"])
            except (ValueError, TypeError) as e:
                log.warning("ignoring saved params: %s", e)

    def _save_state(self, **extra: Any) -> None:
        data = {**self._read_state(), "params": params_to_dict(self.params), **extra}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, indent=1))


def _game_dict(g: Game) -> dict[str, Any]:
    return {
        "event_ticker": g.event_ticker,
        "title": g.title,
        "series": g.series_ticker,
        "markets": [
            {"ticker": m.ticker, "team": m.team, "yes_bid": m.yes_bid, "yes_ask": m.yes_ask}
            for m in g.markets
        ],
    }


# ---- HTTP / WebSocket app -----------------------------------------------------------


def create_app(session: Any, token: str, lifespan: Any = None) -> Starlette:
    """`session` needs handle(msg, t_recv), snapshot(), and a `hub`. Tests pass a stub."""
    if len(token) < 32:
        raise ValueError("COURTSIDE_TOKEN must be at least 32 characters")

    def authorized(candidate: Any) -> bool:
        return isinstance(candidate, str) and hmac.compare_digest(candidate, token)

    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5))
        except (TimeoutError, ValueError, WebSocketDisconnect):
            await ws.close(code=4401)
            return
        if first.get("type") != "auth" or not authorized(first.get("token")):
            await ws.close(code=4401)
            return

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        session.hub.clients.add(queue)
        queue.put_nowait(session.snapshot())

        async def writer() -> None:
            while True:
                await ws.send_text(json.dumps(await queue.get(), default=str))

        async def _reply(msg: dict[str, Any], t_recv: int) -> None:
            queue.put_nowait(await session.handle(msg, t_recv))

        send_task = asyncio.create_task(writer())
        pending: set[asyncio.Task[None]] = set()
        try:
            while True:
                raw = await ws.receive_text()
                t_recv = now_ms()  # stamp before parsing: this is the tap time
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("type") in _SLOW_COMMANDS:  # never make a tap wait behind these
                    pending.add(asyncio.create_task(_reply(msg, t_recv)))
                    pending = {t for t in pending if not t.done()}
                else:
                    queue.put_nowait(await session.handle(msg, t_recv))
        except WebSocketDisconnect:
            pass
        finally:
            session.hub.clients.discard(queue)
            for t in pending:
                t.cancel()
            send_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await send_task

    async def command(request: Request) -> Response:
        t_recv = now_ms()
        auth = request.headers.get("authorization", "")
        if not authorized(auth.removeprefix("Bearer ")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            msg = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        return JSONResponse(json.loads(json.dumps(await session.handle(msg, t_recv), default=str)))

    async def state(request: Request) -> Response:
        auth = request.headers.get("authorization", "")
        if not authorized(auth.removeprefix("Bearer ")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(json.loads(json.dumps(session.snapshot(), default=str)))

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})

    return Starlette(
        lifespan=lifespan,
        routes=[
            WebSocketRoute("/ws", ws_endpoint),
            Route("/api/command", command, methods=["POST"]),
            Route("/api/state", state),
            Route("/api/health", health),
            Mount("/", StaticFiles(directory=WEB_DIR, html=True)),
        ],
    )
