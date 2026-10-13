"""热点事件榜按交易日留档：同一天同一档幂等，研究侧能按日期读回。"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.market_time import CN_TZ
from app.news.hot_rank import (
    SLOT_NEXTDAY,
    SLOT_POSTCLOSE,
    SLOT_PREOPEN,
    archive_hot_rank,
    capture_hot_rank_now,
    due_slots,
    load_hot_event_rank,
)
from app.news.hot_snapshot import clear_hot_snapshot_for_tests, publish_hot_snapshot

FRIDAY = datetime(2026, 10, 9, 9, 10, tzinfo=CN_TZ)
FRIDAY_DATE = date(2026, 10, 9)
MONDAY = date(2026, 10, 12)


def _snapshot(title: str = "华为发布新模型", *, computed_at: str = "2026-10-09T09:10:00+08:00") -> dict:
    return {
        "as_of": "2026-10-09T09:00:00+08:00",
        "trading_day": "2026-10-09",
        "computed_at": computed_at,
        "events": [{
            "key": "ev1",
            "name": title,
            "category": "产业",
            "concepts": ["昇腾", "算力"],
            "mentioned_stocks": [
                {"key": "600519.SH", "name": "贵州茅台", "mentions": 2},
                {"key": "510300.SH", "name": "沪深300ETF", "mentions": 1},
            ],
            "direction": "利好",
            "importance": "重要",
            "score": 12.5,
            "breakdown": {"importance": 4, "confirmation": 3, "mapping": 2, "freshness": 2, "heat": 1.5},
            "confirmation": {"state": "pending", "label": "待开盘验证", "score": 0},
        }],
    }


def test_due_slots_follow_the_session_clock():
    assert [item[0] for item in due_slots(FRIDAY)] == [SLOT_PREOPEN, SLOT_NEXTDAY]
    assert due_slots(FRIDAY)[0][1] == FRIDAY_DATE
    assert due_slots(datetime(2026, 10, 9, 9, 20, tzinfo=CN_TZ)) == []
    post = due_slots(datetime(2026, 10, 9, 15, 6, tzinfo=CN_TZ))
    assert [item[0] for item in post] == [SLOT_POSTCLOSE, SLOT_NEXTDAY]
    assert post[0][1] == FRIDAY_DATE
    assert post[0][2] == "first"
    assert post[1][1] == MONDAY
    weekend = due_slots(datetime(2026, 10, 10, 11, 0, tzinfo=CN_TZ))
    assert weekend == [(SLOT_NEXTDAY, MONDAY, "latest")]
    assert due_slots(datetime(2026, 10, 9, 15, 0, tzinfo=CN_TZ))[0][0] == SLOT_NEXTDAY


def test_postclose_is_first_wins_and_preopen_is_latest_wins(tmp_path):
    close_at = datetime(2026, 10, 9, 15, 6, tzinfo=CN_TZ)
    archive_hot_rank(_snapshot("第一份"), now=close_at, data_dir=tmp_path)
    archive_hot_rank(_snapshot("第二份"), now=close_at, data_dir=tmp_path)
    post = load_hot_event_rank(tmp_path, FRIDAY_DATE, SLOT_POSTCLOSE)
    nxt = load_hot_event_rank(tmp_path, MONDAY, SLOT_NEXTDAY)
    assert post["title"].to_list() == ["第一份"]
    assert nxt["title"].to_list() == ["第二份"]
    assert post["rank"].to_list() == [1]
    assert post["concepts"].to_list() == [["昇腾", "算力"]]
    assert post["stocks"].to_list() == [["600519.SH"]]
    assert post["etfs"].to_list() == [["510300.SH"]]
    assert post["validation_state"].to_list() == ["pending"]
    assert post["validation_label"].to_list() == ["待开盘验证"]

    archive_hot_rank(_snapshot("开盘前"), now=FRIDAY, data_dir=tmp_path)
    archive_hot_rank(_snapshot("开盘前终稿"), now=FRIDAY.replace(minute=14), data_dir=tmp_path)
    pre = load_hot_event_rank(tmp_path, FRIDAY_DATE, SLOT_PREOPEN)
    assert pre["title"].to_list() == ["开盘前终稿"]
    # 当天同时有开盘前、收盘后，以及 09:15 前写下的次日口径（分区日就是当天）。
    friday = load_hot_event_rank(tmp_path, FRIDAY_DATE)
    assert set(friday["slot"].to_list()) == {SLOT_PREOPEN, SLOT_POSTCLOSE, SLOT_NEXTDAY}


def test_empty_board_still_freezes_the_slot(tmp_path):
    archive_hot_rank({"events": [], "trading_day": "2026-10-09"}, now=datetime(2026, 10, 9, 15, 6, tzinfo=CN_TZ), data_dir=tmp_path)
    frame = load_hot_event_rank(tmp_path, FRIDAY_DATE, SLOT_POSTCLOSE)
    assert frame.is_empty()
    assert "event_key" in frame.columns
    archive_hot_rank(_snapshot("来晚了"), now=datetime(2026, 10, 9, 15, 10, tzinfo=CN_TZ), data_dir=tmp_path)
    assert load_hot_event_rank(tmp_path, FRIDAY_DATE, SLOT_POSTCLOSE).is_empty()


def test_outside_the_window_does_not_write(tmp_path):
    written = archive_hot_rank(_snapshot(), now=datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ), data_dir=tmp_path)
    assert written == []
    assert load_hot_event_rank(tmp_path, FRIDAY_DATE).is_empty()


def test_publish_snapshot_does_not_archive(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    publish_hot_snapshot(_snapshot(), persist=True)
    assert not (tmp_path / "news" / "hot_event_rank").exists()
    clear_hot_snapshot_for_tests()


def test_capture_reuses_memory_inside_the_window(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    publish_hot_snapshot(
        _snapshot(computed_at="2026-10-09T15:06:00+08:00"),
        persist=False,
        computed_at="2026-10-09T15:06:00+08:00",
    )
    called = {"refresh": 0}

    def _refresh():
        called["refresh"] += 1

    monkeypatch.setattr("app.news.hot_snapshot.refresh_hot_events_sync", _refresh)
    assert capture_hot_rank_now(datetime(2026, 10, 9, 15, 8, tzinfo=CN_TZ)) is True
    assert called["refresh"] == 0
    assert load_hot_event_rank(tmp_path, FRIDAY_DATE, SLOT_POSTCLOSE)["title"].to_list() == ["华为发布新模型"]
    clear_hot_snapshot_for_tests()


def test_capture_refreshes_when_memory_is_from_another_window(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    publish_hot_snapshot(
        _snapshot(computed_at="2026-10-09T14:00:00+08:00"),
        persist=False,
        computed_at="2026-10-09T14:00:00+08:00",
    )
    called = {"refresh": 0}

    def _refresh():
        called["refresh"] += 1

    monkeypatch.setattr("app.news.hot_snapshot.refresh_hot_events_sync", _refresh)
    capture_hot_rank_now(datetime(2026, 10, 9, 15, 6, tzinfo=CN_TZ))
    assert called["refresh"] == 1
    assert load_hot_event_rank(tmp_path, FRIDAY_DATE, SLOT_POSTCLOSE).is_empty()
    clear_hot_snapshot_for_tests()


def test_hot_rank_api_reads_the_partition(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.api.news import hot_rank
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    archive_hot_rank(_snapshot("接口事件"), now=FRIDAY, data_dir=tmp_path)
    payload = hot_rank(day="2026-10-09", slot=SLOT_PREOPEN)
    assert payload["rows"][0]["title"] == "接口事件"
    assert payload["rows"][0]["rank"] == 1
    missing = hot_rank(day="2026-10-08", slot=None)
    assert missing["rows"] == []
    with pytest.raises(HTTPException) as bad_slot:
        hot_rank(day="2026-10-09", slot="nope")
    assert bad_slot.value.status_code == 400


def test_compute_once_archives_with_the_start_clock(tmp_path, monkeypatch):
    from app.config import settings
    from app.news import hot_snapshot
    from app.news.service import reset_store_for_tests

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    reset_store_for_tests(tmp_path / "news.sqlite")
    monkeypatch.setattr(
        "app.news.hot_events.build_top_hot_events",
        lambda *args, **kwargs: _snapshot("收盘榜"),
    )
    monkeypatch.setattr("app.news.mx_hot.enrich_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.news.hot_snapshot.cn_now",
        lambda: datetime(2026, 10, 9, 15, 6, tzinfo=CN_TZ),
    )
    hot_snapshot._compute_once()
    frame = load_hot_event_rank(tmp_path, FRIDAY_DATE, SLOT_POSTCLOSE)
    assert frame["title"].to_list() == ["收盘榜"]
    assert str(frame["computed_at"][0]).startswith("2026-10-09T15:06")
    clear_hot_snapshot_for_tests()


def test_scheduler_registers_hot_rank_jobs():
    import asyncio

    from app.jobs.daily_pipeline import start_scheduler

    async def _ids() -> set[str]:
        sched = start_scheduler(object(), object())
        try:
            return {job.id for job in sched.get_jobs()}
        finally:
            sched.shutdown(wait=False)

    ids = asyncio.run(_ids())
    assert "hot_rank_preopen" in ids
    assert "hot_rank_postclose" in ids

