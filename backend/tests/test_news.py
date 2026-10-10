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
from app.news.host_collector import (
    CommandRejectedError,
    _run_dws,
    assert_readonly,
    dws_cursor_stamp,
    run_host,
)
from app.news.scoring import MentionEvent, score_candidates
from app.news.service import (
    _ima_post,
    collect_ima,
    collect_inbox,
    feed_for_source,
    get_store,
    hot_candidates,
    hot_messages,
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


def test_sector_fragments_and_fund_names_do_not_become_candidates():
    lexicon = Lexicon(
        [
            ("512100.SH", "中证1000ETF南方", "512100"),
            ("600519.SH", "贵州茅台", "600519"),
        ],
        ["50", "A50", "500", "中", "沪50", "中证500", "半导体"],
    )
    text = "350亿、50万吨、标普500、富时A50，中证5000与中证500，还有中证1000ETF南方"
    mentions = lexicon.extract(text)
    sectors = {item.key for item in mentions if item.kind == "sector"}
    stocks = {item.key for item in mentions if item.kind == "stock"}
    assert sectors == {"中证500"}
    assert stocks == set()
    structured = lexicon.extract("", None, ["50", "中", "沪50", "A50", "中证500"])
    assert {item.key for item in structured if item.kind == "sector"} == {"中证500"}
    coded = lexicon.extract("代码 512100.SH")
    assert {item.key for item in coded} == {"512100.SH"}


def test_legacy_bad_sector_names_leave_the_hot_list(tmp_path):
    path = tmp_path / "news.sqlite"
    reset_store_for_tests(path)
    when = cn_now()
    ingest_items([
        Item(
            source="cls",
            source_id="sec-1",
            published_at=when,
            text="白酒板块的讨论写得长一些，避免被当成短讯。",
            title="白酒",
            sectors=["白酒"],
        ),
    ], Lexicon([], ["白酒"]))
    store = get_store()
    item_id = store._conn.execute("SELECT id FROM news_items").fetchone()["id"]
    store._conn.executemany(
        """
        INSERT INTO news_mentions (item_id, kind, key, name, code, origin)
        VALUES (?, 'sector', ?, ?, '', 'text')
        """,
        [(item_id, key, key) for key in ("50", "中", "A50", "沪50")],
    )
    store._conn.commit()
    ranked = hot_candidates(kind="sector", window_hours=24, baseline_days=1)
    assert [item.key for item in ranked] == ["白酒"]
    feed = feed_for_source("cls", limit=10)
    assert feed["items"][0]["sectors"] == ["白酒"]
    assert hot_messages("sector", "50") == []
    reset_store_for_tests(path)
    keys = [
        row["key"]
        for row in get_store()._conn.execute(
            "SELECT key FROM news_mentions WHERE kind = 'sector' ORDER BY key"
        )
    ]
    assert keys == ["白酒"]


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
    brief, brief_page = parse_zsxq_payload({
        "topics_brief": [{
            "topic_id": "8848",
            "type": "talk",
            "title": "纳指调研",
            "digest": "只在 topics_brief 里的正文",
            "create_time": "2026-10-09T11:00:00.000+0800",
            "owner": {"name": "作者", "user_id": "1"},
        }],
        "has_more": True,
        "next_end_time": "2026-10-09T11:00:00.000+0800",
    })
    assert brief_page["has_more"] is True
    assert brief_page["next_end_time"] == "2026-10-09T11:00:00.000+0800"
    assert brief[0].source_id == "8848"
    assert brief[0].text == "只在 topics_brief 里的正文"
    assert brief[0].author == "作者"
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


def test_cls_and_wscn_page_back_until_a_seen_id(tmp_path):
    from app.news.collectors import (
        Item,
        cls_next_last_time,
        cls_params,
        paginate_until_seen,
        parse_cls,
        parse_wscn,
        wscn_next_cursor,
        wscn_params,
    )
    from app.news.service import collect_cls, collect_wscn

    cls_page = {
        "errno": 0,
        "msg": "ok",
        "data": {
            "roll_data": [
                {
                    "id": 2500998,
                    "ctime": 1791585757,
                    "title": "",
                    "content": "财联社电报：贵州茅台获机构调研",
                    "shareurl": "https://www.cls.cn/detail/2500998",
                    "level": "B",
                    "is_ad": 0,
                    "stock_list": [{"name": "贵州茅台", "StockID": "sh600519"}],
                    "subjects": [{"subject_name": "白酒"}],
                    "plate_list": [],
                },
                {"id": 9, "ctime": 1791500000, "is_ad": 1, "content": "广告"},
            ],
        },
    }
    assert cls_next_last_time(cls_page) == "1791585757"
    assert "last_time" not in cls_params()
    assert cls_params("1791585757")["last_time"] == "1791585757"
    parsed = parse_cls(cls_page)
    assert parsed[0].source_id == "2500998"
    assert parsed[0].stocks[0].code == "sh600519"
    assert parsed[0].sectors == ["白酒"]

    wscn_page = {
        "code": 20000,
        "message": "OK",
        "data": {
            "items": [{
                "id": 3176473,
                "title": "",
                "content_text": "华尔街见闻快讯正文",
                "content": "<p>华尔街见闻快讯正文</p>",
                "display_time": 1791586996,
                "uri": "https://wallstreetcn.com/livenews/3176473",
                "score": 1,
                "author": {"display_name": "罗俊", "id": 694840},
                "symbols": [{"name": "贵州茅台", "symbol": "600519.SH"}],
                "related_themes": [{"name": "白酒"}],
            }],
            "next_cursor": "1791586211",
            "polling_cursor": "3176473",
        },
    }
    assert wscn_next_cursor(wscn_page) == "1791586211"
    assert "cursor" not in wscn_params("global-channel")
    assert wscn_params("a-stock-channel", "1791586211")["cursor"] == "1791586211"
    wscn_item = parse_wscn(wscn_page, "global-channel")[0]
    assert wscn_item.author == "罗俊"
    assert wscn_item.url == "https://wallstreetcn.com/livenews/3176473"
    assert wscn_item.sectors == ["白酒"]

    when = datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ)
    walked = []

    def walk(token):
        walked.append(token)
        if token is None:
            return [Item("cls", "3", when)], "2", False
        if token == "2":
            return [Item("cls", "1", when)], "1", True
        raise AssertionError(token)

    assert [item.source_id for item in paginate_until_seen(walk)] == ["3", "1"]
    assert walked == [None, "2"]

    capped = []

    def always_new(token):
        index = 0 if token is None else int(token)
        capped.append(index)
        return [Item("cls", str(index), when)], str(index + 1), False

    assert len(paginate_until_seen(always_new, cap=10)) == 10
    assert capped == list(range(10))

    reset_store_for_tests(tmp_path / "news.sqlite")
    store = get_store()
    store.insert_item(
        source="cls", source_id="111", published_at=when, author="", title="旧电报",
        clean_text="已经见过", raw={}, content_hash="cls-111", url="", level="",
        media_ids=[], extra={}, mentions=[],
    )
    store.insert_item(
        source="wscn", source_id="3176000", published_at=when, author="", title="旧快讯",
        clean_text="已经见过", raw={}, content_hash="wscn-old", url="", level="",
        media_ids=[], extra={}, mentions=[],
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def roll(rows):
        return {"errno": 0, "data": {"roll_data": [
            {
                "id": item_id,
                "ctime": stamp,
                "content": f"电报{item_id}",
                "shareurl": f"https://www.cls.cn/detail/{item_id}",
                "level": "C",
            }
            for item_id, stamp in rows
        ]}}

    cls_calls = []

    class ClsClient:
        def get(self, url, params=None, headers=None):
            cls_calls.append(dict(params or {}))
            last = (params or {}).get("last_time")
            if not last:
                return Response(roll([(2500998, 1791585757), (2500997, 1791585267)]))
            if last == "1791585267":
                return Response(roll([(2500996, 1791585160), (111, 1791580000)]))
            raise AssertionError(last)

    result = collect_cls(ClsClient())
    assert [call.get("last_time") for call in cls_calls] == [None, "1791585267"]
    assert result["inserted"] == 3
    assert result["duplicate"] == 1

    def lives(rows, cursor):
        return {"code": 20000, "data": {
            "items": [
                {
                    "id": item_id,
                    "content_text": f"快讯{item_id}",
                    "display_time": 1791586996,
                    "uri": f"https://wallstreetcn.com/livenews/{item_id}",
                    "author": {"display_name": "罗俊"},
                }
                for item_id in rows
            ],
            "next_cursor": cursor,
            "polling_cursor": str(rows[0]),
        }}

    wscn_calls = []

    class WscnClient:
        def get(self, url, params=None, headers=None):
            channel = (params or {})["channel"]
            cursor = (params or {}).get("cursor")
            wscn_calls.append((channel, cursor))
            if channel == "global-channel":
                return Response(lives([3176000], "1791589999"))
            if cursor is None:
                return Response(lives([3176456], "1791580518"))
            if cursor == "1791580518":
                return Response(lives([3176449, 3176000], "1791579884"))
            raise AssertionError(cursor)

    result = collect_wscn(WscnClient())
    assert wscn_calls == [
        ("global-channel", None),
        ("a-stock-channel", None),
        ("a-stock-channel", "1791580518"),
    ]
    assert result["inserted"] == 2
    assert result["duplicate"] == 2


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


def test_dws_cursor_overlaps_two_minutes(tmp_path, monkeypatch):
    moment = datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ)
    assert dws_cursor_stamp(moment) == "2026-10-09 09:58:00"
    monkeypatch.setattr("app.news.host_collector.cn_now", lambda: moment)
    monkeypatch.setattr("app.news.host_collector.group_id", lambda _source: "g1")
    monkeypatch.setattr("app.news.host_collector.which", lambda _name: "/usr/bin/dws")

    class Proc:
        returncode = 0
        stdout = '{"messages":[]}'
        stderr = ""

    assert _run_dws(tmp_path, lambda _argv: Proc(), "", "") == "ok"
    cursor = (tmp_path / "news" / "cursors" / "dws.txt").read_text(encoding="utf-8")
    assert cursor == "2026-10-09 09:58:00"


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
    assert "ReadWritePaths=/home/ubuntu/.dws" in text
    assert "ReadWritePaths=/home/ubuntu/.local/share/dws-cli" in text
    assert "ReadWritePaths=/home/ubuntu/.config/zsxq-cli" in text
    assert "ReadWritePaths=/home/ubuntu/.local/share/zsxq-cli" in text
    assert "Environment=PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin" in text
    assert "ExecStart=/home/ubuntu/.venvs/tsp-collector/bin/python " in text
    assert "/usr/bin/python3" not in text
    assert "WorkingDirectory=/home/ubuntu/tick-stock-panel" in text
    assert "/opt/tsp" not in text


def test_gateway_keeps_feed_closed():
    assert required_scope("GET", "/api/news/hot") == "read:analysis"
    assert required_scope("GET", "/api/news/messages") == "read:analysis"
    assert required_scope("GET", "/api/news/stocks/600519.SH") == "read:analysis"
    assert required_scope("GET", "/api/news/dsa-feed") is None
    assert required_scope("GET", "/api/news/health") is None
    assert required_scope("PUT", "/api/news/sources") is None
    assert required_scope("GET", "/api/news/push") is None
    assert required_scope("PUT", "/api/news/push") is None
    assert required_scope("POST", "/api/news/push/test") is None


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


def test_hot_page_lists_concrete_events_and_keeps_sector_rank(tmp_path):
    from datetime import time as dt_time

    from app.api import news as news_api
    from app.market_time import current_trading_day
    from app.news.extract import StructuredStock
    from app.news.push import format_hot_markdown
    from app.news.service import event_detail, hot_event_listing

    reset_store_for_tests(tmp_path / "events.sqlite")
    day = current_trading_day(cn_now())
    lexicon = Lexicon([], ["人工智能", "昇腾"])
    ingest_items([
        Item(
            source="cls",
            source_id="hw-1",
            published_at=datetime.combine(day, dt_time(14, 52), CN_TZ),
            title="华为发布盘古新模型，昇腾链走强",
            text="华为发布盘古新模型，昇腾链走强，正文写长一些以免被当成短讯。",
            url="https://www.cls.cn/detail/9",
            stocks=[StructuredStock(name="贵州茅台", code="600519")],
            sectors=["人工智能", "昇腾"],
        ),
        Item(
            source="wscn",
            source_id="hw-2",
            published_at=datetime.combine(day, dt_time(14, 58), CN_TZ),
            title="华为发布盘古新模型",
            text="华为发布盘古新模型，另一来源再写一条足够长的正文。",
            stocks=[StructuredStock(name="贵州茅台", code="600519")],
            sectors=["昇腾"],
        ),
        Item(
            source="cls",
            source_id="sector-now",
            published_at=cn_now() - timedelta(minutes=30),
            title="人工智能板块午后走强",
            text="人工智能板块午后走强，这条只用来保留板块升温榜。",
            sectors=["人工智能"],
        ),
    ], lexicon)
    listing = hot_event_listing(cn_now(), limit=20)
    names = [item["name"] for item in listing["events"]]
    assert names[0] == "华为发布盘古新模型"
    assert "人工智能" not in names
    head = listing["events"][0]
    assert head["concepts"] == ["昇腾"]
    assert head["mentions"] == 2
    assert head["source_count"] == 2
    assert head["headline"] == "华为发布盘古新模型，昇腾链走强"
    assert head["stocks"][0]["key"] == "600519.SH"
    assert "item_ids" not in head

    detail = event_detail(head["key"], now=cn_now())
    assert {item["title"] for item in detail["items"]} == {
        "华为发布盘古新模型，昇腾链走强",
        "华为发布盘古新模型",
    }
    assert detail["stocks"][0]["name"] == "贵州茅台"

    feed = feed_for_source("hot", limit=10)
    assert feed["source"] == "hot"
    assert feed["items"][0]["title"] == "华为发布盘古新模型"
    assert feed["items"][0]["symbols"] == ["600519.SH"]
    assert feed["items"][0]["sectors"] == ["昇腾"]

    packed = format_hot_markdown(
        [{
            "name": "人工智能",
            "key": "人工智能",
            "story_count": 2,
            "source_count": 1,
            "growth": 1.5,
        }],
        [],
        heading="【热点候选】盘前",
        events=[{
            "name": head["name"],
            "key": head["key"],
            "story_count": head["mentions"],
            "source_count": head["source_count"],
            "concepts": head["concepts"],
            "first_seen": head["first_seen"],
            "headline": head["headline"],
        }],
    )
    assert packed is not None
    text = packed[1]
    assert text.index("具体事件") < text.index("热门板块")
    assert "华为发布盘古新模型" in text
    assert "首见" in text
    assert "相对基线 1.5 倍" in text

    listed = news_api.hot(kind="event", window_hours=24, baseline_days=4, limit=20)
    assert listed["candidates"] == []
    assert listed["events"][0]["name"] == "华为发布盘古新模型"
    sectors = news_api.hot(kind="sector", window_hours=24, baseline_days=1, limit=20)
    assert "events" not in sectors
    assert any(item["key"] == "人工智能" for item in sectors["candidates"])
    opened = news_api.messages(kind="event", key=head["key"], window_hours=24, limit=30)
    assert len(opened["items"]) == 2
    assert opened["stocks"][0]["key"] == "600519.SH"
    assert opened["etfs"] == []


def test_asset_kind_trusts_universe_before_fund_prefix(monkeypatch):
    from app.news.service import asset_kind_of

    monkeypatch.setattr("app.news.service.known_asset_types", lambda: {
        "510300.SH": "stock",
        "600519.SH": "etf",
    })
    assert asset_kind_of("510300.SH", "沪深300ETF") == "stock"
    assert asset_kind_of("600519.SH", "贵州茅台") == "etf"
    assert asset_kind_of("510300", "沪深300ETF") == "stock"

    monkeypatch.setattr("app.news.service.known_asset_types", lambda: {})
    for code in ("510300", "560050", "588000", "159915", "161226", "501018"):
        assert asset_kind_of(code, "普通名称") == "etf"
    assert asset_kind_of("600519.SH", "贵州茅台") == "stock"
    assert asset_kind_of("000001.SZ", "易方达某基金") == "etf"
    assert asset_kind_of("000001.SZ", "平安银行") == "stock"


def test_hot_stocks_exclude_funds_and_events_list_them_apart(tmp_path, monkeypatch):
    from datetime import time as dt_time

    from app.api import news as news_api
    from app.market_time import current_trading_day
    from app.news.push import format_hot_markdown
    from app.news.service import event_detail, hot_event_listing

    monkeypatch.setattr("app.news.service.known_asset_types", lambda: {})
    reset_store_for_tests(tmp_path / "funds.sqlite")
    day = current_trading_day(cn_now())
    lexicon = Lexicon([], [])
    funds = [
        StructuredStock(name="沪深300ETF", code="510300"),
        StructuredStock(name="创业板ETF易方达", code="159915"),
        StructuredStock(name="南方原油", code="501018"),
        StructuredStock(name="中证500LOF", code="161226"),
    ]
    ingest_items([
        Item(
            source="cls",
            source_id="mix-event",
            published_at=datetime.combine(day, dt_time(14, 40), CN_TZ),
            title="沪深300ETF放量，贵州茅台跟涨",
            text="沪深300ETF放量，贵州茅台跟涨，正文写长一些以免被当成短讯。",
            stocks=[StructuredStock(name="贵州茅台", code="600519"), *funds],
        ),
        Item(
            source="cls",
            source_id="mix-hot",
            published_at=cn_now() - timedelta(minutes=20),
            title="资金涌入宽基基金",
            text="资金涌入宽基基金，这条用来把基金和个股同时送进近24小时热度榜。",
            stocks=[StructuredStock(name="贵州茅台", code="600519"), *funds],
        ),
    ], lexicon)

    stock_keys = {item.key for item in hot_candidates(kind="stock", window_hours=24, baseline_days=1)}
    etf_keys = {item.key for item in hot_candidates(kind="etf", window_hours=24, baseline_days=1)}
    assert stock_keys == {"600519.SH"}
    assert etf_keys == {"510300.SH", "159915.SZ", "501018.SH", "161226.SZ"}

    listing = hot_event_listing(cn_now(), limit=20)
    head = next(item for item in listing["events"] if item["name"] == "沪深300ETF放量，贵州茅台跟涨")
    assert [item["key"] for item in head["stocks"]] == ["600519.SH"]
    assert {item["key"] for item in head["etfs"]} == etf_keys

    detail = event_detail(head["key"], now=cn_now())
    assert [item["key"] for item in detail["stocks"]] == ["600519.SH"]
    assert {item["key"] for item in detail["etfs"]} == etf_keys

    listed = news_api.hot(kind="stock", window_hours=24, baseline_days=1, limit=20)
    assert {item["key"] for item in listed["candidates"]} == {"600519.SH"}
    etf_listed = news_api.hot(kind="etf", window_hours=24, baseline_days=1, limit=20)
    assert {item["key"] for item in etf_listed["candidates"]} == etf_keys
    excerpts = news_api.messages(kind="etf", key="510300.SH", window_hours=24, limit=10)
    assert excerpts["items"][0]["title"] == "资金涌入宽基基金"

    packed = format_hot_markdown(
        [],
        [{"name": "贵州茅台", "key": "600519.SH", "story_count": 1, "source_count": 1, "growth": 1.2}],
        heading="【热点候选】盘前",
        etfs=[{"name": "沪深300ETF", "key": "510300.SH", "story_count": 2, "source_count": 1, "growth": 1.4}],
    )
    assert packed is not None
    text = packed[1]
    assert text.index("热门个股") < text.index("热门ETF")
    assert "贵州茅台" in text.split("热门ETF")[0]
    assert "沪深300ETF" in text.split("热门ETF", 1)[1]
    assert "沪深300ETF" not in text.split("热门个股")[1].split("热门ETF")[0]

    monkeypatch.setattr("app.news.service.known_asset_types", lambda: {
        "510300.SH": "stock",
        "600519.SH": "etf",
    })
    overridden_stocks = {item.key for item in hot_candidates(kind="stock", window_hours=24, baseline_days=1)}
    overridden_etfs = {item.key for item in hot_candidates(kind="etf", window_hours=24, baseline_days=1)}
    assert "510300.SH" in overridden_stocks
    assert "600519.SH" not in overridden_stocks
    assert "600519.SH" in overridden_etfs
    assert "510300.SH" not in overridden_etfs
