"""盘后复盘板（星球主题）与只读间隔观测的单元测试。

两块都不碰线上写路径：

- ``theme_table`` / ``theme_board``：只从 ``news.sqlite`` 读 ``source='zsxq'`` 的帖子。
  Markdown 主题表保持原样解析；散文「复盘笔记」按标题和「强势板块」段落补主题。
  **强弱原样透传**，没有则留空，不做买卖判断。
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
    assert {r.source_kind for r in rows} == {"table"}
    assert {r.summary for r in rows} == {""}


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


# 脱敏样例，形态对齐星球「复盘笔记」：标题用 / 列主题，正文三段，
# 强势板块段里主题单独成行，下面跟 ①②。不使用线上原文。
_PROSE_TITLE = "复盘笔记：人形机器人/低空经济/创新药/商业航天"
_PROSE_BODY = """一、重磅财经信息
降准
① 海外通胀读数回落，风险偏好抬升
② 国内例会继续强调设备更新

二、大盘表现
沪指
指数放量上行，成交额回到万亿上方。

三、强势板块和个股分析

人形机器人
① 海外原型机进展带动减速器与丝杠，情绪升温
② 部分标的连板后分歧加大

低空经济
① 低空经济
② 地方航线补贴落地，零部件订单能见度改善

创新药
① 海外临床数据超预期，跟随走强

