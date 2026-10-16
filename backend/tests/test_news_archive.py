"""历史资讯归档库：去重、查询、接口、实时抄送和导入脚本。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import news as news_api
from app.market_time import CN_TZ, cn_now
from app.news import archive as archive_mod
from app.news.archive import NewsArchive
from app.news.collectors import Item
from app.news.extract import Lexicon
from app.news.service import ingest_items, reset_store_for_tests


def _row(sid, when, **kw):
    base = {"source": "cls", "source_id": sid, "published_at": when, "title": f"t{sid}",
            "content": f"正文{sid}", "level": "C", "stock_codes": [], "url": ""}
    base.update(kw)
    return base


def test_insert_ignores_duplicates_and_keeps_first(tmp_path):
    a = NewsArchive(tmp_path / "a.sqlite")
    t = datetime(2025, 1, 2, 9, 30, tzinfo=CN_TZ)
    assert a.insert_many([_row("1", t), _row("2", t)]) == {"inserted": 2, "duplicate": 0}
    got = a.insert_many([_row("1", t, title="改过"), _row("1", t, source="eastmoney_724")])
    assert got == {"inserted": 1, "duplicate": 1}
    rows = a.query(source="cls")
    assert {r["title"] for r in rows} == {"t1", "t2"}
    a.close()


def test_query_filters(tmp_path):
    a = NewsArchive(tmp_path / "a.sqlite")
    t0 = datetime(2024, 9, 1, 8, 0, tzinfo=CN_TZ)
    a.insert_many([
        _row("1", t0, level="B", content="宁德时代 100%"),
        _row("2", t0 + timedelta(days=1), level="C", stock_codes=["sz300750"]),
        _row("3", t0 + timedelta(days=2), source="eastmoney_724", level="", title="并购_重组"),
    ])
    assert [r["source_id"] for r in a.query()] == ["3", "2", "1"]
    assert [r["source_id"] for r in a.query(levels=["B"])] == ["1"]
    assert [r["source_id"] for r in a.query(keyword="宁德")] == ["1"]
    assert [r["source_id"] for r in a.query(keyword="100%")] == ["1"]
    assert a.query(keyword="0%x") == []
    assert [r["source_id"] for r in a.query(keyword="并购_")] == ["3"]
    rng = a.query(start=t0 + timedelta(hours=1), end=t0 + timedelta(days=2))
    assert [r["source_id"] for r in rng] == ["2"]
    assert rng[0]["stock_codes"] == ["sz300750"]
    assert len(a.query(limit=1)) == 1
    a.close()


def test_api_reads_archive(tmp_path):
    a = archive_mod.reset_archive_for_tests(tmp_path / "news_archive.sqlite")
    t = datetime(2025, 3, 3, 10, 0, tzinfo=CN_TZ)
    a.insert_many([_row("9", t, level="A"), _row("8", t - timedelta(days=400), level="C")])
    app = FastAPI()
    app.include_router(news_api.router)
    client = TestClient(app)
    body = client.get("/api/news/archive", params={"level": "A,B"}).json()
    assert [i["source_id"] for i in body["items"]] == ["9"]
    body = client.get("/api/news/archive", params={"start": "2023-01-01", "end": "2024-12-31"}).json()
    assert [i["source_id"] for i in body["items"]] == ["8"]
    assert client.get("/api/news/archive", params={"limit": 9999}).status_code == 422
    stats = client.get("/api/news/archive/stats").json()
    assert stats["sources"][0]["n"] == 2


def test_live_cls_items_are_copied_and_survive_retention(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    a = archive_mod.get_archive()
    assert a.path == tmp_path / "news_archive.sqlite"
    now = cn_now()
    ingest_items([
        Item(source="cls", source_id="live-1", published_at=now, title="标题",
             text="一条足够长的财联社快讯正文，用来测试归档。", level="B"),
        Item(source="wscn", source_id="w-1", published_at=now, title="见闻",
             text="华尔街见闻不进归档，这里写长一点。"),
    ], Lexicon([], []))
    rows = a.query()
    assert [(r["source"], r["source_id"], r["level"]) for r in rows] == [("cls", "live-1", "B")]
    # 再来一次不重复。
    ingest_items([Item(source="cls", source_id="live-1", published_at=now, text="x" * 20)],
                 Lexicon([], []))
    assert len(a.query()) == 1


def test_archive_failure_does_not_break_ingest(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "news.sqlite")

    def boom(rows):
        raise RuntimeError("disk")

    monkeypatch.setattr(archive_mod.NewsArchive, "insert_many", lambda self, rows: boom(rows))
    got = ingest_items([Item(source="cls", source_id="z", published_at=cn_now(),
                             text="正文足够长足够长足够长。")], Lexicon([], []))
    assert got["inserted"] == 1


def test_import_script_idempotent(tmp_path):
    from scripts.import_news_archive import run

    research = tmp_path / "event_driven_buy"
    pq = research / "backfill_server" / "parquet" / "month=2025-01"
    pq.mkdir(parents=True)
    pl.DataFrame({
        "id": ["e1", "e2", "e1"],
        "publish_time": [datetime(2025, 1, 1, 0, 3)] * 3,
        "title": ["a", "b", "a"],
        "summary": ["sa", "sb", "sa"],
        "stock_codes": [["150.515290"], [], ["150.515290"]],
        "real_sort": ["1", "2", "1"],
        "url": ["u1", "u2", "u1"],
    }).with_columns(pl.col("publish_time").dt.replace_time_zone("Asia/Shanghai")).write_parquet(pq / "x.parquet")
    cls_dir = research / "backfill_cls"
    cls_dir.mkdir()
    recs = [
        {"id": 1932969, "ctime": 1738333868, "level": "B", "title": "美股", "content": "正文",
         "stock_list": [{"StockID": "sz300450", "name": "先导智能"}], "shareurl": "s"},
        {"id": 1932969, "ctime": 1738333868, "level": "B", "title": "美股", "content": "正文"},
        {"id": 5, "ctime": 1738333000, "level": "A", "title": "x", "brief": "简讯"},
        {"bad": True},
    ]
    (cls_dir / "raw_2025-01.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    db = tmp_path / "data" / "news_archive.sqlite"
    first = run(research, db)
    assert first["eastmoney_724"]["inserted"] == 2
    assert first["cls"]["inserted"] == 2
    second = run(research, db)
    assert second["eastmoney_724"]["inserted"] == 0 and second["cls"]["inserted"] == 0
    a = NewsArchive(db)
    cls_rows = a.query(source="cls")
    assert cls_rows[0]["published_at"] == "2025-01-31T22:31:08+08:00"
    assert cls_rows[0]["stock_codes"] == ["sz300450"]
    assert a.query(source="cls", levels=["A"])[0]["content"] == "简讯"
    em = a.query(source="eastmoney_724")
    assert em[0]["published_at"].startswith("2025-01-01T00:03:00+08:00")
    assert run(research, tmp_path / "dry.sqlite", dry_run=True)["cls"]["read"] == 3
    assert not (tmp_path / "dry.sqlite").exists()
    a.close()
