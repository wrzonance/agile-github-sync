"""Client-side request pacing shared by the read pool's threads.

The board enforces a quota over a ~60s window, so a burst of concurrent reads earns a 429 with a
~57s Retry-After -- one stall costs more than pacing every request would. The limiter spaces
requests across ALL threads and halves its own rate each time the server pushes back, so a run
converges under a quota nobody has to know in advance.

Run: pytest -q
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agilesync.board.rate_limit import RateLimiter  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _limiter(rate: float, clock: FakeClock, **kw) -> RateLimiter:
    return RateLimiter(rate, monotonic=clock.monotonic, sleep=clock.sleep, **kw)


def test_the_first_request_is_not_delayed():
    clock = FakeClock()

    _limiter(60, clock).acquire()

    assert clock.slept == []


def test_back_to_back_requests_are_spaced_by_the_configured_rate():
    clock = FakeClock()
    limiter = _limiter(60, clock)  # 60/min -> one per second

    for _ in range(3):
        limiter.acquire()

    assert clock.slept == [1.0, 1.0]


def test_a_request_after_an_idle_gap_is_not_delayed():
    clock = FakeClock()
    limiter = _limiter(60, clock)
    limiter.acquire()

    clock.now += 30.0  # the run did other work; the slot is long past
    limiter.acquire()

    assert clock.slept == []


def test_a_rate_limit_signal_halves_the_rate():
    clock = FakeClock()
    limiter = _limiter(60, clock, floor_per_minute=1)
    limiter.acquire()

    limiter.penalize()
    limiter.acquire()
    limiter.acquire()

    assert limiter.rate_per_minute == 30
    # The slot already reserved keeps the old spacing; every slot booked after the penalty is
    # twice as far apart.
    assert clock.slept == [1.0, 2.0]


def test_a_burst_of_rate_limit_signals_only_halves_the_rate_once():
    """Eight pooled workers are rate-limited by the same window and report it within milliseconds
    of each other. Counting each one separately would collapse the rate by 256x for a single
    episode -- far slower than the stall it exists to avoid."""
    clock = FakeClock()
    limiter = _limiter(600, clock, floor_per_minute=1)

    for _ in range(8):
        limiter.penalize()

    assert limiter.rate_per_minute == 300


def test_a_rate_limit_signal_after_the_cooldown_is_a_fresh_episode():
    clock = FakeClock()
    limiter = _limiter(600, clock, floor_per_minute=1)
    limiter.penalize()

    clock.now += RateLimiter.PENALTY_COOLDOWN_SECONDS + 1
    limiter.penalize()

    assert limiter.rate_per_minute == 150


def test_the_rate_never_falls_below_its_floor():
    clock = FakeClock()
    limiter = _limiter(600, clock, floor_per_minute=75)

    for _ in range(10):
        limiter.penalize()
        clock.now += RateLimiter.PENALTY_COOLDOWN_SECONDS + 1

    assert limiter.rate_per_minute == 75


def test_a_non_positive_rate_is_rejected():
    with pytest.raises(ValueError):
        RateLimiter(0)
    with pytest.raises(ValueError):
        RateLimiter(60, floor_per_minute=0)


def test_threads_sharing_the_limiter_are_paced_against_each_other():
    """The pool's whole point is concurrency, so the spacing has to hold ACROSS threads -- eight
    workers must not each get their own private schedule."""
    limiter = RateLimiter(6000)  # 100/s -> 10ms apart
    started = time.monotonic()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: limiter.acquire(), range(8)))

    assert time.monotonic() - started >= 0.07  # 7 gaps of 10ms
