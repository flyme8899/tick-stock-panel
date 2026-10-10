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
    assert _keyword_title([
        {"title": "和之前结构性降息"},
    ]) == "事件"
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
