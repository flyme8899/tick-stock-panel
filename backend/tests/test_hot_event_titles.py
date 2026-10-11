"""热门事件标题要是完整句子，不能是截断的半句话。"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime, timedelta

from app.market_time import CN_TZ
from app.news.hot_events import (
    _cleaned_headline,
    _concept_relevant,
    _dictionary_concepts,
    _ensure_shown_name,
    _fallback_title_ok,
    _fine_concept,
    _is_section_label,
    _keep_shown_event,
    _keyword_title,
    _llm_title_ok,
    _llm_title_reject_reason,
    _relevant_concepts,
    _strip_report_label,
    _title_ok,
)
from app.news.service import get_store, reset_store_for_tests, top_hot_events

NOW = datetime(2026, 10, 10, 8, 30, tzinfo=CN_TZ)

_BAD_TITLES = (
    "橡胶价格大涨超发布公告",
    "叠加昨夜美国针对制裁",
    "和之前结构性降息",
    "根据宏观经济运行出台务实管用的增量政",
)
_GOOD_TITLES = (
    "美联储降息25基点",
    "工信部出台机器人补贴",
    "存储芯片厂宣布涨价",
)


def _publish(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ)


def _insert(source_id: str, title: str, minute: int, source: str = "cls") -> None:
    status = get_store().insert_item(
        source=source,
        source_id=source_id,
        published_at=_publish(date(2026, 10, 9), 10, minute),
        author="",
        title=title,
        clean_text=title,
        raw=None,
        content_hash=source_id,
        url="",
        level="",
        media_ids=[],
        extra=None,
        mentions=[],
    )
    assert status == "inserted"


def test_quality_filter_rejects_fragments_and_keeps_complete_headlines():
    for title in _BAD_TITLES:
        assert not _title_ok(title)
    for title in _GOOD_TITLES:
        assert _title_ok(title)
        assert len(title) <= 20


def test_keyword_title_uses_clause_instead_of_gluing_snippets():
    glued = _keyword_title([
        {
            "title": "橡胶价格大涨超预期",
            "lead": "橡胶价格大涨超",
            "action": "发布",
            "object": "公告",
        },
        {
            "title": "上期所发布库存公告",
            "lead": "上期所",
            "action": "发布",
            "object": "库存公告",
        },
    ])
    assert glued == "橡胶价格大涨超预期"
    assert glued != "橡胶价格大涨超发布公告"

    assert _keyword_title([
        {"title": "【财联社】橡胶价格大涨超预期发布公告"},
    ]) == "橡胶价格大涨超预期"
    assert _keyword_title([
        {"title": "油价走高，叠加昨夜美国针对制裁伊朗"},
    ]) == "油价走高"
    assert _keyword_title([
        {"title": "叠加昨夜美国针对制裁"},
    ]) == "美国制裁"
    assert _keyword_title([
        {"title": "美联储降息25基点，和之前结构性降息不同"},
    ]) == "美联储降息25基点"
    bare = _keyword_title([
        {"title": "和之前结构性降息"},
    ])
    assert bare == "结构性降息"
    assert bare != "事件"
    assert _keyword_title([{"title": "【财联社】事件"}]) == ""
    assert _keyword_title([{"title": "事件"}]) == ""
    policy = _keyword_title([
        {"title": "财联社：根据宏观经济运行情况出台务实管用的增量政策"},
    ])
    assert policy == "宏观经济运行情况出台务实管用的增量政策"
    assert not policy.startswith("根据")
    assert not policy.endswith("政")
    clipped = _keyword_title([
        {"title": "根据宏观经济运行出台务实管用的增量政"},
    ])
    assert clipped == "宏观经济运行出台"
    assert clipped != "根据宏观经济运行出台务实管用的增量政"
    assert _keyword_title([
        {"title": "华尔街见闻：工信部出台机器人补贴"},
    ]) == "工信部出台机器人补贴"
    assert _keyword_title([
        {"title": "【财联社】存储芯片厂宣布涨价"},
    ]) == "存储芯片厂宣布涨价"
    assert _keyword_title([
        {"title": "财联社10月9日电，美联储宣布降息25基点 - Reuters"},
    ]) == "美联储宣布降息25基点"


def test_hot_event_names_are_readable_headlines(tmp_path):
    reset_store_for_tests(tmp_path / "titles.sqlite")
    samples = [
        "【财联社】橡胶价格大涨超预期发布公告",
        "美联储降息25基点，和之前结构性降息不同",
        "财联社：根据宏观经济运行情况出台务实管用的增量政策",
        "华尔街见闻：存储芯片厂宣布涨价",
        "油价走高，叠加昨夜美国针对制裁伊朗",
        "工信部出台机器人补贴",
    ]
    for index, title in enumerate(samples):
        _insert(f"t-{index}", title, index)
    _insert("rubber-2", samples[0], 6, source="wscn")
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert "橡胶价格大涨超预期" in names
    assert "美联储降息25基点" in names
    assert "宏观经济运行情况出台务实管用的增量政策" in names
    assert "存储芯片厂宣布涨价" in names
    assert "油价走高" in names
    assert "工信部出台机器人补贴" in names
    for bad in _BAD_TITLES:
        assert bad not in names
    for name in names:
        assert _title_ok(name)


def test_llm_prefers_good_title_and_rejects_fragment(tmp_path, monkeypatch):
    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "llm-title.sqlite")
    _insert("fed", "【财联社】美联储宣布降息25基点", 0)
    prompts: list[str] = []

    def good(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return '{"title":"美联储降息25基点","category":"海外市场/央行","direction":"利好"}'

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", good)
    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    named = top_hot_events(NOW)
    assert named["events"][0]["name"] == "美联储降息25基点"
    assert len(prompts) == 1
    assert "不要半句话" in prompts[0]

    def bad(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return (
            '{"title":"叠加昨夜美国针对制裁","category":"海外市场/央行","direction":"利好",'
            '"importance":"重大"}'
        )

    monkeypatch.setattr(news_service, "_llm_text", bad)
    hot_mod.clear_hot_event_cache()
    rejected = top_hot_events(NOW + timedelta(minutes=11))
    assert rejected["events"][0]["name"] == "美联储宣布降息25基点"
    assert rejected["events"][0]["category"] == "海外市场/央行"
    assert rejected["events"][0]["importance"] == "重大"
    assert all(
        "叠加昨夜" not in str(item.get("title") or "")
        for item in hot_mod._LLM_CACHE.values()
    )

    hot_mod.clear_hot_event_cache(llm=False)
    again = top_hot_events(NOW + timedelta(minutes=22))
    assert again["events"][0]["name"] == "美联储宣布降息25基点"
    assert len(prompts) == 3

    def clipped(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return '{"title":"根据宏观经济运行情况出台务实管用的增量政策以及配套改革措施"}'

    monkeypatch.setattr(news_service, "_llm_text", clipped)
    hot_mod.clear_hot_event_cache()
    unsliced = top_hot_events(NOW + timedelta(minutes=33))
    assert unsliced["events"][0]["name"] == "美联储宣布降息25基点"
    assert not unsliced["events"][0]["name"].endswith("政")


def test_old_title_cache_is_regenerated(tmp_path, monkeypatch):
    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "llm-cache.sqlite")
    title = "【财联社】美联储宣布降息25基点"
    _insert("fed", title, 1)
    fingerprint = hashlib.sha1(title.encode("utf-8")).hexdigest()
    hot_mod.clear_hot_event_cache()
    hot_mod._LLM_CACHE[fingerprint] = {"title": "叠加昨夜美国针对制裁"}
    hot_mod._LLM_CACHE[f"title-v{hot_mod._TITLE_CACHE_VERSION}:{fingerprint}"] = {
        "title": "和之前结构性降息",
    }
    prompts: list[str] = []

    def good(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return '{"title":"美联储降息25基点"}'

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", good)
    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: True)
    named = top_hot_events(NOW)
    assert named["events"][0]["name"] == "美联储降息25基点"
    assert len(prompts) == 1
    assert fingerprint not in hot_mod._LLM_CACHE


def _insert_rich(
    source_id: str,
    title: str,
    hour: int,
    minute: int,
    *,
    source: str = "cls",
    sectors: list[str] | None = None,
    stocks: list[tuple[str, str]] | None = None,
    summary: str = "",
    level: str = "",
) -> None:
    mentions = [("sector", name, name, "", "structured") for name in (sectors or [])]
    mentions += [
        ("stock", symbol, name, symbol[:6], "structured")
        for symbol, name in (stocks or [])
    ]
    status = get_store().insert_item(
        source=source,
        source_id=source_id,
        published_at=_publish(date(2026, 10, 9), hour, minute),
        author="",
        title=title,
        clean_text=summary or title,
        raw=None,
        content_hash=source_id,
        url="",
        level=level,
        media_ids=[],
        extra=None,
        mentions=mentions,
    )
    assert status == "inserted"


def test_keyword_title_fills_missing_subject_from_the_headline():
    rate = _keyword_title([{
        "title": "美联储主席表示，可能还会有几次加息",
    }])
    assert "美联储" in rate
    assert "加息" in rate
    assert rate != "可能还会有几次加息"
    policy = _keyword_title([{
        "title": "中共中央政治局会议：根据宏观经济运行情况及时谋划出台增量政策",
    }])
    assert "政治局" in policy
    assert "出台" in policy
    assert policy != "宏观经济运行情况及时谋划出台"
    # 标题里没有可补的机构时，保持原来的完整分句。
    assert _keyword_title([{
        "title": "财联社：根据宏观经济运行情况出台务实管用的增量政策",
    }]) == "宏观经济运行情况出台务实管用的增量政策"


def test_dropdown_examples_keep_subject_drop_bare_event_and_unrelated_maps(tmp_path):
    """生产下拉里的四类问题：裸「事件」、概念串味、同主体加息重复、缺主体标题。"""
    reset_store_for_tests(tmp_path / "dropdown.sqlite")
    _insert_rich(
        "rubber",
        "橡胶价格大涨",
        7,
        25,
        sectors=["橡胶", "苹果", "黄金", "固态电池", "智能家居"],
        stocks=[("601118.SH", "海南橡胶")],
    )
    _insert_rich(
        "rubber-2",
        "橡胶价格大涨超预期",
        7,
        40,
        source="wscn",
        sectors=["橡胶", "黄金"],
        stocks=[("601118.SH", "海南橡胶")],
    )
    _insert_rich(
        "battery",
        "宁德时代发布固态电池",
        7,
        28,
        sectors=["固态电池"],
        stocks=[("300750.SZ", "宁德时代")],
    )
    _insert_rich(
        "apple",
        "苹果发布智能家居新品",
        8,
        5,
        sectors=["苹果", "智能家居"],
        stocks=[("AAPL", "苹果")],
    )
    _insert_rich(
        "gold",
        "黄金价格上涨",
        8,
        12,
        sectors=["黄金"],
    )
    _insert_rich(
        "hike-vague",
        "美联储主席表示，可能还会有几次加息",
        7,
        56,
        summary="美联储主席表示，可能还会有几次加息",
    )
    _insert_rich(
        "hike-clear",
        "美联储将从容推进加息",
        7,
        56,
        source="wscn",
    )
    _insert_rich(
        "policy",
        "中共中央政治局会议：根据宏观经济运行情况及时谋划出台增量政策",
        16,
        27,
    )
    _insert_rich(
        "fragment",
        "和之前结构性降息",
        9,
        0,
    )
    _insert_rich(
        "empty-event",
        "【财联社】事件",
        9,
        5,
    )

    events = top_hot_events(NOW)["events"]
    names = [event["name"] for event in events]
    assert "事件" not in names
    assert all(name != "事件" for name in names)
    assert "可能还会有几次加息" not in names
    assert "宏观经济运行情况及时谋划出台" not in names

    rubber = next(event for event in events if "橡胶" in event["name"])
    assert rubber["name"] in {"橡胶价格大涨", "橡胶价格大涨超预期"}
    assert rubber["concepts"] == ["橡胶", "天然橡胶"]
    assert "苹果" not in rubber["concepts"]
    assert "黄金" not in rubber["concepts"]
    assert "固态电池" not in rubber["concepts"]
    assert "智能家居" not in rubber["concepts"]
    rubber_stocks = [stock["name"] for stock in rubber["mentioned_stocks"]]
    assert rubber_stocks == ["海南橡胶"]
    assert "宁德时代" not in rubber_stocks
    assert rubber["category"] == "大宗商品/期货价格异动"
    assert rubber["mentions"] == 2
    assert rubber["first_seen"] == "10-09 07:25"

    battery = next(event for event in events if event["name"] == "宁德时代发布固态电池")
    assert battery["concepts"] == ["固态电池"]
    assert [stock["name"] for stock in battery["mentioned_stocks"]] == ["宁德时代"]
    apple = next(event for event in events if "智能家居" in event["name"])
    assert "橡胶" not in apple["concepts"]
    assert apple["key"] != rubber["key"]

    hikes = [event for event in events if "加息" in event["name"] and "美联储" in event["name"]]
    assert len(hikes) == 1
    hike = hikes[0]
    assert hike["mentions"] == 2
    assert hike["first_seen"] == "10-09 07:56"
    assert hike["category"] == "海外市场/央行"
    assert hike["name"] != "可能还会有几次加息"

    policy = next(event for event in events if "出台" in event["name"] and "政治局" in event["name"])
    assert policy["name"] != "宏观经济运行情况及时谋划出台"
    assert "政治局" in policy["name"]
    assert policy["first_seen"] == "10-09 16:27"

    assert "结构性降息" in names
    assert rubber["key"].startswith("ev_")
    assert len(rubber["key"]) == 15

    again = top_hot_events(NOW + timedelta(minutes=11))
    again_rubber = next(event for event in again["events"] if "橡胶" in event["name"])
    assert again_rubber["key"] == rubber["key"]


_TRUNCATED_TITLES = (
    "财政的小增量政策也出",
    "新增隐债】财政部发布20",
    "苹果公司宣布将于10月13日在纽约发布",
    "IEA下调原油供需预期...",
)


def test_keyword_titles_keep_whole_clauses_objects_and_numbers():
    """生产标题被切在半句、半个动词、数字和宾语前面。"""
    fiscal = _keyword_title([{
        "title": "财政部有关负责人表示财政的小增量政策也出台了一批配套措施",
    }])
    assert fiscal == "财政部有关负责人表示财政的小增量政策也出台了一批配套措施"
    assert fiscal != "财政的小增量政策也出"
    assert "出台" in fiscal

    debt = _keyword_title([{
        "title": "新增隐债】财政部发布2025年一揽子化债方案",
    }])
    assert debt == "财政部发布2025年一揽子化债方案"
    assert "】" not in debt
    assert "【" not in debt
    assert debt != "新增隐债】财政部发布20"
    assert "2025" in debt

    apple = _keyword_title([{
        "title": "苹果公司宣布将于10月13日在纽约发布全新iPhone",
    }])
    assert apple == "苹果公司宣布将于10月13日在纽约发布全新iPhone"
    assert apple != "苹果公司宣布将于10月13日在纽约发布"

    oil = _keyword_title([{
        "title": "IEA下调原油供需预期，预计明年全球石油供应过剩",
    }])
    assert oil == "IEA下调原油供需预期"
    assert oil != "IEA下调原油供需预期..."
    labeled = _keyword_title([{
        "title": "IEA：下调原油供需预期，预计明年全球石油供应过剩",
    }])
    assert labeled == "IEA下调原油供需预期"

    kept = _keyword_title([{
        "title": "财政部宏观经济运行情况及时谋划出台增量政策",
    }])
    assert kept == "财政部宏观经济运行情况及时谋划出台增量政策"
    assert kept != "财政部宏观经济运行情况及时谋划出台"


def test_finance_ministry_items_merge_and_subjects_map_to_concepts(tmp_path):
    """三条财政部政策是一件事；橡胶、原油、加息要有概念，无关概念仍排除。"""
    reset_store_for_tests(tmp_path / "prod-1009.sqlite")
    _insert_rich(
        "fiscal-cut",
        "财政部有关负责人表示财政的小增量政策也出台了一批配套措施",
        9,
        10,
    )
    _insert_rich(
        "fiscal-debt",
        "新增隐债】财政部发布2025年一揽子化债方案",
        9,
        30,
    )
    _insert_rich(
        "fiscal-macro",
        "财政部宏观经济运行情况及时谋划出台增量政策",
        9,
        50,
    )
    _insert_rich(
        "apple",
        "苹果公司宣布将于10月13日在纽约发布全新iPhone",
        10,
        0,
        sectors=["苹果", "固态电池"],
    )
    _insert_rich(
        "iea",
        "IEA下调原油供需预期，预计明年全球石油供应过剩",
        8,
        40,
    )
    _insert_rich(
        "rubber",
        "橡胶价格大涨",
        7,
        25,
        sectors=["苹果", "黄金", "固态电池", "智能家居"],
        stocks=[("601118.SH", "海南橡胶")],
    )
    _insert_rich("hike", "美联储将从容推进加息", 7, 56)
    _insert_rich("cut", "美联储宣布降息25基点", 8, 5)

    events = top_hot_events(NOW)["events"]
    names = [event["name"] for event in events]
    for bad in _TRUNCATED_TITLES:
        assert bad not in names
    assert all("】" not in name and "【" not in name for name in names)

    fiscal_rows = [event for event in events if "财政" in event["name"]]
    assert len(fiscal_rows) == 1
    fiscal = fiscal_rows[0]
    assert fiscal["mentions"] == 3
    assert "财政部" in fiscal["name"]
    assert fiscal["name"] != "财政的小增量政策也出"
    assert fiscal["name"] != "新增隐债】财政部发布20"
    assert fiscal["concepts"] == ["基建", "地方债"]
    assert "苹果" not in fiscal["concepts"]

    apple = next(event for event in events if "iPhone" in event["name"])
    assert apple["name"] == "苹果公司宣布将于10月13日在纽约发布全新iPhone"
    assert "固态电池" not in apple["concepts"]

    oil = next(event for event in events if "原油" in event["name"] or event["name"].startswith("IEA"))
    assert oil["name"] == "IEA下调原油供需预期"
    assert oil["concepts"] == ["石油", "油服"]

    rubber = next(event for event in events if "橡胶" in event["name"])
    assert rubber["concepts"] == ["橡胶", "天然橡胶"]
    assert [stock["name"] for stock in rubber["mentioned_stocks"]] == ["海南橡胶"]
    for banned in ("苹果", "黄金", "固态电池", "智能家居"):
        assert banned not in rubber["concepts"]

    hike = next(event for event in events if "加息" in event["name"])
    assert hike["concepts"] == ["黄金", "银行"]
    cut = next(event for event in events if "降息" in event["name"] and "结构性" not in event["name"])
    assert cut["key"] != hike["key"]
    assert cut["concepts"] == []

    again = top_hot_events(NOW + timedelta(minutes=11))
    again_fiscal = next(event for event in again["events"] if "财政" in event["name"])
    assert again_fiscal["key"] == fiscal["key"]


def test_is_red_uses_cls_level_and_wscn_score():
    from app.news.collectors import is_red

    assert is_red("cls", "A")
    assert is_red("cls", "b")
    assert not is_red("cls", "C")
    assert not is_red("cls", "")
    assert is_red("wscn", "2")
    assert is_red("wscn", 3)
    assert not is_red("wscn", "1")
    assert not is_red("wscn", "1.5")
    assert not is_red("reddit", "A")


def test_red_items_count_more_and_a_single_red_item_stays_listed(tmp_path):
    """加红按 2.5 条计热度；没有映射的单条普通稿不进榜，加红的留下。"""
    from app.news.service import event_detail, hot_event_listing

    reset_store_for_tests(tmp_path / "red.sqlite")

    def add(source_id: str, title: str, minute: int, *, source: str, level: str, sector: str) -> None:
        mentions = [("sector", sector, sector, "", "structured")]
        status = get_store().insert_item(
            source=source,
            source_id=source_id,
            published_at=_publish(date(2026, 10, 9), 11, minute),
            author="",
            title=title,
            clean_text=title,
            raw=None,
            content_hash=source_id,
            url="",
            level=level,
            media_ids=[],
            extra=None,
            mentions=mentions,
        )
        assert status == "inserted"

    add("rubber", "橡胶价格大涨", 0, source="cls", level="A", sector="橡胶")
    add("soy", "豆粕价格走高", 0, source="cls", level="C", sector="豆粕")
    add("wscn-red", "原油价格走高", 0, source="wscn", level="2", sector="原油")
    add("plain", "螺纹价格走高", 5, source="cls", level="", sector="")

    events = top_hot_events(NOW)["events"]
    names = [event["name"] for event in events]
    assert "螺纹价格走高" not in names
    rubber = next(event for event in events if "橡胶" in event["name"])
    soy = next(event for event in events if "豆粕" in event["name"])
    crude = next(event for event in events if "原油" in event["name"])
    assert rubber["red_count"] == 1
    assert rubber["mentions"] == 1
    assert soy["red_count"] == 0
    assert crude["red_count"] == 1
    assert abs(rubber["heat"] / soy["heat"] - 2.5) < 0.01

    listed = hot_event_listing(NOW)["events"]
    listed_rubber = next(event for event in listed if "橡胶" in event["name"])
    assert listed_rubber["red_count"] == 1
    detail = event_detail(listed_rubber["key"], now=NOW)
    assert detail["items"][0]["red"] is True
    assert detail["items"][0]["level"] == "A"
    soy_detail = event_detail(next(event["key"] for event in listed if "豆粕" in event["name"]), now=NOW)
    assert soy_detail["items"][0]["red"] is False


def test_red_items_without_listed_verbs_stay_eligible(tmp_path):
    """线上加红快讯常常没有「大涨/发布」。财联社 B 和见闻评分 2（含整数）仍要进榜。"""
    from app.news.service import hot_event_listing

    path = tmp_path / "red-shaped.sqlite"
    reset_store_for_tests(path)

    def add(source_id: str, title: str, minute: int, *, source: str, level: str) -> None:
        status = get_store().insert_item(
            source=source,
            source_id=source_id,
            published_at=_publish(date(2026, 10, 9), 11, minute),
            author="",
            title=title,
            clean_text=title,
            raw=None,
            content_hash=source_id,
            url="",
            level=level,
            media_ids=[],
            extra=None,
            mentions=[],
        )
        assert status == "inserted"

    add("cls-b", "立讯精密公告涉美国337调查", 1, source="cls", level="B")
    add("wscn-str", "沙特利雅得机场再次遭袭造成12人受伤", 2, source="wscn", level="2")
    add("wscn-int", "欧洲多国电网在凌晨大范围熄灯", 3, source="wscn", level="2")
    add("plain", "图片闲聊没有动作", 4, source="cls", level="C")

    # level 列是 TEXT，整数评分入库后仍是文本。聚类入口对真正的整数同样认加红。
    from app.news.hot_events import _doc

    integer_doc = _doc({
        "id": 1,
        "source": "wscn",
        "level": 2,
        "title": "欧洲多国电网在凌晨大范围熄灯",
        "published": _publish(date(2026, 10, 9), 11, 3),
        "mentions": [],
    })
    assert integer_doc is not None and integer_doc["red"] is True

    events = top_hot_events(NOW)["events"]
    by_name = {event["name"]: event for event in events}
    assert "图片闲聊没有动作" not in by_name
    luxshare = next(event for event in events if "立讯精密" in event["name"])
    airport = next(event for event in events if "利雅得" in event["name"])
    grid = next(event for event in events if "电网" in event["name"])
    assert luxshare["red_count"] >= 1
    assert airport["red_count"] >= 1
    assert grid["red_count"] >= 1

    listed = hot_event_listing(NOW)["events"]
    assert any("立讯精密" in event["name"] and event["red_count"] >= 1 for event in listed)
    assert any("电网" in event["name"] and event["red_count"] >= 1 for event in listed)


_PRODUCTION_BAD_TITLES = (
    "32财政部发布2026年上半年中",
    "橡胶价格大涨超60家轮胎企业集体涨价今年以来",
    "重点...",
    "TIM材料",
    "IEA下调原油供需预期...",
    "隔夜美股半导体硬件波动点评",
)


def test_production_titles_drop_clocks_ellipsis_and_labels():
    """这些字符串是线上展示过的坏标题，清理后不能再原样出现。"""
    fiscal = _keyword_title([{
        "title": "15:32【财联社】财政部发布2026年上半年中长期财政规划",
    }])
    assert "财政部" in fiscal and "2026" in fiscal and "中长期" in fiscal
    assert fiscal != "32财政部发布2026年上半年中"
    assert not fiscal[0].isdigit()
    assert "【" not in fiscal and "】" not in fiscal

    clipped = _keyword_title([{"title": "32财政部发布2026年上半年中"}])
    assert clipped != "32财政部发布2026年上半年中"
    assert clipped.startswith("财政部")
    assert "2026" in clipped

    rubber = _keyword_title([{
        "title": "橡胶价格大涨超60%。60家轮胎企业集体涨价。今年以来橡胶价格持续上涨",
    }])
    assert rubber != "橡胶价格大涨超60家轮胎企业集体涨价今年以来"
    assert "今年以来" not in rubber
    assert "集体涨价" not in rubber
    assert "橡胶" in rubber and "大涨" in rubber

    glued = _keyword_title([{
        "title": "橡胶价格大涨超60家轮胎企业集体涨价今年以来",
    }])
    assert glued != "橡胶价格大涨超60家轮胎企业集体涨价今年以来"
    assert "今年以来" not in glued

    assert _keyword_title([{"title": "重点..."}]) == ""
    assert _keyword_title([{"title": "TIM材料"}]) == ""
    assert _keyword_title([{"title": "隔夜美股半导体硬件波动点评"}]) == ""
    oil = _keyword_title([{"title": "IEA下调原油供需预期..."}])
    assert oil == "IEA下调原油供需预期"
    assert "..." not in oil and "…" not in oil
    assert _keyword_title([{"title": "机构点评美联储降息"}]) == "机构点评美联储降息"

    for bad in _PRODUCTION_BAD_TITLES:
        assert not _llm_title_ok(bad)
    assert _llm_title_ok("美联储降息25基点")
    assert _llm_title_ok("华为发布新模型")
    assert not _llm_title_ok("油价走高")
    long_title = "财政部发布2026年上半年中长期财政规划并明确后续增量政策"
    assert len(long_title) > 28
    assert _llm_title_ok(long_title)
    assert _llm_title_ok("财政部发布2026年上半年中长期财政规划")
    assert _llm_title_ok("橡胶价格大涨至每吨13.1元")
    assert _llm_title_reject_reason("橡胶价格大涨至13") == "数字被截断"
    assert _is_section_label("重点...")
    assert _is_section_label("TIM材料")
    assert _is_section_label("隔夜美股半导体硬件波动点评")
    assert not _is_section_label("机构点评美联储降息")


def test_bad_production_strings_are_absent_from_the_hot_list(tmp_path):
    reset_store_for_tests(tmp_path / "prod-titles.sqlite")
    _insert("fiscal", "15:32【财联社】财政部发布2026年上半年中长期财政规划", 1)
    _insert("rubber", "橡胶价格大涨超60%。60家轮胎企业集体涨价。今年以来橡胶价格持续上涨", 2)
    _insert("label", "重点...", 3)
    _insert("material", "TIM材料", 4)
    _insert("oil", "IEA下调原油供需预期...", 5)
    _insert("note", "隔夜美股半导体硬件波动点评", 6)
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    for bad in _PRODUCTION_BAD_TITLES:
        assert bad not in names
    assert any("财政部" in name and "2026" in name and "中长期" in name for name in names)
    assert any(name == "IEA下调原油供需预期" for name in names)
    assert any("橡胶" in name and "大涨" in name and "今年以来" not in name for name in names)
    assert all("..." not in name and "…" not in name and "【" not in name for name in names)
    assert all(not name[0].isdigit() for name in names)


def test_llm_batch_rewrites_production_titles_and_fills_dictionary_concepts(tmp_path, monkeypatch):
    """模型一次写完展示标题；没有概念的事件只能回填词典里的名称。"""
    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "llm-batch.sqlite")
    _insert("fiscal", "32财政部发布2026年上半年中", 10)
    _insert("rubber", "橡胶价格大涨超60家轮胎企业集体涨价今年以来", 20)
    _insert("oil", "IEA下调原油供需预期...", 30)
    _insert("label", "重点...", 40)
    _insert("material", "TIM材料", 41)
    _insert("note", "隔夜美股半导体硬件波动点评", 42)
    _insert("ship-a", "航运公司上调运价", 50)
    _insert("ship-b", "航运公司上调运价", 55, source="wscn")
    prompts: list[str] = []

    def fake(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            title = "航运公司上调运价"
            concepts: list[str] = []
            if "财政" in rest:
                title = "财政部发布上半年财政规划"
            elif "橡胶" in rest:
                title = "橡胶价格大涨超六成"
            elif "IEA" in rest:
                title = "IEA下调原油供需预期"
            elif "航运" in rest:
                concepts = ["石油", "苹果"]
            rows.append({
                "id": head,
                "title": title,
                "concepts": concepts,
                "direction": "利好",
            })
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", fake)
    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    events = top_hot_events(NOW)["events"]
    names = [event["name"] for event in events]
    assert len(prompts) == 1
    assert "不超过28个字" in prompts[0]
    assert "32财政部发布2026年上半年中" in prompts[0]
    assert "橡胶价格大涨超60家轮胎企业集体涨价今年以来" in prompts[0]
    assert "IEA下调原油供需预期..." in prompts[0]
    for bad in _PRODUCTION_BAD_TITLES:
        assert bad not in names
    assert "财政部发布上半年财政规划" in names
    assert "橡胶价格大涨超六成" in names
    assert "IEA下调原油供需预期" in names
    ship = next(event for event in events if "航运" in event["name"])
    assert ship["concepts"] == ["石油"]
    assert "苹果" not in ship["concepts"]

    def echo(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            rows.append({"id": head, "title": rest.strip(), "concepts": ["半导体"]})
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr(news_service, "_llm_text", echo)
    hot_mod.clear_hot_event_cache()
    fallback = top_hot_events(NOW + timedelta(minutes=11))["events"]
    fallback_names = [event["name"] for event in fallback]
    assert len(prompts) == 3
    assert "上一轮标题不合格" in prompts[-1]
    for bad in _PRODUCTION_BAD_TITLES:
        assert bad not in fallback_names
    # 模型回声仍是「上半年中」这种残词时，回退也不能把它放上榜。
    assert not any(name.endswith("中") or "上半年中" in name for name in fallback_names)
    assert any(name == "IEA下调原油供需预期" for name in fallback_names)
    ship = next(event for event in fallback if "航运" in event["name"])
    assert ship["concepts"] == []


_FALLBACK_BAD = (
    "财政部发布2026年上半年中",
    "橡胶价格大涨超60家轮胎企业集体",
    "🌸固态电池",
    "高争民爆/西藏地矿/西藏铜矿资",
    "市场没有太关注到公司近期正在进行",
)


def test_bad_fallback_titles_are_rejected_and_dropped(tmp_path, monkeypatch, caplog):
    """模型两次都没给出能用的标题时，这些回退字符串不能上屏，事件直接丢掉。"""
    import logging

    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    for text in _FALLBACK_BAD:
        assert not _fallback_title_ok(text)
        event = {"name": text, "headline": text, "headlines": [text]}
        _ensure_shown_name(event)
        assert event["name"] == ""
        assert not _keep_shown_event(event)

    assert _fallback_title_ok("财政部发布2026年上半年中长期财政规划")
    assert _fallback_title_ok("橡胶价格大涨至每吨13.1元")
    assert _llm_title_ok("橡胶价格大涨至每吨13.1元")

    reset_store_for_tests(tmp_path / "fallback-drop.sqlite")
    _insert("fiscal", "财政部发布2026年上半年中", 1)
    _insert("rubber", "橡胶价格大涨超60家轮胎企业集体", 2)
    _insert("flower", "🌸固态电池", 3)
    _insert("mines", "高争民爆/西藏地矿/西藏铜矿资", 4)
    _insert("comment", "市场没有太关注到公司近期正在进行", 5)
    _insert("fed", "美联储降息25基点", 6)

    def reject(_prompt: str, **_kwargs: object) -> str:
        return json.dumps({
            "events": [{
                "id": "1",
                "title": "市场没有太关注到公司近期正在进行",
            }],
        }, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", reject)
    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    with caplog.at_level(logging.INFO, logger="app.news.hot_events"):
        names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert "热门事件标题未采用: 市场没有太关注到公司近期正在进行" in caplog.text
    assert "缺少主体、动作或宾语" in caplog.text
    for bad in _FALLBACK_BAD:
        assert bad not in names
    assert "美联储降息25基点" in names
    assert not any("财政部" in name for name in names)
    assert not any("橡胶" in name for name in names)


_BARE_SOURCES = ("央视财经", "财联社", "华尔街见闻", "路透", "路透社", "彭博", "格隆汇", "金十")


def test_bare_source_and_company_names_are_not_titles(tmp_path, caplog):
    """来源名和没有动作的公司名不能当标题；「央视财经：」要先剥掉再回退。"""
    import logging

    from app.news.hot_events import _accept_title

    for name in _BARE_SOURCES:
        assert not _fallback_title_ok(name)
        assert not _llm_title_ok(name)
        assert _llm_title_reject_reason(name) == "只有来源名"
        event = {"name": name, "headline": name, "headlines": [name], "_fp": name}
        _ensure_shown_name(event)
        assert event["name"] == ""
        assert not _keep_shown_event(event)

    assert _llm_title_reject_reason("光智科技") == "只有主体没有动作"
    assert not _fallback_title_ok("光智科技")
    company = {"name": "光智科技", "headline": "光智科技", "headlines": ["光智科技"]}
    _ensure_shown_name(company)
    assert company["name"] == ""
    assert not _keep_shown_event(company)

    kept = {
        "name": "光智科技",
        "headline": "光智科技发布新品",
        "headlines": ["光智科技发布新品"],
    }
    _ensure_shown_name(kept)
    assert kept["name"] == "光智科技发布新品"
    assert _llm_title_ok("光智科技发布新品")
    assert _fallback_title_ok("光智科技发布新品")

    cleaned = _cleaned_headline("央视财经：橡胶价格大涨超预期")
    assert cleaned != "央视财经"
    assert "橡胶" in cleaned and "大涨" in cleaned
    assert _keyword_title([{"title": "央视财经：橡胶价格大涨超预期"}]) == "橡胶价格大涨超预期"
    assert _cleaned_headline("格隆汇：橡胶价格大涨超预期") != "格隆汇"
    assert _cleaned_headline("金十：原油价格走高") != "金十"
    assert "原油" in _cleaned_headline("金十：原油价格走高")

    rubber = {
        "name": "央视财经",
        "headline": "央视财经：橡胶价格大涨超预期",
        "headlines": ["央视财经：橡胶价格大涨超预期"],
    }
    _ensure_shown_name(rubber)
    assert rubber["name"] != "央视财经"
    assert "橡胶" in rubber["name"] and "大涨" in rubber["name"]

    with caplog.at_level(logging.INFO, logger="app.news.hot_events"):
        assert not _accept_title({"_fp": "cctv"}, {"title": "央视财经"})
        assert not _accept_title({"_fp": "guangzhi"}, {"title": "光智科技"})
    assert "热门事件标题未采用: 央视财经（只有来源名）" in caplog.text
    assert "热门事件标题未采用: 光智科技（只有主体没有动作）" in caplog.text

    reset_store_for_tests(tmp_path / "bare-names.sqlite")
    _insert("rubber", "央视财经：橡胶价格大涨超预期", 1)
    _insert("bare", "光智科技", 2)
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert "央视财经" not in names
    assert "光智科技" not in names
    assert any("橡胶" in name and "大涨" in name for name in names)


def test_garbled_and_non_market_titles_stay_off_the_hot_list(tmp_path):
    """主体被拆开的译稿，以及没有 A 股映射的奖项、文体、体育，不进热榜。"""
    garbled = "美国在前法官获得诺贝尔"
    assert _llm_title_reject_reason(garbled) == "标题不连贯"
    assert not _llm_title_ok(garbled)
    assert not _fallback_title_ok(garbled)
    assert not _title_ok(garbled)
    assert _keyword_title([{"title": garbled}]) == ""
    assert _llm_title_reject_reason("美国在前财长宣布加息") == "标题不连贯"

    kept = _keyword_title([{"title": "财政部在前期出台增量政策"}])
    assert "财政部" in kept and "出台" in kept and "在前期" in kept
    for title in (
        "公司在前三季度净利润增长50%",
        "宁德时代在前十大股东中新增社保基金",
        "沪指在前一交易日大涨后回调",
        "在前述公告基础上追加回购",
    ):
        assert _llm_title_reject_reason(title) != "标题不连贯"
        assert _keyword_title([{"title": title}]) == title
        assert _fallback_title_ok(title)

    reset_store_for_tests(tmp_path / "non-market.sqlite")
    _insert_rich("garbled", garbled, 8, 1, level="A")
    _insert_rich("nobel", "美国前法官获得诺贝尔和平奖", 8, 2, level="A")
    _insert_rich("nobel-verb", "组委会发布诺贝尔奖得主", 8, 3, level="A")
    _insert_rich("cup", "国际足联发布世界杯赛程", 8, 4, level="A")
    _insert_rich("album", "歌手发布新专辑", 8, 5, level="A")
    _insert_rich("show", "文旅部出台演唱会补贴", 8, 6)
    _insert_rich(
        "oscar",
        "光线传媒发布新片获得奥斯卡奖",
        8,
        7,
        stocks=[("300251.SZ", "光线传媒")],
    )
    _insert_rich("fed", "美联储降息25基点", 8, 8)

    events = top_hot_events(NOW)["events"]
    names = [event["name"] for event in events]
    assert garbled not in names
    assert "美国前法官获得诺贝尔和平奖" not in names
    assert not any("诺贝尔" in name for name in names)
    assert not any("世界杯" in name for name in names)
    assert not any("新专辑" in name for name in names)
    assert any("演唱会" in name and "出台" in name for name in names)
    oscar = next(event for event in events if "奥斯卡" in event["name"])
    assert any(stock["name"] == "光线传媒" for stock in oscar["mentioned_stocks"])
    assert "美联储降息25基点" in names


def test_report_file_labels_are_stripped_from_titles(tmp_path):
    """研报文件名、八位日期戳和 # 不能留在展示标题里。"""
    raw = "锂电行业更新-20261009#十五五政策定调"
    assert _strip_report_label(raw) == "十五五政策定调"
    assert _strip_report_label("锂电行业更新-2026-10-09#十五五政策定调") == "十五五政策定调"
    assert _strip_report_label("锂电行业更新-20261009十五五政策定调") == "十五五政策定调"
    assert _cleaned_headline(raw) == "十五五政策定调"
    assert _keyword_title([{"title": raw}]) == "十五五政策定调"
    assert _llm_title_reject_reason(raw) == "含文件标记"
    assert not _llm_title_ok(raw)
    assert not _fallback_title_ok(raw)
    dated = "锂电行业更新-20261009十五五政策定调"
    assert _llm_title_reject_reason(dated) == "含日期戳"
    fiscal = _cleaned_headline("财政部发布2026年上半年中长期财政规划")
    assert "2026" in fiscal and "20261009" not in fiscal
    repo = "央行2026-10-09开展逆回购"
    assert _strip_report_label(repo) == repo
    assert _cleaned_headline(repo) == repo
    assert _llm_title_reject_reason(repo) != "含日期戳"
    compact_repo = "央行20261009开展逆回购"
    assert "20261009" in _strip_report_label(compact_repo)
    assert _llm_title_reject_reason(compact_repo) != "含日期戳"

    reset_store_for_tests(tmp_path / "report-label.sqlite")
    _insert("lithium", raw, 3)
    _insert("fed", "美联储降息25基点", 4)
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert raw not in names
    assert "十五五政策定调" in names
    assert all("#" not in name and "20261009" not in name for name in names)
    assert "美联储降息25基点" in names


