"""Summarize a recording: message counts, gaps, feed latency, and quote changes.

    .venv/bin/python scripts/replay.py data/recordings/demo/<file>.jsonl.gz
    .venv/bin/python scripts/replay.py <file> --csv tops.csv   # also dump top-of-book changes

This is a sanity check that a recording is complete and replayable. Real analysis
(paper fills, time to reprice) comes in Milestone 4.
"""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import Counter
from pathlib import Path

from courtside.recording import read_recording, replay_tops


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("path", type=Path)
    ap.add_argument("--csv", type=Path, help="write top-of-book changes to this CSV")
    args = ap.parse_args()

    meta, msgs = read_recording(args.path)
    offset = meta.get("clock_offset_ms") or 0.0
    types: Counter[str] = Counter()
    lat: list[float] = []
    next_seq: dict[int, int] = {}
    gaps = 0
    first = last = None
    for t, m in msgs:
        first = first if first is not None else t
        last = t
        mtype = m.get("type", "?")
        types[mtype] += 1
        ts = (m.get("msg") or {}).get("ts_ms")
        if isinstance(ts, int):
            lat.append(t + offset - ts)
        sid, seq = m.get("sid"), m.get("seq")
        if sid is not None and seq is not None:
            if sid in next_seq and seq != next_seq[sid] and mtype != "orderbook_snapshot":
                gaps += 1
            next_seq[sid] = seq + 1

    print(f"{meta.get('title')}  {meta.get('event_ticker')}  env={meta.get('env')}")
    if first is not None and last is not None:
        secs = (last - first) / 1000
        print(f"duration {secs:.0f} s, {sum(types.values())} messages, seq gaps {gaps}")
    print("message types:", dict(types))
    if lat:
        print(
            f"exchange->local latency (clock offset {offset} ms applied): "
            f"p10 {pct(lat, 0.1):.0f}  p50 {statistics.median(lat):.0f}  "
            f"p90 {pct(lat, 0.9):.0f}  max {max(lat):.0f} ms"
        )

    tops = list(replay_tops(args.path))
    per_market = Counter(t for t, _ in tops)
    for ticker, n in per_market.items():
        last_top = next(top for t, top in reversed(tops) if t == ticker)
        print(f"  {ticker}: {n} quote changes, final {last_top.yes_bid}/{last_top.yes_ask}")
    if args.csv:
        with args.csv.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_ms", "ticker", "yes_bid", "yes_bid_size", "yes_ask", "yes_ask_size"])
            for ticker, top in tops:
                w.writerow(
                    [top.t_ms, ticker, top.yes_bid, top.yes_bid_size, top.yes_ask, top.yes_ask_size]
                )
        print(f"wrote {args.csv}")


if __name__ == "__main__":
    main()
