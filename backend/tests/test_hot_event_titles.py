"""热门事件标题要是完整句子，不能是截断的半句话。"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime, timedelta

from app.market_time import CN_TZ
from app.news.hot_events import (
    _ANCHOR_HEAD_OK,
    _ANCHOR_TAIL_OK,
    _CONCEPT_SHELL,
    _category_for,
    _cleaned_headline,
    _concept_relevant,
    _dictionary_concepts,
    _direction_for,
    _display_gate_reason,
    _ensure_shown_name,
    _explicit_target_direction,
    _fallback_title_ok,
    _fine_concept,
    _headline_maps_concept,
    _is_section_label,
    _keep_shown_event,
    _keyword_title,
    _llm_title_ok,
    _llm_title_reject_reason,
    _original_reject_reason,
    _relevant_concepts,
    _strip_report_label,
    _title_ok,
)

# 概念子串锚定的正反例。左列是反例：概念名的一个 2 字片撞上了正文里的通用词，
# 语义完全无关，改前全部误挂（`for size in range(...)` 的 2 字暴力命中）。
# 右列是必须保留的真命中，其中「低空飞行」「算力需求」只有 2 字锚，
# 所以修法不能用简单的「子串长度 ≥ 3」（会误杀它们）。
_CONCEPT_ANCHOR_CASES = (
    # (概念, 事件文本, 期望命中)
    ("低空经济", "经济回暖带动消费", False),
    ("低空经济", "经济数据回暖", False),
    ("低空经济", "一季度经济增速", False),
    ("工业母机", "工业增加值超预期", False),
    ("固态电池", "电池级碳酸锂价格", False),
    ("固态电池", "电池厂商扩产", False),
    ("可控核聚变", "可控硅需求回暖", False),
    ("可控核聚变", "可控硅涨价", False),
    ("数据要素", "数据交易规模扩大", False),
    ("新能源行业", "行业景气度回升", False),
    ("低空经济", "低空飞行试点扩容", True),
    ("算力租赁", "算力需求爆发", True),
    ("固态电池", "固态电池量产线投产", True),
    ("储能电池", "储能招标规模扩大", True),
    ("机器人", "人形机器人发布", True),
    ("白酒", "白酒动销回暖", True),
    ("光伏组件", "组件出货量增长", True),
    ("半导体设备", "设备国产化率提升", True),
    ("银行板块", "银行板块午后走强", True),
)


def test_headline_maps_concept_rejects_shell_substring_hits():
    """2 字子串撞通用产业壳不算命中，撞在锚位上的才算。

    线上实测反例：「低空经济」←「经济回暖」、「工业母机」←「工业增加值」、
    「固态电池」←「电池级碳酸锂」、「可控核聚变」←「可控硅」。这些概念都被
    挂到了语义无关的事件上，直接影响展示层的题材归属。
    """
    for name, blob, expected in _CONCEPT_ANCHOR_CASES:
        hit = name in blob or _headline_maps_concept(name, blob)
        assert hit is expected, f"{name} ← {blob} 期望 {expected}，实得 {hit}"


def test_concept_anchor_tables_stay_disjoint_from_shell():
    """壳词表与锚表的重叠必须限于显式白名单 `_ANCHOR_SHELL_EXEMPT`。

    两张表的分工是：壳词表拦「单独撞上一个通用词」，锚表放「这个字在概念名里
    占的是首/尾锚位」。同一个字同时进两张表时，锚表必须赢 —— 这条测试防的是
    以后有人往 `_ANCHOR_TAIL_OK` 里补「电池」「材料」这类壳词，把
    「固态电池」重新变成「电池XX」都能挂上的概念。
    """
    from app.news.hot_events import _ANCHOR_SHELL_EXEMPT

    assert not (_ANCHOR_HEAD_OK & _CONCEPT_SHELL)
    assert (_ANCHOR_TAIL_OK & _CONCEPT_SHELL) <= _ANCHOR_SHELL_EXEMPT
    assert _ANCHOR_SHELL_EXEMPT <= _CONCEPT_SHELL
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
    assert bare != "和之前结构性降息"
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
    assert clipped == "宏观经济运行出台务实管用的增量"
    assert clipped != "宏观经济运行出台"
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
    assert "橡胶价格大涨超预期发布公告" in names
    assert "美联储降息25基点，和之前结构性降息不同" in names
    assert "根据宏观经济运行情况出台务实管用的增量政策" in names
    assert "存储芯片厂宣布涨价" in names
    assert "油价走高，叠加昨夜美国针对制裁伊朗" in names
    assert "工信部出台机器人补贴" in names
    for bad in _BAD_TITLES:
        assert bad not in names
    for name in names:
        assert _original_reject_reason(name) == ""


def test_llm_prefers_good_title_and_rejects_fragment(tmp_path, monkeypatch):
    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "llm-title.sqlite")
    _insert("fed", "【财联社】美联储宣布降息25基点", 0)
    prompts: list[str] = []

    def good(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return '{"title":"美联储宣布降息二十五基点","category":"海外市场/央行","direction":"利好"}'

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", good)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    named = top_hot_events(NOW)
    assert named["events"][0]["name"] == "美联储宣布降息25基点"
    assert named["events"][0]["subtitle"] == "美联储宣布降息二十五基点"
    assert len(prompts) == 1
    assert "不要半句话" in prompts[0]
    assert "补充要点" in prompts[0]

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
    assert rejected["events"][0]["subtitle"] == "叠加昨夜美国针对制裁"
    assert rejected["events"][0]["category"] == "海外市场/央行"
    assert rejected["events"][0]["importance"] == "重大"
    assert any(
        "叠加昨夜" in str(item.get("title") or "")
        for item in hot_mod._LLM_CACHE.values()
        if isinstance(item, dict)
    )

    hot_mod.clear_hot_event_cache(llm=False)
    again = top_hot_events(NOW + timedelta(minutes=22))
    assert again["events"][0]["name"] == "美联储宣布降息25基点"
    assert again["events"][0]["subtitle"] == "叠加昨夜美国针对制裁"
    assert len(prompts) == 2

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
        "title": "早上好",
    }
    prompts: list[str] = []

    def good(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return '{"title":"美联储宣布降息二十五基点"}'

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", good)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    named = top_hot_events(NOW)
    assert named["events"][0]["name"] == "美联储宣布降息25基点"
    assert named["events"][0]["subtitle"] == "美联储宣布降息二十五基点"
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
        14,
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
    assert policy["first_seen"] == "10-09 14:27"

    assert "结构性降息" not in names
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
    assert oil["name"] == "IEA下调原油供需预期，预计明年全球石油供应过剩"
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
    assert not clipped.startswith("32")
    if clipped:
        assert clipped.startswith("财政部") and "2026" in clipped
        assert _display_gate_reason(clipped) == ""

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
    full_rubber = "橡胶价格大涨超60%。60家轮胎企业集体涨价。今年以来橡胶价格持续上涨"
    assert "橡胶价格大涨超60%" in names
    assert full_rubber not in names
    assert all("..." not in name and "…" not in name and "【" not in name for name in names)
    assert all(not name[0].isdigit() for name in names)


def test_llm_batch_rewrites_production_titles_and_fills_dictionary_concepts(tmp_path, monkeypatch):
    """模型一次写完展示标题；词典概念必须在标题里对得上，不能凭名单贴上。"""
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
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    events = top_hot_events(NOW)["events"]
    names = [event["name"] for event in events]
    assert len(prompts) == 1
    assert "补充要点" in prompts[0]
    assert "编号" in prompts[0]
    assert "橡胶价格大涨超60家轮胎企业集体涨价今年以来" in prompts[0]
    # 每条事件各带自己的正文，不把簇内其他候选标题拼进同一行。
    # 早前只喂主标题，模型只能把主标题换个说法；正文才是要点的素材。
    assert "IEA下调原油供需预期..." not in prompts[0]
    assert "1. 已有标题（已定稿，不要重复、不要回填）：IEA下调原油供需预期" in prompts[0]
    for bad in _PRODUCTION_BAD_TITLES:
        if bad == "橡胶价格大涨超60家轮胎企业集体涨价今年以来":
            continue
        assert bad not in names
    assert "橡胶价格大涨超60家轮胎企业集体涨价今年以来" in names
    assert "财政部发布上半年财政规划" not in names
    assert "橡胶价格大涨超六成" not in names
    assert "IEA下调原油供需预期" in names
    ship = next(event for event in events if "航运" in event["name"])
    assert ship["concepts"] == []
    assert "石油" not in ship["concepts"]
    assert "苹果" not in ship["concepts"]

    def echo(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            # 输入行是「N. 已有标题（已定稿，不要重复、不要回填）：<标题>」，
            # 回声只取冒号后的标题本体。
            if "：" in rest:
                rest = rest.split("：", 1)[1]
            rows.append({"id": head, "title": rest.strip(), "concepts": ["半导体"]})
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr(news_service, "_llm_text", echo)
    hot_mod.clear_hot_event_cache()
    fallback = top_hot_events(NOW + timedelta(minutes=11))["events"]
    fallback_names = [event["name"] for event in fallback]
    assert len(prompts) == 2
    assert "上一轮标题不合格" not in prompts[-1]
    for bad in _PRODUCTION_BAD_TITLES:
        if bad == "橡胶价格大涨超60家轮胎企业集体涨价今年以来":
            continue
        assert bad not in fallback_names
    assert "橡胶价格大涨超60家轮胎企业集体涨价今年以来" in fallback_names
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
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    with caplog.at_level(logging.INFO, logger="app.news.hot_events"):
        names = [event["name"] for event in top_hot_events(NOW)["events"]]
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
        assert _accept_title({"_fp": "guangzhi"}, {"title": "光智科技"})
    assert "热门事件标题未采用: 央视财经（来源前缀）" in caplog.text

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
    # 词典返回 (概念, 极性) 三元组：加息利空黄金/利好银行这类方向只有词典知道。
    # 中东（地缘）方向随语境翻转，登记为 0，由事件整体方向定。
    assert _dictionary_concepts(title) == [("中东", 0)]
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


_EARNINGS_TITLES = (
    "公司在前三季度净利润增长50%",
    "宁德时代在前十大股东中新增社保基金",
    "在前述公告基础上追加回购",
)


def test_earnings_shareholder_and_buyback_titles_pass(tmp_path):
    """增长、新增、追加回购都有主体和动作，不能再报缺少主语、谓语或宾语。"""
    for title in _EARNINGS_TITLES:
        assert _llm_title_reject_reason(title) == ""
        assert _llm_title_ok(title)
        assert _title_ok(title)
        assert _keyword_title([{"title": title}]) == title
    assert _llm_title_ok("沪指在前一交易日大涨后回调")

    reset_store_for_tests(tmp_path / "earnings-titles.sqlite")
    for index, title in enumerate(_EARNINGS_TITLES):
        _insert(f"earn-{index}", title, index + 1)
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    for title in _EARNINGS_TITLES:
        assert title in names


def test_commentary_titles_fall_back_or_drop(tmp_path, monkeypatch, caplog):
    """侧面佐证、看好、地位这类没有主体和动作的点评不是事件。"""
    import logging

    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    commentary = "侧面佐证AI电源地位&能力"
    assert _llm_title_reject_reason(commentary) == "不是具体事件"
    assert not _llm_title_ok(commentary)
    assert not _fallback_title_ok(commentary)
    assert _keyword_title([{"title": commentary}]) == ""
    for title in (
        "数据印证需求正在回暖",
        "板块利好继续扩散",
        "机构看好算力产业链",
        "AI电源地位继续提升",
        "算力逻辑继续强化",
    ):
        assert _llm_title_reject_reason(title) == "不是具体事件"
        assert not _fallback_title_ok(title)
        assert _keyword_title([{"title": title}]) == ""

    assert _llm_title_ok("美联储降息利好黄金")
    assert _llm_title_reject_reason("宁德时代回购巩固行业地位") == ""

    kept = {
        "name": commentary,
        "headline": "宁德时代发布固态电池",
        "headlines": ["宁德时代发布固态电池"],
    }
    _ensure_shown_name(kept)
    assert kept["name"] == "宁德时代发布固态电池"
    assert _keep_shown_event(kept)

    dropped = {"name": commentary, "headline": commentary, "headlines": [commentary]}
    _ensure_shown_name(dropped)
    assert dropped["name"] == ""
    assert not _keep_shown_event(dropped)

    reset_store_for_tests(tmp_path / "commentary.sqlite")
    _insert("battery", "宁德时代发布固态电池", 1)
    _insert_rich("note", commentary, 11, 2, level="A")

    def reject(_prompt: str, **_kwargs: object) -> str:
        return json.dumps({
            "events": [
                {"id": "1", "title": commentary},
                {"id": "2", "title": commentary},
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", reject)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    with caplog.at_level(logging.INFO, logger="app.news.hot_events"):
        names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert commentary not in names
    assert "宁德时代发布固态电池" in names


def test_named_company_is_not_replaced_with_generic(tmp_path, monkeypatch, caplog):
    """点名的公司不能换成「公司」。原标题本来就写公司的，仍然可以。"""
    import logging

    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    source = "立讯精密及立讯技术涉337调查尚处立案初期"
    swapped = "公司及立讯技术涉337调查尚处立案初期"
    assert _keyword_title([{"title": source}]) == source
    assert _llm_title_reject_reason(swapped, source) == "主体被换成泛称"
    assert _llm_title_reject_reason("公司在前三季度净利润增长50%") == ""
    assert _llm_title_reject_reason(
        "公司在前三季度净利润增长50%",
        "公司在前三季度净利润增长50%",
    ) == ""
    assert _llm_title_reject_reason("航运公司上调运价", "航运公司上调运价") == ""

    reset_store_for_tests(tmp_path / "luxshare-name.sqlite")
    _insert("lux", source, 3)

    def generic(_prompt: str, **_kwargs: object) -> str:
        return json.dumps({"title": swapped}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", generic)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    with caplog.at_level(logging.INFO, logger="app.news.hot_events"):
        names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert any(name.startswith("立讯精密") for name in names)
    assert all(not name.startswith("公司及") for name in names)


def test_watch_and_advice_commentary_is_not_an_event(tmp_path):
    """关注、建议、推荐、布局、把握，没有主体和动作时是点评。"""
    titles = (
        "继续关注AI视频板块",
        "机构建议继续布局",
        "研报推荐算力链",
        "把握本轮行情",
    )
    for title in titles:
        assert _llm_title_reject_reason(title) == "不是具体事件"
        assert not _fallback_title_ok(title)
        assert _keyword_title([{"title": title}]) == ""
    assert _llm_title_ok("宁德时代回购巩固行业地位")
    assert _llm_title_reject_reason("证监会立案后机构建议回避") == ""

    reset_store_for_tests(tmp_path / "watch-commentary.sqlite")
    _insert_rich("watch", "继续关注AI视频板块", 11, 4, level="A")
    _insert("fed", "美联储降息25基点", 5)
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert "继续关注AI视频板块" not in names
    assert "美联储降息25基点" in names


_POLICY_DIRECTIVES = (
    "国务院布局未来产业",
    "工信部建议加快5G部署",
    "发改委布局算力枢纽",
    "国资委推荐央企试点",
)
_COMMENTARY_DIRECTIVES = (
    "继续关注AI视频板块",
    "建议关注算力",
    "推荐布局低空经济",
)


def test_directive_verbs_keep_policy_events_and_drop_commentary(tmp_path):
    """机构或公司名后面的布局、建议、推荐是事件；没有主体的仍是点评。"""
    for title in _POLICY_DIRECTIVES:
        assert _llm_title_reject_reason(title) == ""
        assert _llm_title_ok(title)
        assert _keyword_title([{"title": title}]) == title
        assert _fallback_title_ok(title)
    for title in _COMMENTARY_DIRECTIVES:
        assert _llm_title_reject_reason(title) == "不是具体事件"
        assert not _llm_title_ok(title)
        assert not _fallback_title_ok(title)
        assert _keyword_title([{"title": title}]) == ""
    assert _llm_title_ok("宁德时代布局固态电池")
    assert _llm_title_reject_reason("机构建议继续布局") == "不是具体事件"

    reset_store_for_tests(tmp_path / "directive-verbs.sqlite")
    for index, title in enumerate(_POLICY_DIRECTIVES):
        _insert(f"policy-{index}", title, index + 1)
    for index, title in enumerate(_COMMENTARY_DIRECTIVES):
        _insert_rich(f"note-{index}", title, 11, index + 1, level="A")
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    for title in _POLICY_DIRECTIVES:
        assert title in names
    for title in _COMMENTARY_DIRECTIVES:
        assert title not in names


def test_runon_titles_shorten_to_the_core_event():
    """叠加、接续粘成的长标题收成不超过 20 字的核心事件。"""
    raw = "赎回接续叠加资本补充需求前三季度银行二永债发行显著放量"
    core = "前三季度银行二永债发行显著放量"
    assert len(raw) > 20
    assert len(core) <= 20
    assert _llm_title_reject_reason(raw) == "标题过长"
    assert not _fallback_title_ok(raw)
    assert _keyword_title([{"title": raw}]) == core
    assert _cleaned_headline(raw) == core
    kept = "财政部有关负责人表示财政的小增量政策也出台了一批配套措施"
    assert _keyword_title([{"title": kept}]) == kept


def test_growth_fallback_needs_a_concrete_subject(tmp_path):
    """同比增长、数据显示增长明显没有具体主体，不能当回退标题。"""
    assert not _fallback_title_ok("同比增长")
    assert not _fallback_title_ok("数据显示增长明显")
    assert _llm_title_reject_reason("数据显示增长明显") == "缺少主体、动作或宾语"
    assert _llm_title_reject_reason("同比增长50%") == "缺少主体、动作或宾语"
    assert _keyword_title([{"title": "同比增长"}]) == ""
    assert _keyword_title([{"title": "数据显示增长明显"}]) == ""
    assert _llm_title_ok("宁德时代净利润同比增长50%")
    assert _fallback_title_ok("宁德时代净利润同比增长50%")

    reset_store_for_tests(tmp_path / "vague-growth.sqlite")
    _insert("yoy", "同比增长", 6)
    _insert("data", "数据显示增长明显", 7)
    _insert("catl", "宁德时代净利润同比增长50%", 8)
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert "同比增长" not in names
    assert "数据显示增长明显" not in names
    assert "宁德时代净利润同比增长50%" in names


def test_company_colon_keeps_the_named_subject():
    """公司名加冒号不是来源。后面以公司/本公司起笔时，换成这家公司。"""
    raw = "立讯精密：公司及立讯技术涉337调查 尚处立案初期"
    expected = "立讯精密及立讯技术涉337调查尚处立案初期"
    assert _keyword_title([{"title": raw}]) == expected
    assert _cleaned_headline(raw) == expected
    assert _llm_title_reject_reason("公司及立讯技术涉337调查尚处立案初期", raw) == "主体被换成泛称"
    own = "立讯精密：本公司及立讯技术涉337调查尚处立案初期"
    assert _keyword_title([{"title": own}]) == expected
    assert _llm_title_reject_reason("本公司及立讯技术涉337调查尚处立案初期", own) == "主体被换成泛称"
    assert _keyword_title([{"title": "央视财经：橡胶价格大涨超预期"}]) == "橡胶价格大涨超预期"
    assert _keyword_title([{"title": "财联社：橡胶价格大涨超预期"}]) == "橡胶价格大涨超预期"
    assert _keyword_title([{"title": "新华社：工信部出台机器人补贴"}]) == "工信部出台机器人补贴"
    assert _llm_title_reject_reason(
        "公司在前三季度净利润增长50%",
        "【财联社】公司在前三季度净利润增长50%",
    ) == ""
    assert _llm_title_reject_reason("航运公司上调运价", "航运公司上调运价") == ""


def test_watch_list_and_broker_prefixes_prefer_the_event():
    """持续关注和 1）、① 不是标题。券商电新/计算机/研究称要去掉。"""
    raw = "景旺电子：Q3利润加速释放…持续关注1）Q3业绩增长强劲"
    assert _llm_title_reject_reason("持续关注1）Q3业绩增长强") == "不是具体事件"
    assert _llm_title_reject_reason("①Q3业绩增长强劲") == "不是具体事件"
    assert not _fallback_title_ok("持续关注1）Q3业绩增长强")
    assert _keyword_title([{"title": "持续关注1）Q3业绩增长强"}]) == ""
    assert _keyword_title([{"title": raw}]) == "景旺电子Q3利润加速释放"
    assert _cleaned_headline(raw) == "景旺电子Q3利润加速释放"

    assert _keyword_title([{"title": "招商电新称碳酸锂报价继续上涨"}]) == "碳酸锂报价继续上涨"
    assert _keyword_title([{"title": "中信计算机称存储芯片厂宣布涨价"}]) == "存储芯片厂宣布涨价"
    assert _keyword_title([{"title": "华泰研究称美联储降息25基点"}]) == "美联储降息25基点"
    kept = _keyword_title([{"title": "国务院研究称将出台增量政策"}])
    assert kept.startswith("国务院") and "出台" in kept and "研究称" in kept


def test_signing_titles_keep_both_parties(tmp_path):
    """签约和战略合作要留下双方，不能剪成只剩建发股份。"""
    paired = "建发股份与优必选战略合作签约"
    assert _keyword_title([{"title": paired}]) == paired
    colon = "建发股份：与优必选达成战略合作并签约"
    colon_title = _keyword_title([{"title": colon}])
    assert "建发股份" in colon_title and "优必选" in colon_title
    long = "建发股份与优必选签署战略合作协议并将在机器人领域展开全面合作"
    long_title = _keyword_title([{"title": long}])
    assert long_title == "建发股份与优必选签署战略合作"
    assert "建发股份" in long_title and "优必选" in long_title
    assert _llm_title_reject_reason("建发股份战略合作签约", paired) == "缺少合作方"
    assert _llm_title_reject_reason(paired, paired) != "缺少合作方"

    reset_store_for_tests(tmp_path / "signing-parties.sqlite")
    _insert("pair", paired, 1)
    _insert("colon", colon, 2)
    _insert_rich("long", long, 11, 3, level="A")
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert "建发股份战略合作签约" not in names
    assert all("建发股份" not in name or "优必选" in name for name in names)
    assert any("建发股份" in name and "优必选" in name for name in names)


def test_swapped_party_order_is_one_event(tmp_path):
    """A与B做同一件事，和 B与A 并成一件。动作不同则分开。"""
    reset_store_for_tests(tmp_path / "party-order.sqlite")
    _insert("seres", "赛力斯与宁德时代签署长期战略合作协议", 1)
    _insert("catl", "宁德时代与赛力斯签署长期战略合作协议", 2, source="wscn")
    _insert("jianfa", "建发股份与优必选签署合作备忘录", 3)
    _insert("ubtech", "优必选与建发股份签署战略协议", 4, source="wscn")
    _insert("buyback", "优必选与建发股份回购股份", 5)
    events = top_hot_events(NOW)["events"]
    seres = [event for event in events if "赛力斯" in event["name"] and "宁德时代" in event["name"]]
    assert len(seres) == 1
    assert seres[0]["mentions"] == 2
    signing = [
        event for event in events
        if "建发股份" in event["name"] and "优必选" in event["name"] and "回购" not in event["name"]
    ]
    assert len(signing) == 1
    assert signing[0]["mentions"] == 2
    assert sum(1 for event in events if "回购" in event["name"]) == 1


def test_misordered_verb_falls_back_to_the_source_headline():
    """句首动词没有主体、后面又有主体和动作时，退回清理后的原标题。"""
    garbled = "重申AI安全白宫《超级智能协议》签署"
    source = "白宫签署超级智能协议"
    assert _llm_title_reject_reason(garbled) == "语序颠倒"
    assert not _llm_title_ok(garbled)
    assert not _fallback_title_ok(garbled)
    assert _keyword_title([{"title": garbled}]) == ""
    assert _llm_title_reject_reason(source) == ""
    assert _llm_title_ok("美联储降息25基点")
    assert _llm_title_reject_reason("宁德时代回购巩固行业地位") == ""
    event = {"name": garbled, "headline": source, "headlines": [source]}
    _ensure_shown_name(event)
    assert event["name"] == source


def test_truncation_stops_at_the_clause_boundary():
    """不能把「集体涨价」切成「集体」。没有句读时，动词留在这一句里。"""
    raw = "央视财经：橡胶价格大涨超60家轮胎企业集体涨价"
    expected = "橡胶价格大涨超60家轮胎企业集体涨价"
    assert _keyword_title([{"title": raw}]) == expected
    assert _cleaned_headline(raw) == expected
    assert not expected.endswith("集体")
    glued = "橡胶价格大涨超60家轮胎企业集体涨价今年以来"
    assert _keyword_title([{"title": glued}]) == expected
    assert "今年以来" not in expected
    punctuated = _keyword_title([{
        "title": "橡胶价格大涨超60%。60家轮胎企业集体涨价。今年以来橡胶价格持续上涨",
    }])
    assert "集体涨价" not in punctuated
    assert punctuated.startswith("橡胶价格大涨")
    assert _keyword_title([{"title": "央视财经：橡胶价格大涨超预期"}]) == "橡胶价格大涨超预期"
    assert _keyword_title([{
        "title": "【财联社】橡胶价格大涨超预期发布公告",
    }]) == "橡胶价格大涨超预期"


def test_title_cache_v4_is_not_reused(tmp_path, monkeypatch):
    """缓存版本升到 v17 以上后，旧键里的标题即使当时能用也不再套用。"""
    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    # 只要求「不低于 v17」：这条用例验的是旧版本键被丢弃，不是具体版本号。
    # 写死数字的话，每次因取文/提示词口径变化升版本都要回来改一次。
    assert hot_mod._TITLE_CACHE_VERSION >= 17
    reset_store_for_tests(tmp_path / "cache-v13.sqlite")
    title = "【财联社】美联储宣布降息25基点"
    _insert("fed", title, 1)
    fingerprint = hashlib.sha1(title.encode("utf-8")).hexdigest()
    hot_mod.clear_hot_event_cache()
    hot_mod._LLM_CACHE[f"title-v4:{fingerprint}"] = {"title": "美联储降息25基点"}
    hot_mod._LLM_CACHE[f"title-v5:{fingerprint}"] = {"title": "美联储降息二十五基点旧缓存"}
    hot_mod._LLM_CACHE[f"title-v6:{fingerprint}"] = {"title": "美联储降息旧缓存第六版"}
    hot_mod._LLM_CACHE[f"title-v7:{fingerprint}"] = {"title": "美联储降息旧缓存第七版"}
    hot_mod._LLM_CACHE[f"title-v8:{fingerprint}"] = {"title": "美联储降息旧缓存第八版"}
    hot_mod._LLM_CACHE[f"title-v9:{fingerprint}"] = {"title": "美联储降息旧缓存第九版"}
    hot_mod._LLM_CACHE[f"title-v10:{fingerprint}"] = {"title": "美联储降息旧缓存第十版"}
    hot_mod._LLM_CACHE[f"title-v11:{fingerprint}"] = {"title": "美联储降息旧缓存第十一版"}
    hot_mod._LLM_CACHE[f"title-v12:{fingerprint}"] = {"title": "美联储降息旧缓存第十二版"}
    hot_mod._LLM_CACHE[f"title-v13:{fingerprint}"] = {"title": "美联储降息旧缓存第十三版"}
    hot_mod._LLM_CACHE[f"title-v14:{fingerprint}"] = {"title": "美联储降息旧缓存第十四版"}
    hot_mod._LLM_CACHE[f"title-v15:{fingerprint}"] = {"title": "美联储降息旧缓存第十五版"}
    prompts: list[str] = []

    def good(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return '{"title":"美联储宣布降息二十五基点"}'

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", good)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    named = top_hot_events(NOW)
    assert named["events"][0]["name"] == "美联储宣布降息25基点"
    assert named["events"][0]["subtitle"] == "美联储宣布降息二十五基点"
    assert len(prompts) == 1
    assert all(
        not str(key).startswith((
            "title-v4:", "title-v5:", "title-v6:", "title-v7:", "title-v8:", "title-v9:",
            "title-v10:", "title-v11:", "title-v12:", "title-v13:", "title-v14:",
            "title-v15:",
        ))
        for key in hot_mod._LLM_CACHE
    )


def _insert_at(source_id: str, title: str, hour: int, minute: int, source: str = "cls") -> None:
    status = get_store().insert_item(
        source=source,
        source_id=source_id,
        published_at=_publish(date(2026, 10, 9), hour, minute),
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


def test_under_limit_titles_stay_whole_and_latin_is_not_split():
    """没超过 28 字不截短。超长时也不能把 VPD 切成 V。"""
    vicor = "Vicor对VPD发起337调查"
    plan = "十五五政策定调固态电池产业化进入关键期"
    assert _keyword_title([{"title": vicor}]) == vicor
    assert _cleaned_headline(vicor) == vicor
    assert _keyword_title([{"title": plan}]) == plan
    assert _cleaned_headline(plan) == plan
    comma = "十五五政策定调，固态电池产业化进入关键期"
    assert _keyword_title([{"title": comma}]) == plan
    assert _cleaned_headline(comma) == plan
    filler = "甲" * 21
    assert len(filler + "Vicor对V") == 28
    clipped = _keyword_title([{"title": filler + vicor}])
    assert "对V" not in clipped.replace("对VPD", "")
    assert _keyword_title([{"title": "锂电行业更新-20261009#十五五政策定调"}]) == "十五五政策定调"


def test_cutoff_titles_do_not_duplicate_the_full_event(tmp_path):
    """半截标题不能和完整事件并存。"""
    vicor = "Vicor对VPD发起337调查"
    plan = "十五五政策定调固态电池产业化进入关键期"
    reset_store_for_tests(tmp_path / "cutoff-dup.sqlite")
    _insert("plan", plan, 1)
    _insert("plan-cut", "十五五政策定调", 2, source="wscn")
    _insert_rich("vicor", vicor, 10, 3, level="A")
    _insert_rich("vicor-cut", "Vicor对V", 10, 4, level="A", source="wscn")
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert names.count(plan) == 1
    assert "十五五政策定调" not in names
    assert names.count(vicor) == 1
    assert "Vicor对V" not in names


def test_broker_tail_report_label_and_subjectless_product():
    """去掉末尾券商组名，研报标签和没写完的数量不上屏，没有主体就退回原标题。"""
    vicor = "Vicor对VPD发起337调查"
    assert _keyword_title([{"title": "某某公司发布新品天风计算机"}]) == "某某公司发布新品"
    assert _keyword_title([{"title": f"天风计算机：{vicor}"}]) == vicor
    assert _llm_title_reject_reason("某某公司发布新品天风计算机") == "券商组名"
    label = "PCB设备&钓针观点更新"
    assert _llm_title_reject_reason(label) == "研报标签"
    assert not _fallback_title_ok(label)
    assert _keyword_title([{"title": label}]) == ""
    assert _keyword_title([{"title": "PCB设备&钓针观点更新2026"}]) == ""
    cutoff = "运作满一年产品近八"
    assert _llm_title_reject_reason(cutoff) == "结尾不完整"
    assert not _fallback_title_ok(cutoff)
    assert _keyword_title([{"title": cutoff}]) == ""
    bare = "Cybercab扩容提速"
    assert _llm_title_reject_reason(bare) == "缺少主体"
    assert not _fallback_title_ok(bare)
    assert _keyword_title([{"title": bare}]) == ""
    source = "特斯拉发布Cybercab扩容提速"
    event = {"name": bare, "headline": source, "headlines": [source]}
    _ensure_shown_name(event)
    assert event["name"] == source
    assert _keyword_title([{"title": "美联储降息25基点"}]) == "美联储降息25基点"
    kept = _keyword_title([{"title": "国务院研究称将出台增量政策"}])
    assert kept.startswith("国务院") and "研究称" in kept


def test_same_company_and_action_merge_even_when_the_lead_differs(tmp_path):
    """东百收购和哈药立案曾各占两条：动词前不是公司名，分桶对不上。

    标题里的集团、股份才是主体。同一公司、同一个动作、6 小时内并成一件。
    动作不同，或超出 6 小时，仍然分开。分桶结果要和两两比较一致。
    """
    from app.news.hot_events import (
        _block_keys,
        _blocked_pairs,
        _can_merge,
        _doc,
        _listed_companies,
        _prepare_merge_doc,
        build_top_hot_events,
    )

    assert _listed_companies("上交所向东百集团下发监管工作函") == ["东百集团"]
    assert _listed_companies("东百集团：拟3.16亿元收购快灵光电股权") == ["东百集团"]
    assert "哈药股份" in _listed_companies("哈药股份：公司董事兼总裁被立案、实施留置")
    assert _listed_companies("优必选与建发股份回购股份") == ["建发股份"]

    reset_store_for_tests(tmp_path / "same-company.sqlite")
    friday = date(2026, 10, 9)
    bodies = {
        "dongbai-buy": (
            "东百集团：拟3.16亿元收购快灵光电股权",
            "东百集团公告，公司全资子公司拟收购杭州快灵光电股权，暂定对价3.16亿元。",
            10, 5,
        ),
        "dongbai-wire": (
            "东百集团：全资子公司拟收购光探测芯片公司快灵光电",
            "东百集团公告，全资子公司东百创智拟收购快灵光电52.67%股权。",
            10, 20,
        ),
        "dongbai-letter": (
            "上交所向东百集团下发监管工作函",
            "上交所向东百集团下发关于福建东百集团股份有限公司收购资产事项的监管工作函。",
            11, 30,
        ),
        "dongbai-buyback": (
            "东百集团拟回购股份",
            "东百集团拟回购股份。",
            11, 40,
        ),
        "hayao-file": (
            "哈药股份：公司董事兼总裁被立案、实施留置",
            "哈药股份公告，公司董事、总裁被立案并实施留置。",
            14, 10,
        ),
        "hayao-wire": (
            "哈药股份：公司董事兼总裁芦传有被实施留置",
            "哈药股份公告，公司于近日收到相关监察委员会关于公司董事、总裁芦传有的立案通知书和留置通知书。",
            14, 40,
        ),
        "huawei": (
            "华为发布盘古新模型",
            "华为发布盘古新模型。",
            9, 0,
        ),
    }
    for source_id, (title, text, hour, minute) in bodies.items():
        status = get_store().insert_item(
            source="cls" if not source_id.endswith("wire") else "wscn",
            source_id=source_id,
            published_at=_publish(friday, hour, minute),
            author="",
            title=title,
            clean_text=text,
            raw=None,
            content_hash=source_id,
            url="",
            level="",
            media_ids=[],
            extra=None,
            mentions=[],
        )
        assert status == "inserted"

    def prepared(source_id: str) -> dict:
        title, text, hour, minute = bodies[source_id]
        doc = _doc({
            "id": source_id,
            "source": "cls",
            "title": title,
            "clean_text": text,
            "published": _publish(friday, hour, minute),
            "mentions": [],
            "level": "",
        })
        assert doc is not None
        _prepare_merge_doc(doc)
        return doc

    buy = prepared("dongbai-buy")
    letter = prepared("dongbai-letter")
    assert buy["action"] == letter["action"] == "收购"
    assert _can_merge(buy, letter)
    shared = set(_block_keys(buy)) & set(_block_keys(letter))
    assert any(key.startswith("s:收购:东百") for key in shared)
    hayao_a = prepared("hayao-file")
    hayao_b = prepared("hayao-wire")
    assert hayao_a["action"] == hayao_b["action"] == "立案"
    assert _can_merge(hayao_a, hayao_b)
    assert set(_block_keys(hayao_a)) & set(_block_keys(hayao_b))
    buyback = prepared("dongbai-buyback")
    assert not _can_merge(buy, buyback)
    late = prepared("dongbai-buy")
    late["published"] = _publish(friday, 18, 30)
    assert not _can_merge(buy, late)

    docs = [buy, letter, hayao_a, hayao_b]
    for doc in docs:
        _prepare_merge_doc(doc)
    pairs = set(_blocked_pairs(docs))
    assert (0, 1) in pairs
    assert (2, 3) in pairs

    store = get_store()

    def signature(snapshot: dict) -> list[tuple]:
        return [
            (event["name"], event["mentions"], event["key"], tuple(event.get("headlines") or []))
            for event in snapshot["events"]
        ]

    naive = build_top_hot_events(NOW, store, refresh=True, blocked=False)
    fast = build_top_hot_events(NOW, store, refresh=True, blocked=True)
    assert signature(fast) == signature(naive)
    events = fast["events"]
    dongbai = [event for event in events if "东百" in event["name"] and "回购" not in event["name"]]
    assert len(dongbai) == 1
    assert dongbai[0]["mentions"] >= 2
    assert any("收购" in title or "快灵" in title for title in dongbai[0]["headlines"])
    assert sum(1 for event in events if "回购" in event["name"] and "东百" in event["name"]) == 1
    hayao = [event for event in events if "哈药" in event["name"]]
    assert len(hayao) == 1
    assert hayao[0]["mentions"] >= 2
    huawei = [event for event in events if "盘古" in event["name"]]
    assert len(huawei) == 1
    assert huawei[0]["mentions"] == 1


def test_same_party_merges_across_the_trading_day(tmp_path):
    """同一组双方、同一动作，00:13 和 08:39 并成一件。别的合并仍看 6 小时。"""
    reset_store_for_tests(tmp_path / "party-day.sqlite")
    _insert_at("seres-night", "赛力斯与宁德时代签署长期战略合作协议", 0, 13)
    _insert_at("seres-morning", "宁德时代与赛力斯签署长期战略合作协议", 8, 39, source="wscn")
    _insert_at("seres-buyback", "赛力斯与宁德时代回购股份", 8, 40)
    _insert_at("huawei-night", "华为发布盘古新模型", 0, 13)
    _insert_at("huawei-morning", "华为发布盘古新模型", 8, 39, source="wscn")
    events = top_hot_events(NOW)["events"]
    seres = [event for event in events if "赛力斯" in event["name"] and "宁德时代" in event["name"]]
    signing = [event for event in seres if "回购" not in event["name"]]
    assert len(signing) == 1
    assert signing[0]["mentions"] == 2
    assert sum(1 for event in events if "回购" in event["name"]) == 1
    huawei = [event for event in events if "盘古" in event["name"]]
    assert len(huawei) == 2


_APPLE_CUT = "大摩——苹果公司(AAPL.US)：已经开始削减iPhone零部件订单"
_TESLA_PT = "杰富瑞将特斯拉目标价从350美元上调至400美元"


def test_negative_actions_are_bearish_and_cluster_concepts_stay_on_the_title(tmp_path, monkeypatch):
    """削减订单是利空；同簇或模型带上的石油/油服、海外市场不能贴到特斯拉目标价。"""
    assert _direction_for(_APPLE_CUT, "订单") == "利空"
    assert _direction_for(_TESLA_PT, "上调") == "利好"
    for title, action in (
        ("主力砸单离场", "砸单"),
        ("公司取消年度订单", "订单"),
        ("股价暴跌", "暴跌"),
        ("OPEC宣布减产", "宣布"),
        ("光伏组件厂下调报价", "下调"),
        ("碳酸锂厂商宣布砸价", "宣布"),
    ):
        assert _direction_for(title, action) == "利空"
    assert _direction_for("英伟达追加订单传闻", "追加") == "利好"
    smash = "碳酸锂厂商宣布砸价，利好下游电池"
    assert _direction_for(smash, "宣布") == "利空"
    assert _explicit_target_direction("电池", smash) == "利好"
    assert _explicit_target_direction("碳酸锂", smash) == ""
    assert _explicit_target_direction("电池", "碳酸锂厂商宣布砸价") == ""

    reset_store_for_tests(tmp_path / "direction-tags.sqlite")
    _insert_rich(
        "apple",
        _APPLE_CUT,
        9,
        10,
        sectors=["苹果产业链", "石油", "油服"],
    )
    _insert_rich(
        "tesla",
        _TESLA_PT,
        9,
        20,
        sectors=["特斯拉", "石油", "油服"],
        stocks=[("TSLA", "特斯拉")],
    )
    _insert_rich(
        "tesla-oil",
        "特斯拉产业链跟随原油价格上调",
        9,
        30,
        sectors=["石油", "油服"],
    )
    _insert_rich(
        "smash",
        smash,
        9,
        40,
        sectors=["碳酸锂", "电池"],
    )
    _insert("cut-order", "公司取消年度订单", 45)

    events = top_hot_events(NOW)["events"]
    apple = next(event for event in events if "削减" in event["name"] or "iPhone" in event["name"])
    assert "削减" in apple["name"]
    assert apple["direction"] == "利空"
    assert "石油" not in apple["concepts"]
    assert "油服" not in apple["concepts"]
    assert all(item["direction"] == "利空" for item in apple["mapping"])

    tesla = next(event for event in events if "杰富瑞" in event["name"] or "400美元" in event["name"])
    assert "上调" in tesla["name"]
    assert tesla["direction"] == "利好"
    assert tesla["category"] != "海外市场/央行"
    assert "石油" not in tesla["concepts"]
    assert "油服" not in tesla["concepts"]
    assert "特斯拉" in tesla["concepts"] or any(
        stock["name"] == "特斯拉" for stock in tesla["mentioned_stocks"]
    )

    smashed = next(event for event in events if "砸价" in event["name"])
    assert smashed["direction"] == "利空"
    battery = next(item for item in smashed["mapping"] if item["name"] == "电池")
    lithium = next(item for item in smashed["mapping"] if item["name"] == "碳酸锂")
    assert battery["direction"] == "利好"
    assert lithium["direction"] == "利空"

    assert next(event for event in events if "取消" in event["name"])["direction"] == "利空"

    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    def wrong(prompt: str, **_kwargs: object) -> str:
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            rows.append({
                "id": head,
                "title": "补充要点",
                "concepts": ["石油", "油服"],
                "category": "海外市场/央行",
                "direction": "利好",
            })
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", wrong)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    labeled = top_hot_events(NOW + timedelta(minutes=11))["events"]
    apple = next(event for event in labeled if "削减" in event["name"] or "iPhone" in event["name"])
    tesla = next(event for event in labeled if "杰富瑞" in event["name"] or "400美元" in event["name"])
    assert apple["direction"] == "利空"
    assert "石油" not in apple["concepts"] and "油服" not in apple["concepts"]
    assert tesla["direction"] == "利好"
    assert tesla["category"] != "海外市场/央行"
    assert "石油" not in tesla["concepts"] and "油服" not in tesla["concepts"]


_SANCTION_ODDS = "全面严格制裁概率低"
_PENTAGON = "五角大楼对伊战事"
_NY_FED = "纽约联储称特朗普关税推高商品价格"
_JINDAN_HIKE = "金丹科技产品提价"
_JINDAN_RAISE = "金丹科技上调产品价格"


def test_negation_and_entity_tags_keep_live_titles(tmp_path, monkeypatch):
    """否定先于利空词；对伊、联储、公司提价和金丹的产品概念留在这条标题上。"""
    assert _direction_for(_SANCTION_ODDS, "制裁") == "利好"
    assert _direction_for("不会实施全面制裁", "制裁") == "利好"
    assert _direction_for("美方否认将扩大制裁", "制裁") == "利好"
    assert _direction_for("暂不加息", "加息") == "利好"
    assert _direction_for("未启动制裁", "制裁") == "利好"
    assert _direction_for("央行下调准备金率", "下调") == "利好"
    assert _direction_for("央行下调利率", "下调") == "利好"
    assert _direction_for("多地取消限购", "取消") == "利好"
    assert _direction_for("缓和冲突", "冲突") == "利好"
    assert _direction_for("中东冲突有所缓和", "冲突") == "利好"
    assert _direction_for("美国对华芯片制裁升级", "制裁") == "利空"
    assert _direction_for("公司取消年度订单", "订单") == "利空"
    assert _direction_for("光伏组件厂下调报价", "下调") == "利空"
    assert _direction_for("地区冲突升级", "冲突") == "利空"

    assert _category_for(_PENTAGON, "") == "地缘政治"
    assert _dictionary_concepts(_PENTAGON) == [("中东", 0)]
    assert _category_for(_NY_FED, "") == "海外市场/央行"
    assert _category_for(_JINDAN_HIKE, "提价") == "公司重大事项"
    assert _dictionary_concepts(_JINDAN_HIKE) == [("聚乳酸", 1)]
    assert _category_for(_JINDAN_RAISE, "上调") == "公司重大事项"
    assert "聚乳酸" in {name for name, _polarity in _dictionary_concepts(_JINDAN_RAISE)}
    assert _category_for("华为发布盘古新模型", "发布") == "科技与产业"
    assert _category_for("杰富瑞将特斯拉目标价从350美元上调至400美元", "上调") != "海外市场/央行"

    reset_store_for_tests(tmp_path / "negation-tags.sqlite")
    # 短句「五角大楼对伊战事」没有谓语，进不了榜。上屏标题保留这句，并带上宣布。
    _insert_rich(
        "pentagon",
        "五角大楼对伊战事宣布延长部署",
        10,
        5,
        sectors=["中东", "石油", "油服"],
    )
    _insert_rich(
        "jindan-hike",
        _JINDAN_HIKE,
        10,
        15,
        sectors=["聚乳酸", "石油"],
        stocks=[("300829.SZ", "金丹科技")],
    )
    _insert_rich(
        "jindan-raise",
        _JINDAN_RAISE,
        10,
        40,
        sectors=["石油", "油服"],
        stocks=[("300829.SZ", "金丹科技")],
    )
    events = top_hot_events(NOW)["events"]
    pentagon = next(event for event in events if "对伊战事" in event["name"])
    assert pentagon["category"] == "地缘政治"
    assert "中东" in pentagon["concepts"]
    assert "石油" not in pentagon["concepts"]
    assert "油服" not in pentagon["concepts"]
    jindan = [event for event in events if "金丹" in event["name"] or any("金丹" in title for title in event["headlines"])]
    assert len(jindan) == 2
    for event in jindan:
        assert event["category"] == "公司重大事项"
        assert event["direction"] == "利好"
        assert "聚乳酸" in event["concepts"]
        assert "石油" not in event["concepts"]
        assert "油服" not in event["concepts"]

    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    def wrong(prompt: str, **_kwargs: object) -> str:
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            rows.append({
                "id": head,
                "title": "补充要点",
                "concepts": ["石油", "油服"],
                "category": "科技与产业",
                "direction": "利空",
            })
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", wrong)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    labeled = top_hot_events(NOW + timedelta(minutes=11))["events"]
    pentagon = next(event for event in labeled if "对伊战事" in event["name"])
    assert pentagon["category"] == "地缘政治"
    assert "中东" in pentagon["concepts"]
    assert "石油" not in pentagon["concepts"]
    jindan = [event for event in labeled if "金丹" in event["name"]]
    assert len(jindan) == 2
    for event in jindan:
        assert event["category"] == "公司重大事项"
        assert "聚乳酸" in event["concepts"]
        assert "石油" not in event["concepts"]


def test_dictionary_carries_polarity_per_concept():
    """同一个词典键下的概念方向可以相反，极性必须逐条还原。

    `(("美联储","加息"), ("黄金","银行"))`：加息压黄金、抬银行息差。原实现
    返回裸概念名，把方向丢了，展示层只能按事件整体方向给两个概念贴同一个标签
    —— 其中必有一个是反的（线上实测：加息事件里黄金被标成利好）。
    """
    from app.news.hot_events import _concept_polarity_map, _polarity_direction

    hike = _concept_polarity_map("美联储宣布加息25基点")
    assert hike == {"黄金": -1, "银行": 1}
    assert _polarity_direction(hike["黄金"], "利好") == "利空"
    assert _polarity_direction(hike["银行"], "利好") == "利好"
    # 没登记的条目退回事件方向，不猜。
    assert _polarity_direction(0, "利空") == "利空"
    assert _polarity_direction(None, "利好") == "利好"
    assert _polarity_direction(None, "") == ""


def test_semantic_exclusion_blocks_opposite_narratives():
    """题材不能挂在语义相反的事件上。

    线上实测：「上交所向东百集团下发监管工作函」被挂上东百集团的收购题材
    （光芯片收购/光探测芯片）。收购是利好叙事，监管函是利空动作，挂在一起
    等于用利好题材解释利空事件。
    """
    from app.news.hot_events import _semantically_excluded

    # 监管动作否决收购类题材。
    assert _semantically_excluded("光芯片收购", "上交所向某公司下发监管工作函")
    assert _semantically_excluded("资产重组", "公司收到证监会立案告知书")
    assert _semantically_excluded("借壳上市", "交易所下发问询函")
    # 题材与事件同向时不拦。
    assert not _semantically_excluded("光芯片收购", "公司宣布溢价收购标的资产")
    # 降价否决涨价题材；中标不被订单题材拦住。
    assert _semantically_excluded("产品涨价", "公司宣布主力产品降价")
    assert not _semantically_excluded("轨道交通订单", "公司中标地铁项目")
    # 关键词不在概念名里就不参与判定。
    assert not _semantically_excluded("商业零售", "上交所下发监管工作函")


def test_semantic_exclusion_applies_on_both_concept_paths():
    """互斥判定必须同时挂在 `_concept_relevant` 和 `_merge_mapped_concepts`。

    只挂一边会漏：`_concept_relevant` 管「细概念延伸标题」那条路径，
    `_merge_mapped_concepts` 管词典路径 —— 监管函挂收购题材正是经词典路径
    进来的，词典不受前者约束。
    """
    from app.news.hot_events import _merge_mapped_concepts

    blob = "上交所向东百集团下发监管工作函"
    assert _concept_relevant("光芯片收购", blob, set()) is False
    assert _merge_mapped_concepts(["光芯片收购", "商业零售"], blob) == ["商业零售"]
