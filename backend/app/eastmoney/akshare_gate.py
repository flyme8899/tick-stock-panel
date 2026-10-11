"""把 akshare 里打到东财的 HTTP 收进共用限流。

akshare 自己用 ``requests``。这里只改 ``Session.request``：主机是
``eastmoney.com`` 时改走 ``EastMoneyLimiter``，其它主机仍走原来的 requests。
调用结束会把补丁卸掉，避免测试或别的源一直被包在里面。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from app.eastmoney.client import EastMoneyLimiter, _host_allowed, get_limiter

_lock = threading.Lock()
_depth = 0
_original: Callable[..., Any] | None = None
_limiter: EastMoneyLimiter | None = None


class _Response:
    """httpx 响应上 akshare 会用到的那几个属性。"""

    def __init__(self, raw: Any) -> None:
        self.status_code = int(getattr(raw, "status_code", 200) or 200)
        self.url = str(getattr(raw, "url", "") or "")
        self.headers = getattr(raw, "headers", {}) or {}
        self.encoding = getattr(raw, "encoding", None) or "utf-8"
        self._raw = raw
        content = getattr(raw, "content", b"")
        if isinstance(content, str):
            content = content.encode(self.encoding, errors="replace")
        self._content = content or b""

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        raw_text = getattr(self._raw, "text", None)
        if isinstance(raw_text, str):
            return raw_text
        return self._content.decode(self.encoding or "utf-8", errors="replace")

    @property
    def apparent_encoding(self) -> str:
        return self.encoding or "utf-8"

    def json(self, **_kwargs: Any) -> Any:
        reader = getattr(self._raw, "json", None)
        if callable(reader):
            return reader()
        import json

        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"eastmoney HTTP {self.status_code}")


def _timeout_seconds(value: Any) -> float:
    if isinstance(value, (tuple, list)) and value:
        value = value[-1]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 15.0
    if number <= 0:
        return 15.0
    return number


def _patched(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
    limiter = _limiter
    original = _original
    if limiter is not None and str(method).upper() == "GET" and _host_allowed(str(url)):
        response = limiter.get(
            str(url),
            params=kwargs.get("params"),
            headers=kwargs.get("headers"),
            timeout=_timeout_seconds(kwargs.get("timeout")),
        )
        return _Response(response)
    if original is None:
        raise RuntimeError("东财 akshare 补丁没有原请求")
    return original(self, method, url, *args, **kwargs)


def install(limiter: EastMoneyLimiter | None = None) -> None:
    """套上补丁。嵌套调用只记一层，卸的时候才还原。"""
    global _depth, _original, _limiter
    import requests

    with _lock:
        if _depth == 0:
            _original = requests.sessions.Session.request
            _limiter = limiter or get_limiter()
            requests.sessions.Session.request = _patched  # type: ignore[method-assign]
        _depth += 1


def uninstall() -> None:
    global _depth, _original, _limiter
    import requests

    with _lock:
        _depth = max(0, _depth - 1)
        if _depth == 0 and _original is not None:
            requests.sessions.Session.request = _original  # type: ignore[method-assign]
            _original = None
            _limiter = None


def reset_for_tests() -> None:
    global _depth, _original, _limiter
    import requests

    with _lock:
        if _original is not None:
            requests.sessions.Session.request = _original  # type: ignore[method-assign]
        _depth = 0
        _original = None
        _limiter = None


def call_akshare(name: str, /, **kwargs: Any) -> Any:
    """调用允许的 akshare 函数。东财 GET 会经过共用限流。"""
    import akshare as ak

    fn = getattr(ak, name)
    install(get_limiter())
    try:
        return fn(**kwargs)
    finally:
        uninstall()
