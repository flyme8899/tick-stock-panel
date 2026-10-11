"""akshare 打到东财的 GET 必须经过共用限流。中证指数公司不进这把锁。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from app.eastmoney.akshare_gate import install, reset_for_tests
from app.eastmoney.client import EastMoneyLimiter


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

    def original(self, method, url, *args, **kwargs):  # noqa: ANN001
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
