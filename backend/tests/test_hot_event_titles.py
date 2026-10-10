"""热门事件标题要是完整句子，不能是截断的半句话。"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from app.market_time import CN_TZ
from app.news.hot_events import _keyword_title, _title_ok
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

    def good(prompt: str) -> str:
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

    def bad(prompt: str) -> str:
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
    assert len(prompts) == 2

    def clipped(prompt: str) -> str:
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

    def good(prompt: str) -> str:
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
        level="",
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
