"""资讯清洗、打分、抽取、入库和宿主机只读约束。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.market_time import CN_TZ
from app.news.cleaning import clean_text, content_hash
from app.news.cls_sign import CLS_BASE_PARAMS, sign_query
from app.news.collectors import (
    Item,
    latest_date_folders,
    load_inbox_payload,
    parse_dws_payload,
    parse_zsxq_payload,
)
from app.news.extract import Lexicon, StructuredStock, canonical_symbol
from app.news.host_collector import CommandRejectedError, assert_readonly
from app.news.scoring import MentionEvent, score_candidates
from app.news.service import (
    _ima_post,
    collect_inbox,
    feed_for_source,
    get_store,
    ingest_items,
    reset_store_for_tests,
)
from app.services.api_gateway import required_scope


def test_cleaning_phrases_and_hashtags():
    assert clean_text("投ziji金和投zi") == "投资基金和投资"
    assert "股份" in clean_text("公司谷份回购")
    assert clean_text("中谷物流和谷歌、谷物") == "中谷物流和谷歌、谷物"
    raw = '<e type="hashtag" hid="1" title="%23%E7%BA%B3%E6%8C%87%23" />正文'
    assert clean_text(raw) == "正文"
    assert "纳指" not in clean_text(raw)


def test_content_hash_cross_source_and_images():
    body = "同一条足够长的财经正文用来跨源去重"
    assert content_hash(body) == content_hash(body)
    assert content_hash("图片") == content_hash("图片")
    assert content_hash("", ["media-a"]) != content_hash("", ["media-b"])
    assert content_hash("短", ["media-a"]) != content_hash("短", ["media-b"])


def test_cls_sign_matches_rsshub_vector():
    assert sign_query(CLS_BASE_PARAMS) == "10bdc2da403ad7415bb639aa22dc6cd3"


def test_scoring_dedupe_cross_source_and_spam():
    now = datetime(2026, 10, 9, 15, 0)
    same = [
        MentionEvent("stock", "600519.SH", "贵州茅台", "cls", "h1", now - timedelta(hours=1)),
        MentionEvent("stock", "600519.SH", "贵州茅台", "cls", "h1", now - timedelta(minutes=20)),
    ]
    once = score_candidates(same, now=now)[0]
    assert once.story_count == 1
    assert once.effective_mentions == 1

    cross = score_candidates([
        MentionEvent("stock", "600519.SH", "贵州茅台", "cls", "h1", now - timedelta(hours=1)),
        MentionEvent("stock", "600519.SH", "贵州茅台", "wscn", "h1", now - timedelta(hours=1)),
    ], now=now)[0]
    assert cross.story_count == 1
    assert cross.sources == ("cls", "wscn")
    assert cross.score > once.score

    spam = [
        MentionEvent("sector", "白酒", "白酒", "cls", f"s{i}", now - timedelta(minutes=i))
        for i in range(5)
    ]
    dampened = score_candidates(spam, now=now)[0]
    assert dampened.story_count == 5
    assert dampened.effective_mentions < 3

    grown = score_candidates([
        MentionEvent("sector", "半导体", "半导体", "cls", "n1", now - timedelta(hours=1)),
        MentionEvent("sector", "半导体", "半导体", "cls", "old", now - timedelta(days=2)),
    ], now=now, baseline_days=4)[0]
    assert grown.baseline_effective > 0
    assert grown.growth > 0


def test_extract_codes_names_and_ambiguous():
    lexicon = Lexicon(
        [
            ("600519.SH", "贵州茅台", "600519"),
            ("603565.SH", "中谷物流", "603565"),
            ("300037.SZ", "新宙邦", "300037"),
            ("000001.SZ", "平安银行", "000001"),
            ("601318.SH", "平安银行", "601318"),
            ("600000.SH", "浦发银行", "600000"),
        ],
        ["半导体", "半导体设备"],
    )
    mentions = lexicon.extract("关注 sz300037 与贵州茅台，板块是半导体设备")
    keys = {item.key for item in mentions}
    assert "300037.SZ" in keys
    assert "600519.SH" in keys
    assert "半导体设备" in keys
    assert "半导体" not in keys
    ambiguous = lexicon.extract("平安银行今天公告")
    assert not any(item.origin == "text" for item in ambiguous)
    structured = lexicon.extract("", [StructuredStock(code="sz300037")])
    assert structured[0].key == "300037.SZ"
    assert canonical_symbol("201234") == ""
    assert not any(item.code.startswith("20") for item in lexicon.extract("代码 201234 无标的"))


def test_store_insert_duplicate_and_feed(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    lexicon = Lexicon([("600519.SH", "贵州茅台", "600519")], ["白酒"])
    when = datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ)
    text = "贵州茅台在白酒板块的投资说明，正文足够长"
    first = Item(source="cls", source_id="1", published_at=when, text=text, title="茅台")
    other = Item(source="wscn", source_id="9", published_at=when, text=text, title="茅台")
    again = Item(source="cls", source_id="1", published_at=when, text=text, title="茅台")
    assert ingest_items([first], lexicon)["inserted"] == 1
    assert ingest_items([other], lexicon)["inserted"] == 1
    assert ingest_items([again], lexicon)["duplicate"] == 1
    feed = feed_for_source("cls", limit=10)
    assert feed["items"][0]["symbols"] == ["600519.SH"]
    assert "白酒" in feed["items"][0]["sectors"]
    assert "raw" not in feed["items"][0]


def test_inbox_parse_and_error_source(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    source, payload = load_inbox_payload(
        '{"source":"dws","payload":{"messages":[{"messageId":"m1","text":"投zi机会","createTime":"2026-10-09T10:00:00+08:00"}]}}'
    )
    assert source == "dws"
    items = parse_dws_payload(payload)
    assert items[0].text.startswith("投zi")
    topics, page = parse_zsxq_payload([
        {
            "topic_id": "t1",
            "content": '<e type="hashtag" title="%23x%23" />纳指调研',
            "create_time": "2026-10-09T11:00:00+08:00",
            "owner": {"name": "作者"},
        }
    ])
    assert page["has_more"] is False
    assert topics[0].source_id == "t1"
    folders = latest_date_folders([
        {"name": "2026-10-8", "folder_id": "new"},
        {"name": "20261007", "folder_id": "old"},
        {"name": "杂项", "folder_id": "misc"},
    ], 2)
    assert [row["folder_id"] for row in folders] == ["new", "old"]

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "zsxq-1.json").write_text("{", encoding="utf-8")
    collect_inbox(inbox)
    rows = {row["source"]: row for row in get_store().health_rows()}
    assert "zsxq" in rows
    assert "dws" not in rows


def test_ima_rate_limit_and_unreadable(monkeypatch):
    monkeypatch.setattr("app.news.service.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    class Response:
        def __init__(self, code: int):
            self.code = code

        def raise_for_status(self):
            return None

        def json(self):
            if self.code == 0:
                return {"retcode": 0, "data": {"knowledge_list": []}}
            return {"retcode": self.code}

    class Client:
        def post(self, *_args, **_kwargs):
            calls["n"] += 1
            return Response(110021 if calls["n"] == 1 else 0)

    payload = _ima_post(Client(), {}, "get_knowledge_list", {})
    assert payload["retcode"] == 0
    assert calls["n"] == 2

    class ContentClient:
        def post(self, *_args, **_kwargs):
            return Response(220030)

    empty = _ima_post(ContentClient(), {}, "get_knowledge", {})
    assert empty["data"] == {}


def test_host_collector_rejects_writes():
    assert_readonly(["/usr/local/bin/dws", "auth", "status"])
    assert_readonly(["zsxq-cli", "group", "+topics", "--group-id", "1", "--json"])
    with pytest.raises(CommandRejectedError):
        assert_readonly(["dws", "chat", "message", "send", "hello"])


def test_gateway_keeps_feed_closed():
    assert required_scope("GET", "/api/news/hot") == "read:analysis"
    assert required_scope("GET", "/api/news/messages") == "read:analysis"
    assert required_scope("GET", "/api/news/stocks/600519.SH") == "read:analysis"
    assert required_scope("GET", "/api/news/dsa-feed") is None
    assert required_scope("GET", "/api/news/health") is None
    assert required_scope("PUT", "/api/news/sources") is None


def _request(path: str, header: str = "", query: bytes = b"") -> Request:
    headers = []
    if header:
        headers.append((b"x-news-feed-token", header.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": headers,
        "client": ("10.1.1.8", 1234),
        "server": ("test", 80),
    }
    return Request(scope)


async def _ok(_request):
    return JSONResponse({"ok": True})


def test_dsa_feed_middleware(monkeypatch):
    monkeypatch.setattr("app.news.config.feed_token", lambda: "feed-secret")
    from app.main import auth_middleware

    allowed = asyncio.run(auth_middleware(
        _request("/api/news/dsa-feed", header="feed-secret", query=b"source=cls"),
        _ok,
    ))
    assert allowed.status_code == 200
    queried = asyncio.run(auth_middleware(
        _request("/api/news/dsa-feed", query=b"source=hot&token=feed-secret"),
        _ok,
    ))
    assert queried.status_code == 200
    denied = asyncio.run(auth_middleware(
        _request("/api/news/dsa-feed", header="nope"),
        _ok,
    ))
    assert denied.status_code == 404
