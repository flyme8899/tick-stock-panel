"""东财 7×24 只写归档：解析、翻页回补、碰到已归档即停、不写 news.sqlite。"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from app.market_time import CN_TZ
from app.news import em724_archive as em
from app.news.archive import NewsArchive

NOW = datetime(2026, 10, 17, 8, 0, tzinfo=CN_TZ)


def _item(i: int, when: datetime) -> dict:
    return {"code": f"2026{i:08d}", "showTime": when.strftime("%Y-%m-%d %H:%M:%S"),
            "title": f"t{i}", "summary": f"s{i}", "stockList": ["1.600000", "90.BK1366"],
            "realSort": str(10_000_000 - i)}


def _client(pages: dict[str, list[dict]], calls: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        end = request.url.params.get("sortEnd", "")
        calls.append(end)
        assert request.url.params["biz"] == "web_724"
        return httpx.Response(200, json={"code": "1", "data": {"fastNewsList": pages.get(end, [])}})
    return httpx.Client(transport=httpx.MockTransport(handler))


def _pages(items: list[dict], size: int = 3) -> dict[str, list[dict]]:
    out, end = {}, ""
    for k in range(0, len(items), size):
        chunk = items[k:k + size]
        out[end] = chunk
        end = chunk[-1]["realSort"]
    return out


def test_parse_row():
    row = em.parse_row(_item(1, NOW))
    assert row["source"] == "eastmoney_724" and row["level"] == ""
    assert row["published_at"] == NOW
    assert row["stock_codes"] == ["1.600000", "90.BK1366"]
    assert row["url"].endswith("/202600000001.html")
    assert em.parse_row({"code": "x"}) is None
    assert em.parse_row({"code": "x", "showTime": "bad"}) is None


def test_catchup_pages_back_until_overlap(tmp_path):
    a = NewsArchive(tmp_path / "a.sqlite")
    items = [_item(i, NOW - timedelta(minutes=10 * i)) for i in range(10)]
    a.insert_many([em.parse_row(items[7])])  # 上次停机前已归档到第 7 条
    calls: list[str] = []
    got = em.collect(_client(_pages(items), calls), archive=a, now=NOW)
    assert got["stop"] == "overlap" and got["pages"] == 3
    assert got["inserted"] == 8  # 0..6 新增，第 8 条与 7 同页一起写入
    assert len(a.query(source="eastmoney_724")) == 9
    # 下一轮只拉第一页就停。
    calls.clear()
    got = em.collect(_client(_pages(items), calls), archive=a, now=NOW)
    assert got == {"inserted": 0, "pages": 1, "stop": "overlap"} and calls == [""]


def test_catchup_capped_at_24h(tmp_path):
    a = NewsArchive(tmp_path / "a.sqlite")
    items = [_item(i, NOW - timedelta(hours=5 * i)) for i in range(12)]
    got = em.collect(_client(_pages(items), []), archive=a, now=NOW)
    assert got["stop"] == "floor"
    assert got["inserted"] == 5  # 0,5,10,15,20 小时
    oldest = a.query(source="eastmoney_724")[-1]["published_at"]
    assert oldest >= (NOW - timedelta(hours=24)).isoformat()


def test_empty_and_max_pages(tmp_path, monkeypatch):
    a = NewsArchive(tmp_path / "a.sqlite")
    assert em.collect(_client({}, []), archive=a, now=NOW)["stop"] == "empty"
    monkeypatch.setattr(em, "MAX_PAGES", 2)
    items = [_item(i, NOW - timedelta(minutes=i)) for i in range(9)]
    got = em.collect(_client(_pages(items), []), archive=a, now=NOW)
    assert got == {"inserted": 6, "pages": 2, "stop": "max_pages"}


def test_http_error_raises_and_writes_nothing(tmp_path):
    a = NewsArchive(tmp_path / "a.sqlite")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    try:
        em.collect(client, archive=a, now=NOW)
        raise AssertionError("应抛错")
    except httpx.HTTPStatusError:
        pass
    assert a.query() == []


def test_not_a_live_news_source():
    from app.news.config import SOURCE_ORDER

    assert "eastmoney_724" not in SOURCE_ORDER
    import inspect

    src = inspect.getsource(em)
    for banned in ("news.sqlite", "ingest_items", "push", "hot_"):
        assert banned not in src.split('"""', 2)[2]


def test_scheduler_runs_archive_step_and_swallows_errors(monkeypatch):
    from app.news import scheduler as sch

    calls = []
    monkeypatch.setattr(sch, "_em724_collect", lambda: calls.append(1) or (_ for _ in ()).throw(RuntimeError("x")))
    s = sch.NewsScheduler()
    s._run_em724(1000.0)
    assert calls == [1] and s._next["em724_archive"] == 1000.0 + 120
    s._run_em724(1001.0)  # 未到期不跑
    assert calls == [1]
    monkeypatch.setattr(sch, "_em724_collect", lambda: {"inserted": 3, "pages": 1, "stop": "overlap"})
    s._run_em724(1200.0)
    assert s._next["em724_archive"] == 1200.0 + sch.EM724_POLL_SECONDS