这是一段单独成行但是明显超出主题名长度的说明文字
"""


def test_prose_title_lists_themes_by_slash() -> None:
    rows = theme_table.theme_rows_from_text(
        "",
        title=_PROSE_TITLE,
        published_at="2026-10-16T15:40:00+08:00",
    )
    assert [r.theme for r in rows] == ["人形机器人", "低空经济", "创新药", "商业航天"]
    assert {r.strength for r in rows} == {""}
    assert {r.source_kind for r in rows} == {"prose"}
    assert {r.display_day for r in rows} == {"2026-10-16"}
    assert {r.summary for r in rows} == {""}


def test_prose_title_accepts_enumeration_comma_and_fullwidth_slash() -> None:
    rows = theme_table.theme_rows_from_text(
        "",
        title="【复盘笔记】：固态电池、铜缆／CPO",
        published_at="2026-10-16T15:40:00+08:00",
    )
    assert [r.theme for r in rows] == ["固态电池", "铜缆", "CPO"]


def test_prose_title_does_not_split_sentences_on_comma() -> None:
    """中文逗号是句子，不是主题分隔。整句超长或带句读时不抽。"""
    rows = theme_table.theme_rows_from_text(
        "",
        title="复盘笔记：今天市场整体回暖，科技仍是主线",
    )
    assert rows == []
    rows = theme_table.theme_rows_from_text("", title="复盘笔记：人形机器人，低空经济")
    assert rows == []


def test_prose_section_extracts_standalone_theme_lines() -> None:
    rows = theme_table.theme_rows_from_text(
        _PROSE_BODY,
        title=_PROSE_TITLE,
        published_at="2026-10-16T15:40:00+08:00",
    )
    assert [r.theme for r in rows] == ["人形机器人", "低空经济", "创新药", "商业航天"]
    by_name = {r.theme: r for r in rows}
    assert by_name["人形机器人"].summary.startswith("海外原型机进展带动减速器与丝杠")
    assert "连板后分歧加大" in by_name["人形机器人"].summary
    assert by_name["低空经济"].summary.startswith("低空经济；地方航线补贴落地")
    assert by_name["创新药"].strength == ""
    assert by_name["创新药"].source_kind == "prose"
    assert by_name["商业航天"].summary == ""
    assert by_name["人形机器人"].display_day == "2026-10-16"


def test_prose_skips_section_headers_points_and_other_sections() -> None:
    rows = theme_table.theme_rows_from_text(_PROSE_BODY, title=_PROSE_TITLE)
    themes = {r.theme for r in rows}
    blocked = {
        "一、重磅财经信息",
        "重磅财经信息",
        "二、大盘表现",
        "大盘表现",
        "三、强势板块和个股分析",
        "强势板块和个股分析",
        "强势板块",
        "降准",
        "沪指",
        "海外通胀读数回落，风险偏好抬升",
        "低空经济；地方航线补贴落地，零部件订单能见度改善",
    }
    assert themes.isdisjoint(blocked)
    # ① 行即使去掉圈号后很短，也仍然是要点，不是新主题。
    assert [r.theme for r in rows].count("低空经济") == 1


def test_point_summary_keeps_only_a_prefix() -> None:
    point = "甲" * 100
    text = f"三、强势板块和个股分析\n\n人形机器人\n① {point}\n"
    rows = theme_table.theme_rows_from_text(text, published_at="2026-10-16T15:00:00+08:00")
    assert [row.theme for row in rows] == ["人形机器人"]
    assert rows[0].summary.endswith("…")
    assert len(rows[0].summary) == 80
    assert rows[0].strength == ""


def test_mentioning_strong_sectors_without_a_section_extracts_nothing() -> None:
    text = "今天强势板块仍是科技，但没有单独成段。"
    assert theme_table.theme_rows_from_text(text, title="随便聊聊") == []


def test_image_review_posts_are_not_parsed() -> None:
    assert theme_table.theme_rows_from_text("", title="涨停板复盘") == []
    assert theme_table.theme_rows_from_text("[图片消息]", title="雁阵图") == []


def test_markdown_table_still_wins_strength_when_prose_overlaps() -> None:
    text = (
        "| 主题 | 强弱 |\n| --- | --- |\n| 人形机器人 | 强 |\n\n"
        + _PROSE_BODY
    )
    rows = theme_table.theme_rows_from_text(
        text,
        title="复盘笔记：人形机器人",
        published_at="2026-10-16T15:40:00+08:00",
    )
    robot = next(row for row in rows if row.theme == "人形机器人")
    assert robot.strength == "强"
    assert robot.source_kind == "mixed"
    assert "原型机" in robot.summary
    assert "低空经济" in {row.theme for row in rows}
    assert next(row for row in rows if row.theme == "低空经济").source_kind == "prose"


def test_cleaning_keeps_markdown_table_and_prose_lines() -> None:
    """清洗会去掉标签和图片占位，但不会拆掉表格，也不会把单独成行的主题粘成一段。"""
    from app.news.cleaning import clean_text

    raw = (
        "一、重磅财经信息\n\n"
        "| 主题 | 强弱 |\n"
        "| --- | --- |\n"
        "| 算力租赁 | 强 |\n\n\n"
        "三、强势板块和个股分析\n\n"
        "人形机器人\n"
        "① 原型机进展\n"
        "[图片消息](mediaId=abc)\n"
    )
    cleaned = clean_text(raw)
    assert "| 主题 | 强弱 |" in cleaned
    assert "人形机器人" in cleaned.splitlines()
    rows = theme_table.theme_rows_from_text(
        cleaned,
        title=clean_text("复盘笔记：人形机器人/算力租赁"),
        published_at="2026-10-16T15:00:00+08:00",
    )
    by_name = {row.theme: row for row in rows}
    assert by_name["算力租赁"].strength == "强"
    assert by_name["算力租赁"].source_kind == "mixed"
    assert by_name["人形机器人"].summary == "原型机进展"


def test_odd_prose_never_raises() -> None:
    samples = [
        ("", ""),
        ("三、强势板块和个股分析\n\n① 只有要点没有主题\n", "复盘笔记："),
        ("三、强势板块\n\n一\n好\n", "复盘笔记：/、"),
        ("三、强势板块和个股分析\n\n" + ("甲" * 40) + "\n① 太长的行不是主题\n", ""),
    ]
    for text, title in samples:
        theme_table.theme_rows_from_text(text, title=title)


# ----------------------------------------------------------------------
# 复盘板 service（真读 sqlite）
# ----------------------------------------------------------------------

def _seed_news_db(root: Path, posts: list[tuple]) -> Path:
    """建一个最小 news.sqlite。

    posts 每项至少 (source_id, published_at ISO, clean_text)，
    其后可选 title、group_name、url。
    """
    news_dir = root / "news"
    news_dir.mkdir(parents=True, exist_ok=True)
    path = news_dir / "news.sqlite"
    from app.news.store import NewsStore

    store = NewsStore(path)
    try:
        for post in posts:
            source_id, published, text = post[0], post[1], post[2]
            title = post[3] if len(post) > 3 and post[3] is not None else f"复盘 {source_id}"
            group = post[4] if len(post) > 4 and post[4] else "每日投研"
            url = post[5] if len(post) > 5 else ""
            store.insert_item(
                source="zsxq",
                source_id=source_id,
                published_at=datetime.fromisoformat(published),
                author="星球",
                title=title,
                clean_text=text,
                raw={"group_name": group},
                content_hash=f"hash-{source_id}",
                url=url,
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
    assert payload["posts_parsed"] == 2
    themes = {row["theme"] for row in payload["rows"]}
    assert themes == {"算力租赁", "固态电池"}
    assert {row["source_kind"] for row in payload["rows"]} == {"table"}
    # 标注为只读复盘视图，避免被当成信号源
    assert "非买卖信号" in payload["note"]


def test_theme_board_parses_prose_and_dedupes_same_day(tmp_path) -> None:
    from app.news import service

    _seed_news_db(tmp_path, [
        (
            "p1",
            "2026-10-16T15:40:00+08:00",
            _PROSE_BODY,
            _PROSE_TITLE,
            "每日投研",
            "https://example.test/p1",
        ),
        (
            "p2",
            "2026-10-16T18:00:00+08:00",
            "三、强势板块和个股分析\n\n人形机器人\n① 另一星球补充了丝杠订单\n",
            "复盘笔记：人形机器人",
            "纳指星球调研",
            "https://example.test/p2",
        ),
        (
            "p3",
            "2026-10-16T12:00:00+08:00",
            "",
            "涨停板复盘",
            "每日投研",
            "",
        ),
        (
            "p4",
            "2026-10-16T09:00:00+08:00",
            "",
            "雁阵图",
            "纳指星球调研",
            "",
        ),
        (
            "p5",
            "2026-10-15T15:30:00+08:00",
            "| 主题 | 强弱 |\n| --- | --- |\n| 人形机器人 | 强 |\n",
            "复盘 p5",
            "每日投研",
            "https://example.test/p5",
        ),
    ])
    store = service.reset_store_for_tests(tmp_path / "news" / "news.sqlite")
    try:
        payload = service.theme_board(days=30)
    finally:
        store.close()
        service.reset_store_for_tests(tmp_path / "news" / "news.sqlite")

    assert payload["posts"] == 5
    assert payload["posts_parsed"] == 3
    assert payload["posts_with_table"] == 1
    assert "非买卖信号" in payload["note"]

    by_key = {(row["day"], row["theme"]): row for row in payload["rows"]}
    assert set(by_key) == {
        ("2026-10-16", "人形机器人"),
        ("2026-10-16", "低空经济"),
        ("2026-10-16", "创新药"),
        ("2026-10-16", "商业航天"),
        ("2026-10-15", "人形机器人"),
    }
    same_day = by_key[("2026-10-16", "人形机器人")]
    assert same_day["source"] == "知识星球·纳指星球调研、知识星球·每日投研"
    assert same_day["url"] == ""
    assert "丝杠订单" in same_day["summary"]
    assert "原型机" in same_day["summary"]
    assert same_day["strength"] == ""
    assert same_day["source_kind"] == "prose"
    earlier = by_key[("2026-10-15", "人形机器人")]
    assert earlier["strength"] == "强"
    assert earlier["source_kind"] == "table"
    assert earlier["url"] == "https://example.test/p5"
    assert "降准" not in {row["theme"] for row in payload["rows"]}
    assert "涨停板复盘" not in {row["theme"] for row in payload["rows"]}


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
