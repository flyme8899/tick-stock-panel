"""个股详情分时轮询的 live 直拉路径测试。

背景: 盘中分钟增量落盘后, 当日本地分区很快达到 90% 完整度,
/api/kline/minute 的本地优先启发式会拦截实时补拉, 详情分时图停在
上一增量轮 (≥60s 滞后)。live=1 让详情轮询在连续竞价时段绕过本地优先。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kline import router
from app.market_time import CN_TZ, in_continuous_session
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

# 2026-08-26 是周三; 10:00 处于上午连续竞价, expected(已交易分钟) = 30
_NOW = datetime(2026, 8, 26, 10, 0, tzinfo=CN_TZ)
_TODAY = date(2026, 8, 26)
_LOCAL_ROWS = 30


class _FakeRepo:
    def __init__(self) -> None:
        # /minute 读取 repo.store.data_dir 判断分钟基准标记 (无标记即旧行为)
        import tempfile
        from types import SimpleNamespace
        from pathlib import Path
        self.store = SimpleNamespace(data_dir=Path(tempfile.mkdtemp()))

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_instruments(self) -> pl.DataFrame:
        return pl.DataFrame(
            {"symbol": [], "name": [], "total_shares": [], "float_shares": []}
        )

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        return pl.DataFrame({"date": [], "close": []})

    def get_minute(self, symbol, trade_date, asset_type="stock") -> pl.DataFrame:
        return pl.DataFrame({
            "datetime": [
                datetime(2026, 8, 26, 9, 30 + offset // 60, offset % 60)
                for offset in range(_LOCAL_ROWS)
            ],
            "close": [10.0] * _LOCAL_ROWS,
        })


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _FakeRepo()
    app.state.capabilities = CapabilitySet({
        Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits(),
    })
    return TestClient(app)


def _patch_market(monkeypatch, *, in_session: bool) -> None:
    import app.api.kline as kline_api

    monkeypatch.setattr(kline_api, "cn_now", lambda: _NOW)
    monkeypatch.setattr(kline_api, "cn_today", lambda: _TODAY)
    monkeypatch.setattr(kline_api, "in_continuous_session", lambda: in_session)
    # 缺省日期路径会问交易日探针; 钉成交易日 (_NOW 是周三), 不打真实行情请求
    monkeypatch.setattr(kline_api.trading_day, "is_trading_day", lambda now=None: True)


def _patch_live_fetch(monkeypatch) -> None:
    import app.api.kline as kline_api

    def _fake_fetch(symbol, trade_date, asset_type="stock", *, capset, raw_basis=False):
        return pl.DataFrame({
            "datetime": [datetime(2026, 8, 26, 9, 59)],
            "close": [11.11],
        })

    monkeypatch.setattr(
        kline_api.kline_sync, "fetch_minute_single", _fake_fetch
    )


def test_minute_live_param_bypasses_local_first_during_session(monkeypatch):
    _patch_market(monkeypatch, in_session=True)
    _patch_live_fetch(monkeypatch)

    resp = _client().get(
        "/api/kline/minute", params={"symbol": "600000.SH", "live": 1}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    assert body["rows"][0]["close"] == 11.11


def test_minute_without_live_keeps_local_first(monkeypatch):
    _patch_market(monkeypatch, in_session=True)
    _patch_live_fetch(monkeypatch)

    resp = _client().get(
        "/api/kline/minute", params={"symbol": "600000.SH"}
    )

    assert resp.status_code == 200
    body = resp.json()
    # 本地 30 根 >= expected(30)*0.9 → 完整, 走本地
    assert body["source"] == "local"
    assert body["rows"][0]["close"] == 10.0
    assert isinstance(body["t_trade"]["marks"], list)
    assert body["t_trade"]["date"] == "2026-08-26"


def test_minute_live_rows_carry_the_same_t_trade_marks(monkeypatch):
    _patch_market(monkeypatch, in_session=True)
    import app.api.kline as kline_api

    def _fake_fetch(symbol, trade_date, asset_type="stock", *, capset, raw_basis=False):
        del symbol, trade_date, asset_type, capset, raw_basis
        return pl.DataFrame({
            "datetime": [datetime(2026, 8, 26, 9, 35), datetime(2026, 8, 26, 9, 36)],
            "open": [10.0, 10.4],
            "high": [10.0, 10.4],
            "low": [10.0, 10.4],
            "close": [10.0, 10.4],
            "volume": [1000.0, 1.0],
            "amount": [1_000_000.0, 1040.0],
        })

    monkeypatch.setattr(kline_api.kline_sync, "fetch_minute_single", _fake_fetch)
    resp = _client().get("/api/kline/minute", params={"symbol": "600000.SH", "live": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    mark = body["t_trade"]["marks"][0]
    assert mark["time"] == "09:36"
    assert mark["side"] == "sell"
    assert mark["date"] == "2026-08-26"
    assert mark["price"] == pytest.approx(10.4)


def test_minute_without_bars_returns_empty_marks(monkeypatch):
    _patch_market(monkeypatch, in_session=False)
    import app.api.kline as kline_api

    class _EmptyRepo(_FakeRepo):
        def get_minute(self, symbol, trade_date, asset_type="stock") -> pl.DataFrame:
            del symbol, trade_date, asset_type
            return pl.DataFrame({"datetime": [], "close": []})

    def _empty(*_args, **_kwargs):
        return pl.DataFrame({"datetime": [], "close": []})

    monkeypatch.setattr(kline_api.kline_sync, "fetch_minute_single", _empty)
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _EmptyRepo()
    app.state.capabilities = CapabilitySet({Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()})
    resp = TestClient(app).get("/api/kline/minute", params={"symbol": "600000.SH", "date": "2026-08-26"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["t_trade"]["marks"] == []


def test_minute_live_param_falls_back_to_local_off_session(monkeypatch):
    _patch_market(monkeypatch, in_session=False)
    _patch_live_fetch(monkeypatch)

    resp = _client().get(
        "/api/kline/minute", params={"symbol": "600000.SH", "live": 1}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "local"


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 29, False),
        (9, 30, True),
        (11, 30, True),
        (11, 31, False),
        (12, 30, False),
        (13, 0, True),
        (15, 0, True),
        (15, 1, False),
    ],
)
def test_in_continuous_session_boundaries(hour: int, minute: int, expected: bool):
    now = datetime(2026, 8, 26, hour, minute, tzinfo=CN_TZ)  # 周三
    assert in_continuous_session(now) is expected


def test_in_continuous_session_rejects_weekend():
    assert in_continuous_session(datetime(2026, 8, 29, 10, 0, tzinfo=CN_TZ)) is False
