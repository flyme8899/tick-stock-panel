"""ima 个人知识库的券商研报全文。不出网，签名链接不能落盘。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app.config import Settings, settings
from app.market_time import CN_TZ
from app.news.extract import Lexicon
from app.news.service import (
    feed_for_source,
    get_store,
    health_payload,
    reset_store_for_tests,
    run_due,
)
from app.research_reports import ima_store
from app.research_reports.ima_jobs import (
    HOT_CAP,
    LIST_LIMIT,
    MIN_GAP_S,
    sync_ima_research,
)
from app.research_reports.ima_schedule import (
    ImaResearchWorker,
    due_slot_keys,
    parse_clocks,
)
from app.research_reports.ima_text import short_summary, split_report_title, split_source_header

_KB = "kb-from-env"
_TOKEN = "signtoken-do-not-store"
_MARK = "正文末尾独有标记ZZZ"
_LEXICON = Lexicon([("601012.SH", "隆基绿能", "601012")])
_NOW = datetime(2026, 10, 12, 6, 40, tzinfo=CN_TZ)


def test_title_and_source_note_stay_out_of_the_summary():
    broker, title = split_report_title("「招商证券｜光伏链条跟踪.md」")
    assert broker == "招商证券"
    assert title == "光伏链条跟踪"
    note, prose = split_source_header("<!-- 来源：内部转存 -->\n来源：东财\n# 标题\n\n组件排产回升。")
    assert "内部转存" in note
    assert "东财" in note
    summary = short_summary(prose, "光伏链条跟踪")
    assert summary == "组件排产回升。"
    assert "来源" not in summary
    assert "内部转存" not in summary


def test_clocks_default_to_the_two_morning_slots(monkeypatch):
    monkeypatch.delenv("IMA_RESEARCH_TIMES", raising=False)
    monkeypatch.setattr(settings, "ima_research_times", "06:30,08:30")
    from app.news.config import ima_research_times

    assert ima_research_times() == ("06:30", "08:30")
    assert parse_clocks("7:05, 18:00, bad, 25:99") == ("07:05", "18:00")
    monkeypatch.setenv("IMA_RESEARCH_TIMES", "nope")
    assert ima_research_times() == ("06:30", "08:30")
    early = datetime(2026, 10, 12, 6, 29, tzinfo=CN_TZ)
    at_open = datetime(2026, 10, 12, 6, 30, tzinfo=CN_TZ)
    both = datetime(2026, 10, 12, 8, 30, tzinfo=CN_TZ)
    assert due_slot_keys(early, ("06:30", "08:30"), []) == []
    assert due_slot_keys(at_open, ("06:30", "08:30"), []) == ["2026-10-12 06:30"]
    assert due_slot_keys(both, ("06:30", "08:30"), ["2026-10-12 06:30"]) == ["2026-10-12 08:30"]


def test_knowledge_base_id_is_not_hardcoded():
    assert Settings.model_fields["ima_research_kb_id"].default == ""
    needle = "ak-X2lYob-kG7ynbrrHCsGUY4g6kjGEW1R10JlE24lo="
    root = Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        assert needle not in path.read_text(encoding="utf-8")


def test_incremental_download_keeps_signed_urls_out_of_storage(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    monkeypatch.setattr("app.research_reports.ima_jobs.HOT_CAP", 1)
    clock = _Clock()
    client = _Client()
    headers = {"ima-openapi-clientid": "client", "ima-openapi-apikey": "secret"}
    first = sync_ima_research(
        tmp_path,
        client=client,
        kb_id=_KB,
        headers=headers,
        lexicon=_LEXICON,
        now=_NOW,
        sleep=clock.sleep,
        clock=clock.now,
    )
    assert first["ok"] is True
    assert first["inserted"] == 2
    assert first["hot"] == 1
    assert HOT_CAP == 24
    assert LIST_LIMIT == 20
    assert MIN_GAP_S >= 0.6
    listed = [item for item in client.calls if item.get("method") == "get_knowledge_list"]
    assert listed
    assert {item["json"]["limit"] for item in listed} == {20}
    assert {item["json"]["knowledge_base_id"] for item in listed} == {_KB}
    root_calls = [item for item in listed if "folder_id" not in item["json"]]
    assert [item["json"]["cursor"] for item in root_calls] == ["", "page-2"]
    media = [item for item in client.calls if item.get("method") == "get_media_info"]
    assert [item["json"]["media_id"] for item in media] == ["m-new", "m-mid"]
    downloads = [item for item in client.calls if item["kind"] == "get"]
    assert [item["url"] for item in downloads] == [
        f"https://files.example/m-new?token={_TOKEN}",
        f"https://files.example/m-mid?token={_TOKEN}",
    ]
    assert downloads[0]["headers"]["Authorization"] == "signed-header"
    assert [item["timeout"] for item in media] == [20.0, 20.0]
    assert [item["timeout"] for item in downloads] == [30.0, 30.0]
    assert clock.slept
    assert min(clock.slept) >= 0.6
    _assert_media_then_download(client.calls)

    rows = ima_store.for_code(tmp_path, "601012.SH")
    assert len(rows) == 1
    assert rows[0]["broker"] == "招商证券"
    assert rows[0]["title"] == "光伏链条跟踪"
    assert rows[0]["publish_date"] == "2026-10-11"
    assert "内部转存" in rows[0]["source_note"]
    assert _MARK in rows[0]["body"]
    assert "来源" not in rows[0]["summary"]
    assert _MARK not in rows[0]["summary"]
    assert len(rows[0]["summary"]) <= 160

    news = list(get_store()._conn.execute(
        "SELECT source_id, title, clean_text, raw_json FROM news_items WHERE source = 'ima_research'",
    ))
    assert len(news) == 1
    assert news[0]["source_id"] == "m-new"
    assert _MARK not in news[0]["clean_text"]
    assert _TOKEN not in news[0]["clean_text"]
    assert _TOKEN not in news[0]["raw_json"]
    assert len(news[0]["clean_text"]) <= 160
    assert "组件排产" in news[0]["clean_text"]
    blob = ima_store.db_path(tmp_path).read_bytes().decode("utf-8", errors="ignore")
    state = ima_store.state_path(tmp_path).read_text(encoding="utf-8")
    assert _TOKEN not in blob
    assert _TOKEN not in state
    health = next(item for item in health_payload()["sources"] if item["id"] == "ima_research")
    assert health["last_inserted"] == 2
    assert health["enabled"] is True
    assert "601012.SH" in feed_for_source("ima_research")["items"][0]["symbols"]

    client.calls.clear()
    clock.slept.clear()
    second = sync_ima_research(
        tmp_path,
        client=client,
        kb_id=_KB,
        headers=headers,
        lexicon=_LEXICON,
        now=_NOW,
        sleep=clock.sleep,
        clock=clock.now,
    )
    assert second["inserted"] == 0
    assert [item for item in client.calls if item.get("method") == "get_media_info"] == []
    assert [item for item in client.calls if item["kind"] == "get"] == []


def test_download_error_does_not_keep_the_signed_url(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)

    class Broken:
        def post(self, url, headers=None, json=None, timeout=None):
            raise RuntimeError(f"HTTP 500 https://files.example/x?token={_TOKEN}")

        def get(self, url, headers=None, timeout=None):
            raise AssertionError("列表失败后不应该下载")

    result = sync_ima_research(
        tmp_path,
        client=Broken(),
        kb_id=_KB,
        headers={},
        lexicon=_LEXICON,
        now=_NOW,
        sleep=lambda _seconds: None,
        clock=lambda: 0.0,
        gap=0,
    )
    assert result["ok"] is False
    assert result["listed"] is False
    state = ima_store.state_path(tmp_path).read_text(encoding="utf-8")
    assert _TOKEN not in state
    health = next(item for item in health_payload()["sources"] if item["id"] == "ima_research")
    assert _TOKEN not in health["last_error"]
    assert health["last_inserted"] == 0


def test_retention_drops_reports_older_than_sixty_days(tmp_path):
    ima_store.insert_report(tmp_path, {
        "media_id": "old",
        "broker": "招商证券",
        "publish_date": "2026-01-01",
        "title": "旧稿",
        "body": "旧正文",
        "summary": "旧",
        "source_note": "",
        "symbols": [{"symbol": "601012.SH", "name": "隆基绿能", "code": "601012"}],
        "ingested_at": "2026-01-01T20:00:00+08:00",
    })
    ima_store.insert_report(tmp_path, {
        "media_id": "new",
        "broker": "招商证券",
        "publish_date": "2026-10-11",
        "title": "新稿",
        "body": "新正文",
        "summary": "新",
        "source_note": "",
        "symbols": [{"symbol": "601012.SH", "name": "隆基绿能", "code": "601012"}],
        "ingested_at": "2026-10-11T20:00:00+08:00",
    })
    removed = ima_store.apply_retention(tmp_path, datetime(2026, 10, 12, tzinfo=CN_TZ).date())
    assert removed == 1
    assert [row["media_id"] for row in ima_store.for_code(tmp_path, "601012")] == ["new"]


def test_schedule_runs_configured_slots_and_cools_down_after_failure(tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    monkeypatch.setenv("IMA_RESEARCH_TIMES", "06:30,08:30")
    calls = []

    def runner(data_dir, stop):
        calls.append(stop)
        return {"ok": True, "listed": True, "inserted": 4}

    worker = ImaResearchWorker(tmp_path)
    moment = datetime(2026, 10, 12, 6, 30, tzinfo=CN_TZ)
    assert worker.run_once(moment, runner=runner) == "2026-10-12 06:30"
    assert worker.run_once(moment, runner=runner) is None
    assert len(calls) == 1
    later = datetime(2026, 10, 12, 8, 30, tzinfo=CN_TZ)
    assert worker.run_once(later, runner=runner) == "2026-10-12 08:30"
    assert len(calls) == 2

    monkeypatch.setenv("IMA_RESEARCH_TIMES", "09:00")
    other = ImaResearchWorker(tmp_path / "other")
    assert other.run_once(later, runner=runner) is None

    def boom(data_dir, stop):
        calls.append("boom")
        raise RuntimeError("down")

    monkeypatch.setenv("IMA_RESEARCH_TIMES", "06:30,08:30")
    failed = ImaResearchWorker(tmp_path / "failed")
    assert failed.run_once(moment, runner=boom) == "error"
    assert failed.run_once(moment + timedelta(minutes=1), runner=boom) is None
    assert failed.run_once(moment + timedelta(minutes=11), runner=boom) == "error"
    monkeypatch.delenv("NEWS_IMA_RESEARCH_ENABLED")
    monkeypatch.setattr(settings, "news_ima_research_enabled", "")
    quiet = []
    assert ImaResearchWorker(tmp_path).run_once(
        later, runner=lambda data_dir, stop: quiet.append(1),
    ) is None
    assert quiet == []


def test_context_line_and_poll_does_not_fetch(tmp_path, monkeypatch):
    ima_store.insert_report(tmp_path, {
        "media_id": "m1",
        "broker": "招商证券",
        "publish_date": "2026-10-11",
        "title": "光伏链条跟踪",
        "body": "很长的正文" * 50,
        "summary": "组件排产回升",
        "source_note": "来源：内部转存",
        "symbols": [{"symbol": "601012.SH", "name": "隆基绿能", "code": "601012"}],
        "ingested_at": "2026-10-12T06:40:00+08:00",
    })
    from app.custom.dsa.stock_context import render_context

    lines = ima_store.context_lines(tmp_path, "601012.SH")
    payload = render_context(
        symbol="601012.SH",
        now=_NOW,
        events=[],
        red_news=[],
        flow_rows=[],
        quote={"trade_date": None, "auction_price": None, "prev_close": None, "volume": None, "avg_volume_5d": None, "sample_days": 0},
        ima_lines=lines,
    )
    assert "招商证券" in payload["text"]
    assert "光伏链条跟踪" in payload["text"]
    assert "很长的正文" not in payload["text"]
    assert run_due("ima_research")["skipped"] is True


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class _Response:
    def __init__(self, payload=None, content: bytes = b""):
        self._payload = payload or {}
        self.content = content
        self.status_code = 200
        self.text = content.decode()

    def json(self):
        return self._payload


class _Client:
    def __init__(self):
        self.calls: list[dict] = []
        self.n = 0

    def post(self, url, headers=None, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        body = dict(json or {})
        self.calls.append({"kind": "post", "method": method, "json": body, "timeout": timeout, "headers": headers})
        if method == "get_media_info":
            media_id = body["media_id"]
            return _Response({"retcode": 0, "data": {"url_info": {
                "url": f"https://files.example/{media_id}?token={_TOKEN}",
                "headers": {"Authorization": "signed-header"},
            }}})
        return _Response(self._page(body))

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"kind": "get", "url": url, "timeout": timeout, "headers": headers or {}})
        if url.endswith(f"m-new?token={_TOKEN}"):
            text = (
                "<!-- 来源：内部转存 -->\n"
                "# 招商证券｜光伏链条跟踪\n\n"
                "组件排产回升，关注隆基绿能（601012.SH）。\n"
                + ("甲" * 400)
                + _MARK
            )
        else:
            text = "# 中信证券｜宏观周报\n\n利率维持。\n"
        return _Response(content=text.encode())

    def _page(self, body: dict) -> dict:
        folder = body.get("folder_id") or ""
        cursor = body.get("cursor") or ""
        if folder == "folder_broker":
            return {"retcode": 0, "data": {"knowledge_list": [
                {"media_id": "folder_1011", "title": "2026-10-11"},
                {"media_id": "folder_1010", "title": "2026-10-10"},
                {"media_id": "folder_1009", "title": "2026-10-09"},
            ], "is_end": True}}
        if folder == "folder_1011":
            return {"retcode": 0, "data": {"knowledge_list": [
                {"media_id": "m-new", "title": "招商证券｜光伏链条跟踪.md"},
                {"media_id": "m-pdf", "title": "图表.pdf"},
            ], "is_end": True}}
        if folder == "folder_1010":
            return {"retcode": 0, "data": {"knowledge_list": [
                {"media_id": "m-mid", "title": "中信证券｜宏观周报.md"},
            ], "is_end": True}}
        if folder == "folder_1009":
            return {"retcode": 0, "data": {"knowledge_list": [
                {"media_id": "m-old", "title": "招商证券｜更早的稿.md"},
            ], "is_end": True}}
        if cursor == "":
            return {"retcode": 0, "data": {
                "knowledge_list": [{"media_id": "folder_broker", "title": "招商证券"}],
                "is_end": False,
                "next_cursor": "page-2",
            }}
        return {"retcode": 0, "data": {"knowledge_list": [], "is_end": True}}


def _enable(monkeypatch, data_dir: Path) -> None:
    monkeypatch.setattr(settings, "data_dir", data_dir)
    monkeypatch.setattr(settings, "ima_client_id", "client")
    monkeypatch.setattr(settings, "ima_api_key", "secret")
    monkeypatch.setenv("IMA_RESEARCH_KB_ID", _KB)
    monkeypatch.setenv("NEWS_IMA_RESEARCH_ENABLED", "true")
    reset_store_for_tests(data_dir / "news.sqlite")


def _assert_media_then_download(calls: list[dict]) -> None:
    kinds = [(item.get("method") or item.get("kind"), item.get("json", {}).get("media_id") or item.get("url")) for item in calls]
    new_at = kinds.index(("get_media_info", "m-new"))
    assert kinds[new_at + 1][0] == "get"
    assert "m-new" in str(kinds[new_at + 1][1])


def test_page_cap_counts_requests(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("app.research_reports.ima_jobs.ROOT_PAGE_CAP", 3)
    seen = {"n": 0}

    class Loop:
        def post(self, url, headers=None, json=None, timeout=None):
            seen["n"] += 1
            assert json["limit"] == 20
            return _Response({"retcode": 0, "data": {
                "knowledge_list": [{"media_id": f"folder_{seen['n']}", "title": "2026-10-01"}],
                "is_end": False,
                "next_cursor": f"c{seen['n']}",
            }})

        def get(self, url, headers=None, timeout=None):
            raise AssertionError("不应下载")

    result = sync_ima_research(
        tmp_path,
        client=Loop(),
        kb_id=_KB,
        headers={},
        lexicon=_LEXICON,
        now=_NOW,
        sleep=lambda _seconds: None,
        clock=lambda: 0.0,
        gap=0,
    )
    assert seen["n"] == 3
    assert result["listed"] is True
    assert result["inserted"] == 0
