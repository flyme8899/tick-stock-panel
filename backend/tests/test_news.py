"""资讯清洗、打分、抽取、入库和宿主机只读约束。"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.market_time import CN_TZ, cn_now
from app.news.cleaning import clean_text, content_hash
from app.news.cls_sign import CLS_BASE_PARAMS, sign_query
from app.news.collectors import (
    Item,
    ima_next_cursor,
    latest_date_folders,
    load_inbox_payload,
    parse_dws_payload,
    parse_zsxq_payload,
    pick_knowledge_base,
    split_ima_list,
)
from app.news.config import feed_matches, group_id, source_configured
from app.news.extract import Lexicon, StructuredStock, canonical_symbol
from app.news.host_collector import CommandRejectedError, assert_readonly, run_host
from app.news.scoring import MentionEvent, score_candidates
from app.news.service import (
    _ima_post,
    collect_ima,
    collect_inbox,
    feed_for_source,
    get_store,
    hot_candidates,
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

    light = score_candidates([
        MentionEvent("stock", "300033.SZ", "同花顺", "cls", "a", now - timedelta(hours=1), 0.5),
    ], now=now)[0]
    full = score_candidates([
        MentionEvent("stock", "300033.SZ", "同花顺", "cls", "a", now - timedelta(hours=1), 1.0),
    ], now=now)[0]
    assert light.effective_mentions == 0.5
    assert light.score < full.score


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


def test_extract_rejects_bare_numbers_and_attributions():
    lexicon = Lexicon(
        [
            ("300000.SZ", "测试股份", "300000"),
            ("300037.SZ", "新宙邦", "300037"),
            ("300033.SZ", "同花顺", "300033"),
            ("300059.SZ", "东方财富", "300059"),
            ("600030.SH", "中信证券", "600030"),
            ("300024.SZ", "机器人", "300024"),
            ("000591.SZ", "太阳能", "000591"),
            ("000061.SZ", "农产品", "000061"),
        ],
    )
    bare = lexicon.extract("公司回购 300000 股，进口 500000 吨，另有 300037")
    assert not any(item.kind == "stock" for item in bare)
    marked = lexicon.extract("关注 sz300037、(300037) 与 300037新宙邦，以及 300037.SZ")
    assert {item.key for item in marked if item.kind == "stock"} == {"300037.SZ"}
    attributed = lexicon.extract("据同花顺数据，东方财富Choice显示，中信证券研报称市场回暖")
    assert not any(item.origin == "text-name" for item in attributed)
    common = lexicon.extract("机器人概念升温，太阳能装机，农产品价格波动")
    assert not any(item.origin == "text-name" for item in common)
    tagged = lexicon.extract("", [StructuredStock(code="300033", name="同花顺")])
    assert tagged[0].key == "300033.SZ"
    assert tagged[0].origin == "structured"


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


def test_duplicate_skips_extract_and_llm(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    calls = {"extract": 0, "llm": 0}
    original = Lexicon.extract

    def counting(self, *args, **kwargs):
        calls["extract"] += 1
        return original(self, *args, **kwargs)

    def llm(*_args, **_kwargs):
        calls["llm"] += 1
        return []

    monkeypatch.setattr(Lexicon, "extract", counting)
    monkeypatch.setattr("app.news.service._llm_mentions", llm)
    monkeypatch.setattr("app.news.service.llm_extract_enabled", lambda: True)
    when = datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ)
    text = "这条正文没有词典里的股票名，长度超过四十个字，用来确认重复条目不会再次抽取，也不会再调用模型。"
    item = Item(source="cls", source_id="dup-1", published_at=when, text=text, title="重复")
    lexicon = Lexicon([], [])
    first = ingest_items([item, item], lexicon)
    assert first == {"inserted": 1, "duplicate": 1}
    assert calls == {"extract": 1, "llm": 1}
    again = ingest_items([item], lexicon)
    assert again["duplicate"] == 1
    assert calls == {"extract": 1, "llm": 1}


def test_text_name_weighs_less_than_structured_tag(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    lexicon = Lexicon([("600519.SH", "贵州茅台", "600519")], [])
    when = cn_now()
    ingest_items([
        Item(
            source="cls",
            source_id="name-1",
            published_at=when,
            text="贵州茅台相关说明，正文写长一点以免被当成短讯",
            title="茅台",
        ),
    ], lexicon)
    named = hot_candidates(kind="stock", window_hours=24, baseline_days=1)
    assert named[0].key == "600519.SH"
    assert named[0].effective_mentions == 0.5
    reset_store_for_tests(tmp_path / "news-structured.sqlite")
    ingest_items([
        Item(
            source="cls",
            source_id="tag-1",
            published_at=when,
            text="这条公告没有出现股票简称，只靠来源自带的标签。",
            title="公告",
            stocks=[StructuredStock(code="600519", name="贵州茅台")],
        ),
    ], lexicon)
    tagged = hot_candidates(kind="stock", window_hours=24, baseline_days=1)
    assert tagged[0].effective_mentions == 1.0


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
    assert not (inbox / "zsxq-1.json").exists()
    assert (inbox / "failed" / "zsxq-1.json").is_file()


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


def test_ima_kb_fields_and_pagination(tmp_path, monkeypatch):
    found = pick_knowledge_base(
        {"data": {"info_list": [{"kb_id": "kb-9", "kb_name": "【爱分享】的财经资讯"}]}},
        "【爱分享】的财经资讯",
    )
    assert found == "kb-9"
    legacy = pick_knowledge_base({"data": {"infos": [{"id": "legacy", "name": "爱分享"}]}}, "爱分享")
    assert legacy == "legacy"
    folders, files = split_ima_list({
        "data": {
            "knowledge_list": [
                {"media_id": "folder_20261009", "title": "2026-10-09"},
                {"media_id": "m-file", "title": "一条标题"},
            ],
            "is_end": False,
            "next_cursor": "page-2",
        },
    })
    assert folders[0]["folder_id"] == "folder_20261009"
    assert files[0]["media_id"] == "m-file"
    assert ima_next_cursor({"data": {"is_end": True, "next_cursor": "ignored"}}) == ""

    reset_store_for_tests(tmp_path / "news.sqlite")
    monkeypatch.setattr("app.news.service.time.sleep", lambda _seconds: None)
    from app.config import settings
    monkeypatch.setattr(settings, "ima_client_id", "client")
    monkeypatch.setattr(settings, "ima_api_key", "secret")
    monkeypatch.setattr(settings, "ima_kb_id", "")
    monkeypatch.setattr(settings, "ima_kb_name", "【爱分享】的财经资讯")
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def post(self, url, headers=None, json=None):
            method = url.rsplit("/", 1)[-1]
            calls.append((method, dict(json or {})))
            if method == "search_knowledge_base":
                return Response({"retcode": 0, "data": {"info_list": [
                    {"kb_id": "kb-1", "kb_name": "【爱分享】的财经资讯"},
                ]}})
            cursor = (json or {}).get("cursor") or ""
            folder = (json or {}).get("folder_id") or ""
            if folder == "folder_20261009":
                return Response({"retcode": 0, "data": {
                    "knowledge_list": [{"media_id": "m1", "title": "茅台简讯"}],
                    "is_end": True,
                }})
            if folder == "folder_20261008":
                return Response({"retcode": 0, "data": {
                    "knowledge_list": [{"media_id": "m2", "title": "半导体简讯"}],
                    "is_end": True,
                }})
            if cursor == "":
                return Response({"retcode": 0, "data": {
                    "knowledge_list": [{"media_id": "folder_20261009", "title": "2026-10-09"}],
                    "is_end": False,
                    "next_cursor": "page-2",
                }})
            return Response({"retcode": 0, "data": {
                "knowledge_list": [{"media_id": "folder_20261008", "name": "20261008"}],
                "is_end": True,
                "next_cursor": "",
            }})

    result = collect_ima(Client())
    assert result["inserted"] == 2
    listed = [body for method, body in calls if method == "get_knowledge_list"]
    assert [body.get("cursor") for body in listed[:2]] == ["", "page-2"]
    assert {body.get("folder_id") for body in listed if body.get("folder_id")} == {
        "folder_20261009",
        "folder_20261008",
    }
    ids = {item["source_id"] for item in feed_for_source("ima")["items"]}
    assert ids == {"m1", "m2"}

    calls.clear()

    class LoopClient:
        def __init__(self):
            self.n = 0

        def post(self, url, headers=None, json=None):
            self.n += 1
            calls.append(url)
            return Response({"retcode": 0, "data": {
                "knowledge_list": [],
                "is_end": False,
                "next_cursor": f"c{self.n}",
            }})

    monkeypatch.setattr(settings, "ima_kb_id", "kb-fixed")
    collect_ima(LoopClient())
    assert len(calls) == 8


def test_inbox_drops_disabled_files_blocking_the_queue(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    monkeypatch.setattr("app.news.service.source_enabled", lambda source: source == "zsxq")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    old = 1_700_000_000
    for index in range(19):
        path = inbox / f"dws-{index:02d}.json"
        path.write_text(
            json.dumps({
                "source": "dws",
                "payload": {"messages": [{
                    "messageId": f"m{index}",
                    "text": "旧消息",
                    "createTime": "2026-10-09T10:00:00+08:00",
                }]},
            }),
            encoding="utf-8",
        )
        os.utime(path, (old, old))
    poison = inbox / "dws-bad.json"
    poison.write_text("{", encoding="utf-8")
    os.utime(poison, (old - 50, old - 50))
    good = inbox / "zsxq-new.json"
    good.write_text(json.dumps({
        "source": "zsxq",
        "payload": [{
            "topic_id": "t-new",
            "content": "纳指调研新消息",
            "create_time": "2026-10-09T12:00:00+08:00",
        }],
    }), encoding="utf-8")
    os.utime(good, (old + 500, old + 500))
    first = collect_inbox(inbox)
    assert first["inserted"] == 0
    assert not any(inbox.glob("dws-*.json"))
    assert (inbox / "failed" / "dws-bad.json").is_file()
    assert good.is_file()
    second = collect_inbox(inbox)
    assert second["inserted"] == 1
    assert not good.exists()
    assert feed_for_source("zsxq")["items"][0]["title"]


def test_host_collector_rejects_writes():
    assert_readonly(["/usr/local/bin/dws", "auth", "status"])
    assert_readonly(["zsxq-cli", "group", "+topics", "--group-id", "1", "--json"])
    with pytest.raises(CommandRejectedError):
        assert_readonly(["dws", "chat", "message", "send", "hello"])


def test_empty_group_id_is_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("NEWS_DWS_GROUP_ID", raising=False)
    monkeypatch.delenv("NEWS_ZSXQ_GROUP_ID", raising=False)
    from app.config import settings
    monkeypatch.setattr(settings, "news_dws_group_id", "")
    monkeypatch.setattr(settings, "news_zsxq_group_id", "")
    assert group_id("dws") == ""
    assert source_configured("dws") is False
    assert source_configured("zsxq") is False
    prefs = tmp_path / "user_data"
    prefs.mkdir()
    (prefs / "preferences.json").write_text(
        '{"news_sources":{"dws":{"enabled":true},"zsxq":{"enabled":true}}}',
        encoding="utf-8",
    )
    calls = []

    class Proc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    summary = run_host(tmp_path, lambda argv: calls.append(argv) or Proc())
    assert summary["dws"] == "unconfigured"
    assert summary["zsxq"] == "unconfigured"
    assert calls == []
    monkeypatch.setenv("NEWS_DWS_GROUP_ID", "group-from-env")
    assert group_id("dws") == "group-from-env"
    assert source_configured("dws") is True


def test_private_group_ids_are_not_defaults():
    root = Path(__file__).resolve().parents[2]
    needles = ("cid4Ua9gFB3K1KuSaEKnjNrNA==", "51115521812114")
    paths = [
        root / "backend/app/config.py",
        root / "backend/app/news/host_collector.py",
        root / ".env.example",
        root / "docs/news-sources.md",
        root / "deploy/tsp-news-collector.service",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, path


def test_systemd_unit_runs_as_user_with_hardening():
    text = (Path(__file__).resolve().parents[2] / "deploy/tsp-news-collector.service").read_text(encoding="utf-8")
    assert "User=ubuntu" in text
    assert "Environment=HOME=/home/ubuntu" in text
    assert "TimeoutStartSec=3600" in text
    assert "NoNewPrivileges=yes" in text
    assert "ProtectSystem=strict" in text
    assert "ReadWritePaths=/home/ubuntu/tick-stock-panel/data/news" in text
    assert "WorkingDirectory=/home/ubuntu/tick-stock-panel" in text
    assert "/opt/tsp" not in text


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
    assert queried.status_code == 404
    denied = asyncio.run(auth_middleware(
        _request("/api/news/dsa-feed", header="nope"),
        _ok,
    ))
    assert denied.status_code == 404
    short = asyncio.run(auth_middleware(
        _request("/api/news/dsa-feed", header="x"),
        _ok,
    ))
    assert short.status_code == 404


def test_feed_matches_compare_digest(monkeypatch):
    monkeypatch.setattr("app.news.config.feed_token", lambda: "feed-secret")
    seen = {}

    def fake(left, right):
        seen["pair"] = (left, right)
        return left == right

    monkeypatch.setattr("app.news.config.hmac.compare_digest", fake)
    assert feed_matches("feed-secret") is True
    assert seen["pair"] == ("feed-secret", "feed-secret")
    assert feed_matches("feed-secreT") is False
    assert feed_matches("feed-secret-longer") is False
    assert feed_matches("") is False

    def raising(_left, _right):
        raise ValueError("length")

    monkeypatch.setattr("app.news.config.hmac.compare_digest", raising)
    assert feed_matches("feed-secret") is False
    from app.api.news import dsa_feed
    assert "token" not in inspect.signature(dsa_feed).parameters
    with pytest.raises(HTTPException) as caught:
        dsa_feed(source="cls", limit=1, header_token="")
    assert caught.value.status_code == 404
