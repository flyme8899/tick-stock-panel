"""盘后复盘板（星球主题表）与只读间隔观测的单元测试。

两块都不碰线上写路径：

- ``theme_table`` / ``theme_board``：只从 ``news.sqlite`` 读 ``source='zsxq'`` 的帖子，
  解析正文里的 Markdown 表格。**强弱原样透传**，不做买卖判断。
- ``intraday_gap``：只读 parquet 与 ``status.json``。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from app.fund_flow import intraday_gap
from app.news import theme_table

CN = timezone(timedelta(hours=8))


# ----------------------------------------------------------------------
# 主题表解析
# ----------------------------------------------------------------------

def test_parses_standard_theme_table() -> None:
    text = (
        "盘后复盘：\n\n"
        "| 主题 | 强弱 | 日期 |\n"
        "| --- | --- | --- |\n"
        "| 算力租赁 | 强 | 2026-10-14 |\n"
        "| 固态电池 | 弱转强 | 2026-10-15 |\n"
    )
    rows = theme_table.theme_rows_from_text(text, published_at="2026-10-15T15:30:00+08:00")
    assert [r.theme for r in rows] == ["算力租赁", "固态电池"]
    assert [r.strength for r in rows] == ["强", "弱转强"]
    assert [r.day for r in rows] == ["2026-10-14", "2026-10-15"]


def test_strength_is_passed_through_verbatim() -> None:
    """强弱是作者手写的，原样保留 —— 不归一化、不映射、不排序。"""
    text = "| 主题 | 强弱 |\n| --- | --- |\n| A | 弱转强 |\n| B | 分歧→一致 |\n"
    rows = theme_table.theme_rows_from_text(text)
    assert [r.strength for r in rows] == ["弱转强", "分歧→一致"]


@pytest.mark.parametrize(
    "text,expect",
    [
        ("今天大盘放量上涨，主线仍是科技。", 0),
        ("", 0),
        ("| 主题 | 强弱 |\n| --- | --- |\n", 0),
        ("| 股票 | 涨幅 |\n| --- | --- |\n| 贵州茅台 | 3.2% |\n", 0),
        ("| 主题 | 强弱 |\n| --- | --- |\n|  | 强 |\n| 存储 | 强 |\n", 1),
    ],
)
def test_odd_input_never_raises(text: str, expect: int) -> None:
    """解析不了就少抽，不能抛 —— 一条怪表格不该让复盘板打不开。"""
    assert len(theme_table.theme_rows_from_text(text)) == expect


def test_date_normalized_and_falls_back_to_post_day() -> None:
    table = "| 板块 | 日期 |\n| --- | --- |\n| 光伏 | 2026/10/12 |\n"
    rows = theme_table.theme_rows_from_text(table, published_at="2026-10-15T09:00:00+08:00")
    assert rows[0].day == "2026-10-12"

    no_date = "| 主题 | 强弱 |\n| --- | --- |\n| 铜缆 | 强 |\n"
    rows = theme_table.theme_rows_from_text(no_date, published_at="2026-10-15T09:00:00+08:00")
    assert rows[0].day == ""
    assert rows[0].display_day == "2026-10-15"


def test_synonym_headers_and_escaped_pipe() -> None:
    table = "| 核心题材 | 强度 | 交易日 |\n|:---|---:|:---:|\n| A \\| B | 强 | 2026.10.13 |\n"
    rows = theme_table.theme_rows_from_text(table)
    assert rows[0].theme == "A | B"
    assert rows[0].strength == "强"
    assert rows[0].day == "2026-10-13"


def test_two_tables_in_one_post() -> None:
    text = (
        "| 主题 | 强弱 |\n| --- | --- |\n| A主题 | 强 |\n\n"
        "中间说明文字\n\n"
        "| 主题 | 强弱 |\n| --- | --- |\n| B主题 | 弱 |\n"
    )
    rows = theme_table.theme_rows_from_text(text)
    assert [r.theme for r in rows] == ["A主题", "B主题"]


# ----------------------------------------------------------------------
# 复盘板 service（真读 sqlite）
# ----------------------------------------------------------------------

def _seed_news_db(root: Path, posts: list[tuple[str, str, str]]) -> Path:
    """建一个最小 news.sqlite。posts: (source_id, published_at ISO, clean_text)。"""
    news_dir = root / "news"
    news_dir.mkdir(parents=True, exist_ok=True)
    path = news_dir / "news.sqlite"
    from app.news.store import NewsStore

    store = NewsStore(path)
    try:
        for source_id, published, text in posts:
            store.insert_item(
                source="zsxq",
                source_id=source_id,
                published_at=datetime.fromisoformat(published),
                author="星球",
                title=f"复盘 {source_id}",
                clean_text=text,
                raw={"group_name": "每日投研"},
                content_hash=f"hash-{source_id}",
                url="",
                level="",
                media_ids=[],
                extra={},
                mentions=[],
            )
    finally:
        store.close()
    return path


def test_theme_board_reads_zsxq_tables(tmp_path, monkeypatch) -> None:
    from app.news import service

    _seed_news_db(tmp_path, [
        ("p1", "2026-10-15T15:30:00+08:00",
         "| 主题 | 强弱 |\n| --- | --- |\n| 算力租赁 | 强 |\n"),
        ("p2", "2026-10-14T15:30:00+08:00",
         "| 主题 | 强弱 |\n| --- | --- |\n| 固态电池 | 弱 |\n"),
        ("p3", "2026-10-15T16:00:00+08:00", "今天没有表格，只有一段话。"),
    ])

    store = service.reset_store_for_tests(tmp_path / "news" / "news.sqlite")
    try:
        payload = service.theme_board(days=30)
    finally:
        store.close()
        service.reset_store_for_tests(tmp_path / "news" / "news.sqlite")

    assert payload["posts"] == 3
    assert payload["posts_with_table"] == 2
    themes = {row["theme"] for row in payload["rows"]}
    assert themes == {"算力租赁", "固态电池"}
    # 标注为只读复盘视图，避免被当成信号源
    assert "非买卖信号" in payload["note"]


def test_theme_board_empty_db_is_safe(tmp_path) -> None:
    from app.news import service

    _seed_news_db(tmp_path, [])
    store = service.reset_store_for_tests(tmp_path / "news" / "news.sqlite")
    try:
        payload = service.theme_board(days=7)
    finally:
        store.close()
        service.reset_store_for_tests(tmp_path / "news" / "news.sqlite")
    assert payload["rows"] == []


# ----------------------------------------------------------------------
# 间隔观测（只读）
# ----------------------------------------------------------------------

def _write_day(root: Path, day: str, gaps_min: list[int]) -> None:
    base = datetime.fromisoformat(f"{day}T09:35:00").replace(tzinfo=CN)
    stamps, cursor = [], base
    for gap in gaps_min:
        stamps.append(cursor.strftime("%Y-%m-%dT%H:%M:%S"))
        cursor += timedelta(minutes=gap)
    frame = pl.DataFrame({
        "symbol": ["600519.SH"] * len(stamps),
        "code": ["600519"] * len(stamps),
        "captured_at": stamps,
        "main_net": [1.0] * len(stamps),
    })
    out = root / f"date={day}"
    out.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(out / "part.parquet")


def test_intraday_gap_flags_dense_and_sparse(tmp_path) -> None:
    root = tmp_path / "fund_flow" / "stock_intraday"
    _write_day(root, "2026-10-13", [6] * 5)
    _write_day(root, "2026-10-14", [1] * 8)
    _write_day(root, "2026-10-15", [30] * 3)

    report = intraday_gap.analyze(root, days=5, data_dir=tmp_path)
    assert report.verdict == "suspect"
    assert report.exit_code == 1
    assert report.short_count == 7
    assert report.long_count == 2
    assert report.writing is True


def test_intraday_gap_clean_day_is_ok(tmp_path) -> None:
    root = tmp_path / "fund_flow" / "stock_intraday"
    _write_day(root, "2026-10-13", [6] * 5)
    report = intraday_gap.analyze(root, days=5, data_dir=tmp_path)
    assert report.verdict == "ok"
    assert report.exit_code == 0


def test_intraday_gap_missing_dir_is_nodata(tmp_path) -> None:
    report = intraday_gap.analyze(tmp_path / "nope", days=5)
    assert report.verdict == "nodata"
    assert report.exit_code == 2
    assert report.writing is False


def test_intraday_gap_reads_status_and_prefers_last_snapshot(tmp_path) -> None:
    root = tmp_path / "fund_flow" / "stock_intraday"
    _write_day(root, "2026-10-13", [6] * 3)
    state_dir = tmp_path / "fund_flow" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "status.json").write_text(
        '{"kinds": {"stock_intraday": {'
        '"last_snapshot": "2026-10-15T13:00:00", '
        '"last_success_at": "2026-10-15T13:00:05", '
        '"last_attempt_at": "2026-10-15T13:02:00"}}}',
        encoding="utf-8",
    )
    report = intraday_gap.analyze(root, days=5, data_dir=tmp_path)
    assert report.state["last_snapshot"] == "2026-10-15T13:00:00"
    # _intraday_due 读 last_snapshot，优先于 last_success_at
    assert report.due_stamp == datetime(2026, 10, 15, 13, 0, tzinfo=CN)


def test_intraday_gap_corrupt_partition_is_reported_not_raised(tmp_path) -> None:
    root = tmp_path / "fund_flow" / "stock_intraday"
    bad = root / "date=2026-10-13"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "part.parquet").write_bytes(b"not a parquet file")
    report = intraday_gap.analyze(root, days=5, data_dir=tmp_path)
    assert report.verdict == "nodata"
    assert report.problems and "无法读取" in report.problems[0]


def test_intraday_gap_does_not_touch_files(tmp_path) -> None:
    """只读保证：跑完前后文件集合与内容都不变。"""
    root = tmp_path / "fund_flow" / "stock_intraday"
    _write_day(root, "2026-10-13", [6] * 4)
    before = {
        p: p.read_bytes()
        for p in sorted(tmp_path.rglob("*"))
        if p.is_file()
    }
    intraday_gap.analyze(root, days=5, data_dir=tmp_path)
    after = {
        p: p.read_bytes()
        for p in sorted(tmp_path.rglob("*"))
        if p.is_file()
    }
    assert before == after


def test_intraday_gap_module_does_not_import_scheduler() -> None:
    """只读入口不应把调度栈（含写盘路径）拉进来。"""
    src = Path(intraday_gap.__file__).read_text(encoding="utf-8")
    assert "from app.fund_flow import scheduler" not in src
    assert "from app.fund_flow import jobs" not in src
    assert "from app.fund_flow import store" not in src
    assert "app.fund_flow.jobs" not in src
    assert "app.fund_flow.store" not in src
