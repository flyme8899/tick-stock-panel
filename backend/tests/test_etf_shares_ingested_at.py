"""ETF 份额入库时刻：首次写入保留，历史缺列留空，读取方不因新列失败。"""
from __future__ import annotations

import csv
import gzip
from datetime import date, datetime

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.fund_flow import etf_inflow, query, store
from app.market_time import CN_TZ
from scripts import export_research_snapshot as snap

DAY = "2026-10-09"
LATER = "2026-10-12"
FIRST = datetime(2026, 10, 9, 16, 40, tzinfo=CN_TZ)
SECOND = datetime(2026, 10, 10, 16, 41, tzinfo=CN_TZ)
THIRD = datetime(2026, 10, 12, 16, 42, tzinfo=CN_TZ)


def _row(code: str, day: str, shares: float, *, name: str | None = None) -> dict:
    return {
        "symbol": code,
        "code": code,
        "name": name or code,
        "trade_date": day,
        "shares": shares,
        "source": "akshare",
    }


def _write_legacy(root, day: str, rows: list[dict]) -> None:
    """上线前的分区：没有入库时刻，测试不能靠这次写入去补。"""
    path = root / "fund_flow" / "etf_shares" / f"date={day}"
    path.mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(path / "part.parquet")


def _stamp(frame: pl.DataFrame, code: str, column: str):
    matched = frame.filter(pl.col("code") == code)
    assert matched.height == 1
    return matched[column][0]


def test_first_write_stamps_ingested_at_and_leaves_updated_at_empty(tmp_path) -> None:
    store.write_rows(tmp_path, "etf_shares", DAY, [_row("510300", DAY, 1e8)], now=FIRST)
    frame = store.read_partition(tmp_path, "etf_shares", DAY)
    assert frame.schema["ingested_at"] == pl.Datetime(time_unit="us", time_zone="Asia/Shanghai")
    assert frame.schema["updated_at"] == pl.Datetime(time_unit="us", time_zone="Asia/Shanghai")
    assert _stamp(frame, "510300", "ingested_at") == FIRST
    assert _stamp(frame, "510300", "updated_at") is None


def test_rewrite_keeps_first_ingested_at_and_sets_updated_at_only_when_values_change(tmp_path) -> None:
    store.write_rows(tmp_path, "etf_shares", DAY, [_row("510300", DAY, 1e8, name="沪深300")], now=FIRST)
    store.write_rows(tmp_path, "etf_shares", DAY, [_row("510300", DAY, 1e8, name="沪深300")], now=SECOND)
    same = store.read_partition(tmp_path, "etf_shares", DAY)
    assert _stamp(same, "510300", "ingested_at") == FIRST
    assert _stamp(same, "510300", "updated_at") is None

    store.write_rows(tmp_path, "etf_shares", DAY, [_row("510300", DAY, 1.1e8, name="沪深300")], now=SECOND)
    changed = store.read_partition(tmp_path, "etf_shares", DAY)
    assert _stamp(changed, "510300", "ingested_at") == FIRST
    assert _stamp(changed, "510300", "updated_at") == SECOND

    store.write_rows(tmp_path, "etf_shares", DAY, [_row("510300", DAY, 1.1e8, name="沪深300")], now=THIRD)
    again = store.read_partition(tmp_path, "etf_shares", DAY)
    assert _stamp(again, "510300", "ingested_at") == FIRST
    assert _stamp(again, "510300", "updated_at") == SECOND


def test_caller_supplied_stamp_is_ignored(tmp_path) -> None:
    row = _row("510300", DAY, 1e8)
    row["ingested_at"] = "2000-01-01T00:00:00+08:00"
    row["updated_at"] = FIRST
    store.write_rows(tmp_path, "etf_shares", DAY, [row], now=FIRST)
    frame = store.read_partition(tmp_path, "etf_shares", DAY)
    assert _stamp(frame, "510300", "ingested_at") == FIRST
    assert _stamp(frame, "510300", "updated_at") is None


