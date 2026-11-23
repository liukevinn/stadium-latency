"""Trading parameters (CLAUDE.md §7). Sport presets only change timing."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal

# deadline_ms per sport. Nothing else about a sport is special-cased.
SPORT_DEADLINE_MS: dict[str, int] = {
    "football": 20_000,
    "baseball": 10_000,
    "basketball": 6_000,
}


@dataclass(frozen=True, slots=True)
class TradeParams:
    size: int = 10  # contracts per tap
    chase_cents: int = 5  # max cents above the pre-play ask we'll pay
    lookback_ms: int = 1_500  # how far before the tap to take the pre-play price
    min_move_cents: int = 4  # bid gain that counts as "caught up" (≥4 covers ~3.5c taker fees)
    wait_max_ms: int | None = None  # max time in WAITING; None = deadline_ms / 2
    deadline_ms: int = 20_000  # must be flat this long after the fill
    stop_cents: int = 0  # auto-stop distance below entry; 0 = off
    max_position: int = 30  # hard cap on contracts held
    max_price: int = 97  # never buy above this (cents)
    daily_loss_limit: Decimal = Decimal(20)  # dollars; stop accepting taps once hit
    requote_ms: int = 250  # min time between passive-exit requotes
    forced_slip_cents: int = 5  # forced exit sells down to best bid minus this
    forced_retry_ms: int = 200  # first retry delay for a forced exit (doubles, max 1 s)
    flat_alert_ms: int = 3_000  # alert the phone if not flat this long into a forced exit

    @property
    def wait_ms(self) -> int:
        return self.wait_max_ms if self.wait_max_ms is not None else self.deadline_ms // 2

    def for_sport(self, sport: str) -> TradeParams:
        return dataclasses.replace(self, deadline_ms=SPORT_DEADLINE_MS[sport], wait_max_ms=None)

    def validate(self) -> None:
        if not 1 <= self.max_price <= 99:
            raise ValueError("max_price must be 1..99")
        if self.size < 1 or self.size > self.max_position:
            raise ValueError("size must be 1..max_position")
        if self.lookback_ms < 0 or self.deadline_ms <= 0 or self.wait_ms > self.deadline_ms:
            raise ValueError("need lookback_ms >= 0 and 0 < wait_ms <= deadline_ms")
