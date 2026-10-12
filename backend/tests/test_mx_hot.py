"""妙想热点补全：只在榜首第一次变化时请求，失败不抛给快照。"""
from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from app.market_time import CN_TZ
from app.news.extract import Lexicon
from app.news.mx_hot import (
    CACHE_VERSION,
    apply_articles,
    enrich_snapshot,
    event_query,
    search_news,
    set_enabled,
    status,
    used_today,
)
from app.news.service import public_hot_event

_NOW = datetime(2026, 10, 12, 10, 30, tzinfo=CN_TZ)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    monkeypatch.delenv("NEWS_MX_HOT_ENABLED", raising=False)
    monkeypatch.delenv("NEWS_MX_HOT_DAILY_BUDGET", raising=False)
    monkeypatch.delenv("MX_APIKEY", raising=False)
    monkeypatch.setattr("app.news.mx_hot._load_lexicon", lambda: None)
    from app.news.mx_hot import reset_for_tests

    reset_for_tests()
    return tmp_path


def _event(key: str = "ev_a", name: str = "华为发布新模型") -> dict:
    return {
        "trading_day": "2026-10-12",
        "events": [{
            "key": key,
            "name": name,
            "headline": name,
            "direction": "利好",
            "mentioned_stocks": [],
            "mapping": [],
        }],
    }


def _article(**overrides) -> dict:
    item = {
        "title": "华为发布新模型",
        "content": "补充说明",
        "date": "2026-10-12",
        "source": "新华社",
        "authority": "L1-media",
        "url": "https://example.com/a",
    }
    item.update(overrides)
    return item


def test_query_strips_source_prefix():
    assert event_query({"name": "财联社：华为发布新模型"}) == "华为发布新模型"
    assert event_query({"name": "【华尔街见闻】美联储宣布降息"}) == "美联储宣布降息"


def test_first_top_calls_same_top_does_not(isolated):
    calls = []

    def search(query: str):
        calls.append(query)
        return [_article()]

    first = _event(name="财联社：华为发布新模型")
    enrich_snapshot(first, "", searcher=search, now=_NOW)
    assert calls == ["华为发布新模型"]
    assert first["events"][0]["summary"] == "补充说明"
    assert first["events"][0]["mx_sources"][0]["source"] == "新华社"

    cache = isolated / "news" / "mx_hot" / "2026-10-12" / "ev_a.json"
    cache.unlink()
    again = _event(name="财联社：华为发布新模型")
    enrich_snapshot(again, "ev_a", searcher=search, now=_NOW)
    assert calls == ["华为发布新模型"]
    assert "summary" not in again["events"][0]

    nxt = _event("ev_b", "【华尔街见闻】美联储宣布降息")
    enrich_snapshot(nxt, "ev_a", searcher=search, now=_NOW)
    assert calls == ["华为发布新模型", "美联储宣布降息"]


def test_cache_hit_does_not_call(isolated):
    path = isolated / "news" / "mx_hot" / "2026-10-12" / "ev_a.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "v": CACHE_VERSION,
        "query": "华为发布新模型",
        "items": [_article(content="缓存摘要")],
    }, ensure_ascii=False), encoding="utf-8")
    calls = []
    snapshot = _event()
    enrich_snapshot(snapshot, "", searcher=lambda query: calls.append(query), now=_NOW)
    assert calls == []
    assert snapshot["events"][0]["summary"] == "缓存摘要"


def test_budget_exhausted_does_not_call(isolated, monkeypatch):
    monkeypatch.setenv("MX_APIKEY", "test-mx-key")
    monkeypatch.setenv("NEWS_MX_HOT_ENABLED", "true")
    monkeypatch.setenv("NEWS_MX_HOT_DAILY_BUDGET", "1")
    calls = []

    def search(query: str):
        calls.append(query)
        return [_article(title=query, content="补充说明")]

    enrich_snapshot(_event("ev_a", "华为发布新模型"), "", searcher=search, now=_NOW)
    enrich_snapshot(_event("ev_b", "美联储宣布降息"), "ev_a", searcher=search, now=_NOW)
    assert calls == ["华为发布新模型"]
    assert used_today(now=_NOW) == 1

    monkeypatch.setenv("NEWS_MX_HOT_DAILY_BUDGET", "25")
    root = isolated / "news" / "mx_hot"
    (root / "budget.json").write_text(
        json.dumps({"day": "2026-10-12", "used": 25}),
        encoding="utf-8",
    )
    enrich_snapshot(_event("ev_c", "金丹科技产品提价"), "ev_b", searcher=search, now=_NOW)
    assert calls == ["华为发布新模型"]