def test_partial_rewrite_keeps_rows_that_were_not_sent(tmp_path) -> None:
    store.write_rows(tmp_path, "etf_shares", DAY, [
        _row("510300", DAY, 1e8),
        _row("159915", DAY, 2e8),
    ], now=FIRST)
    store.write_rows(tmp_path, "etf_shares", DAY, [
        _row("510300", DAY, 1.5e8),
        _row("588000", DAY, 3e8),
    ], now=SECOND)
    frame = store.read_partition(tmp_path, "etf_shares", DAY)
    assert _stamp(frame, "159915", "ingested_at") == FIRST
    assert _stamp(frame, "159915", "updated_at") is None
    assert _stamp(frame, "159915", "shares") == pytest.approx(2e8)
    assert _stamp(frame, "510300", "ingested_at") == FIRST
    assert _stamp(frame, "510300", "updated_at") == SECOND
    assert _stamp(frame, "588000", "ingested_at") == SECOND
    assert _stamp(frame, "588000", "updated_at") is None


def test_legacy_partition_stays_null_and_is_not_rewritten_on_read(tmp_path) -> None:
    legacy = [_row("510300", DAY, 2.4e10, name="沪深300ETF")]
    _write_legacy(tmp_path, DAY, legacy)
    raw_path = tmp_path / "fund_flow" / "etf_shares" / f"date={DAY}" / "part.parquet"
    before = raw_path.read_bytes()
    frame = store.read_partition(tmp_path, "etf_shares", DAY)
    assert "ingested_at" not in pl.read_parquet_schema(raw_path).names()
    assert raw_path.read_bytes() == before
    assert frame["ingested_at"].to_list() == [None]
    assert frame["updated_at"].to_list() == [None]
    assert frame["shares"][0] == pytest.approx(2.4e10)


def test_legacy_read_does_not_materialize_python_rows(tmp_path, monkeypatch) -> None:
    _write_legacy(tmp_path, DAY, [_row("510300", DAY, 1e8)])

    def boom(*_args, **_kwargs):
        raise AssertionError("历史分区不应收成 Python 行")

    monkeypatch.setattr(pl.DataFrame, "to_dicts", boom)
    frame = store.read_partition(tmp_path, "etf_shares", DAY)
    assert frame["ingested_at"].to_list() == [None]


def test_rewriting_legacy_does_not_invent_first_seen_time(tmp_path) -> None:
    _write_legacy(tmp_path, DAY, [_row("510300", DAY, 1e8, name="沪深300ETF")])
    store.write_rows(tmp_path, "etf_shares", DAY, [_row("510300", DAY, 1e8, name="沪深300ETF")], now=SECOND)
    same = store.read_partition(tmp_path, "etf_shares", DAY)
    assert _stamp(same, "510300", "ingested_at") is None
    assert _stamp(same, "510300", "updated_at") is None

    store.write_rows(
        tmp_path, "etf_shares", DAY,
        [_row("510300", DAY, 1.2e8, name="沪深300ETF"), _row("159915", DAY, 3e8)],
        now=THIRD,
    )
    changed = store.read_partition(tmp_path, "etf_shares", DAY)
    assert _stamp(changed, "510300", "ingested_at") is None
    assert _stamp(changed, "510300", "updated_at") == THIRD
    assert _stamp(changed, "159915", "ingested_at") == THIRD
    assert _stamp(changed, "159915", "updated_at") is None


def test_name_change_marks_updated_at(tmp_path) -> None:
    store.write_rows(tmp_path, "etf_shares", DAY, [_row("510300", DAY, 1e8, name="旧名")], now=FIRST)
    store.write_rows(tmp_path, "etf_shares", DAY, [_row("510300", DAY, 1e8, name="新名")], now=SECOND)
    frame = store.read_partition(tmp_path, "etf_shares", DAY)
    assert _stamp(frame, "510300", "ingested_at") == FIRST
    assert _stamp(frame, "510300", "updated_at") == SECOND
    assert _stamp(frame, "510300", "name") == "新名"


def test_inflow_reads_legacy_and_stamped_partitions(tmp_path) -> None:
    _write_legacy(tmp_path, DAY, [_row("510300", DAY, 1e8)])
    store.write_rows(tmp_path, "etf_shares", LATER, [_row("510300", LATER, 2e8)], now=THIRD)
    for trade_day, price in ((DAY, 2.0), (LATER, 2.0)):
        part = tmp_path / "kline_etf_daily" / f"date={trade_day}"
        part.mkdir(parents=True)
        pl.DataFrame({"symbol": ["510300.SH"], "close": [price]}).write_parquet(part / "part.parquet")

    flows = etf_inflow.latest_inflows(tmp_path, as_of=LATER)
    assert flows["510300"]["net_inflow_1d"] == pytest.approx(2.0)
    mixed = store.read_range(tmp_path, "etf_shares").sort("trade_date")
    assert mixed["ingested_at"].to_list() == [None, THIRD]


