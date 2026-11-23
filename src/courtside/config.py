"""Environment settings. Secrets come from the environment or .env, never from code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Endpoints:
    rest: str  # base URL including /trade-api/v2
    ws: str  # market data (orderbook_delta, ticker, trade)
    user_ws: str  # fill and user_orders only; lower latency


# Source: https://docs.kalshi.com/getting_started/api_environments (checked 2026-11-23)
ENDPOINTS: dict[str, Endpoints] = {
    "demo": Endpoints(
        rest="https://external-api.demo.kalshi.co/trade-api/v2",
        ws="wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
        user_ws="wss://user-ws.demo.kalshi.co/trade-api/ws/v2",
    ),
    "prod": Endpoints(
        rest="https://external-api.kalshi.com/trade-api/v2",
        ws="wss://external-api-ws.kalshi.com/trade-api/ws/v2",
        user_ws="wss://user-ws.kalshi.com/trade-api/ws/v2",
    ),
}


@dataclass(frozen=True, slots=True)
class Settings:
    env: str
    key_id: str
    key_path: Path
    endpoints: Endpoints


def load_settings() -> Settings:
    load_dotenv()
    env = os.environ.get("KALSHI_ENV", "demo")
    if env not in ENDPOINTS:
        raise ValueError(f"KALSHI_ENV must be one of {sorted(ENDPOINTS)}, got {env!r}")
    try:
        key_id = os.environ["KALSHI_API_KEY_ID"]
        key_path = Path(os.environ["KALSHI_PRIVATE_KEY_PATH"])
    except KeyError as e:
        raise RuntimeError(f"missing {e.args[0]}; copy .env.example to .env") from None
    return Settings(env=env, key_id=key_id, key_path=key_path, endpoints=ENDPOINTS[env])
