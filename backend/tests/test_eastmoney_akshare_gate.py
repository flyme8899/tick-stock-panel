"""akshare 打到东财的 GET 必须经过共用限流，且只限当前线程。中证指数公司不进这把锁。"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
import requests

from app.eastmoney.akshare_gate import install, reset_for_tests
from app.eastmoney.client import DEFAULT_MIN_INTERVAL, EastMoneyLimiter, min_interval


def test_eastmoney_gets_sleep_between_calls_and_other_hosts_do_not() -> None:
    sleeps: list[float] = []
    now = {"t": 0.0}
    limiter_urls: list[str] = []
    original_urls: list[str] = []

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    def limited(url, **_kwargs):
        limiter_urls.append(url)
        return SimpleNamespace(
            status_code=200,
            url=url,
            content=b"{}",
            text="{}",
            headers={},
            encoding="utf-8",
        )

    def original(self, method, url, *args, **kwargs):
        original_urls.append(str(url))
        return SimpleNamespace(status_code=200, url=url, content=b"ok", text="ok", headers={})

    limiter = EastMoneyLimiter(
        min_interval_s=1.0,
        clock=clock,
        sleeper=sleeper,
        request=limited,
        jitter=lambda: 0.1,
    )
    saved = requests.sessions.Session.request
    requests.sessions.Session.request = original  # type: ignore[method-assign]
    reset_for_tests()
    try:
        install(limiter)
        session = requests.Session()
        first = session.get("https://datacenter-web.eastmoney.com/api/data/v1/get", timeout=5)
        second = session.get("https://data.eastmoney.com/hsgtcg/list.html", timeout=(3, 8))
        session.get(
            "https://www.csindex.com.cn/csindex-home/indexInfo/index-constituents",
            timeout=5,
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert sleeps == [pytest.approx(1.1)]
        assert limiter_urls == [
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            "https://data.eastmoney.com/hsgtcg/list.html",
        ]
        assert original_urls == [
            "https://www.csindex.com.cn/csindex-home/indexInfo/index-constituents",
        ]
    finally:
        reset_for_tests()
        requests.sessions.Session.request = saved  # type: ignore[method-assign]


def test_other_threads_are_not_rate_limited() -> None:
    sleeps: list[float] = []
    now = {"t": 0.0}
    limiter_urls: list[str] = []
    original_urls: list[str] = []

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    def limited(url, **_kwargs):
        limiter_urls.append(url)
        return SimpleNamespace(status_code=200, url=url, content=b"{}", text="{}", headers={})

    def original(self, method, url, *args, **kwargs):
        original_urls.append(str(url))
        return SimpleNamespace(
            status_code=200,
            url=url,
            content=b"ok",
            text="ok",
            headers={},
            ok=True,
            marker="raw",
        )

    limiter = EastMoneyLimiter(
        min_interval_s=1.0,
        clock=clock,
        sleeper=sleeper,
        request=limited,
        jitter=lambda: 0.1,
    )
    saved = requests.sessions.Session.request
    requests.sessions.Session.request = original  # type: ignore[method-assign]
    reset_for_tests()
    other = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    seen: dict = {}

    def worker() -> None:
        response = requests.Session().get(other, timeout=5)
        seen["marker"] = getattr(response, "marker", None)
        seen["ok"] = getattr(response, "ok", None)

    try:
        install(limiter)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(2)
        session = requests.Session()
        session.get("https://datacenter-web.eastmoney.com/api/data/v1/get", timeout=5)
        session.get("https://data.eastmoney.com/hsgtcg/list.html", timeout=5)
        assert seen["marker"] == "raw"
        assert seen["ok"] is True
        assert other in original_urls
        assert other not in limiter_urls
        assert sleeps == [pytest.approx(1.1)]
    finally:
        reset_for_tests()
        requests.sessions.Session.request = saved  # type: ignore[method-assign]


def test_default_interval_stays_one_second(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EASTMONEY_MIN_INTERVAL", raising=False)
    assert DEFAULT_MIN_INTERVAL == 1.0
    assert min_interval() == 1.0
