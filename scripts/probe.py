"""Milestone 0 probe: check how Kalshi actually behaves, then record the results.

    .venv/bin/python scripts/probe.py               # full probe; demo only; 1-contract orders
    .venv/bin/python scripts/probe.py --readonly    # no orders; safe on prod
    .venv/bin/python scripts/probe.py --ticker KX...  # choose the market to trade in

Writes data/probe/<env>-<utc timestamp>.json and prints a summary. Every step is
recorded with whether it succeeded, how long it took, and the raw response, so the
results can go into CLAUDE.md §11.

Trading steps run only when KALSHI_ENV=demo. They always finish with cancel-all
and, if a position is left over, a reduce-only flatten.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import websockets

from courtside.config import Settings, load_settings
from courtside.fees import trade_fee
from courtside.kalshi.auth import Signer
from courtside.kalshi.rest import KalshiError, KalshiREST, OrderAck
from courtside.kalshi.sides import BookSide
from courtside.kalshi.units import parse_count, parse_price

IOC, FOK, GTC = "immediate_or_cancel", "fill_or_kill", "good_till_canceled"


def jsonable(x: Any) -> Any:
    if is_dataclass(x) and not isinstance(x, type):
        return jsonable(asdict(x))
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [jsonable(v) for v in x]
    if isinstance(x, Decimal):
        return str(x)
    return x


class Probe:
    def __init__(self, settings: Settings, rest: KalshiREST, signer: Signer) -> None:
        self.s = settings
        self.rest = rest
        self.signer = signer
        self.report: dict[str, Any] = {
            "env": settings.env,
            "started_utc": datetime.now(UTC).isoformat(),
            "steps": {},
        }
        self.ticker = ""
        self.fee_type = "quadratic"
        self.fee_multiplier: float = 1.0

    # ---- bookkeeping -------------------------------------------------------

    async def step(self, name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        t0 = time.perf_counter()
        entry: dict[str, Any] = {}
        try:
            result = await fn()
            entry = {"ok": True, "result": jsonable(result)}
        except KalshiError as e:
            result = e
            entry = {"ok": False, "status": e.status, "code": e.code, "message": e.message}
        except Exception as e:  # record and continue; the report is what matters
            result = e
            entry = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        entry["rate_limit"] = dict(self.rest.last_rate_limit)
        self.report["steps"][name] = entry
        mark = "ok " if entry["ok"] else "ERR"
        detail = "" if entry["ok"] else f"  {entry.get('code') or entry.get('error')}"
        print(f"  [{mark}] {name:<32} {entry['ms']:>7.1f} ms{detail}")
        return result

    async def top_of_book(self) -> tuple[int, int]:
        m = await self.rest.market(self.ticker)
        return parse_price(m["yes_bid_dollars"]), parse_price(m["yes_ask_dollars"])

    async def position(self) -> Decimal:
        rows = await self.rest.positions(self.ticker)
        return sum((parse_count(r["position_fp"]) for r in rows), Decimal(0))

    async def order(
        self,
        side: BookSide,
        price: int,
        count: int,
        tif: str,
        *,
        coid: str | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        coid = coid or f"probe-{uuid.uuid4().hex[:20]}"
        sent_ms = time.time() * 1000
        ack: OrderAck = await self.rest.create_order(
            ticker=self.ticker,
            side=side,
            yes_price=price,
            count=count,
            time_in_force=tif,
            client_order_id=coid,
            **kw,
        )
        recv_ms = time.time() * 1000
        out: dict[str, Any] = {
            "request": {"side": side.value, "yes_price": price, "count": count, "tif": tif, **kw},
            "ack": jsonable(ack),
            "client_order_id": coid,
            "round_trip_ms": round(recv_ms - sent_ms, 1),
            # Kalshi stamp relative to our send time. Includes clock skew.
            "exchange_ts_minus_send_ms": round(ack.ts_ms - sent_ms, 1),
        }
        if ack.fill_count > 0 and ack.avg_fill_price is not None:
            px = int(ack.avg_fill_price * 100)
            out["predicted_fee_total"] = str(
                trade_fee(
                    px,
                    ack.fill_count,
                    is_maker=False,
                    fee_type=self.fee_type,
                    fee_multiplier=self.fee_multiplier,
                )
            )
        out["position_after"] = str(await self.position())
        return out

    # ---- read-only checks ---------------------------------------------------

    async def readonly(self, ticker: str | None) -> None:
        print("read-only checks")
        r = self.rest
        await self.step("exchange_status", r.exchange_status)
        await self.step("account_limits", r.account_limits)
        await self.step("endpoint_costs", r.endpoint_costs)
        await self.step("balance", r.balance)

        async def rtt() -> dict[str, float]:
            samples = []
            for _ in range(10):
                t0 = time.perf_counter()
                await r.exchange_status()
                samples.append((time.perf_counter() - t0) * 1000)
            return {"median_ms": statistics.median(samples), "min_ms": min(samples)}

        await self.step("rest_rtt_x10", rtt)
        await self.step("sports_filters", r.sports_filters)
        await self.step("sports_series_fees", self.sports_series_fees)

        self.ticker = ticker or await self.step("pick_market", self.pick_market)
        if not isinstance(self.ticker, str) or not self.ticker:
            raise SystemExit("no suitable market found; pass --ticker")
        market = await self.step("market", lambda: r.market(self.ticker))
        if isinstance(market, dict):
            event = await r.event(market["event_ticker"])
            series = await self.step("series", lambda: r.series(event["event"]["series_ticker"]))
            if isinstance(series, dict):
                ev = event["event"]
                self.fee_type = ev.get("fee_type_override") or series["fee_type"]
                self.fee_multiplier = float(
                    ev.get("fee_multiplier_override") or series["fee_multiplier"]
                )
        await self.step("orderbook_rest", lambda: r.orderbook(self.ticker, depth=5))
        await self.step("ws_orderbook_snapshot", self.ws_orderbook)

    async def sports_series_fees(self) -> dict[str, Any]:
        """Fee settings for sports series whose ticker looks like a game-winner market."""
        data = await self.rest.request("GET", "/series", params={"category": "Sports"})
        rows = [
            {k: s.get(k) for k in ("ticker", "title", "fee_type", "fee_multiplier")}
            for s in data.get("series", [])
        ]
        games = [x for x in rows if "GAME" in (x["ticker"] or "").upper()]
        by_type: dict[str, int] = {}
        for x in rows:
            key = f"{x['fee_type']} x{x['fee_multiplier']}"
            by_type[key] = by_type.get(key, 0) + 1
        return {"n_sports_series": len(rows), "fee_settings_count": by_type, "game_series": games}

    async def pick_market(self) -> str:
        """Busiest open binary market with a tight 1-cent book and some size on both sides."""
        data = await self.rest.markets(status="open", limit=1000, mve_filter="exclude")
        best: tuple[float, str] | None = None
        for m in data.get("markets", []):
            try:
                bid, ask = parse_price(m["yes_bid_dollars"]), parse_price(m["yes_ask_dollars"])
                steps = {Decimal(pr["step"]) for pr in m.get("price_ranges") or []}
            except (KeyError, ValueError):
                continue
            if steps and steps != {Decimal("0.01")}:
                continue
            if not (5 <= bid < ask <= 95 and ask - bid <= 5):
                continue
            if Decimal(m["yes_bid_size_fp"]) < 3 or Decimal(m["yes_ask_size_fp"]) < 3:
                continue
            vol = float(m.get("volume_24h_fp") or 0)
            if best is None or vol > best[0]:
                best = (vol, m["ticker"])
        if best is None:
            raise RuntimeError("no market met the criteria")
        return best[1]

    async def ws_orderbook(self) -> dict[str, Any]:
        url = self.s.endpoints.ws
        headers = self.signer.headers("GET", urlsplit(url).path)
        t0 = time.perf_counter()
        async with websockets.connect(url, additional_headers=headers) as ws:
            t_conn = time.perf_counter()
            sub = {"channels": ["orderbook_delta"], "market_ticker": self.ticker}
            await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": sub}))
            msgs: list[dict[str, Any]] = []
            snapshot: dict[str, Any] | None = None
            deadline = time.monotonic() + 10
            while snapshot is None and time.monotonic() < deadline:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                msgs.append({k: msg.get(k) for k in ("type", "sid", "seq")})
                if msg.get("type") == "orderbook_snapshot":
                    snapshot = msg
            t_snap = time.perf_counter()
        if snapshot is None:
            raise RuntimeError(f"no snapshot within 10 s; got {msgs}")
        body = snapshot["msg"]
        return {
            "connect_ms": round((t_conn - t0) * 1000, 1),
            "subscribe_to_snapshot_ms": round((t_snap - t_conn) * 1000, 1),
            "message_sequence": msgs,
            "snapshot_keys": sorted(body),
            "yes_top3": (body.get("yes_dollars_fp") or [])[-3:],
            "no_top3": (body.get("no_dollars_fp") or [])[-3:],
        }

    # ---- trading checks (demo only) -----------------------------------------

    async def trading(self) -> None:
        print(f"trading checks on {self.ticker} (1-contract orders)")
        start_pos = await self.position()
        if start_pos != 0:
            raise SystemExit(f"existing position {start_pos} in {self.ticker}; pick another")
        bid, ask = await self.top_of_book()
        self.report["book_at_start"] = {"yes_bid": bid, "yes_ask": ask}
        B, A = BookSide.BID, BookSide.ASK

        try:
            # IOC that cannot cross: should fill 0 and leave nothing resting.
            coid = f"probe-{uuid.uuid4().hex[:20]}"
            res = await self.step("ioc_no_cross", lambda: self.order(B, 1, 1, IOC, coid=coid))
            if isinstance(res, dict):
                oid = res["ack"]["order_id"]
                await self.step("ioc_no_cross_status", lambda: self.rest.order(oid))
            # Reusing a client_order_id: does Kalshi reject the duplicate?
            await self.step(
                "duplicate_client_order_id", lambda: self.order(B, 1, 1, IOC, coid=coid)
            )
            # FOK larger than the book: expect fill_or_kill_insufficient_resting_volume.
            await self.step("fok_insufficient", lambda: self.order(B, ask, 100000, FOK))

            # Round trip on YES: buy at ask, sell at bid with reduce_only.
            bid, ask = await self.top_of_book()
            await self.step("buy_yes_ioc_at_ask", lambda: self.order(B, ask, 1, IOC))
            bid, ask = await self.top_of_book()
            await self.step(
                "sell_yes_ioc_reduce_only", lambda: self.order(A, bid, 1, IOC, reduce_only=True)
            )

            # From flat, an ask should open a NO position (position_fp negative).
            bid, ask = await self.top_of_book()
            await self.step("ask_from_flat_opens_no", lambda: self.order(A, bid, 1, IOC))
            # A reduce_only order larger than the position should be capped at the position.
            bid, ask = await self.top_of_book()
            await self.step(
                "reduce_only_capped_3_vs_1", lambda: self.order(B, ask, 3, IOC, reduce_only=True)
            )
            bid, ask = await self.top_of_book()
            await self.step(
                "reduce_only_when_flat", lambda: self.order(A, bid, 1, IOC, reduce_only=True)
            )
            await self.step(
                "reduce_only_with_gtc", lambda: self.order(B, 1, 1, GTC, reduce_only=True)
            )

            # Resting order, then cancel: measure latency and check the final status.
            res = await self.step(
                "rest_post_only_1c", lambda: self.order(B, 1, 1, GTC, post_only=True)
            )
            if isinstance(res, dict):
                oid = res["ack"]["order_id"]
                await self.step("cancel_order", lambda: self.rest.cancel_order(oid, self.ticker))
                await self.step("cancelled_status", lambda: self.rest.order(oid))
            await self.step("rest_again_1c", lambda: self.order(B, 1, 1, GTC, post_only=True))
            await self.step("cancel_all", self.rest.cancel_all)
            await self.step(
                "resting_after_cancel_all", lambda: self.rest.orders(self.ticker, "resting")
            )
            await self.step("order_group_create", lambda: self.rest.create_order_group(5))
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        print("cleanup")
        await self.step("cleanup_cancel_all", self.rest.cancel_all)
        pos = await self.position()
        if pos > 0:
            await self.step(
                "cleanup_flatten",
                lambda: self.order(BookSide.ASK, 1, int(pos), IOC, reduce_only=True),
            )
        elif pos < 0:
            await self.step(
                "cleanup_flatten",
                lambda: self.order(BookSide.BID, 99, int(-pos), IOC, reduce_only=True),
            )
        self.report["final_position"] = str(await self.position())


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--readonly", action="store_true", help="skip all order placement")
    ap.add_argument("--ticker", help="market to probe (default: auto-pick a liquid one)")
    args = ap.parse_args()

    settings = load_settings()
    if settings.env != "demo" and not args.readonly:
        raise SystemExit("trading checks run on demo only; use --readonly for prod")
    signer = Signer.from_pem_file(settings.key_id, settings.key_path)
    print(f"env={settings.env} rest={settings.endpoints.rest}")

    async with KalshiREST(settings.endpoints.rest, signer) as rest:
        probe = Probe(settings, rest, signer)
        try:
            await probe.readonly(args.ticker)
            probe.report["ticker"] = probe.ticker
            probe.report["fee_model"] = {"type": probe.fee_type, "multiplier": probe.fee_multiplier}
            if not args.readonly:
                await probe.trading()
        finally:
            out = Path("data/probe")
            out.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = out / f"{settings.env}-{stamp}.json"
            path.write_text(json.dumps(probe.report, indent=2, default=str))
            print(f"report: {path}")


if __name__ == "__main__":
    asyncio.run(main())
