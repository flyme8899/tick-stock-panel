"""向前扩展历史支持股票 / ETF / 指数三类资产。

背景: 此前 `/api/kline/extend_history` 只能补股票日K, ETF 与指数日K
深度长期停在首次同步的窗口内, 无法补齐。这里锁定:
  1. 路由按 asset_type 分派并校验取值;
  2. repository 按资产族读取各自视图的最早日期;
  3. 非法 asset_type / unit 明确报错而不是静默落到股票。
"""

from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import heavy_job_limiter, pipeline_jobs, preferences
from app.services.heavy_job_limiter import HeavyJobLimiter
from app.services.pipeline_jobs import JobStore


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """干净的 job store / limiter, 避免跨用例污染。"""
    monkeypatch.setattr(preferences, "load", lambda: {})
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    store = JobStore(store_dir=tmp_path / "jobs")
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.005)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", limiter)
    yield store, limiter
    # 用例间必须清掉活跃 job: create() 对 pending/running 去重,
    # 残留一个未收尾的 job 会让后续用例拿到 reused、服务层根本不被调用。
    store._active_id = None
    store._active_jobs.clear()


def _make_app(monkeypatch, capture, *, has_cap: bool = True):
    """构造只挂 kline 路由的最小 app, 并拦截 run_extend_history 收参。

    路由内部是 `from app.services.extend_history import run_extend_history`
    (函数内 import), 因此必须打补丁到源模块属性上。
    """
    from app.api import kline
    from app.services import extend_history

    def fake_run(repo, capset, value, unit, on_progress=None, asset_type="stock"):
        capture.update(
            repo=repo, capset=capset, value=value, unit=unit, asset_type=asset_type
        )
        return {"status": "ok", "asset_type": asset_type}

    monkeypatch.setattr(extend_history, "run_extend_history", fake_run)

    app = FastAPI()
    app.include_router(kline.router)
    app.state.repo = SimpleNamespace()
    app.state.capabilities = SimpleNamespace(has=lambda _: has_cap)
    return app


def _post_and_wait(monkeypatch, capture, body, *, has_cap=True):
    """POST 后等后台任务真正跑完(路由是 fire-and-forget)。

    create() 是单飞的: 若上一个用例的 job 还没被标成终态, 本次会拿到
    "reused" 而根本不调用服务层。因此这里先等 capture 落值, 超时再
    断言失败, 把「调用了一次」作为明确的验收点。
    """
    app = _make_app(monkeypatch, capture, has_cap=has_cap)
    with TestClient(app) as client:
        resp = client.post("/api/kline/extend_history", json=body)
        if resp.status_code == 200 and resp.json().get("status") == "started":
            wait_for(lambda: bool(capture), timeout=3)
    return resp


@pytest.mark.parametrize("asset_type", ["stock", "etf", "index"])
def test_extend_history_passes_asset_type_through(env, monkeypatch, asset_type):
    capture: dict = {}
    resp = _post_and_wait(
        monkeypatch, capture,
        {"value": 3, "unit": "year", "asset_type": asset_type},
    )
    assert resp.status_code == 200, resp.text
    assert capture["asset_type"] == asset_type
    assert capture["value"] == 3 and capture["unit"] == "year"


def test_extend_history_defaults_to_stock(env, monkeypatch):
    """旧版前端不发 asset_type 时仍按股票处理, 不能因改签名而 500。"""
    capture: dict = {}
    resp = _post_and_wait(monkeypatch, capture, {"value": 1, "unit": "month"})
    assert resp.status_code == 200, resp.text
    assert capture["asset_type"] == "stock"


@pytest.mark.parametrize("bad", ["bond", "", "STOCK", "etfs"])
def test_extend_history_rejects_unknown_asset_type(env, monkeypatch, bad):
    capture: dict = {}
    app = _make_app(monkeypatch, capture)
    with TestClient(app) as client:
        resp = client.post(
            "/api/kline/extend_history",
            json={"value": 1, "unit": "month", "asset_type": bad},
        )
    assert resp.status_code == 400, resp.text
    assert capture == {}, "非法 asset_type 不应触达服务层"


