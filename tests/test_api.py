"""Phone API tests: auth, command routing, the HTTP fallback, and paper/live rules.

No Kalshi connection: the app tests use a stub session, and the Session tests put
a stub trader in place of a real one.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from courtside.api import Hub, Session, create_app
from courtside.config import ENDPOINTS, Settings
from courtside.engine import Direction
from courtside.kalshi.auth import Signer
from courtside.params import TradeParams, params_from_dict, params_to_dict

TOKEN = "t" * 40


class StubSession:
    def __init__(self) -> None:
        self.hub = Hub()
        self.seen: list[tuple[dict[str, Any], int]] = []

    async def handle(self, msg: dict[str, Any], t_recv: int) -> dict[str, Any]:
        self.seen.append((msg, t_recv))
        return {"type": "ack", "id": msg.get("id"), "ok": True, "t_server": t_recv}

    def snapshot(self) -> dict[str, Any]:
        return {"type": "snapshot", "mode": "paper"}


def client() -> tuple[TestClient, StubSession]:
    s = StubSession()
    return TestClient(create_app(s, TOKEN)), s


def test_short_token_refused() -> None:
    with pytest.raises(ValueError):
        create_app(StubSession(), "short")


def test_ws_requires_auth_first() -> None:
    c, s = client()
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": "wrong" * 10})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    assert e.value.code == 4401
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "tap", "dir": "up"})  # skipping auth
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert s.seen == []


def test_ws_snapshot_then_acks() -> None:
    c, s = client()
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"type": "tap", "dir": "up", "id": "a1"})
        ack = ws.receive_json()
        assert ack["id"] == "a1" and ack["ok"]
        s.hub.publish({"type": "alert", "alert": "x"})  # pushes reach the phone
        assert ws.receive_json()["alert"] == "x"
    assert s.seen[0][0]["dir"] == "up"
    assert isinstance(s.seen[0][1], int)  # receive time stamped by the server


def test_http_fallback_needs_bearer_token() -> None:
    c, s = client()
    assert c.post("/api/command", json={"type": "flatten"}).status_code == 401
    r = c.post(
        "/api/command",
        json={"type": "flatten", "id": "f1"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 200 and r.json()["id"] == "f1"
    assert c.get("/api/state").status_code == 401


def test_static_pwa_is_served() -> None:
    c, _ = client()
    page = c.get("/")
    assert page.status_code == 200 and "Courtside" in page.text
    assert c.get("/app.js").status_code == 200
    assert c.get("/manifest.webmanifest").status_code == 200


# ---- Session rules (with a stub trader) ----------------------------------------------


class StubTrader:
    def __init__(self, *, engine: bool) -> None:
        self.engine = object() if engine else None
        self.calls: list[tuple[str, Any]] = []
        self.params = TradeParams()

    def tap(self, d: Direction, client_ts: Any, tap_id: Any, t_ms: int) -> None:
        self.calls.append(("tap", d))

    def mark(self, d: Direction, client_ts: Any, mark_id: Any, t_ms: int) -> dict[str, Any]:
        self.calls.append(("mark", d))
        return {"direction": d.value}

    def flatten(self) -> None:
        self.calls.append(("flatten", None))

    def set_params(self, p: TradeParams) -> None:
        self.calls.append(("params", p))


def session(tmp_path: Path, *, engine: bool) -> tuple[Session, StubTrader]:
    settings = Settings("demo", "k", tmp_path / "k.pem", ENDPOINTS["demo"])
    s = Session(settings, Signer("k", ed25519.Ed25519PrivateKey.generate()),
                trading_allowed=engine, data_dir=tmp_path)  # fmt: skip
    t = StubTrader(engine=engine)
    s.trader = t  # type: ignore[assignment]
    s.snapshot = lambda: {"type": "snapshot"}  # type: ignore[method-assign]
    return s, t


async def test_paper_mode_taps_become_marks(tmp_path: Path) -> None:
    s, t = session(tmp_path, engine=True)
    ack = await s.handle({"type": "tap", "dir": "up", "client_ts": 990}, 1000)
    assert ack["ok"] and ack["traded"] is False and ack["phone_to_server_ms"] == 10
    assert t.calls == [("mark", Direction.UP)]


async def test_live_mode_taps_trade(tmp_path: Path) -> None:
    s, t = session(tmp_path, engine=True)
    assert (await s.handle({"type": "mode", "mode": "live"}, 0))["ok"]
    ack = await s.handle({"type": "tap", "dir": "down"}, 1)
    assert ack["traded"] is True and t.calls == [("tap", Direction.DOWN)]


async def test_live_mode_refused_without_trading(tmp_path: Path) -> None:
    s, t = session(tmp_path, engine=False)
    ack = await s.handle({"type": "mode", "mode": "live"}, 0)
    assert not ack["ok"] and "not enabled" in ack["error"]
    assert s.mode == "paper"


async def test_commands_without_game_fail_cleanly(tmp_path: Path) -> None:
    s, _ = session(tmp_path, engine=True)
    s.trader = None
    ack = await s.handle({"type": "tap", "dir": "up"}, 0)
    assert not ack["ok"] and ack["error"] == "no game selected"
    assert not (await s.handle({"type": "bogus"}, 0))["ok"]


async def test_params_validated_and_persisted(tmp_path: Path) -> None:
    s, t = session(tmp_path, engine=True)
    bad = await s.handle({"type": "params", "params": {"size": 0}}, 0)
    assert not bad["ok"]
    ok = await s.handle({"type": "params", "params": {"size": "5", "stop_cents": "3"}}, 0)
    assert ok["ok"] and s.params.size == 5 and s.params.stop_cents == 3
    s2, _ = session(tmp_path, engine=True)  # a restarted server loads them back
    assert s2.params.size == 5


def test_params_round_trip() -> None:
    p = TradeParams(size=4, daily_loss_limit=Decimal("12.5"), wait_max_ms=None)
    assert params_from_dict(params_to_dict(p)) == p
    assert params_from_dict({"wait_max_ms": "3000"}).wait_max_ms == 3000
    assert params_from_dict({"unknown": 1}) == TradeParams()
