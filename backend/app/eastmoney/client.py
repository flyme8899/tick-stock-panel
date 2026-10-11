"""东财请求的共用限流和熔断。

节奏来自 simonlin1212/a-stock-data SKILL.md 的 ``em_get``（Apache-2.0，
Copyright 2026 Simon Lin）。这里改成 httpx、进程内一把锁，并且拒绝 PDF。
所有 ``eastmoney.com`` 请求都应走 ``em_get``，避免打板、研报和资金账单叠在一起超过 2 次/秒。
"""
from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 两次请求的最小间隔。0.5 秒对应 2 次/秒，是上限，不能再快。
MIN_INTERVAL_FLOOR = 0.5
DEFAULT_MIN_INTERVAL = 1.0
FAILURE_LIMIT = 5
COOLDOWN_SECONDS = 600.0

_ALLOWED_SUFFIXES = ("eastmoney.com", "eastmoney.com.cn")


class CircuitOpenError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("eastmoney 熔断冷却中")


def min_interval() -> float:
    raw = (os.environ.get("EASTMONEY_MIN_INTERVAL") or "").strip()
    if not raw:
        return DEFAULT_MIN_INTERVAL
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MIN_INTERVAL
    if value < MIN_INTERVAL_FLOOR:
        return MIN_INTERVAL_FLOOR
    if value > 5:
        return 5.0
    return value


def _host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("pdf."):
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_SUFFIXES)


def _default_session() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(15.0, connect=5.0),
        headers={"User-Agent": UA},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
    )


class EastMoneyLimiter:
    """串行限流。持锁期间才打网络，保证全进程对东财不超过配置的速率。"""

    def __init__(
        self,
        *,
        min_interval_s: float | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        request: Callable[..., Any] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self.min_interval_s = (
            DEFAULT_MIN_INTERVAL if min_interval_s is None else max(MIN_INTERVAL_FLOOR, min_interval_s)
        )
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._request = request
        self._jitter = jitter if jitter is not None else (lambda: random.uniform(0.05, 0.2))
        self._lock = threading.Lock()
        self._next = 0.0
        self.consecutive_failures = 0
        self.open_until = 0.0
        self._session: httpx.Client | None = None

    def snapshot(self) -> dict:
        now = self._clock()
        return {
            "state": "open" if now < self.open_until else "closed",
            "consecutive_failures": self.consecutive_failures,
            "open_until": self.open_until,
            "min_interval": self.min_interval_s,
        }

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        timeout: float | tuple[float, float] = 15.0,
    ) -> Any:
        if not _host_allowed(url) or url.lower().split("?", 1)[0].endswith(".pdf"):
            raise ValueError("东财限流器只接受 eastmoney 的非 PDF 地址")
        with self._lock:
            now = self._clock()
            if now < self.open_until:
                raise CircuitOpenError
            wait = self._next - now
            if wait > 0:
                self._sleep(wait)
            try:
                response = self._send(url, params=params, headers=headers, timeout=timeout)
            except CircuitOpenError:
                raise
            except Exception:
                self._fail()
                raise
            status = int(getattr(response, "status_code", 200) or 200)
            final = str(getattr(response, "url", url) or url)
            if not _host_allowed(final):
                self._fail()
                raise RuntimeError("eastmoney 跳转到了站外")
            if status == 403:
                self.open_until = self._clock() + COOLDOWN_SECONDS
                self.consecutive_failures = 0
                raise RuntimeError("eastmoney 403")
            if status == 429 or status >= 500:
                self._fail()
                raise RuntimeError(f"eastmoney {status}")
            self.consecutive_failures = 0
            self._next = self._clock() + self.min_interval_s + max(0.0, self._jitter())
            return response

    def _send(
        self,
        url: str,
        *,
        params: dict | None,
        headers: dict | None,
        timeout: float | tuple[float, float],
    ) -> Any:
        if self._request is not None:
            return self._request(url, params=params, headers=headers, timeout=timeout)
        if self._session is None:
            self._session = _default_session()
        return self._session.get(
            url,
            params=params,
            headers=headers,
            timeout=httpx_timeout(timeout),
        )

    def _fail(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= FAILURE_LIMIT:
            self.open_until = self._clock() + COOLDOWN_SECONDS
            self.consecutive_failures = 0

    def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            session.close()


_limiter: EastMoneyLimiter | None = None
_limiter_lock = threading.Lock()


def get_limiter() -> EastMoneyLimiter:
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = EastMoneyLimiter(min_interval_s=min_interval())
        return _limiter


def reset_for_tests() -> None:
    global _limiter
    with _limiter_lock:
        if _limiter is not None:
            _limiter.close()
        _limiter = None


def httpx_timeout(timeout: float | tuple[float, float]) -> httpx.Timeout | float:
    """``(connect, read)`` 交给 httpx。单独的秒数保持原样，其它调用方不受影响。"""
    if isinstance(timeout, tuple):
        connect, read = timeout
        return httpx.Timeout(read, connect=connect)
    return timeout


def em_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float | tuple[float, float] = 15.0,
) -> Any:
    """东财统一请求。PDF 地址直接拒绝，不发起下载。"""
    return get_limiter().get(url, params=params, headers=headers, timeout=timeout)
