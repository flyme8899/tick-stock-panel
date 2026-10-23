"""重试与熔断。

最多 3 次重试（一共 4 次调用），失败后依次等待 2s、4s、8s。
同一数据源连续 8 次调用失败后熔断，冷却 10 分钟。冷却期内不再打该源，
交给调用方换下一个源。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

BACKOFF_SECONDS = (2.0, 4.0, 8.0)
MAX_RETRIES = 3
FAILURE_LIMIT = 8
COOLDOWN_SECONDS = 600.0

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    def __init__(self, source: str) -> None:
        super().__init__(f"{source} 熔断冷却中")
        self.source = source


class CircuitBreaker:
    def __init__(
        self,
        source: str,
        *,
        clock: Callable[[], float] | None = None,
        failure_limit: int = FAILURE_LIMIT,
        cooldown_s: float = COOLDOWN_SECONDS,
    ) -> None:
        self.source = source
        self._clock = clock or time.monotonic
        self.failure_limit = failure_limit
        self.cooldown_s = cooldown_s
        self.consecutive_failures = 0
        self.open_until = 0.0
        # 盘中东财排名在旁路线程里跑，可能和本轮其它 akshare 任务同时记失败。
        self._lock = threading.RLock()

    def allow(self) -> bool:
        with self._lock:
            return self._clock() >= self.open_until

    @property
    def state(self) -> str:
        return "closed" if self.allow() else "open"

    def success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0

    def failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failure_limit:
                self.open_until = self._clock() + self.cooldown_s
                self.consecutive_failures = 0

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            open_now = now < self.open_until
            return {
                "source": self.source,
                "state": "open" if open_now else "closed",
                "consecutive_failures": self.consecutive_failures,
                "open_until": self.open_until,
                # ``open_until`` 是 ``self._clock()`` 基准上的绝对时刻, 缺省时钟是
                # ``time.monotonic()`` —— 进程重启后从 0 重新计数。把它原样写盘、
                # 再在重启后读回来, 冷却期会被放大成「重启前已运行了多久」。
                # 落盘只认这个剩余秒数; ``open_until`` 保留只为兼容旧状态文件。
                "cooldown_remaining_s": max(0.0, self.open_until - now),
            }

    def restore(self, row: dict) -> None:
        """从 ``snapshot()`` 的结果恢复。``row`` 可能来自旧版本。

        ``open_until`` 的绝对值在新进程里没有意义 (monotonic 从 0 起算), 所以有
        ``cooldown_remaining_s`` 时按剩余秒数重算。

        旧状态文件只有 ``open_until``, 无法判断它是「monotonic 残留」还是「别的东西」。
        两种可能的代价不对称: 当成有效值会白等几天, 当成过期只是提前重试一次 (失败
        会重新触发熔断)。取后者 —— 一律视为已过期。
        """
        with self._lock:
            try:
                self.consecutive_failures = int(row.get("consecutive_failures") or 0)
            except (TypeError, ValueError):
                self.consecutive_failures = 0
            remaining = row.get("cooldown_remaining_s")
            if remaining is None:
                # 旧格式, 见上: 丢弃 open_until, 下一轮正常重试。
                self.open_until = 0.0
                return
            try:
                left = max(0.0, float(remaining))
            except (TypeError, ValueError):
                left = 0.0
            self.open_until = self._clock() + left if left > 0 else 0.0


def call_with_retry(
    source: str,
    fn: Callable[[], T],
    breaker: CircuitBreaker,
    *,
    sleeper: Callable[[float], None] | None = None,
) -> T:
    """调用 ``fn``。熔断打开时立刻失败，不消耗重试。"""
    if not breaker.allow():
        raise CircuitOpenError(source)
    sleep = sleeper or time.sleep
    last: BaseException | None = None
    for attempt in range(MAX_RETRIES + 1):
        if not breaker.allow():
            raise CircuitOpenError(source)
        try:
            value = fn()
        except CircuitOpenError:
            raise
        except Exception as exc:
            last = exc
            breaker.failure()
            if attempt >= MAX_RETRIES or not breaker.allow():
                if not breaker.allow():
                    raise CircuitOpenError(source) from exc
                raise
            sleep(BACKOFF_SECONDS[attempt])
            continue
        breaker.success()
        return value
    assert last is not None
    raise last