def test_duckdb_view_unions_legacy_nulls_with_new_stamps(tmp_path) -> None:
    import duckdb

    _write_legacy(tmp_path, DAY, [_row("510300", DAY, 1e8)])
    store.write_rows(tmp_path, "etf_shares", LATER, [_row("510300", LATER, 2e8)], now=THIRD)
    con = duckdb.connect()
    try:
        con.execute(store.etf_shares_view_sql(tmp_path))
        rows = con.execute(
            """
            SELECT trade_date, shares, ingested_at IS NULL AS missing
            FROM etf_shares
            ORDER BY trade_date
            """
        ).fetchall()
    finally:
        con.close()
    assert rows[0][0] == DAY
    assert rows[0][2] is True
    assert rows[1][0] == LATER
    assert rows[1][2] is False


def test_datastore_registers_etf_shares_view(tmp_path) -> None:
    from app.tickflow.repository import DataStore, KlineRepository

    _write_legacy(tmp_path, DAY, [_row("510300", DAY, 1e8)])
    store.write_rows(tmp_path, "etf_shares", LATER, [_row("159915", LATER, 2e8)], now=THIRD)
    data = DataStore(tmp_path)
    found = data.db.execute(
        "SELECT code, ingested_at IS NULL FROM etf_shares ORDER BY trade_date, code"
    ).fetchall()
    assert found == [("510300", True), ("159915", False)]
    KlineRepository(data).rebuild_views()
    again = data.db.execute("SELECT count(*) FROM etf_shares").fetchone()
    assert again == (2,)


def test_export_keeps_legacy_ingested_at_null(tmp_path) -> None:
    root = tmp_path / "data"
    _write_legacy(root, DAY, [_row("510300", DAY, 1e8, name="沪深300ETF")])
    store.write_rows(root, "etf_shares", LATER, [_row("510300", LATER, 2e8, name="沪深300ETF")], now=THIRD)
    out = tmp_path / "snaps"
    snap.run(
        data_dir=root,
        out_root=out,
        tag="20261018",
        start=date(2026, 10, 1),
        end=date(2026, 10, 31),
        tables=["etf_shares"],
        min_free_gb=0,
        chunk_rows=1,
        commit="abc",
        now=lambda: THIRD,
        log=lambda _message: None,
    )
    exported = pl.read_parquet(out / "20261018" / "etf_shares.parquet").sort("trade_date")
    assert exported["ingested_at"].to_list()[0] is None
    assert exported["ingested_at"].to_list()[1] == THIRD
    assert exported["updated_at"].to_list() == [None, None]
    with gzip.open(out / "20261018" / "csv" / "etf_shares.csv.gz", "rt", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    by_day = {row["trade_date"]: row for row in rows}
    assert by_day[DAY]["ingested_at"] == ""
    assert by_day[DAY]["updated_at"] == ""
    assert by_day[LATER]["ingested_at"] == "2026-10-12T16:42:00+0800"
    assert by_day[LATER]["updated_at"] == ""


def test_api_returns_offset_text_and_board_omits_stamps(tmp_path, monkeypatch) -> None:
    from app.api.fund_flow import router

    store.write_rows(tmp_path, "etf_shares", DAY, [_row("510300", DAY, 1e8, name="沪深300ETF")], now=FIRST)
    monkeypatch.setattr(query, "_dir", lambda data_dir=None: tmp_path)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    item = client.get("/api/fund-flow/etf-shares").json()["items"][0]
    assert item["ingested_at"] == "2026-10-09T16:40:00+08:00"
    assert item["updated_at"] is None
    assert item["shares"] == pytest.approx(1e8)
    board_item = client.get("/api/fund-flow/board").json()["etf_shares"]["items"][0]
    assert "ingested_at" not in board_item
    assert "updated_at" not in board_item


def test_other_kinds_do_not_gain_stamp_columns(tmp_path) -> None:
    store.write_rows(tmp_path, "southbound", DAY, [{
        "trade_date": DAY, "net_flow": 1.0, "source": "akshare",
    }], now=FIRST)
    frame = store.read_partition(tmp_path, "southbound", DAY)
    assert "ingested_at" not in frame.columns
    assert frame["net_flow"][0] == pytest.approx(1.0)
