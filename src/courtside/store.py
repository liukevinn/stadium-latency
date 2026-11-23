"""Structured event log (CLAUDE.md §9): one JSON object per line.

Every tap, order, fill, exit path, alert and closed trade is written here. The
analysis tools in Milestone 4 read these files. `keep=True` also keeps records in
memory, which the tests use.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import IO, Any


def _default(o: object) -> object:
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


class EventLog:
    def __init__(self, path: Path | None = None, *, keep: bool = False) -> None:
        self.records: list[dict[str, Any]] = []
        self._keep = keep
        self._f: IO[str] | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._f = path.open("a", encoding="utf-8")

    def write(self, kind: str, **fields: Any) -> None:
        rec = {"kind": kind, **fields}
        if self._keep:
            self.records.append(rec)
        if self._f is not None:
            self._f.write(json.dumps(rec, default=_default, separators=(",", ":")) + "\n")
            self._f.flush()

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r["kind"] == kind]

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


def realized_today(path: Path) -> Decimal:
    """Sum of net P&L of trades closed in an existing log (for the daily loss limit)."""
    total = Decimal(0)
    if not path.exists():
        return total
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                rec.get("kind") == "trade_closed"
                and not rec.get("settled")
                and not rec.get("adopted")
            ):
                total += Decimal(rec["net"])
    return total
