"""东财共用限流。时钟和网络都是假的，不打外网。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.eastmoney.client import (
    MIN_INTERVAL_FLOOR,
    CircuitOpenError,
    EastMoneyLimiter,
    em_get,
    reset_for_tests,
)


def _ok(url: str = "https://push2ex.eastmoney.com/getTopicZTPool"):
    return SimpleNamespace(status_code=200, url=url)


def test_first_call_does_not_sleep_and_later_calls_keep_the_gap() -> None:
    sleeps: list[float] = []
    now = {"t": 0.0}
    calls: list[float] = []

    def clock() -> float:
        return now["t"]

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    def request(url, **_kwargs):
        calls.append(now["t"])
        return _ok(url)

    limiter = EastMoneyLimiter(
        min_interval_s=1.0,
        clock=clock,
        sleeper=sleeper,
        request=request,
        jitter=lambda: 0.1,
    )
    limiter.get("https://push2ex.eastmoney.com/a")
    limiter.get("https://push2his.eastmoney.com/b")
    assert sleeps == [1.1]
    assert calls == [0.0, 1.1]


def test_interval_never_faster_than_two_per_second() -> None:
    limiter = EastMoneyLimiter(min_interval_s=0.1, jitter=lambda: 0)
    assert limiter.min_interval_s == MIN_INTERVAL_FLOOR


def test_403_opens_the_circuit_immediately() -> None:
    seen = {"n": 0}

    def request(url, **_kwargs):
        seen["n"] += 1
        return SimpleNamespace(status_code=403, url=url)

    limiter = EastMoneyLimiter(
        min_interval_s=0.5,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        request=request,
        jitter=lambda: 0,
    )
    with pytest.raises(RuntimeError, match="403"):
        limiter.get("https://reportapi.eastmoney.com/report/list")
    with pytest.raises(CircuitOpenError):
        limiter.get("https://reportapi.eastmoney.com/report/list")
    assert seen["n"] == 1
    assert limiter.snapshot()["state"] == "open"


def test_fifth_failure_opens_the_circuit() -> None:
    seen = {"n": 0}

    def request(_url, **_kwargs):
        seen["n"] += 1
        raise RuntimeError("reset")

    limiter = EastMoneyLimiter(
        min_interval_s=0.5,
        clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        request=request,
        jitter=lambda: 0,
    )
    for _ in range(4):
        with pytest.raises(RuntimeError, match="reset"):
            limiter.get("https://push2ex.eastmoney.com/pool")
    with pytest.raises(RuntimeError, match="reset"):
        limiter.get("https://push2ex.eastmoney.com/pool")
    with pytest.raises(CircuitOpenError):
        limiter.get("https://push2ex.eastmoney.com/pool")
    assert seen["n"] == 5


@pytest.mark.parametrize(
    "url",
    [
        "https://pdf.dfcfw.com/pdf/H3_AP1_1.pdf",
        "https://example.com/quote",
        "https://push2ex.eastmoney.com/file.PDF",
    ],
)
def test_pdf_and_foreign_hosts_are_rejected(url: str) -> None:
    limiter = EastMoneyLimiter(request=lambda *_a, **_k: pytest.fail("不应出网"))
    with pytest.raises(ValueError):
        limiter.get(url)


def test_tuple_timeout_keeps_connect_and_read() -> None:
    from app.eastmoney.client import httpx_timeout

    timeout = httpx_timeout((10, 30))
    assert timeout.connect == 10
    assert timeout.read == 30
    assert httpx_timeout(15) == 15


def test_em_get_uses_the_shared_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_for_tests()
    monkeypatch.setenv("EASTMONEY_MIN_INTERVAL", "0.1")
    reset_for_tests()
    with pytest.raises(ValueError):
        em_get("https://pdf.dfcfw.com/pdf/H3_AP1_1.pdf")
    from app.eastmoney.client import get_limiter

    assert get_limiter().min_interval_s == MIN_INTERVAL_FLOOR
    reset_for_tests()
