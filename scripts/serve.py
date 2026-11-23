"""Run the Courtside server: phone PWA + WebSocket API + trading session.

    .venv/bin/python scripts/serve.py --gen-token        # print a new COURTSIDE_TOKEN for .env
    .venv/bin/python scripts/serve.py                    # http://127.0.0.1:8000
    .venv/bin/python scripts/serve.py --host 0.0.0.0     # reachable from a phone on the LAN

Open the page once as  http://<host>:8000/#token=<COURTSIDE_TOKEN> . The phone saves
the token and removes it from the URL. Live trading is enabled on demo. On prod it
also needs COURTSIDE_ALLOW_LIVE=1 (Milestone 5 only). Without that, the server records
and takes MARKs (paper mode).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette

from courtside.api import Session, create_app
from courtside.config import load_settings
from courtside.kalshi.auth import Signer


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--gen-token", action="store_true", help="print a new token and exit")
    args = ap.parse_args()
    if args.gen_token:
        print(secrets.token_urlsafe(32))
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    token = os.environ.get("COURTSIDE_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("set COURTSIDE_TOKEN in .env (scripts/serve.py --gen-token makes one)")

    settings = load_settings()
    signer = Signer.from_pem_file(settings.key_id, settings.key_path)
    trading = settings.env == "demo" or os.environ.get("COURTSIDE_ALLOW_LIVE") == "1"
    session = Session(settings, signer, trading_allowed=trading, data_dir=Path("data"))
    logging.info("env=%s trading_allowed=%s", settings.env, trading)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await session.resume()
        yield
        await session.close()

    app = create_app(session, token, lifespan=lifespan)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