def test_switch_off_does_not_call(isolated, monkeypatch):
    monkeypatch.setenv("NEWS_MX_HOT_ENABLED", "false")
    monkeypatch.setenv("MX_APIKEY", "test-mx-key")
    calls = []
    enrich_snapshot(
        _event(),
        "",
        searcher=lambda query: calls.append(query) or [_article()],
        now=_NOW,
    )
    assert calls == []
    assert used_today(now=_NOW) == 0


def test_fund_flow_quota_does_not_block(isolated, monkeypatch):
    monkeypatch.setenv("FUND_FLOW_MX_MAX_CALLS", "0")
    monkeypatch.setenv("MX_APIKEY", "test-mx-key")
    monkeypatch.setenv("NEWS_MX_HOT_ENABLED", "true")
    calls = []
    enrich_snapshot(
        _event(),
        "",
        searcher=lambda query: calls.append(query) or [_article()],
        now=_NOW,
    )
    assert calls == ["华为发布新模型"]


def test_errors_are_silent(isolated, monkeypatch):
    monkeypatch.setenv("MX_APIKEY", "test-mx-key")
    monkeypatch.setenv("NEWS_MX_HOT_ENABLED", "true")
    snapshot = _event()

    def boom(query: str):
        raise RuntimeError("down")

    enrich_snapshot(snapshot, "", searcher=boom, now=_NOW)
    assert "summary" not in snapshot["events"][0]
    assert not (isolated / "news" / "mx_hot" / "2026-10-12" / "ev_a.json").exists()

    def post(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("app.news.mx_hot.httpx.post", post)
    assert search_news("华为发布新模型") is None

    class Denied:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr("app.news.mx_hot.httpx.post", lambda *args, **kwargs: Denied())
    assert search_news("华为发布新模型") is None

    class Bad:
        status_code = 200

        def json(self):
            return {"data": {}}

    monkeypatch.setattr("app.news.mx_hot.httpx.post", lambda *args, **kwargs: Bad())
    assert search_news("华为发布新模型") is None


def test_empty_result_is_cached_without_changing_event(isolated, monkeypatch):
    monkeypatch.setenv("MX_APIKEY", "test-mx-key")
    monkeypatch.setenv("NEWS_MX_HOT_ENABLED", "true")
    calls = []

    def search(query: str):
        calls.append(query)
        return []

    snapshot = _event()
    enrich_snapshot(snapshot, "", searcher=search, now=_NOW)
    assert calls == ["华为发布新模型"]
    assert "summary" not in snapshot["events"][0]
    cache = isolated / "news" / "mx_hot" / "2026-10-12" / "ev_a.json"
    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert stored["v"] == CACHE_VERSION
    assert stored["items"] == []

    enrich_snapshot(_event(), "ev_other", searcher=search, now=_NOW)
    assert calls == ["华为发布新模型"]


def test_http_parses_news_search_payload(monkeypatch):
    monkeypatch.setenv("MX_APIKEY", "test-mx-key")
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "data": {
                    "data": {
                        "llmSearchResponse": {
                            "data": [{
                                "title": "华为发布新模型",
                                "content": "权威细节",
                                "date": "2026-10-12",
                                "source": "新华社",
                                "authorityLevel": "L1-media",
                                "jumpUrl": "https://example.com/a",
                            }],
                        },
                    },
                },
            }

    def post(url, **kwargs):
        seen["url"] = url
        seen["apikey"] = kwargs["headers"]["apikey"]
        seen["body"] = kwargs["json"]
        seen["timeout"] = kwargs["timeout"]
        return Response()

    monkeypatch.setattr("app.news.mx_hot.httpx.post", post)
    items = search_news("华为发布新模型")
    assert seen["url"] == "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search"
    assert seen["apikey"] == "test-mx-key"
    assert seen["body"] == {"query": "华为发布新模型"}
    assert items is not None
    assert items[0]["authority"] == "L1-media"
    assert items[0]["url"] == "https://example.com/a"
    assert items[0]["content"] == "权威细节"


def test_cut_orders_override_and_negation_does_not():
    lexicon = Lexicon([("002475.SZ", "立讯精密", "002475")])
    event = {
        "key": "ev_apple",
        "name": "苹果削减零部件订单",
        "headline": "苹果削减零部件订单",
        "direction": "利好",
        "mapping": [{"name": "苹果产业链", "kind": "concept", "direction": "利好"}],
        "mentioned_stocks": [{
            "key": "AAPL",
            "name": "苹果",
            "mentions": 1,
            "direction": "利好",
        }],
    }
    apply_articles(event, [_article(
        title="苹果削减零部件订单，利空立讯精密",
        content="公司已经开始削减订单",
        url="https://example.com/cut",
    )], lexicon=lexicon, now=_NOW)
    assert event["direction"] == "利空"
    assert event["mentioned_stocks"][0]["direction"] == "利空"
    added = event["mentioned_stocks"][1]
    assert added["name"] == "立讯精密"
    assert added["key"] == "002475.SZ"
    assert added["direction"] == "利空"

    negated = {
        "key": "ev_apple",
        "name": "苹果削减零部件订单",
        "headline": "苹果削减零部件订单",
        "direction": "利好",
        "mapping": [],
        "mentioned_stocks": [{
            "key": "AAPL",
            "name": "苹果",
            "mentions": 1,
            "direction": "利好",
        }],
    }
    apply_articles(negated, [_article(
        title="苹果不会削减零部件订单",
        content="公司否认将削减订单",
    )], lexicon=Lexicon([]), now=_NOW)
    assert negated["direction"] == "利好"
    assert negated["mentioned_stocks"][0]["direction"] == "利好"
    assert len(negated["mentioned_stocks"]) == 1


