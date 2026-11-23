"""Async Kalshi REST client.

Design rules:
  - One persistent HTTP/2 connection (httpx), auth headers signed at send time.
  - No hidden retries or sleeps. Every call is one attempt. The caller (engine)
    decides what to retry, because on the hot path a silent 1 s backoff is worse
    than an error. Order retries are safe when they reuse the same client_order_id.
  - Responses come back as plain dicts, except order acks, which are parsed into a
    typed OrderAck because the engine branches on them.

Orders go through the V2 endpoints under /portfolio/events/orders. The legacy
/portfolio/orders write endpoints return 410 Gone (checked 2026-11-23).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from courtside.kalshi.auth import Signer
from courtside.kalshi.sides import BookSide
from courtside.kalshi.units import format_count, format_price, parse_count, parse_dollars

JSON = dict[str, Any]


class KalshiError(Exception):
    """Non-2xx response. `code` is Kalshi's error code, e.g. "order_already_exists"."""

    def __init__(self, status: int, code: str, message: str, body: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.body = body


@dataclass(frozen=True, slots=True)
class OrderAck:
    order_id: str
    client_order_id: str | None
    fill_count: Decimal
    remaining_count: Decimal
    avg_fill_price: Decimal | None  # dollars, YES leg
    avg_fee: Decimal | None  # dollars per contract
    ts_ms: int

    @classmethod
    def from_wire(cls, d: JSON) -> OrderAck:
        avg_price = d.get("average_fill_price")
        avg_fee = d.get("average_fee_paid")
        return cls(
            order_id=d["order_id"],
            client_order_id=d.get("client_order_id"),
            fill_count=parse_count(d["fill_count"]),
            remaining_count=parse_count(d["remaining_count"]),
            avg_fill_price=parse_dollars(avg_price) if avg_price is not None else None,
            avg_fee=parse_dollars(avg_fee) if avg_fee is not None else None,
            ts_ms=int(d["ts_ms"]),
        )


class KalshiREST:
    def __init__(
        self,
        base_url: str,
        signer: Signer,
        *,
        timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._base_path = urlsplit(self._base_url).path  # "/trade-api/v2", part of the signature
        self._signer = signer
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            http2=transport is None,
            timeout=timeout_s,
            transport=transport,
        )
        self.last_rate_limit: dict[str, str] = {}

    async def __aenter__(self) -> KalshiREST:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- transport ---------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: JSON | None = None,
        json: JSON | None = None,
    ) -> JSON:
        """One signed request. `path` is relative to the base, e.g. "/portfolio/balance"."""
        headers = self._signer.headers(method, self._base_path + path)
        resp = await self._http.request(method, path, params=params, json=json, headers=headers)
        self.last_rate_limit = {
            k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")
        }
        if resp.is_error:
            raise _to_error(resp)
        if resp.status_code == 204 or not resp.content:
            return {}
        result: JSON = resp.json()
        return result

    # ---- exchange / account ------------------------------------------------

    async def exchange_status(self) -> JSON:
        return await self.request("GET", "/exchange/status")

    async def account_limits(self) -> JSON:
        return await self.request("GET", "/account/limits")

    async def endpoint_costs(self) -> JSON:
        return await self.request("GET", "/account/endpoint_costs")

    async def balance(self) -> JSON:
        return await self.request("GET", "/portfolio/balance")

    # ---- market data -------------------------------------------------------

    async def markets(self, **params: Any) -> JSON:
        return await self.request("GET", "/markets", params=params)

    async def market(self, ticker: str) -> JSON:
        return (await self.request("GET", f"/markets/{ticker}"))["market"]  # type: ignore[no-any-return]

    async def events(self, **params: Any) -> JSON:
        return await self.request("GET", "/events", params=params)

    async def event(self, event_ticker: str) -> JSON:
        return await self.request("GET", f"/events/{event_ticker}")

    async def series(self, series_ticker: str) -> JSON:
        return (await self.request("GET", f"/series/{series_ticker}"))["series"]  # type: ignore[no-any-return]

    async def sports_filters(self) -> JSON:
        return await self.request("GET", "/search/filters_by_sport")

    async def orderbook(self, ticker: str, depth: int = 0) -> JSON:
        return await self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    # ---- portfolio -----------------------------------------------------------

    async def positions(self, ticker: str | None = None) -> list[JSON]:
        params = {"ticker": ticker} if ticker else None
        data = await self.request("GET", "/portfolio/positions", params=params)
        return data.get("market_positions", [])  # type: ignore[no-any-return]

    async def orders(self, ticker: str | None = None, status: str | None = None) -> list[JSON]:
        params = {k: v for k, v in {"ticker": ticker, "status": status}.items() if v}
        data = await self.request("GET", "/portfolio/orders", params=params)
        return data.get("orders", [])  # type: ignore[no-any-return]

    async def order(self, order_id: str) -> JSON:
        return (await self.request("GET", f"/portfolio/orders/{order_id}"))["order"]  # type: ignore[no-any-return]

    # ---- orders (V2) -----------------------------------------------------------

    async def create_order(
        self,
        *,
        ticker: str,
        side: BookSide,
        yes_price: int,
        count: int | Decimal,
        time_in_force: str,
        client_order_id: str,
        reduce_only: bool = False,
        post_only: bool = False,
        order_group_id: str | None = None,
        cancel_order_on_pause: bool | None = None,
        expiration_time: int | None = None,
        self_trade_prevention_type: str = "taker_at_cross",
    ) -> OrderAck:
        """Place one order. `side` and `yes_price` are on the YES leg; see sides.to_wire."""
        body: JSON = {
            "ticker": ticker,
            "side": side.value,
            "price": format_price(yes_price),
            "count": format_count(count),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": self_trade_prevention_type,
            "client_order_id": client_order_id,
        }
        if reduce_only:
            body["reduce_only"] = True
        if post_only:
            body["post_only"] = True
        if order_group_id is not None:
            body["order_group_id"] = order_group_id
        if cancel_order_on_pause is not None:
            body["cancel_order_on_pause"] = cancel_order_on_pause
        if expiration_time is not None:
            body["expiration_time"] = expiration_time
        return OrderAck.from_wire(await self.request("POST", "/portfolio/events/orders", json=body))

    async def cancel_order(self, order_id: str, ticker: str | None = None) -> JSON:
        """Returns {order_id, client_order_id, reduced_by, ts_ms}. `ticker` enables auto-routing."""
        params = {"market_ticker": ticker} if ticker else None
        return await self.request("DELETE", f"/portfolio/events/orders/{order_id}", params=params)

    async def cancel_all(self) -> None:
        """Cancel every resting event-market order on all shards (204, no body)."""
        await self.request("DELETE", "/portfolio/events/orders")

    async def create_order_group(self, contracts_limit: int) -> JSON:
        return await self.request(
            "POST", "/portfolio/order_groups/create", json={"contracts_limit": contracts_limit}
        )


def _to_error(resp: httpx.Response) -> KalshiError:
    code, message = "unknown", resp.text
    try:
        err = resp.json().get("error", {})
        code = err.get("code", code)
        message = err.get("message", message)
    except ValueError:
        pass
    return KalshiError(resp.status_code, code, message, resp.text)
