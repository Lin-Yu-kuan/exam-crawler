"""所有站點請求共用的限速器：在途上限、全域啟動間隔、退避與 Retry-After。

設計重點：
- 以**全域單一節奏**控制請求啟動，worker 不各自計時。
- 在途請求上限與啟動間隔同時生效；併發不等於放寬限速。
- 啟動間隔是**最小間隔**：若單次請求本身耗时超過間隔（例如首次 TLS 握手），
  實際啟動時間差可能大於上限値，屬正常現象。
- 時鐘與睡眠可注入，測試可用假時鐘驗證，不必真的等待。
"""

from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

#: 官方站點允許的在途上限範圍（SPEC §7.1）
MIN_IN_FLIGHT = 1
MAX_IN_FLIGHT = 4
DEFAULT_IN_FLIGHT = 2


@dataclass(frozen=True)
class RateLimitConfig:
    """限速設定；預設值取自 SPEC §7。"""

    max_in_flight: int = DEFAULT_IN_FLIGHT
    min_interval: float = 0.5
    max_interval: float = 1.5
    backoff_base: float = 1.0
    backoff_cap: float = 30.0

    def __post_init__(self) -> None:
        if not MIN_IN_FLIGHT <= self.max_in_flight <= MAX_IN_FLIGHT:
            raise ValueError(
                f"在途上限必須介於 {MIN_IN_FLIGHT}～{MAX_IN_FLIGHT}，收到 {self.max_in_flight}"
            )
        if self.min_interval < 0 or self.max_interval < self.min_interval:
            raise ValueError("啟動間隔設定不合法")


class RateLimiter:
    """執行緒安全的全站限速器。

    clock 需回傳單調遞增秒數（預設 time.monotonic）。
    sleep 供等待使用（預設 time.sleep），測試可換成推進假時鐘的函式。
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config or RateLimitConfig()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._cond = threading.Condition()
        self._in_flight = 0
        self._next_start_at = 0.0
        self._cooldown_until = 0.0
        self._starts: list[float] = []
        self._peak_in_flight = 0

    # --- 對外查詢狀態（測試與診斷用） ---------------------------------
    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    @property
    def peak_in_flight(self) -> int:
        with self._cond:
            return self._peak_in_flight

    @property
    def start_times(self) -> tuple[float, ...]:
        with self._cond:
            return tuple(self._starts)

    # --- 主要行為 -----------------------------------------------------
    def _next_interval(self) -> float:
        low, high = self.config.min_interval, self.config.max_interval
        return low + (high - low) * self._rng.random()

    @contextmanager
    def slot(self) -> Iterator[None]:
        """取得一個請求許可；離開時釋放在途名額。

        等待期間不持有鎖，並以可注入的 sleep 等待，便於用假時鐘測試。
        """
        while True:
            with self._cond:
                if self._in_flight >= self.config.max_in_flight:
                    self._cond.wait()
                    continue
                now = self._clock()
                ready_at = max(self._next_start_at, self._cooldown_until)
                if ready_at <= now:
                    self._in_flight += 1
                    self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
                    self._starts.append(now)
                    self._next_start_at = now + self._next_interval()
                    break
                delay = ready_at - now
            self._sleep(delay)
        try:
            yield
        finally:
            with self._cond:
                self._in_flight -= 1
                self._cond.notify_all()

    def apply_retry_after(self, seconds: float) -> None:
        """收到 429／503 的 Retry-After：所有 worker 一起暫停。"""
        if seconds <= 0:
            return
        with self._cond:
            self._cooldown_until = max(self._cooldown_until, self._clock() + seconds)
            self._cond.notify_all()

    def backoff_delay(self, attempt: int) -> float:
        """指數退避秒數；attempt 從 1 起算，含 jitter。"""
        if attempt < 1:
            raise ValueError("attempt 必須從 1 起算")
        raw = self.config.backoff_base * (2 ** (attempt - 1))
        capped = min(raw, self.config.backoff_cap)
        jitter = capped * 0.25 * self._rng.random()
        return capped + jitter

    def wait_backoff(self, attempt: int) -> float:
        """退避等待並回傳實際等待秒數。"""
        delay = self.backoff_delay(attempt)
        self._sleep(delay)
        return delay