def test_unrelated_or_lower_authority_does_not_retag():
    event = {
        "key": "ev_hw",
        "name": "华为发布新模型",
        "headline": "华为发布新模型",
        "direction": "利好",
        "mentioned_stocks": [],
        "mapping": [],
    }
    apply_articles(
        event,
        [_article(title="股价暴跌", content="市场大跌")],
        lexicon=Lexicon([]),
        now=_NOW,
    )
    assert event["direction"] == "利好"

    mixed = {
        "key": "ev_hw",
        "name": "华为发布新模型",
        "headline": "华为发布新模型",
        "direction": "利好",
        "mentioned_stocks": [],
        "mapping": [],
    }
    apply_articles(mixed, [
        _article(
            title="华为发布新模型已经开始削减订单",
            content="低权威说削减",
            authority="L2-web",
            source="博客",
        ),
        _article(title="华为发布新模型", content="新华社确认发布进展", source="新华社"),
    ], lexicon=Lexicon([]), now=_NOW)
    assert mixed["direction"] == "利好"
    assert mixed["summary"] == "新华社确认发布进展"
    assert mixed["mx_sources"][0]["authority"] == "L1-media"
    assert mixed["mx_sources"][0]["source"] == "新华社"


def test_only_l2_enriches_text_but_not_direction():
    event = {
        "key": "ev_hw",
        "name": "华为发布新模型",
        "headline": "华为发布新模型",
        "direction": "利好",
        "mentioned_stocks": [],
        "mapping": [],
    }
    apply_articles(event, [_article(
        title="华为发布新模型已经开始削减订单",
        content="低权威说削减",
        authority="L2-web",
        source="博客",
    )], lexicon=Lexicon([]), now=_NOW)
    assert event["direction"] == "利好"
    assert event["summary"] == "低权威说削减"
    assert event["mx_sources"][0]["authority"] == "L2-web"


def test_old_high_authority_does_not_crowd_out_same_day():
    event = {
        "key": "ev_us",
        "name": "美股三大指数集体收涨",
        "headline": "美股三大指数集体收涨",
        "direction": "利好",
        "mentioned_stocks": [],
        "mapping": [],
    }
    apply_articles(event, [
        _article(
            title="美股三大指数集体收涨",
            content="八月证券时报",
            date="2026-08-20",
            authority="L1-1",
            source="证券时报网",
            url="",
        ),
        _article(
            title="美股三大指数集体收涨",
            content="九月证券时报",
            date="2026-09-02",
            authority="L1-1",
            source="证券时报网",
            url="",
        ),
        _article(
            title="美股三大指数集体收涨",
            content="超出三天窗口",
            date="2026-10-09",
            authority="L1-1",
            source="证券时报网",
            url="https://example.com/1009",
        ),
        _article(
            title="美股三大指数集体收涨",
            content="月日旧稿",
            date="08-09",
            authority="L1-1",
            source="证券时报网",
            url="https://example.com/0809",
        ),
        _article(
            title="美股三大指数集体收涨",
            content="隔夜稿",
            date="2026-10-10",
            authority="L2-web",
            source="隔夜来源",
            url="https://example.com/1010",
        ),
        _article(
            title="美股三大指数集体收涨",
            content="昨日稿",
            date="2026-10-11",
            authority="L1-media",
            source="昨日来源",
            url="https://example.com/1011",
        ),
        _article(
            title="美股三大指数集体收涨",
            content="当日无链接",
            date="2026-10-12",
            authority="L1-1",
            source="新华社",
            url="",
        ),
        _article(
            title="美股三大指数集体收涨",
            content="当日有链接",
            date="2026-10-12 08:30:00",
            authority="L2-web",
            source="界面新闻",
            url="https://example.com/1012",
        ),
    ], now=_NOW)
    assert event["summary"] == "当日无链接"
    assert [item["source"] for item in event["mx_sources"]] == ["界面新闻", "昨日来源", "隔夜来源"]
    assert [item["url"] for item in event["mx_sources"]] == [
        "https://example.com/1012",
        "https://example.com/1011",
        "https://example.com/1010",
    ]
    assert event["direction"] == "利好"


