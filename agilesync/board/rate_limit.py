"""Client-side request pacing for the AgilePlace API. Stdlib only, no I/O.

The board enforces a quota over a ~60s window: a burst of concurrent reads earns a 429 whose
Retry-After is ~57s, so one stall costs more than pacing every request would have. This paces
requests across every thread sharing the limiter, and halves its own rate whenever the server
pushes back -- so a run converges under a quota that is nowhere documented, instead of needing one
configured up front.

Multiplicative decrease only, and only for the life of one limiter: a run that trips the quota
stays slower rather than probing its way back up into another 57s stall.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Spaces `acquire()` calls to at most `rate_per_minute`, shared across threads.

    The clock and sleep are injectable so pacing is testable without spending the wall-clock time
    it exists to spend."""

    # One rate-limit window produces one 429 per in-flight request -- eight, with the read pool
    # saturated -- all within milliseconds. They are one episode and cost one halving; counting
    # each separately would collapse the rate by 256x over a single stall.
    PENALTY_COOLDOWN_SECONDS = 5.0

    def __init__(self, rate_per_minute: float, *, floor_per_minute: float = 60.0,
                 monotonic=time.monotonic, sleep=time.sleep) -> None:
        if rate_per_minute <= 0:
            raise ValueError(f"rate_per_minute must be positive, got {rate_per_minute!r}")
        if floor_per_minute <= 0:
            raise ValueError(f"floor_per_minute must be positive, got {floor_per_minute!r}")
        self._rate = float(rate_per_minute)
        self._floor = float(min(floor_per_minute, rate_per_minute))
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_slot: float | None = None
        self._last_penalty: float | None = None

    @property
    def rate_per_minute(self) -> float:
        with self._lock:
            return self._rate

    def acquire(self) -> None:
        """Block until this thread's slot comes up. The slot is reserved under the lock and waited
        out outside it, so waiting threads don't serialize behind each other's sleeps."""
        with self._lock:
            now = self._monotonic()
            slot = now if self._next_slot is None else max(now, self._next_slot)
            self._next_slot = slot + 60.0 / self._rate
            wait = slot - now
        if wait > 0:
            self._sleep(wait)

    def penalize(self) -> float:
        """Halve the rate in response to a rate-limit signal, down to the floor. Signals arriving
        within PENALTY_COOLDOWN_SECONDS of the last one are the same episode and cost nothing."""
        with self._lock:
            now = self._monotonic()
            if (self._last_penalty is not None
                    and now - self._last_penalty < self.PENALTY_COOLDOWN_SECONDS):
                return self._rate
            self._last_penalty = now
            self._rate = max(self._floor, self._rate / 2)
            return self._rate