def test_extend_history_rejects_bad_unit_with_asset_type(env, monkeypatch):
    capture: dict = {}
    app = _make_app(monkeypatch, capture)
    with TestClient(app) as client:
        resp = client.post(
            "/api/kline/extend_history",
            json={"value": 1, "unit": "week", "asset_type": "etf"},
        )
    assert resp.status_code == 400, resp.text
    assert capture == {}


def test_extend_history_requires_batch_capability(env, monkeypatch):
    capture: dict = {}
    app = _make_app(monkeypatch, capture, has_cap=False)
    with TestClient(app) as client:
        resp = client.post(
            "/api/kline/extend_history",
            json={"value": 1, "unit": "month", "asset_type": "index"},
        )
    assert resp.status_code == 403, resp.text
    assert capture == {}


# --------------------------------------------------------------------------
# repository 层: 三族各读自己的视图
# --------------------------------------------------------------------------

def _fake_repo(rows):
    """构造一个只实现 execute_one 的 KlineRepository 实例(绕过 __init__)。"""
    from app.tickflow.repository import KlineRepository

    repo = object.__new__(KlineRepository)
    seen: list[str] = []

    def execute_one(sql, *args, **kwargs):
        seen.append(sql)
        return rows(sql)

    repo.execute_one = execute_one  # type: ignore[method-assign]
    return repo, seen


def test_repository_earliest_date_uses_per_asset_view():
    repo, seen = _fake_repo(lambda sql: (dt.date(2021, 6, 1),))

    assert repo.earliest_daily_date_for("stock") == dt.date(2021, 6, 1)
    assert "kline_daily" in seen[0] and "etf" not in seen[0] and "index" not in seen[0]

    assert repo.earliest_daily_date_for("etf") == dt.date(2021, 6, 1)
    assert "kline_etf_daily" in seen[1]

    assert repo.earliest_daily_date_for("index") == dt.date(2021, 6, 1)
    assert "kline_index_daily" in seen[2]


def test_repository_earliest_date_unknown_asset_falls_back_to_stock():
    repo, seen = _fake_repo(lambda sql: (None,))

    assert repo.earliest_daily_date_for("nope") is None
    assert "kline_daily" in seen[0]


def test_repository_earliest_date_swallows_query_errors():
    """视图缺失(旧部署未建表)时返回 None, 不应把接口打挂。"""
    def boom(sql):
        raise RuntimeError("table does not exist")

    repo, _ = _fake_repo(boom)
    assert repo.earliest_daily_date_for("etf") is None
    assert repo.earliest_daily_date_for("index") is None


def test_repository_earliest_date_parses_iso_string():
    """某些驱动把 date 回读成字符串, 需要归一化成 date。"""
    repo, _ = _fake_repo(lambda sql: ("2021-06-01",))
    assert repo.earliest_daily_date_for("stock") == dt.date(2021, 6, 1)


# --------------------------------------------------------------------------
# 两处白名单必须一致
# --------------------------------------------------------------------------

def test_asset_dirs_cover_all_supported_types():
    from app.services.extend_history import ASSET_TYPES, _ASSET_DIRS

    assert set(ASSET_TYPES) == {"stock", "etf", "index"}
    assert set(_ASSET_DIRS) == set(ASSET_TYPES)
    # 每一族都要有「日K + enriched」两个视图, 否则重算无落点
    for asset_type, (raw_view, enriched_view) in _ASSET_DIRS.items():
        assert "kline" in raw_view, asset_type
        assert "enriched" in enriched_view, asset_type
        assert raw_view != enriched_view


def test_repository_earliest_date_view_map_matches_asset_dirs():
    """两处白名单必须一致, 避免补了 ETF 却把 enriched 算到股票视图。"""
    from app.services.extend_history import _ASSET_DIRS
    from app.tickflow.repository import KlineRepository

    mapping = KlineRepository._EARLIEST_DATE_VIEWS
    assert set(mapping) == set(_ASSET_DIRS)
    for asset_type, view in mapping.items():
        assert view == _ASSET_DIRS[asset_type][0], asset_type