def test_old_articles_do_not_change_direction():
    def fresh_event() -> dict:
        return {
            "key": "ev_apple",
            "name": "苹果削减零部件订单",
            "headline": "苹果削减零部件订单",
            "direction": "利好",
            "mapping": [],
            "mentioned_stocks": [],
        }

    article = dict(
        title="苹果削减零部件订单",
        content="公司已经开始削减订单",
        authority="L1-1",
        source="证券时报网",
        url="",
    )
    stale = fresh_event()
    apply_articles(stale, [_article(**article, date="2026-08-09")], now=_NOW)
    assert stale["direction"] == "利好"
    assert "summary" not in stale
    assert "mx_sources" not in stale

    current = fresh_event()
    apply_articles(current, [_article(**article, date="2026-10-12")], now=_NOW)
    assert current["direction"] == "利空"
    assert current["summary"] == "公司已经开始削减订单"


def test_stale_cache_is_hidden_and_not_refetched(isolated):
    path = isolated / "news" / "mx_hot" / "2026-10-12" / "ev_a.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "query": "美股三大指数集体收涨",
        "items": [
            _article(
                title="美股三大指数集体收涨",
                content="八月旧稿",
                date="2026-08-09",
                authority="L1-1",
                source="证券时报网",
                url="",
            ),
            _article(
                title="美股三大指数集体收涨",
                content="今日收涨",
                date="2026-10-12",
            ),
        ],
    }, ensure_ascii=False), encoding="utf-8")
    calls = []
    snapshot = _event(name="美股三大指数集体收涨")
    enrich_snapshot(
        snapshot,
        "ev_other",
        searcher=lambda query: calls.append(query) or [_article(content="新请求")],
        now=_NOW,
    )
    assert calls == []
    assert used_today(now=_NOW) == 0
    assert "summary" not in snapshot["events"][0]
    assert "mx_sources" not in snapshot["events"][0]
    assert path.is_file()


def test_public_event_exposes_optional_sources():
    shown = public_hot_event({
        "key": "ev",
        "name": "华为发布新模型",
        "summary": "权威摘要",
        "mx_sources": [{
            "title": "华为发布新模型",
            "source": "新华社",
            "url": "https://example.com/a",
            "authority": "L1-media",
            "date": "2026-10-12",
        }],
    })
    assert shown["summary"] == "权威摘要"
    assert shown["sources"][0]["source"] == "新华社"
    assert shown["sources"][0]["url"] == "https://example.com/a"
    blocked = public_hot_event({
        "key": "ev",
        "name": "华为发布新模型",
        "mx_sources": [{
            "title": "华为发布新模型",
            "source": "新华社",
            "url": "javascript:alert(1)",
            "authority": "L1-media",
        }],
    })
    assert blocked["sources"][0]["url"] == ""
    plain = public_hot_event({"key": "ev", "name": "华为发布新模型"})
    assert "summary" not in plain
    assert "sources" not in plain


def test_page_toggle_and_env_lock(isolated, monkeypatch):
    assert status()["enabled"] is True
    assert status()["locked"] is False
    assert set_enabled(False)["enabled"] is False
    assert status()["enabled"] is False

    monkeypatch.setenv("NEWS_MX_HOT_ENABLED", "true")
    assert status()["enabled"] is True
    assert status()["locked"] is True
    assert set_enabled(False)["enabled"] is True

    monkeypatch.setenv("NEWS_MX_HOT_ENABLED", "false")
    snapshot = _event()
    calls = []
    enrich_snapshot(snapshot, "", searcher=lambda query: calls.append(query), now=_NOW)
    assert calls == []


def test_refresh_passes_previous_top(monkeypatch):
    from app.news import hot_snapshot as snap

    snap.clear_hot_snapshot_for_tests()
    snap.publish_hot_snapshot(
        {"events": [{"key": "ev_old", "name": "旧事件"}]},
        persist=False,
    )
    seen = {}

    def build(now, store, **kwargs):
        del now, store, kwargs
        return {
            "events": [{"key": "ev_new", "name": "新事件"}],
            "trading_day": "2026-10-12",
        }

    def enrich(snapshot, previous_top_key="", **kwargs):
        del kwargs
        seen["previous"] = previous_top_key
        seen["key"] = snapshot["events"][0]["key"]

    monkeypatch.setattr("app.news.hot_events.build_top_hot_events", build)
    monkeypatch.setattr("app.news.service.get_store", lambda: object())
    monkeypatch.setattr("app.news.mx_hot.enrich_snapshot", enrich)
    monkeypatch.setattr(snap, "_write_disk", lambda payload: None)
    try:
        snap.refresh_hot_events_sync()
        assert seen == {"previous": "ev_old", "key": "ev_new"}
    finally:
        snap.clear_hot_snapshot_for_tests()
