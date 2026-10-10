"""决策仪表盘移出与同日大盘复盘去重。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from src.services.dashboard_history import collapse_market_reviews, market_review_day
from src.services.history_service import HistoryService
from src.storage import AnalysisHistory, DatabaseManager


def _review(record_id: int, summary: str, created: datetime, region: str = "cn") -> SimpleNamespace:
    return SimpleNamespace(
        id=record_id,
        code="MARKET",
        report_type="market_review",
        analysis_summary=summary,
        news_content="",
        created_at=created,
        context_snapshot=f'{{"market_review_region": "{region}"}}',
    )


def _stock(record_id: int, code: str, created: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=record_id,
        code=code,
        report_type="detailed",
        analysis_summary="个股结论",
        news_content="",
        created_at=created,
    )


def test_same_day_market_reviews_keep_the_latest_only() -> None:
    morning = _review(1, "2026-10-10 大盘复盘 早盘", datetime(2026, 10, 10, 15, 10))
    close = _review(2, "2026-10-10 大盘复盘 收盘", datetime(2026, 10, 10, 18, 40))
    previous = _review(3, "2026-10-09 大盘复盘", datetime(2026, 10, 9, 18, 0))
    stock = _stock(4, "600519", datetime(2026, 10, 10, 18, 1))

    collapsed = collapse_market_reviews([morning, stock, close, previous])

    assert [item.id for item in collapsed] == [2, 4, 3]
    assert market_review_day(close) == "2026-10-10"
    assert market_review_day(stock) is None


def test_chinese_heading_date_groups_with_iso_date() -> None:
    early = _review(1, "2026年10月10日 大盘复盘", datetime(2026, 10, 10, 12, 0))
    later = _review(2, "2026/10/10 大盘复盘", datetime(2026, 10, 10, 19, 0))

    collapsed = collapse_market_reviews([early, later])

    assert [item.id for item in collapsed] == [2]


def test_missing_heading_date_uses_created_at() -> None:
    first = _review(1, "大盘复盘报告已生成。", datetime(2026, 10, 10, 18, 0))
    second = _review(2, "大盘复盘报告已生成。", datetime(2026, 10, 10, 19, 0))
    next_day = _review(3, "大盘复盘报告已生成。", datetime(2026, 10, 11, 9, 0))

    collapsed = collapse_market_reviews([first, next_day, second])

    assert [item.id for item in collapsed] == [3, 2]


def test_aware_created_at_uses_shanghai_calendar_day() -> None:
    utc_late = datetime(2026, 10, 10, 16, 30, tzinfo=UTC)  # 10月11日 00:30 上海
    record = _review(1, "大盘复盘报告已生成。", utc_late)

    assert market_review_day(record) == "2026-10-11"


@pytest.fixture
def history_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ignored.db"))
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'history.db'}")
    try:
        yield manager
    finally:
        DatabaseManager.reset_instance()


def _insert(manager: DatabaseManager, **kwargs) -> int:
    row = AnalysisHistory(
        query_id=kwargs["query_id"],
        code=kwargs["code"],
        name=kwargs.get("name"),
        report_type=kwargs.get("report_type"),
        sentiment_score=kwargs.get("sentiment_score", 50),
        operation_advice=kwargs.get("operation_advice", "观望"),
        trend_prediction=kwargs.get("trend_prediction", "震荡"),
        analysis_summary=kwargs.get("analysis_summary", ""),
        created_at=kwargs["created_at"],
    )
    session = manager.get_session()
    try:
        session.add(row)
        session.commit()
        return int(row.id)
    finally:
        session.close()


def test_dismiss_read_and_old_rows_drop_out_of_the_list(history_db: DatabaseManager) -> None:
    now = datetime.now()
    fresh = _insert(
        history_db,
        query_id="fresh",
        code="600519",
        name="贵州茅台",
        report_type="detailed",
        analysis_summary="仍要看",
        created_at=now,
    )
    aged = _insert(
        history_db,
        query_id="aged",
        code="300750",
        name="宁德时代",
        report_type="detailed",
        analysis_summary="太旧",
        created_at=now - timedelta(days=10),
    )
    read_id = _insert(
        history_db,
        query_id="read",
        code="603626",
        name="科森科技",
        report_type="detailed",
        analysis_summary="已读",
        created_at=now - timedelta(hours=1),
    )
    morning = _insert(
        history_db,
        query_id="review-am",
        code="MARKET",
        name="大盘复盘",
        report_type="market_review",
        analysis_summary="2026-10-10 大盘复盘 早盘",
        created_at=now - timedelta(hours=3),
    )
    closing = _insert(
        history_db,
        query_id="review-pm",
        code="MARKET",
        name="大盘复盘",
        report_type="market_review",
        analysis_summary="2026-10-10 大盘复盘 收盘",
        created_at=now - timedelta(hours=2),
    )

    service = HistoryService(history_db)
    assert service.mark_history_read([read_id, 999999]) == 1
    assert service.dismiss_history_records(only_read=True) == 1
    assert service.dismiss_history_records(older_than=now - timedelta(days=7)) == 1
    assert service.dismiss_history_records(record_ids=[morning]) == 1

    listed = service.get_history_list(limit=20, dedupe_market_review=True)
    visible = {item["id"] for item in listed["items"]}
    assert fresh in visible
    assert closing in visible
    assert aged not in visible
    assert read_id not in visible
    assert morning not in visible
    assert service.resolve_and_get_detail(str(read_id)) is None

    again = service.get_history_list(limit=20, dedupe_market_review=False)
    summaries = [item["analysis_summary"] for item in again["items"]]
    assert "2026-10-10 大盘复盘 收盘" in summaries
    assert service.dismiss_history_records() == 0


def test_deduped_list_hides_repeated_same_day_review(history_db: DatabaseManager) -> None:
    now = datetime.now()
    _insert(
        history_db,
        query_id="am",
        code="MARKET",
        name="大盘复盘",
        report_type="market_review",
        analysis_summary="2026-10-10 大盘复盘 早盘",
        created_at=now - timedelta(hours=2),
    )
    closing = _insert(
        history_db,
        query_id="pm",
        code="MARKET",
        name="大盘复盘",
        report_type="market_review",
        analysis_summary="2026-10-10 大盘复盘 收盘",
        created_at=now - timedelta(hours=1),
    )

    listed = HistoryService(history_db).get_history_list(limit=20, dedupe_market_review=True)

    assert listed["total"] == 1
    assert listed["items"][0]["id"] == closing
    assert listed["items"][0]["read_at"] is None
