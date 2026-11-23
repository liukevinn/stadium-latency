"""Write and read raw market-data recordings.

Format: JSON Lines, one object per line.
    first line   {"t": <ms>, "meta": {...}}      what was recorded and why
    other lines  {"t": <ms>, "m": <raw msg>}     a Kalshi WS message as received
`t` is the local receive time in wall-clock ms.

Raw messages, rather than derived quotes, are stored so that any analysis (paper
fills, repricing speed) can replay them through the same OrderBook code the
engine uses. When the writer closes, it gzips the file. The plain .jsonl is only
there while recording, so a crash still leaves a readable file.
"""

from __future__ import annotations

import gzip
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

from courtside.book import OrderBook, Top


class RecordingWriter:
    def __init__(self, path: Path, meta: dict[str, Any], *, now_ms: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lines = 0
        self._f: IO[str] | None = path.open("w", encoding="utf-8")
        self._write({"t": now_ms, "meta": meta})

    def write(self, recv_ms: int, msg: dict[str, Any]) -> None:
        self._write({"t": recv_ms, "m": msg})

    def flush(self) -> None:
        if self._f is not None:
            self._f.flush()

    def close(self, *, compress: bool = True) -> Path:
        """Close the file. Returns the final path (.jsonl.gz if compressed)."""
        if self._f is None:
            return self.path
        self._f.close()
        self._f = None
        if not compress:
            return self.path
        gz = self.path.with_suffix(self.path.suffix + ".gz")
        with self.path.open("rb") as src, gzip.open(gz, "wb") as dst:
            shutil.copyfileobj(src, dst)
        self.path.unlink()
        self.path = gz
        return gz

    def _write(self, obj: dict[str, Any]) -> None:
        assert self._f is not None, "writer is closed"
        self._f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self.lines += 1


def read_recording(path: Path) -> tuple[dict[str, Any], Iterator[tuple[int, dict[str, Any]]]]:
    """Returns (meta, iterator of (recv_ms, msg)). Handles .jsonl and .jsonl.gz."""
    opener = gzip.open if path.suffix == ".gz" else open
    f = opener(path, "rt", encoding="utf-8")
    first = json.loads(f.readline())

    def lines() -> Iterator[tuple[int, dict[str, Any]]]:
        with f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:  # torn last line after a crash
                    return
                yield obj["t"], obj["m"]

    return first.get("meta", {}), lines()


def replay_tops(path: Path) -> Iterator[tuple[str, Top]]:
    """Rebuild each market's top of book from a recording: yields (ticker, Top)
    whenever a market's quote changes. Uses the same OrderBook code as the live
    feed. Seq gaps aren't re-checked here; the live feed already logged them."""
    _, msgs = read_recording(path)
    books: dict[str, OrderBook] = {}
    last: dict[str, Top] = {}
    for t, msg in msgs:
        mtype = msg.get("type")
        if mtype not in ("orderbook_snapshot", "orderbook_delta"):
            continue
        body = msg["msg"]
        ticker = body["market_ticker"]
        book = books.setdefault(ticker, OrderBook(ticker))
        if mtype == "orderbook_snapshot":
            book.apply_snapshot(body)
        elif book.valid:
            book.apply_delta(body)
        else:
            continue
        top = book.top(t)
        if ticker not in last or not last[ticker].same_quote(top):
            last[ticker] = top
            yield ticker, top
