"""Local clock offset against NTP, so latency numbers that compare our clock with
Kalshi's timestamps can be corrected.

On 2026-11-23 the dev Mac was 83 ms behind NTP. That's enough to make
"local receive - exchange ts_ms" negative. Each recording stores the offset in
its metadata: true time ≈ local time + offset_ms.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time

_NTP_EPOCH_DELTA = 2_208_988_800  # seconds from 1900 to 1970


def _query(host: str, timeout_s: float) -> float:
    """One SNTP query. Returns the offset in ms (positive = local clock is behind)."""
    packet = b"\x1b" + 47 * b"\0"  # LI=0, version 3, mode 3 (client)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout_s)
        t0 = time.time()
        s.sendto(packet, (host, 123))
        data, _ = s.recvfrom(48)
        t3 = time.time()
    t1 = _ntp_ts(data, 32)  # server receive
    t2 = _ntp_ts(data, 40)  # server transmit
    return ((t1 - t0) + (t2 - t3)) / 2 * 1000


def _ntp_ts(data: bytes, offset: int) -> float:
    secs, frac = struct.unpack("!II", data[offset : offset + 8])
    return float(secs - _NTP_EPOCH_DELTA + frac / 2**32)


async def ntp_offset_ms(host: str = "time.apple.com", samples: int = 3) -> float | None:
    """Median offset over a few queries, or None if NTP is unreachable."""
    results: list[float] = []
    for _ in range(samples):
        try:
            results.append(await asyncio.to_thread(_query, host, 1.0))
        except OSError:
            continue
    if not results:
        return None
    results.sort()
    return round(results[len(results) // 2], 1)
