"""Reddit Atom：每次一个子版，两次请求至少隔 75 秒，429 按 Retry-After 退避。"""
from __future__ import annotations

import json
from datetime import datetime
from email.utils import formatdate
from pathlib import Path

from app.market_time import CN_TZ
from app.news.collectors import (
    REDDIT_USER_AGENT,
    parse_reddit_atom,
    reddit_feed_url,
)
from app.news.config import reddit_oauth_ready, reddit_subreddits
from app.news.scheduler import next_delay
from app.news.service import (
    collect_reddit,
    feed_for_source,
    get_store,
    reset_store_for_tests,
    run_due,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "news"
_WSB = "https://www.reddit.com/r/wallstreetbets/new/.rss"
_STOCKS = "https://www.reddit.com/r/stocks/new/.rss"
_INVESTING = "https://www.reddit.com/r/investing/new/.rss"
_IMAGE = "https://preview.redd.it/b0fhnz1khkuh1.jpeg"


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
        if isinstance(spec, list):
            spec = spec.pop(0)
        if callable(spec):
            return spec(sent)
        return spec


def _atom(sub: str, post_id: str, title: str = "Hello") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <author><name>/u/tester</name><uri>https://www.reddit.com/user/tester</uri></author>
    <id>{post_id}</id>
    <link href="https://www.reddit.com/r/{sub}/comments/{post_id}/hello/"/>
    <published>2026-10-10T01:00:00+00:00</published>
    <title>{title}</title>
    <content type="html">A note submitted by /u/tester [link] [comments]</content>
  </entry>
</feed>
"""


def _clear(monkeypatch) -> None:
    from app.config import settings

    for name in (
        "NEWS_REDDIT_ENABLED",
        "NEWS_REDDIT_SUBREDDITS",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "NEWS_LLM_EXTRACT",
    ):
        monkeypatch.delenv(name, raising=False)
    for attr in (
        "news_reddit_enabled",
        "news_reddit_subreddits",
        "reddit_client_id",
        "reddit_client_secret",
        "news_llm_extract",
    ):
        monkeypatch.setattr(settings, attr, "")


def _rows() -> list[dict]:
    return [
        dict(row)
        for row in get_store()._conn.execute(
            """
            SELECT source_id, author, title, clean_text, url, published_at, raw_json
            FROM news_items ORDER BY published_at
            """,
        )
    ]


def test_parser_keeps_author_summary_and_drops_images():
    assert reddit_feed_url("r/WallStreetBets") == _WSB
    assert ".json" not in reddit_feed_url("stocks")
    items = parse_reddit_atom(_xml("reddit_wsb.xml"), feed_url=_WSB, subreddit="wallstreetbets")
    assert [item.source_id for item in items] == ["t3_1x25ztn", "t3_1x241e4"]
    image, post = items
    assert image.author == "/u/level99mewtwo"
    assert "reddit.com/user" not in image.author
    assert image.url.endswith("/small_potatoes_to_some_big_potatoes_to_me/")
    assert image.published_at.isoformat(timespec="seconds") == "2026-10-10T12:26:03+08:00"
    assert image.text == image.title
    assert _IMAGE not in image.text
    assert "submitted by" not in image.text
    assert set(image.raw) == {"guid", "feed", "subreddit"}
    assert image.raw["subreddit"] == "wallstreetbets"
    assert _IMAGE not in json.dumps(image.raw)
    assert post.author == "/u/yourbestdegen"
    assert "subprime mortgage bubble" in post.text
    assert "submitted by" not in post.text
    assert "[link]" not in post.text
    assert "[comments]" not in post.text
    assert post.published_at.isoformat(timespec="seconds") == "2026-10-10T10:38:55+08:00"

    leaked = """
    <feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <author><name>/u/tester</name><uri>https://www.reddit.com/user/tester</uri></author>
      <id>t3_img</id>
      <link href="https://www.reddit.com/r/stocks/comments/img/hello/"/>
      <published>2026-10-10T01:00:00+00:00</published>
      <title>Chart</title>
      <content type="html">See https://preview.redd.it/abc.jpg before submitted by /u/tester [link]</content>
    </entry></feed>
    """
    stripped = parse_reddit_atom(leaked, feed_url=_STOCKS, subreddit="stocks")[0]
    assert "redd.it" not in stripped.text
    assert stripped.text == "See before"


def test_round_robin_one_subreddit_and_seventy_five_second_gap(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    _clear(monkeypatch)
    monkeypatch.setattr("app.news.service.time.sleep", _forbid_sleep)

    def wsb(headers):
        if headers.get("If-None-Match") == '"wsb"':
            return _Response(304, "")
        return _Response(200, _xml("reddit_wsb.xml"), {"ETag": '"wsb"'})

    client = _Client({
        _WSB: wsb,
        _STOCKS: _Response(200, _atom("stocks", "t3_stocks")),
        _INVESTING: _Response(200, _atom("investing", "t3_investing")),
    })
    first = collect_reddit(client, now=1000)
    assert first["inserted"] == 2
    assert first["retry_after"] == 75
    skipped = collect_reddit(client, now=1074)
    assert skipped["skipped"] is True
    assert skipped["retry_after"] == 1
    assert len(client.calls) == 1
    assert collect_reddit(client, now=1075)["inserted"] == 1
    assert collect_reddit(client, now=1150)["inserted"] == 1
    again = collect_reddit(client, now=1225)
    assert again == {"inserted": 0, "duplicate": 0, "retry_after": 75}
    urls = [url for url, _headers in client.calls]
    assert urls == [_WSB, _STOCKS, _INVESTING, _WSB]
    for url, headers in client.calls:
        assert url.endswith("/new/.rss")
        assert ".json" not in url
        assert _IMAGE not in url
        assert headers["User-Agent"] == REDDIT_USER_AGENT
        assert "astock888888@mail.grokbot.com" in headers["User-Agent"]
        assert "Authorization" not in headers
        assert "authorization" not in {key.lower() for key in headers}
    assert client.calls[3][1]["If-None-Match"] == '"wsb"'
    rows = _rows()
    blob = json.dumps(rows, ensure_ascii=False)
    assert _IMAGE not in blob
    assert "i.redd.it" not in blob
    image = next(row for row in rows if row["source_id"] == "t3_1x25ztn")
    post = next(row for row in rows if row["source_id"] == "t3_1x241e4")
    assert image["author"] == "/u/level99mewtwo"
    assert image["published_at"] == "2026-10-10T12:26:03+08:00"
    assert "subprime mortgage bubble" in post["clean_text"]
    assert feed_for_source("reddit")["name"] == "Reddit"
    health = {row["source"]: row for row in get_store().health_rows()}
    assert health["reddit"]["last_error"] == ""


def test_retry_after_holds_the_same_subreddit(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    _clear(monkeypatch)
    client = _Client({
        _WSB: [
            _Response(429, "", {"Retry-After": "120"}),
            _Response(200, _xml("reddit_wsb.xml")),
        ],
    })
    blocked = collect_reddit(client, now=1000)
    assert blocked["retry_after"] == 120
    assert "429" in blocked["error"]
    health = {row["source"]: row for row in get_store().health_rows()}
    assert "429" in health["reddit"]["last_error"]
    assert collect_reddit(client, now=1119)["skipped"] is True
    assert len(client.calls) == 1
    ok = collect_reddit(client, now=1120)
    assert ok["inserted"] == 2
    assert [url for url, _headers in client.calls] == [_WSB, _WSB]
    health = {row["source"]: row for row in get_store().health_rows()}
    assert health["reddit"]["last_error"] == ""


def test_headerless_429_backs_off_then_resets(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    _clear(monkeypatch)
    monkeypatch.setenv("NEWS_REDDIT_SUBREDDITS", "wallstreetbets")
    client = _Client({
        _WSB: [
            _Response(429, ""),
            _Response(429, ""),
            _Response(200, _atom("wallstreetbets", "t3_ok")),
            _Response(429, ""),
        ],
    })
    first = collect_reddit(client, now=1000)
    second = collect_reddit(client, now=1075)
    success = collect_reddit(client, now=1225)
    third = collect_reddit(client, now=1300)
    assert first["retry_after"] == 75
    assert second["retry_after"] == 150
    assert success["inserted"] == 1
    assert third["retry_after"] == 75
    assert [url for url, _headers in client.calls] == [_WSB, _WSB, _WSB, _WSB]
    dated = formatdate(2000 + 200, usegmt=True)
    dated_client = _Client({_WSB: _Response(429, "", {"Retry-After": dated})})
    assert collect_reddit(dated_client, now=2000)["retry_after"] == 200


def test_other_errors_wait_seventy_five_seconds_and_retry_same_sub(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    _clear(monkeypatch)
    broken = _Client({
        _WSB: [
            _Response(200, "<rss></rss>"),
            _Response(503, "down"),
            _Response(200, _atom("wallstreetbets", "t3_ok")),
        ],
    })
    failed = collect_reddit(broken, now=1000)
    assert "error" in failed
    assert failed["retry_after"] == 75
    denied = collect_reddit(broken, now=1075)
    assert denied["retry_after"] == 75
    assert "503" in denied["error"]
    assert collect_reddit(broken, now=1150)["inserted"] == 1
    assert [url for url, _headers in broken.calls] == [_WSB, _WSB, _WSB]

    down = _Down()
    reset_store_for_tests(tmp_path / "down.sqlite")
    error = collect_reddit(down, now=1000)
    assert error["retry_after"] == 75
    assert collect_reddit(down, now=1074)["skipped"] is True
    assert down.calls == [_WSB]


def test_subreddit_list_is_configurable_and_fails_closed(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    _clear(monkeypatch)
    monkeypatch.setenv("NEWS_REDDIT_SUBREDDITS", "r/Stocks,stocks,../x,a")
    assert reddit_subreddits() == ("stocks",)
    client = _Client({_STOCKS: _Response(200, _atom("stocks", "t3_only"))})
    assert collect_reddit(client, now=1000)["inserted"] == 1
    assert [url for url, _headers in client.calls] == [_STOCKS]

    monkeypatch.setenv("NEWS_REDDIT_SUBREDDITS", "../x,!!")
    assert reddit_subreddits() == ()
    boom = _Down()
    failed = collect_reddit(boom, now=2000)
    assert failed["error"]
    assert boom.calls == []
    health = {row["source"]: row for row in get_store().health_rows()}
    assert health["reddit"]["last_error"]


def test_oauth_settings_are_not_sent(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    _clear(monkeypatch)
    from app.config import settings

    monkeypatch.setattr(settings, "reddit_client_id", "client-id")
    monkeypatch.setattr(settings, "reddit_client_secret", "super-secret")
    monkeypatch.setenv("NEWS_REDDIT_SUBREDDITS", "wallstreetbets")
    assert reddit_oauth_ready() is True
    client = _Client({_WSB: _Response(200, _atom("wallstreetbets", "t3_plain"))})
    assert collect_reddit(client, now=1000)["inserted"] == 1
    sent = client.calls[0][1]
    assert "Authorization" not in sent
    assert "super-secret" not in json.dumps(sent)
    assert "access_token" not in client.calls[0][0]
    monkeypatch.setattr(settings, "reddit_client_secret", "")
    assert reddit_oauth_ready() is False


def test_llm_sees_summary_not_image_urls(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    _clear(monkeypatch)
    monkeypatch.setenv("NEWS_REDDIT_SUBREDDITS", "wallstreetbets")
    monkeypatch.setattr("app.news.service.llm_extract_enabled", lambda: True)
    import app.news.service as news_service
    news_service._LLM_TIMES.clear()
    prompts: list[str] = []

    def fake(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"summary_zh": "摘要", "stocks": [], "sectors": []}, ensure_ascii=False)

    monkeypatch.setattr("app.news.service._llm_text", fake)
    client = _Client({_WSB: _Response(200, _xml("reddit_wsb.xml"))})
    assert collect_reddit(client, now=1000)["inserted"] == 2
    assert prompts
    assert any("subprime mortgage bubble" in prompt for prompt in prompts)
    assert all("preview.redd.it" not in prompt for prompt in prompts)
    assert all("i.redd.it" not in prompt for prompt in prompts)


def test_next_delay_honors_reddit_retry_after_only():
    morning = datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ)
    night = datetime(2026, 10, 9, 23, 30, tzinfo=CN_TZ)
    assert next_delay("reddit", {"retry_after": 120}, morning) == 120
    assert next_delay("reddit", {}, morning) == 75
    assert next_delay("reddit", {"retry_after": 0}, night) == 75
    assert next_delay("reddit", {"retry_after": 99999}, morning) == 3600
    assert next_delay("reddit", {"retry_after": True}, morning) == 75
    assert next_delay("cnbc", {"retry_after": 120}, morning) == 300
    assert next_delay("cnbc", {"retry_after": 120}, night) == 300


def test_scheduler_uses_reddit_retry_after(monkeypatch):
    from app.news.scheduler import NewsScheduler

    morning = datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ)
    monkeypatch.setattr("app.news.scheduler.cn_now", lambda: morning)
    monkeypatch.setattr("app.news.scheduler.source_enabled", lambda source: source == "reddit")
    monkeypatch.setattr("app.news.scheduler.backfill_mentions", lambda lexicon: None)
    monkeypatch.setattr("app.news.scheduler.get_lexicon", lambda: None)
    monkeypatch.setattr("app.news.push.tick", lambda now: None)
    sched = NewsScheduler()

    def due(source):
        sched._stop.set()
        assert source == "reddit"
        return {"inserted": 0, "duplicate": 0, "retry_after": 120}

    monkeypatch.setattr("app.news.scheduler.run_due", due)
    sched._loop()
    assert sched._next["reddit"] == morning.timestamp() + 120


def test_run_due_dispatches_reddit(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        "app.news.service.collect_reddit",
        lambda client=None, now=None: seen.append("reddit") or {"inserted": 0, "duplicate": 0},
    )
    assert run_due("reddit") == {"inserted": 0, "duplicate": 0}
    assert seen == ["reddit"]


def _forbid_sleep(*_args, **_kwargs):
    raise AssertionError("Reddit 采集不能 sleep")


class _Down:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url, headers=None):
        self.calls.append(url)
        raise RuntimeError("connection reset")
