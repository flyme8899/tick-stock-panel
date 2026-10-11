"""热门事件快照：请求只读，过期单飞重算，重启能从磁盘恢复。"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app.market_time import cn_now
from app.news.hot_snapshot import (
    HOT_REFRESH_SECONDS,
    clear_hot_snapshot_for_tests,
    load_persisted_snapshot,
    publish_hot_snapshot,
    read_served_snapshot,
    wait_hot_refresh,
)
from app.news.scheduler import NewsScheduler


@pytest.fixture(autouse=True)
def _isolate_hot_snapshot():
    clear_hot_snapshot_for_tests()
    yield
    wait_hot_refresh(2)
    clear_hot_snapshot_for_tests()


def _event_snapshot(**overrides) -> dict:
    payload = {
        "events": [{
            "key": "ev_old",
            "name": "华为发布新模型",
            "mentions": 2,
            "source_count": 2,
            "mentioned_stocks": [{"key": "600519.SH", "name": "贵州茅台", "mentions": 1}],
            "concepts": ["昇腾"],
            "direction": "利好",
            "headline": "华为发布新模型",
            "headlines": ["华为发布新模型"],
            "first_seen": "14:52",
            "heat": 3,
            "importance": "重大",
            "score": 10,
            "category": "科技与产业",
        }],
        "as_of": "2026-10-09",
        "trading_day": "2026-10-09",
        "fallback": False,
        "hint": "更新于 14:52",
        "updated_at": "14:52",
    }
    payload.update(overrides)
    return payload


def test_refresh_interval_is_five_minutes():
    assert HOT_REFRESH_SECONDS == 300


def test_stale_threshold_adapts_to_compute_cost(monkeypatch):
    """冷算比基础间隔慢时，过期阈值要跟着抬高，别刚发布就判过期。

    线上冷算含两批模型调用约 282 秒，固定 300 秒阈值会让请求路径每次都触发重算。
    """
    import app.news.hot_snapshot as snap

    original = snap._LAST_COST_S
    try:
        monkeypatch.setattr(snap, "_LAST_COST_S", 0.0)
        assert snap._stale_seconds() == HOT_REFRESH_SECONDS
        # 上一轮耗时 400 秒 → 阈值抬到 400×1.5=600。
        monkeypatch.setattr(snap, "_LAST_COST_S", 400.0)
        assert snap._stale_seconds() == 600.0
        # 耗时再长也不超过上限，否则盘中数据会明显滞后。
        monkeypatch.setattr(snap, "_LAST_COST_S", 10_000.0)
        assert snap._stale_seconds() == snap.HOT_REFRESH_MAX_SECONDS
    finally:
        snap._LAST_COST_S = original


def test_missing_snapshot_returns_computing_without_blocking(monkeypatch):
    done = threading.Event()

    def fake(now, store, **kwargs):
        del now, store, kwargs
        done.set()
        return _event_snapshot(events=[], hint=None, updated_at=None, as_of=None)

    monkeypatch.setattr("app.news.hot_events.build_top_hot_events", fake)
    payload = read_served_snapshot()
    assert payload["events"] == []
    assert payload["status"] == "computing"
    assert payload["hint"] == "正在计算"
    assert payload["updated_at"] is None
    assert payload["computed_at"] is None
    assert done.wait(2)


def test_fresh_snapshot_is_served_without_recompute(monkeypatch):
    def boom(*args, **kwargs):
        del args, kwargs
        raise AssertionError("新鲜快照不应重算")

    monkeypatch.setattr("app.news.hot_events.build_top_hot_events", boom)
    publish_hot_snapshot(_event_snapshot(), persist=False)
    served = read_served_snapshot()
    assert served["status"] == "ready"
    assert served["updated_at"] == "14:52"
    assert served["computed_at"]
    assert served["events"][0]["name"] == "华为发布新模型"
    assert served["hint"] == "更新于 14:52"


def test_stale_snapshot_returns_old_result_and_single_flights(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow(now, store, **kwargs):
        del now, store, kwargs
        calls["n"] += 1
        started.set()
        assert release.wait(2)
        return _event_snapshot(name="新事件")

    monkeypatch.setattr("app.news.hot_events.build_top_hot_events", slow)
    stale_at = (cn_now() - timedelta(seconds=HOT_REFRESH_SECONDS + 5)).isoformat()
    publish_hot_snapshot(_event_snapshot(), persist=False, computed_at=stale_at)
    try:
        first = read_served_snapshot()
        second = read_served_snapshot()
        assert first["status"] == "stale"
        assert second["status"] == "stale"
        assert first["events"][0]["key"] == "ev_old"
        assert first["updated_at"] == "14:52"
        assert first["hint"] == "更新于 14:52"
        assert started.wait(1)
        assert calls["n"] == 1
    finally:
        release.set()
    assert wait_hot_refresh(2)
    assert calls["n"] == 1


def test_snapshot_reloads_from_disk(monkeypatch, tmp_path):
    path = tmp_path / "news" / "hot_events_snapshot.json"
    monkeypatch.setattr("app.news.hot_snapshot.snapshot_path", lambda: path)

    def boom(*args, **kwargs):
        del args, kwargs
        raise AssertionError("磁盘快照不应现算")

    publish_hot_snapshot(_event_snapshot(), persist=True)
    assert path.is_file()
    clear_hot_snapshot_for_tests()
    monkeypatch.setattr("app.news.hot_events.build_top_hot_events", boom)
    assert load_persisted_snapshot() is True
    served = read_served_snapshot()
    assert served["status"] == "ready"
    assert served["events"][0]["name"] == "华为发布新模型"
    assert served["updated_at"] == "14:52"


def test_corrupt_snapshot_file_is_ignored(monkeypatch, tmp_path):
    path = tmp_path / "hot_events_snapshot.json"
    path.write_text("{", encoding="utf-8")
    monkeypatch.setattr("app.news.hot_snapshot.snapshot_path", lambda: path)
    assert load_persisted_snapshot() is False


def test_request_callers_read_snapshot_only(monkeypatch):
    def boom(*args, **kwargs):
        del args, kwargs
        raise AssertionError("请求路径不应聚类")

    monkeypatch.setattr("app.news.hot_events.build_top_hot_events", boom)
    publish_hot_snapshot(_event_snapshot(), persist=False)

    from app.api import news as news_api
    from app.custom.dsa.stock_context import _load_events
    from app.research_reports.jobs import _hot_symbols
    from app.services.picker import load_top_events_from_news

    listed = news_api.hot(kind="event", window_hours=24, baseline_days=4, limit=20)
    assert listed["events"][0]["name"] == "华为发布新模型"
    assert listed["status"] == "ready"
    assert listed["updated_at"] == "14:52"
    assert listed["computed_at"]
    assert "item_ids" not in listed["events"][0]

    picked = load_top_events_from_news(cn_now())
    assert picked["status"] == "ready"
    assert picked["events"][0]["key"] == "ev_old"

    loaded = _load_events("600519.SH", cn_now())
    assert loaded["events"]
    assert loaded["events"][0]["name"] == "华为发布新模型"
    assert "更新于 14:52" in loaded["note"]

    assert "600519.SH" in _hot_symbols()


def test_scheduler_loads_snapshot_and_refreshes_in_background(monkeypatch):
    loaded = []
    hot_loop = NewsScheduler._hot_loop
    monkeypatch.setattr(NewsScheduler, "_loop", lambda self: None)
    monkeypatch.setattr(NewsScheduler, "_hot_loop", lambda self: None)
    monkeypatch.setattr(
        "app.news.hot_snapshot.load_persisted_snapshot",
        lambda: loaded.append(1) or True,
    )
    sched = NewsScheduler()
    sched.start()
    sched.stop()
    assert loaded == [1]

    calls = []

    def refresh():
        calls.append("run")
        sched._stop.set()

    monkeypatch.setattr("app.news.hot_snapshot.refresh_hot_events_sync", refresh)
    sched._stop.clear()
    hot_loop(sched)
    assert calls == ["run"]