def test_concept_tags_follow_entities_not_column_names(tmp_path):
    """伊朗稿不能标成俄乌；快报是栏目名，不是概念。"""
    title = "普京向特朗普转达伊朗方面解决冲突构想"
    assert _fine_concept("俄乌冲突快报", "俄乌冲突快报") is False
    assert _concept_relevant("俄乌冲突快报", "俄罗斯与乌克兰冲突升级", set()) is False
    assert _concept_relevant("俄乌冲突", title, set()) is False
    assert _concept_relevant("俄乌冲突快报", title, set()) is False
    assert _dictionary_concepts(title) == ["中东"]
    kept = _relevant_concepts(
        Counter({"俄乌冲突": 1, "俄乌冲突快报": 2}),
        [title],
        set(),
    )
    assert kept == []

    reset_store_for_tests(tmp_path / "iran-concept.sqlite")
    _insert_rich(
        "iran",
        title,
        9,
        10,
        level="A",
        sectors=["俄乌冲突快报", "俄乌冲突"],
    )
    events = top_hot_events(NOW)["events"]
    iran = next(event for event in events if "伊朗" in event["name"])
    assert iran["concepts"] == ["中东"]
    assert iran["category"] == "地缘政治"
    assert all("俄乌" not in name and "快报" not in name for name in iran["concepts"])
    assert all("俄乌" not in item["name"] and "快报" not in item["name"] for item in iran["mapping"])


def test_title_cache_v4_is_not_reused(tmp_path, monkeypatch):
    """缓存版本升到 v5 后，旧键里的标题即使当时能用也不再套用。"""
    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    assert hot_mod._TITLE_CACHE_VERSION == 5
    reset_store_for_tests(tmp_path / "cache-v5.sqlite")
    title = "【财联社】美联储宣布降息25基点"
    _insert("fed", title, 1)
    fingerprint = hashlib.sha1(title.encode("utf-8")).hexdigest()
    hot_mod.clear_hot_event_cache()
    hot_mod._LLM_CACHE[f"title-v4:{fingerprint}"] = {"title": "美联储降息25基点"}
    prompts: list[str] = []

    def good(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return '{"title":"美联储宣布降息二十五基点"}'

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", good)
    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: True)
    named = top_hot_events(NOW)
    assert named["events"][0]["name"] == "美联储宣布降息二十五基点"
    assert len(prompts) == 1
    assert all(not str(key).startswith("title-v4:") for key in hot_mod._LLM_CACHE)
