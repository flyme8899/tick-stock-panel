"""格隆汇快讯：解析、翻页停止、股票代码和来源前缀。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.market_time import CN_TZ
from app.news.collectors import (
    GLH_HEADERS,
    GLH_POLL_SECONDS,
    GLH_URL,
    Item,
    glh_error_wait,
    glh_next_live_id,
    glh_params,
    glh_symbol,
    glh_url,
    is_red,
    parse_glh,
    parse_time,
    strip_glh_prefix,
)
from app.news.config import source_configured, source_enabled, source_locked
from app.news.extract import Lexicon
from app.news.hot_events import _headline_tier
from app.news.scheduler import interval_seconds, next_delay, outbound_paused
from app.news.scoring import MentionEvent, score_candidates
from app.news.service import (
    collect_glh,
    get_store,
    health_payload,
    ingest_items,
    reset_store_for_tests,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "news" / "glh_lives.json"


def _payload() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_parse_strips_prefix_and_maps_a_share_codes():
    payload = _payload()
    items = {item.source_id: item for item in parse_glh(payload)}
    assert glh_next_live_id(payload) == "2706028"
    assert "liveId" not in glh_params()
    assert glh_params("2706028")["liveId"] == "2706028"

    a_share = items["2706040"]
    assert a_share.source == "glh"
    assert a_share.published_at == parse_time(1791780247)
    assert a_share.url == "https://www.gelonghui.com/live/2706040"
    assert a_share.level == "0"
    assert "格隆汇10月12日" not in a_share.text
    assert "格隆汇10月12日" not in a_share.title
    assert a_share.text.startswith("兆易创新入股高性能芯片公司蓝芯算力\n")
    assert "蓝芯算力是一家专注于" in a_share.text
    assert [(stock.code, stock.name, stock.origin) for stock in a_share.stocks] == [
        ("603986.SH", "兆易创新", "source"),
    ]

    sz = items["2706031"]
    assert sz.stocks[0].code == "300800.SZ"
    assert sz.stocks[0].origin == "source"
    assert sz.text.startswith("力合科技：公司GC-MS设备已用于半导体车间AMC有机物监测\n")
    assert "格隆汇" not in sz.text

    assert items["2706033"].stocks == []
    assert items["2706028"].stocks == []
    assert is_red("glh", "0") is False
    assert is_red("glh", 1) is False


def test_prefix_brackets_level_and_relative_route():
    assert strip_glh_prefix("「格隆汇10月12日｜」正文", "格隆汇10月12日｜") == "正文"
    assert strip_glh_prefix("【格隆汇10月12日｜】正文", "格隆汇10月12日｜") == "正文"
    assert glh_symbol("BJ", "920123") == "920123.BJ"
    assert glh_symbol("SH", "600000") == "600000.SH"
    assert glh_symbol("HK", "00388") == ""
    assert glh_symbol("HK", "HSI") == ""
    assert glh_symbol("US", "MPC") == ""
    assert glh_url("/live/9") == "https://www.gelonghui.com/live/9"
    row = {
        "statusCode": 200,
        "result": [{
            "id": 9,
            "title": "北交所公司发布新品",
            "content": "格隆汇10月12日｜北交所公司发布新品并披露订单。",
            "contentPrefix": "格隆汇10月12日｜",
            "createTimestamp": 1791780247,
            "level": 1,
            "route": "/live/9",
            "relatedStocks": [
                {"market": "BJ", "code": "920123", "name": "北交示例"},
                {"market": "US", "code": "AAPL", "name": "苹果"},
            ],
        }],
    }
    item = parse_glh(row)[0]
    assert item.level == "1"
    assert item.url == "https://www.gelonghui.com/live/9"
    assert item.stocks[0].code == "920123.BJ"
    assert "格隆汇10月12日" not in item.text
    with pytest.raises(ValueError, match="没有 result"):
        parse_glh({"statusCode": 200})


def test_mentions_use_source_origin_and_identical_text_dedupes(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    items = parse_glh(_payload())
    assert ingest_items(items, Lexicon([], []))["inserted"] == 4
    rows = list(get_store().mention_events_since(datetime(2020, 1, 1, tzinfo=CN_TZ)))
    found = {(row["key"], row["origin"], row["name"]) for row in rows}
    assert ("603986.SH", "source", "兆易创新") in found
    assert ("300800.SZ", "source", "力合科技") in found
    assert not any(key.endswith((".HK", ".US")) or key in {"00388", "MPC", "03986"} for key, _origin, _name in found)
    stored = {
        row["source_id"]: row["clean_text"]
        for row in get_store()._conn.execute("SELECT source_id, clean_text FROM news_items")
    }
    assert "格隆汇10月12日" not in stored["2706031"]
    assert stored["2706031"].startswith("力合科技：")

    when = datetime(2026, 10, 12, 12, 0, tzinfo=CN_TZ)
    body = "同一条足够长的财经正文用来跨源去重"
    ingest_items([
        Item(source="cls", source_id="same", published_at=when, title="回购", text=body),
        Item(source="glh", source_id="same", published_at=when, title="回购", text=body),
    ], Lexicon([("600519.SH", "贵州茅台", "600519")], []))
    hashes = list(get_store()._conn.execute(
        "SELECT source, content_hash FROM news_items WHERE source_id = 'same'",
    ))
    assert {row["source"] for row in hashes} == {"cls", "glh"}
    assert len({row["content_hash"] for row in hashes}) == 1
    scored = score_candidates([
        MentionEvent("stock", "600519.SH", "贵州茅台", row["source"], row["content_hash"], when)
        for row in hashes
    ], now=when + timedelta(minutes=5))[0]
    assert scored.story_count == 1
    assert scored.sources == ("cls", "glh")


def test_pages_back_until_a_seen_id(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    when = datetime(2026, 10, 12, 12, 0, tzinfo=CN_TZ)
    get_store().insert_item(
        source="glh", source_id="100", published_at=when, author="", title="旧快讯",
        clean_text="已经见过的格隆汇快讯正文", raw={}, content_hash="glh-100",
        url="", level="0", media_ids=[], extra={}, mentions=[],
    )

    def row(item_id, stamp):
        return {
            "id": item_id,
            "title": f"标题{item_id}",
            "content": f"格隆汇10月12日｜正文{item_id}足够区分",
            "contentPrefix": "格隆汇10月12日｜",
            "createTimestamp": stamp,
            "level": 0,
            "route": f"https://www.gelonghui.com/live/{item_id}",
            "relatedStocks": [],
        }

    calls = []

    class Client:
        def get(self, url, params=None, headers=None):
            calls.append((url, dict(params or {}), dict(headers or {})))
            live_id = (params or {}).get("liveId")
            if not live_id:
                page = [row(303, 1791780300), row(302, 1791780200)]
            elif live_id == "302":
                page = [row(301, 1791780100), row(100, 1791770000)]
            else:
                raise AssertionError(live_id)
            return _Response({"statusCode": 200, "result": page})

    result = collect_glh(Client(), now=1_000)
    assert [call[1].get("liveId") for call in calls] == [None, "302"]
    assert calls[0][0] == GLH_URL
    assert calls[0][1]["category"] == "all"
    assert calls[0][1]["limit"] == "15"
    assert calls[0][2]["User-Agent"] == GLH_HEADERS["User-Agent"]
    assert calls[0][2]["User-Agent"].startswith("Mozilla/5.0")
    assert result["inserted"] == 3
    assert result["duplicate"] == 1
    assert result["retry_after"] == GLH_POLL_SECONDS
    health = {row["source"]: row for row in get_store().health_rows()}
    assert health["glh"]["last_error"] == ""


def test_errors_back_off_and_success_resets(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    request = httpx.Request("GET", GLH_URL)

    class Client:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = 0

        def get(self, url, params=None, headers=None):
            self.calls += 1
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    down = httpx.HTTPStatusError(
        "HTTP 503",
        request=request,
        response=httpx.Response(503, request=request),
    )
    limited = httpx.HTTPStatusError(
        "HTTP 429",
        request=request,
        response=httpx.Response(429, headers={"Retry-After": "120"}, request=request),
    )
    ok = _Response({"statusCode": 200, "result": []})
    client = Client([down, limited, ok, down])
    first = collect_glh(client, now=1000)
    assert first["retry_after"] == 60
    assert collect_glh(client, now=1059)["skipped"] is True
    assert client.calls == 1
    second = collect_glh(client, now=1060)
    assert second["retry_after"] == 120
    assert "429" in second["error"]
    success = collect_glh(client, now=1180)
    assert success["retry_after"] == GLH_POLL_SECONDS
    assert "error" not in success
    again = collect_glh(client, now=1181)
    assert again["retry_after"] == 60
    assert client.calls == 4
    assert glh_error_wait(0, 30) == 60
    assert glh_error_wait(60, 120) == 120
    assert glh_error_wait(600, None) == 900


def test_switch_defaults_on_and_wire_weight_matches_cls(monkeypatch):
    monkeypatch.delenv("NEWS_GLH_ENABLED", raising=False)
    monkeypatch.setattr(settings, "news_glh_enabled", "")
    monkeypatch.setattr("app.services.preferences.load", lambda: {})
    assert source_configured("glh") is True
    assert source_enabled("glh") is True
    assert source_locked("glh") is False
    monkeypatch.setenv("NEWS_GLH_ENABLED", "false")
    assert source_enabled("glh") is False
    assert source_locked("glh") is True

    morning = datetime(2026, 10, 12, 10, 0, tzinfo=CN_TZ)
    night = datetime(2026, 10, 12, 23, 30, tzinfo=CN_TZ)
    assert interval_seconds("glh", morning) == 45
    assert interval_seconds("glh", night) == 45
    assert next_delay("glh", {"retry_after": 120}, morning) == 120
    assert next_delay("glh", {}, night) == 45
    assert next_delay("glh", {"retry_after": 99999}, morning) == 900
    headline = "兆易创新入股高性能芯片公司"
    assert _headline_tier("glh", headline) == _headline_tier("cls", headline) == 2
    assert _headline_tier("wscn", headline) == 2
    assert outbound_paused("glh") is True
    assert outbound_paused("cls") is False
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert outbound_paused("glh") is False


def test_health_lists_gelonghui(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    monkeypatch.delenv("NEWS_GLH_ENABLED", raising=False)
    monkeypatch.setattr(settings, "news_glh_enabled", "")
    monkeypatch.setattr("app.services.preferences.load", lambda: {})
    row = next(item for item in health_payload()["sources"] if item["id"] == "glh")
    assert row["label"] == "格隆汇"
    assert row["configured"] is True
    assert row["enabled"] is True


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload
