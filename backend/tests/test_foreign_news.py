"""外文 RSS / SEC Atom：只存标题和摘要，按链接和 guid 去重，条件请求。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from app.market_time import CN_TZ, cn_now
from app.news.collectors import FOREIGN_FEEDS, parse_feed_time, parse_feed_xml
from app.news.config import SOURCE_ORDER, sec_user_agent, source_configured, source_enabled
from app.news.extract import Lexicon
from app.news.scheduler import interval_seconds
from app.news.service import (
    collect_foreign,
    feed_for_source,
    get_store,
    health_payload,
    hot_candidates,
    hot_messages,
    ingest_items,
    reset_store_for_tests,
    run_due,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "news"
_PAYWALL = "FULL PAYWALLED BODY"
_FILING = "FULL FILING BODY"
_SEC_LINK = (
    "https://www.sec.gov/Archives/edgar/data/1234567/"
    "000123456726000008/0001234567-26-000008-index.html"
)


def _xml(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class _Response:
    def __init__(self, status: int, text: str = "", headers: dict | None = None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, bodies: dict):
        self.bodies = bodies
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None):
        sent = dict(headers or {})
        self.calls.append((url, sent))
        spec = self.bodies[url]
        if callable(spec):
            return spec(sent)
        return spec


def _clear_flags(monkeypatch) -> None:
    from app.config import settings

    names = (
        "NEWS_CNBC_ENABLED",
        "NEWS_MARKETWATCH_ENABLED",
        "NEWS_WSJ_ENABLED",
        "NEWS_BLOOMBERG_ENABLED",
        "NEWS_SEC_ENABLED",
        "SEC_USER_AGENT",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    for attr in (
        "news_cnbc_enabled",
        "news_marketwatch_enabled",
        "news_wsj_enabled",
        "news_bloomberg_enabled",
        "news_sec_enabled",
        "sec_user_agent",
    ):
        monkeypatch.setattr(settings, attr, "")
    monkeypatch.setattr("app.services.preferences.load", lambda: {})


def test_feed_time_converts_rfc822_to_beijing():
    parsed = parse_feed_time("Fri, 09 Oct 2026 14:30:00 GMT")
    assert parsed is not None
    assert parsed.isoformat(timespec="seconds") == "2026-10-09T22:30:00+08:00"
    atom = parse_feed_time("2026-10-09T16:10:00-04:00")
    assert atom is not None
    assert atom.isoformat(timespec="seconds") == "2026-10-10T04:10:00+08:00"


def test_parser_keeps_summary_and_drops_paywalled_body():
    items = parse_feed_xml(_xml("wsj_markets.xml"), "wsj", feed_url="https://example.test/wsj")
    assert len(items) == 1
    assert items[0].source_id == "wsj-55"
    assert items[0].title == "Treasury yields climb"
    assert items[0].text == "Yields rose after the jobs report."
    assert _PAYWALL not in items[0].text
    assert _PAYWALL not in json.dumps(items[0].raw)
    assert set(items[0].raw) == {"guid", "feed"}

    sec = parse_feed_xml(_xml("sec_8k.xml"), "sec")
    assert sec[0].source_id.startswith("urn:tag:sec.gov")
    assert sec[0].url == _SEC_LINK
    assert "Filed:" in sec[0].text
    assert "<b>" not in sec[0].text
    assert _FILING not in sec[0].text
    assert sec[0].published_at == datetime(2026, 10, 10, 4, 10, tzinfo=CN_TZ)


def test_parser_uses_link_when_guid_missing():
    xml = """
    <rss version="2.0"><channel><item>
      <title>No guid</title>
      <link>https://example.test/only-link</link>
      <description>Summary only.</description>
      <pubDate>Fri, 09 Oct 2026 14:30:00 GMT</pubDate>
    </item></channel></rss>
    """
    items = parse_feed_xml(xml, "marketwatch")
    assert items[0].source_id == "https://example.test/only-link"
    assert items[0].url == items[0].source_id


def test_cnbc_dedups_guid_and_link_across_feeds(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    feeds = FOREIGN_FEEDS["cnbc"]
    client = _Client({
        feeds[0].url: _Response(200, _xml("cnbc_top.xml")),
        feeds[1].url: _Response(200, _xml("cnbc_markets.xml")),
    })
    result = collect_foreign("cnbc", client)
    assert result == {"inserted": 3, "duplicate": 2}
    ids = {item["source_id"] for item in feed_for_source("cnbc")["items"]}
    assert ids == {"cnbc-1001", "cnbc-1002", "cnbc-1003"}
    requested = [url for url, _headers in client.calls]
    assert requested == [feeds[0].url, feeds[1].url]
    assert "https://www.cnbc.com/2026/10/09/oil.html" not in requested
    stored = feed_for_source("cnbc")["items"]
    fed = next(item for item in stored if item["source_id"] == "cnbc-1001")
    assert fed["published_at"] == "2026-10-09T22:30:00+08:00"


def test_paywalled_feeds_never_store_or_fetch_body(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    prompts: list[str] = []
    monkeypatch.setattr("app.news.service.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(
        "app.news.service._llm_text",
        lambda prompt: prompts.append(prompt) or "",
    )
    import app.news.service as news_service
    news_service._LLM_TIMES.clear()

    wsj = FOREIGN_FEEDS["wsj"][0].url
    client = _Client({wsj: _Response(200, _xml("wsj_markets.xml"))})
    result = collect_foreign("wsj", client)
    assert result["inserted"] == 1
    assert [url for url, _headers in client.calls] == [wsj]
    blob = json.dumps([
        dict(row) for row in get_store()._conn.execute("SELECT title, clean_text, raw_json, url FROM news_items")
    ], ensure_ascii=False)
    assert _PAYWALL not in blob
    assert "Yields rose after the jobs report." in blob
    assert prompts
    assert _PAYWALL not in prompts[0]
    assert "Yields rose after the jobs report." in prompts[0]

    bbg = FOREIGN_FEEDS["bloomberg"]
    bbg_client = _Client({
        bbg[0].url: _Response(500, "nope"),
        bbg[1].url: _Response(200, _xml("bloomberg_tech.xml")),
    })
    partial = collect_foreign("bloomberg", bbg_client)
    assert partial["inserted"] == 1
    assert "error" not in partial
    assert _PAYWALL not in json.dumps([
        dict(row) for row in get_store()._conn.execute("SELECT clean_text, raw_json FROM news_items")
    ])


def test_conditional_get_skips_body_on_not_modified(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    url = FOREIGN_FEEDS["marketwatch"][0].url
    xml = _xml("mw_top.xml")

    def body(headers):
        if headers.get("If-None-Match") == '"mw-etag"':
            return _Response(304)
        return _Response(200, xml, {
            "ETag": '"mw-etag"',
            "Last-Modified": "Fri, 09 Oct 2026 13:00:00 GMT",
        })

    client = _Client({url: body})
    first = collect_foreign("marketwatch", client)
    assert first["inserted"] == 1
    second = collect_foreign("marketwatch", client)
    assert second == {"inserted": 0, "duplicate": 0}
    third = collect_foreign("marketwatch", client)
    assert third["inserted"] == 0
    assert client.calls[1][1]["If-None-Match"] == '"mw-etag"'
    assert client.calls[1][1]["If-Modified-Since"] == "Fri, 09 Oct 2026 13:00:00 GMT"
    assert client.calls[2][1]["If-None-Match"] == '"mw-etag"'
    assert client.calls[0][1]["User-Agent"] == "tsp-news/1.0"


def test_sec_requires_contact_email_and_sends_it(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    _clear_flags(monkeypatch)
    from app.config import settings

    assert source_configured("sec") is False
    assert sec_user_agent() == ""
    skipped = collect_foreign("sec", _Client({}))
    assert skipped["skipped"] is True

    monkeypatch.setenv("SEC_USER_AGENT", "tsp-news/1.0")
    assert source_configured("sec") is False
    blocked = collect_foreign("sec", _Client({}))
    assert blocked.get("skipped") is True

    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(settings, "sec_user_agent", "TSP-News ops@example.com")
    assert sec_user_agent() == "TSP-News ops@example.com"
    assert source_configured("sec") is True
    assert source_enabled("sec") is False

    url = FOREIGN_FEEDS["sec"][0].url
    client = _Client({url: _Response(200, _xml("sec_8k.xml"))})
    saved = collect_foreign("sec", client)
    assert saved["inserted"] == 1
    assert client.calls[0][0] == url
    assert client.calls[0][1]["User-Agent"] == "TSP-News ops@example.com"
    assert _SEC_LINK not in [call[0] for call in client.calls]
    item = feed_for_source("sec")["items"][0]
    assert item["source_id"].startswith("urn:tag:sec.gov")
    assert "Filed:" in item["summary"]
    assert _FILING not in item["summary"]
    assert item["url"] == _SEC_LINK


def test_foreign_failure_is_isolated(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    url = FOREIGN_FEEDS["marketwatch"][0].url
    broken = collect_foreign("marketwatch", _Client({url: _Response(200, "<rss>")}))
    assert broken["inserted"] == 0
    assert "error" in broken
    down = collect_foreign("marketwatch", _Client({url: _Response(503, "down")}))
    assert down["inserted"] == 0
    assert "error" in down
    rows = {row["source"]: row for row in get_store().health_rows()}
    assert rows["marketwatch"]["last_error"]


def test_llm_summary_and_mentions_enter_hot_events(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    monkeypatch.setattr("app.news.service.llm_extract_enabled", lambda: True)
    import app.news.service as news_service
    news_service._LLM_TIMES.clear()

    def fake(prompt: str) -> str:
        assert "Fed holds" in prompt
        return json.dumps({
            "summary_zh": "美联储维持利率不变，并提到贵州茅台",
            "stocks": [{"name": "贵州茅台", "code": "600519"}],
            "sectors": ["白酒"],
        }, ensure_ascii=False)

    monkeypatch.setattr("app.news.service._llm_text", fake)
    lexicon = Lexicon([("600519.SH", "贵州茅台", "600519")], ["白酒"])
    from app.news.collectors import Item
    ingest_items([Item(
        source="cnbc",
        source_id="llm-1",
        published_at=cn_now(),
        title="Fed holds",
        text="The Federal Reserve left rates unchanged and cited liquor demand.",
        url="https://www.cnbc.com/example-fed",
    )], lexicon)
    feed = feed_for_source("cnbc")["items"][0]
    assert feed["summary"] == "美联储维持利率不变，并提到贵州茅台"
    assert feed["symbols"] == ["600519.SH"]
    assert feed["sectors"] == ["白酒"]
    ranked = hot_candidates(kind="stock", window_hours=24, baseline_days=1)
    assert ranked[0].key == "600519.SH"
    assert ranked[0].effective_mentions == 1.0
    excerpt = hot_messages("stock", "600519.SH")[0]["excerpt"]
    assert excerpt.startswith("美联储")
    row = get_store()._conn.execute("SELECT clean_text FROM news_items").fetchone()
    assert "Federal Reserve" in row["clean_text"]


def test_llm_disabled_skips_foreign_enrich(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    monkeypatch.setattr("app.news.service.llm_extract_enabled", lambda: False)
    calls: list[str] = []
    monkeypatch.setattr("app.news.service._llm_text", lambda prompt: calls.append(prompt) or "")
    from app.news.collectors import Item
    ingest_items([Item(
        source="bloomberg",
        source_id="b1",
        published_at=cn_now(),
        title="Dollar steadies",
        text="The dollar was little changed in afternoon trading.",
        url="https://www.bloomberg.com/news/articles/example",
    )], Lexicon([], []))
    assert calls == []
    assert "little changed" in feed_for_source("bloomberg")["items"][0]["summary"]


def test_intervals_stay_flat_outside_the_a_share_session():
    morning = datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ)
    night = datetime(2026, 10, 9, 23, 30, tzinfo=CN_TZ)
    assert interval_seconds("cls", morning) == 45
    assert interval_seconds("cls", night) == 1800
    assert interval_seconds("cnbc", morning) == 300
    assert interval_seconds("cnbc", night) == 300
    assert interval_seconds("wsj", night) == 300
    assert interval_seconds("bloomberg", morning) == 300
    assert interval_seconds("sec", morning) == 180
    assert interval_seconds("sec", night) == 180


def test_run_due_and_health_include_foreign_sources(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    seen: list[str] = []
    monkeypatch.setattr(
        "app.news.service.collect_foreign",
        lambda source: seen.append(source) or {"inserted": 0, "duplicate": 0},
    )
    assert run_due("sec")["inserted"] == 0
    assert seen == ["sec"]
    _clear_flags(monkeypatch)
    ids = [row["id"] for row in health_payload()["sources"]]
    assert ids == list(SOURCE_ORDER)
    sec = next(row for row in health_payload()["sources"] if row["id"] == "sec")
    cnbc = next(row for row in health_payload()["sources"] if row["id"] == "cnbc")
    assert sec["configured"] is False
    assert sec["enabled"] is False
    assert cnbc["configured"] is True
    assert cnbc["enabled"] is False


def test_dsa_feed_pattern_accepts_foreign_sources():
    from app.api.news import _FEED_SOURCE_PATTERN

    for source in ("cnbc", "marketwatch", "wsj", "bloomberg", "sec", "hot"):
        assert re.fullmatch(_FEED_SOURCE_PATTERN, source)
    assert re.fullmatch(_FEED_SOURCE_PATTERN, "sec.gov") is None
    assert re.fullmatch(_FEED_SOURCE_PATTERN, "cnbc-extra") is None
