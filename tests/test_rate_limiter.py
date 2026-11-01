"""限速器測試：以假時鐘驗證啟動間隔、在途上限、退避與 Retry-After。"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.crawler.rate_limiter import RateLimitConfig, RateLimiter


class FakeClock:
    """可控制時鐘：sleep 只推進時間，不會真的等待。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_limiter(clock: FakeClock, **overrides) -> RateLimiter:
    config = RateLimitConfig(**overrides)
    return RateLimiter(config, clock=clock.monotonic, sleep=clock.sleep, rng=random.Random(0))


def test_config_rejects_out_of_range_in_flight() -> None:
    with pytest.raises(ValueError):
        RateLimitConfig(max_in_flight=0)
    with pytest.raises(ValueError):
        RateLimitConfig(max_in_flight=5)
    with pytest.raises(ValueError):
        RateLimitConfig(min_interval=2.0, max_interval=1.0)


def test_start_interval_is_enforced_between_requests() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock, max_in_flight=2, min_interval=0.5, max_interval=0.5)
    for _ in range(3):
        with limiter.slot():
            pass
    starts = limiter.start_times
    assert len(starts) == 3
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(gap >= 0.5 - 1e-9 for gap in gaps)
    assert clock.now >= 1.0


def test_default_interval_within_spec_bounds() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock)
    for _ in range(5):
        with limiter.slot():
            pass
    gaps = [b - a for a, b in zip(limiter.start_times, limiter.start_times[1:])]
    assert all(0.5 - 1e-9 <= gap <= 1.5 + 1e-9 for gap in gaps)


def test_max_in_flight_is_respected_across_threads() -> None:
    limiter = RateLimiter(
        RateLimitConfig(max_in_flight=2, min_interval=0.0, max_interval=0.0),
        rng=random.Random(1),
    )
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait()
        with limiter.slot():
            time.sleep(0.05)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: worker(), range(6)))

    assert limiter.peak_in_flight <= 2
    assert limiter.in_flight == 0


def test_backoff_grows_and_respects_cap() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock, backoff_base=1.0, backoff_cap=30.0)
    delays = [limiter.backoff_delay(attempt) for attempt in range(1, 8)]
    assert delays[0] >= 1.0
    assert delays == sorted(delays)
    assert max(delays) <= 30.0 * 1.25


def test_retry_after_pauses_all_requests() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock, max_in_flight=1, min_interval=0.0, max_interval=0.0)
    limiter.apply_retry_after(5.0)
    with limiter.slot():
        pass
    assert limiter.start_times[0] >= 5.0 - 1e-9
    assert clock.now >= 5.0 - 1e-9


def test_wait_backoff_reports_delay() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock, backoff_base=2.0, backoff_cap=30.0)
    delay = limiter.wait_backoff(2)
    assert delay >= 4.0
    assert clock.now == pytest.approx(delay)
