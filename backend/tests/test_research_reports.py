"""研报列表解析和增量同步。不下载 PDF，请求里也不出现 pdf 域名。

合成夹具按 simonlin1212/a-stock-data SKILL.md 的 reportapi 字段写成。
``recorded_stock_reports.json`` 是东财个股研报列表裁到 1 条的真实响应。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.research_reports import store
from app.research_reports.jobs import sync_industry, sync_symbol
from app.research_reports.parse import parse_stock_reports, pdf_url, research_code

_FIXTURES = Path(__file__).parent / "fixtures" / "research"
_TODAY = date(2026, 10, 11)


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, payload) -> None:
        self.status_code = 200
        self._payload = payload
        self.url = "https://reportapi.eastmoney.com/report/list"

    def json(self):
        return self._payload


def test_stock_report_builds_pdf_url_without_fetching_it() -> None:
    rows = parse_stock_reports(_load("stock_reports.json"), "600519.SH")
    assert rows is not None
    assert rows[0]["pdf_url"] == pdf_url("AP20261009001")
    assert rows[0]["pdf_url"] == "https://pdf.dfcfw.com/pdf/H3_AP20261009001_1.pdf"
    assert rows[0]["broker"] == "中信证券"
    assert rows[0]["rating"] == "买入"
    assert rows[0]["eps_this"] == 68.5
    assert rows[0]["eps_next"] == 75.2
    assert rows[1]["eps_this"] is None
    assert rows[0]["symbol"] == "600519.SH"


def test_recorded_stock_report_keeps_pdf_link_only() -> None:
    rows = parse_stock_reports(_load("recorded_stock_reports.json"), "600519")
    assert rows is not None and len(rows) == 1
    row = rows[0]
    assert row["info_code"] == "AP202609211829719676"
    assert row["publish_date"] == "2026-09-21"
    assert row["eps_this"] == pytest.approx(65.14)
    assert row["pdf_url"] == "https://pdf.dfcfw.com/pdf/H3_AP202609211829719676_1.pdf"
    assert row["broker"] == "诚通证券"


def test_old_bj_and_index_codes_are_rejected() -> None:
    with pytest.raises(ValueError, match="43/83/87"):
        research_code("830001.BJ")
    with pytest.raises(ValueError, match="43/83/87"):
        research_code("430001")
    with pytest.raises(ValueError, match="指数"):
        research_code("000300.SH")


def test_incremental_sync_stops_at_known_code_and_ingests_research(tmp_path, monkeypatch) -> None:
    payload = _load("stock_reports.json")
    calls: list[dict] = []

    def fetch(url, *, params=None, headers=None, timeout=None):
        assert "pdf.dfcfw.com" not in url
        assert not str(url).lower().endswith(".pdf")
        calls.append(params or {})
        return _Resp(payload)

    ingested: list = []

    def fake_ingest(items, lexicon):
        ingested.extend(items)
        assert lexicon.by_code == {}
        return {"inserted": len(items)}

    monkeypatch.setattr("app.news.config.source_enabled", lambda source: source == "research")
    monkeypatch.setattr("app.news.service.ingest_items", fake_ingest)
    monkeypatch.setattr(
        "app.news.service.get_store",
        lambda: type("Store", (), {"mark_health": lambda *args, **kwargs: None})(),
    )
    first = sync_symbol(tmp_path, "600519.SH", get=fetch, today=_TODAY)
    assert first["new"] == 2
    assert first["fetched_pages"] == 1
    assert calls[0]["qType"] == "0"
    assert calls[0]["pageSize"] == "50"
    assert calls[0]["code"] == "600519"
    saved = store.read_partition(tmp_path, "stock", "2026-10-09")
    assert saved["info_code"].to_list() == ["AP20261009001"]
    assert [item.source for item in ingested] == ["research", "research"]
    assert ingested[0].url.startswith("https://pdf.dfcfw.com/")

    calls.clear()
    ingested.clear()
    second = sync_symbol(tmp_path, "600519", get=fetch, today=_TODAY)
    assert second["new"] == 0
    assert second["fetched_pages"] == 1
    assert len(calls) == 1
    assert ingested == []


def test_new_report_after_a_known_code_does_not_request_page_two(tmp_path) -> None:
    known = _load("stock_reports.json")
    known["TotalPage"] = 1
    fresh = json.loads(json.dumps(known))
    fresh["data"] = [
        known["data"][0],
        {
            "infoCode": "AP20261010003",
            "title": "提价跟踪",
            "orgSName": "中金公司",
            "emRatingName": "买入",
            "predictThisYearEps": "69.0",
            "publishDate": "2026-10-10",
        },
    ]
    fresh["TotalPage"] = 2
    mode = {"fresh": False}
    pages: list[str] = []

    def fetch(_url, *, params=None, headers=None, timeout=None):
        pages.append(str((params or {}).get("pageNo")))
        if pages[-1] == "2":
            raise AssertionError("已经见到旧研报，不应再翻页")
        return _Resp(fresh if mode["fresh"] else known)

    sync_symbol(tmp_path, "600519.SH", get=fetch, today=_TODAY, ingest=False)
    mode["fresh"] = True
    again = sync_symbol(tmp_path, "600519.SH", get=fetch, today=_TODAY, ingest=False)
    assert pages == ["1", "1"]
    assert again["new"] == 1
    codes = store.read_partition(tmp_path, "stock", "2026-10-10")["info_code"].to_list()
    assert codes == ["AP20261010003"]


def test_research_routes_read_local_rows(tmp_path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.research import router

    rows = parse_stock_reports(_load("stock_reports.json"), "600519.SH") or []
    store.write_rows(tmp_path, "stock", "2026-10-09", rows[:1])
    monkeypatch.setattr("app.api.research._dir", lambda: tmp_path)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    listed = client.get("/api/research/stock/600519.SH")
    assert listed.status_code == 200
    body = listed.json()["reports"][0]
    assert body["title"] == "贵州茅台：需求稳健"
    assert body["pdf_url"].startswith("https://pdf.dfcfw.com/pdf/H3_")
    rejected = client.get("/api/research/stock/830001.BJ")
    assert rejected.status_code == 400


def test_industry_window_is_fourteen_days(tmp_path) -> None:
    seen: dict = {}

    def fetch(_url, *, params=None, headers=None, timeout=None):
        seen.update(params or {})
        assert "pdf.dfcfw.com" not in _url
        return _Resp(_load("industry_reports.json"))

    result = sync_industry(tmp_path, get=fetch, today=_TODAY, ingest=False)
    assert result["new"] == 1
    assert seen["qType"] == "1"
    assert seen["beginTime"] == "2026-09-27"
    assert seen["pageSize"] == "50"
    frame = store.read_partition(tmp_path, "industry", "2026-10-09")
    assert frame["title"].to_list() == ["电力设备：排产上行"]
    assert frame["pdf_url"][0].endswith("H3_AP20261009088_1.pdf")
