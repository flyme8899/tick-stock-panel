"""研究快照导出脚本：只读、日期截取、分批、清单校验、覆盖保护和磁盘门槛。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime

import polars as pl
import pytest

from scripts import export_research_snapshot as snap
from scripts.export_research_snapshot import CN_TZ, SnapshotError, connect_ro, run


def _kline(d: str, n: int, volume_float: bool = False, extra: bool = False) -> pl.DataFrame:
    df = pl.DataFrame({
        "symbol": [f"{i:06d}.SZ" for i in range(n)],
        "close": [10.0 + i for i in range(n)],
        "volume": [float(i) for i in range(n)] if volume_float else list(range(n)),
        "date": [date.fromisoformat(d)] * n,
    })
    if extra:
        df = df.with_columns(pl.lit(1.5).alias("amount"))
    return df


@pytest.fixture
def data_dir(tmp_path):
    root = tmp_path / "data"
    for d, n, vf, ex in [("2026-09-28", 3, False, False), ("2026-09-29", 4, True, True),
                         ("2026-09-30", 2, False, False), ("2026-10-08", 5, False, True)]:
        p = root / "kline_daily" / f"date={d}"
        p.mkdir(parents=True)
        _kline(d, n, vf, ex).write_parquet(p / "part.parquet")
    for d in ["2026-09-29", "2026-09-30"]:
        p = root / "fund_flow" / "stock" / f"date={d}"
        p.mkdir(parents=True)
        pl.DataFrame({"symbol": ["000001.SZ"], "trade_date": [d], "main_net": [1e6]}).write_parquet(
            p / "part.parquet")
    (root / "instruments").mkdir(parents=True)
    pl.DataFrame({"symbol": ["000001.SZ", "600000.SH"], "code": ["000001", "600000"],
                  "name": ["平安银行", "浦发银行"], "list_status": [None, None],
                  "as_of": [date(2026, 10, 16)] * 2}).write_parquet(
        root / "instruments" / "instruments.parquet")
    (root / "news").mkdir()
    c = sqlite3.connect(root / "news" / "news.sqlite")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE news_items (id INTEGER PRIMARY KEY, source TEXT, source_id TEXT, "
              "published_at TEXT, title TEXT, clean_text TEXT, url TEXT, extra_json TEXT, ingested_at TEXT)")
    c.executemany("INSERT INTO news_items VALUES (?,?,?,?,?,?,?,?,?)", [
        (1, "etf_flow", "a", "2026-09-29T07:30:00+08:00", "申赎1", "正文", "", "{}", "x"),
        (2, "cls", "b", "2026-09-29T08:00:00+08:00", "快讯", "正文", "", "{}", "x"),
        (3, "etf_flow", "c", "2026-10-09T07:30:00+08:00", "申赎2", "正文", "", "{}", "x"),
    ])
    c.commit()
    c.close()
    c = sqlite3.connect(root / "news_archive.sqlite")
    c.execute("CREATE TABLE news_archive (id INTEGER PRIMARY KEY, source TEXT, source_id TEXT, "
              "published_at TEXT, title TEXT, content TEXT, level TEXT, stock_codes TEXT, url TEXT, "
              "ingested_at TEXT)")
    rows = []
    for i in range(1, 26):
        day = 27 + (i % 4)  # 9/27 ~ 9/30
        rows.append((i, "eastmoney_724", str(i), f"2026-09-{day:02d}T09:{i:02d}:00+08:00",
                     f"t{i}", f"内容{i}", "", '["000001"]', "", "x"))
    c.executemany("INSERT INTO news_archive VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return root


def _now():
    return datetime(2026, 10, 17, 16, 30, tzinfo=CN_TZ)


def _run(data_dir, out, **kw):
    base = dict(data_dir=data_dir, out_root=out, tag="20261017", start=date(2026, 9, 28),
                end=date(2026, 9, 30), chunk_rows=7, min_free_gb=0, commit="abc123",
                now=_now, log=lambda _m: None)
    base.update(kw)
    return run(**base)


def test_dry_run_counts_without_writing(data_dir, tmp_path):
    out = tmp_path / "snaps"
    s = _run(data_dir, out, dry_run=True)
    assert not out.exists()
    t = s["tables"]
    assert t["kline_daily"]["rows"] == 9
    assert (t["kline_daily"]["date_min"], t["kline_daily"]["date_max"]) == ("2026-09-28", "2026-09-30")
    assert t["fund_flow_stock"]["rows"] == 2
    assert t["etf_flow_news"]["rows"] == 1
    assert t["news_archive"]["rows"] == 19  # 9/27 那 6 条在范围外
    assert t["adj_factor"]["status"] == "missing"
    assert s["estimated_bytes"] > 0


def test_export_writes_manifest_and_files(data_dir, tmp_path):
    out = tmp_path / "snaps"
    s = _run(data_dir, out)
    d = out / "20261017"
    m = json.loads((d / "MANIFEST.json").read_text(encoding="utf-8"))
    assert m["git_commit"] == "abc123"
    assert m["created_at"] == "2026-10-17T16:30:00+08:00"
    assert m["timezone"] == "Asia/Shanghai"
    assert m == s["manifest"]
    kl = m["tables"]["kline_daily"]
    assert kl["rows"] == 9 and kl["source_files"] == 3
    df = pl.read_parquet(d / "kline_daily.parquet")
    assert df.height == 9
    # 分区间字段不一致：volume 统一为浮点，缺 amount 的分区补空
    assert df.schema["volume"] == pl.Float64
    assert df["amount"].null_count() == 5
    assert df["date"].max() == date(2026, 9, 30)
    na = m["tables"]["news_archive"]
    assert (na["rows"], na["date_min"], na["date_max"]) == (19, "2026-09-28", "2026-09-30")
    news = pl.read_parquet(d / "news_archive.parquet")
    assert news["id"].to_list() == sorted(news["id"].to_list())
    assert pl.read_parquet(d / "etf_flow_news.parquet")["source"].to_list() == ["etf_flow"]
    assert m["tables"]["instruments"]["rows"] == 2
    assert m["tables"]["adj_factor"]["status"] == "missing"
    assert not (d / "adj_factor.parquet").exists()
    for name, t in m["tables"].items():
        if t["status"] == "ok":
            assert hashlib.sha256((d / t["file"]).read_bytes()).hexdigest() == t["sha256"], name
    readme = (d / "README.md").read_text(encoding="utf-8")
    assert "kline_daily" in readme and "源数据不存在" in readme
    assert not [p for p in out.iterdir() if p.name.startswith(".")]


def test_parquet_is_zstd(data_dir, tmp_path):
    import pyarrow.parquet as pq

    _run(data_dir, tmp_path / "s", tables=["kline_daily"])
    md = pq.ParquetFile(tmp_path / "s" / "20261017" / "kline_daily.parquet").metadata
    assert md.row_group(0).column(0).compression == "ZSTD"


def test_tables_subset(data_dir, tmp_path):
    _run(data_dir, tmp_path / "s", tables=["instruments", "news_archive"])
    d = tmp_path / "s" / "20261017"
    assert sorted(p.name for p in d.iterdir()) == [
        "MANIFEST.json", "README.md", "csv", "instruments.parquet", "news_archive.parquet"]
    assert sorted(p.name for p in (d / "csv").iterdir()) == [
        "csv_manifest.json", "csv_manifest.txt", "instruments.csv.gz", "news_archive.csv.gz"]


def test_unknown_table_rejected(data_dir, tmp_path):
    with pytest.raises(SnapshotError, match="未知表"):
        _run(data_dir, tmp_path / "s", tables=["nope"])


def test_refuses_overwrite_without_force(data_dir, tmp_path):
    out = tmp_path / "s"
    _run(data_dir, out, tables=["instruments"])
    with pytest.raises(SnapshotError, match="已存在"):
        _run(data_dir, out, tables=["instruments"])
    _run(data_dir, out, tables=["kline_daily"], force=True, csv=False)
    assert sorted(p.name for p in (out / "20261017").iterdir()) == [
        "MANIFEST.json", "README.md", "kline_daily.parquet"]
    assert [p.name for p in out.iterdir()] == ["20261017"]


def test_aborts_when_disk_would_drop_below_threshold(data_dir, tmp_path, monkeypatch):
    class Usage:
        free = 5 * snap.GB + 10  # 预估再小也会低于 5G

    monkeypatch.setattr(snap.shutil, "disk_usage", lambda _p: Usage)
    out = tmp_path / "s"
    s = _run(data_dir, out, min_free_gb=5, dry_run=True)
    assert s["fits"] is False
    with pytest.raises(SnapshotError, match="空间不足"):
        _run(data_dir, out, min_free_gb=5)
    assert not (out / "20261017").exists()


def test_failure_leaves_no_partial_snapshot(data_dir, tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("写到一半失败")

    monkeypatch.setattr(snap, "_sha256", boom)
    out = tmp_path / "s"
    with pytest.raises(RuntimeError):
        _run(data_dir, out, tables=["instruments"])
    assert list(out.iterdir()) == []


def test_sqlite_opened_read_only(data_dir):
    db = data_dir / "news_archive.sqlite"
    before = db.read_bytes()
    conn = connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM news_archive")
    conn.close()
    assert db.read_bytes() == before


def test_export_does_not_modify_sources(data_dir, tmp_path):
    files = sorted(p for p in data_dir.rglob("*") if p.is_file() and not p.name.endswith(("-wal", "-shm")))
    before = {p: p.read_bytes() for p in files}
    _run(data_dir, tmp_path / "s")
    assert {p: p.read_bytes() for p in files} == before


def test_main_cli_dry_run(data_dir, tmp_path, capsys):
    code = snap.main(["--data-dir", str(data_dir), "--out", str(tmp_path / "s"), "--dry-run",
                      "--start", "2026-09-28", "--end", "2026-09-30", "--min-free-gb", "0",
                      "--no-nice"])
    assert code == 0
    assert "kline_daily" in capsys.readouterr().out
    assert not (tmp_path / "s").exists()


def test_three_years_before_leap_day():
    assert snap.three_years_before(date(2028, 2, 29)) == date(2025, 2, 28)
    assert snap.three_years_before(date(2026, 10, 17)) == date(2023, 10, 17)


# ---------- CSV ----------

def _read_csv(path):
    import pandas as pd

    return pd.read_csv(path, dtype=str, keep_default_na=False, compression="gzip")


def test_csv_written_with_manifest_and_md5(data_dir, tmp_path):
    out = tmp_path / "s"
    s = _run(data_dir, out)
    d = out / "20261017"
    cm = json.loads((d / "csv" / "csv_manifest.json").read_text(encoding="utf-8"))
    pm = json.loads((d / "MANIFEST.json").read_text(encoding="utf-8"))
    ok = {n for n, t in pm["tables"].items() if t["status"] == "ok"}
    assert set(cm["tables"]) == ok
    assert set(pm["csv"]["tables"]) == ok and pm == s["manifest"]
    for name, t in cm["tables"].items():
        f = d / "csv" / t["file"]
        assert t["rows"] == pm["tables"][name]["rows"] == len(_read_csv(f)), name
        assert hashlib.md5(f.read_bytes()).hexdigest() == t["md5"] == pm["csv"]["tables"][name]["md5"]
    txt = (d / "csv" / "csv_manifest.txt").read_text(encoding="utf-8").splitlines()
    assert txt[1] == "file\trows\tbytes\tmd5"
    assert len(txt) == 2 + len(ok)
    assert "csv/" in (d / "README.md").read_text(encoding="utf-8")


def test_csv_keeps_leading_zeros_dates_and_nulls(data_dir, tmp_path):
    import gzip

    _run(data_dir, tmp_path / "s", tables=["instruments", "kline_daily"])
    d = tmp_path / "s" / "20261017" / "csv"
    ins = _read_csv(d / "instruments.csv.gz")
    assert ins["code"].tolist() == ["000001", "600000"]
    assert ins["symbol"].tolist() == ["000001.SZ", "600000.SH"]
    assert ins["as_of"].tolist() == ["2026-10-16"] * 2
    assert ins["list_status"].tolist() == ["", ""]
    raw = gzip.decompress((d / "instruments.csv.gz").read_bytes()).decode("utf-8")
    assert '"000001"' in raw  # 文本字段加引号，Excel 之类也不会吞掉前导零
    kl = _read_csv(d / "kline_daily.csv.gz")
    assert kl["symbol"].iloc[0] == "000000.SZ"
    assert sorted(set(kl["date"])) == ["2026-09-28", "2026-09-29", "2026-09-30"]


def test_csv_readable_with_stdlib_only(data_dir, tmp_path):
    import csv
    import gzip

    _run(data_dir, tmp_path / "s", tables=["news_archive"])
    with gzip.open(tmp_path / "s" / "20261017" / "csv" / "news_archive.csv.gz", "rt",
                   encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 19 and rows[0]["stock_codes"] == '["000001"]'


def test_no_csv_flag(data_dir, tmp_path):
    s = _run(data_dir, tmp_path / "s", tables=["instruments"], csv=False)
    assert not (tmp_path / "s" / "20261017" / "csv").exists()
    assert "csv" not in s["manifest"]


def test_csv_only_on_existing_snapshot(data_dir, tmp_path):
    out = tmp_path / "s"
    _run(data_dir, out, tables=["instruments", "fund_flow_stock"], csv=False)
    r = snap.csv_only(out_root=out, tag="20261017", now=_now, min_free_gb=0, log=lambda _m: None)
    assert {n: t["rows"] for n, t in r["tables"].items()} == {"instruments": 2, "fund_flow_stock": 2}
    with pytest.raises(SnapshotError, match="已存在"):
        snap.csv_only(out_root=out, tag="20261017", min_free_gb=0, log=lambda _m: None)
    snap.csv_only(out_root=out, tag="20261017", force=True, min_free_gb=0, log=lambda _m: None)
    assert not [p for p in (out / "20261017").iterdir() if p.name.startswith(".")]
    with pytest.raises(SnapshotError, match="快照不存在"):
        snap.csv_only(out_root=out, tag="nope", min_free_gb=0, log=lambda _m: None)


def test_csv_only_cli(data_dir, tmp_path):
    out = tmp_path / "s"
    _run(data_dir, out, tables=["instruments"], csv=False)
    assert snap.main(["--out", str(out), "--tag", "20261017", "--csv-only", "--no-nice",
                      "--min-free-gb", "0"]) == 0
    assert (out / "20261017" / "csv" / "instruments.csv.gz").is_file()
    assert snap.main(["--out", str(out), "--csv-only", "--no-nice"]) == 2


# ---------- 财务表 ----------

def test_financial_tables_exported_all_periods(data_dir, tmp_path):
    for period, codes in [("2018-03-31", ["000001"]), ("2026-06-30", ["000001", "000002"])]:
        p = data_dir / "financials" / "em_report" / f"report_period={period}"
        p.mkdir(parents=True)
        pl.DataFrame({"code": codes, "report_period": [period] * len(codes),
                      "announce_date": ["2026-08-15"] * len(codes)}).write_parquet(p / "part.parquet")
    # 不是 report_period= 的目录不算
    (data_dir / "financials" / "em_report" / "date=2026-01-01").mkdir()
    (data_dir / "financials" / "metrics").mkdir(parents=True)
    pl.DataFrame({"symbol": ["000001.SZ"], "period_end": ["2026-06-30"],
                  "announce_date": ["2026-08-15"], "bps": [1.0]}).write_parquet(
        data_dir / "financials" / "metrics" / "part.parquet")
    names = ["financial_report", "financial_forecast", "financials_metrics"]
    s = _run(data_dir, tmp_path / "s", tables=names)  # 日期范围 9/28~9/30 不截财务表
    t = s["manifest"]["tables"]
    assert t["financial_report"]["rows"] == 3
    assert (t["financial_report"]["date_min"], t["financial_report"]["date_max"]) == (
        "2018-03-31", "2026-06-30")
    assert t["financial_forecast"]["status"] == "missing"
    assert t["financials_metrics"]["rows"] == 1
    d = tmp_path / "s" / "20261017"
    rep = _read_csv(d / "csv" / "financial_report.csv.gz")
    assert sorted(rep["code"]) == ["000001", "000001", "000002"]
    assert set(rep["report_period"]) == {"2018-03-31", "2026-06-30"}


def test_minute_kline_not_in_snapshot():
    assert not [t for t in snap.TABLES if "minute" in t.name or "minute" in t.path]
