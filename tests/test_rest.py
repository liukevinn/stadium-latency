import base64
import json
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from courtside.kalshi.auth import Signer
from courtside.kalshi.rest import KalshiError, KalshiREST
from courtside.kalshi.sides import BookSide

BASE = "https://example.test/trade-api/v2"


def make_client(handler: httpx.MockTransport) -> tuple[KalshiREST, ed25519.Ed25519PrivateKey]:
    key = ed25519.Ed25519PrivateKey.generate()
    return KalshiREST(BASE, Signer("kid", key), transport=handler), key


async def test_create_order_sends_v2_body_and_signs_full_path() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        seen["ts"] = req.headers["KALSHI-ACCESS-TIMESTAMP"]
        seen["sig"] = req.headers["KALSHI-ACCESS-SIGNATURE"]
        return httpx.Response(
            201,
            json={
                "order_id": "o1",
                "client_order_id": "c1",
                "fill_count": "3.00",
                "remaining_count": "0.00",
                "average_fill_price": "0.5700",
                "average_fee_paid": "0.017199",
                "ts_ms": 123,
            },
        )

    client, key = make_client(httpx.MockTransport(handler))
    async with client:
        ack = await client.create_order(
            ticker="T",
            side=BookSide.BID,
            yes_price=57,
            count=3,
            time_in_force="immediate_or_cancel",
            client_order_id="c1",
        )
    assert seen["url"] == f"{BASE}/portfolio/events/orders"
    assert seen["body"] == {
        "ticker": "T",
        "side": "bid",
        "price": "0.5700",
        "count": "3.00",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": "c1",
    }
    key.public_key().verify(
        base64.b64decode(str(seen["sig"])),
        f"{seen['ts']}POST/trade-api/v2/portfolio/events/orders".encode(),
    )
    assert ack.fill_count == Decimal(3)
    assert ack.avg_fill_price == Decimal("0.57")


async def test_error_is_parsed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"error": {"code": "order_already_exists", "message": "dup"}}
        )

    client, _ = make_client(httpx.MockTransport(handler))
    async with client:
        with pytest.raises(KalshiError) as ei:
            await client.cancel_all()
    assert (ei.value.status, ei.value.code) == (409, "order_already_exists")


async def test_query_params_not_signed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["ticker"] == "T"
        return httpx.Response(200, json={"market_positions": [{"ticker": "T"}]})

    client, _ = make_client(httpx.MockTransport(handler))
    async with client:
        assert await client.positions("T") == [{"ticker": "T"}]
