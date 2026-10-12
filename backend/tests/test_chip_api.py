"""个股筹码接口和选股列拼接。分布按 (symbol, date) 缓存，选股只读已落盘分区。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.chip import router
from app.chip.jobs import write_date
from app.chip.query import attach_cache, attach_result, clear_cache


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _days(count: int, end: date = date(2026, 10, 9)) -> list[date]:
    found: list[date] = []
    cursor = end
    while len(found) < count:
        if cursor.weekday() < 5:
            found.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(found))


def _write_bars(data_dir: Path, symbol: str = "300829.SZ", count: int = 6) -> list[date]:
    days = _days(count)
    rows = []
    for index, day in enumerate(days):
        price = 10 + index * 0.2
        rows.append({
            "symbol": symbol,
            "date": day,
            "open": price,
            "high": price + 0.4,
            "low": price - 0.2,
            "close": price + 0.1,
            "turnover_rate": 5.0,
        })
    frame = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    for key, group in frame.group_by("date"):
        trade_day = key[0] if isinstance(key, tuple) else key
        folder = data_dir / "kline_daily_enriched" / f"date={trade_day.isoformat()}"
        folder.mkdir(parents=True, exist_ok=True)
        group.write_parquet(folder / "part.parquet")
    return days


def test_chip_api_returns_bins_and_caches(tmp_path: Path, monkeypatch):
    days = _write_bars(tmp_path)
    from app.chip import query as chip_query

    calls = {"n": 0}
    real = chip_query._compute_distribution

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(chip_query, "_compute_distribution", wrapped)
    clear_cache()
    client = _client(tmp_path)
    first = client.get(f"/api/chip/300829.SZ?date={days[-1].isoformat()}")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["computable"] is True
    assert body["source"] == "本地计算（东财算法）"
    assert body["estimate"] is True
    assert body["bins"]
    assert body["benefit_ratio"] is not None
    assert "avg_cost" in body
    second = client.get(f"/api/chip/300829.SZ?date={days[-1].isoformat()}")
    assert second.status_code == 200
    assert calls["n"] == 1
    assert second.json()["avg_cost"] == body["avg_cost"]


def test_chip_api_missing_bar_and_bad_date(tmp_path: Path):
    _write_bars(tmp_path)
    clear_cache()
    client = _client(tmp_path)
    missing = client.get("/api/chip/002212.SZ?date=2026-10-09")
    assert missing.status_code == 404
    bad = client.get("/api/chip/300829.SZ?date=10-09")
    assert bad.status_code == 400


def test_screener_attach_merges_fields_and_reports_a_gap(tmp_path: Path):
    payload = {
        "as_of": "2026-10-09",
        "rows": [{"symbol": "300829.SZ", "close": 12.0}],
        "results": {"s1": {"rows": [{"symbol": "300829.SZ"}]}},
    }
    gapped = attach_result(payload, tmp_path)
    assert "筹码分布" in gapped["field_gaps"]
    assert "benefit_ratio" in gapped["gap_keys"]
    assert "avg_cost_bias" in gapped["gap_keys"]

    _write_bars(tmp_path)
    write_date(tmp_path, date(2026, 10, 9))
    merged = attach_result(payload, tmp_path)
    assert merged["rows"][0]["benefit_ratio"] is not None
    assert merged["rows"][0]["concentration_90"] is not None
    assert "筹码分布" not in merged.get("field_gaps", [])
    cached = attach_cache(payload, tmp_path)
    assert cached["results"]["s1"]["rows"][0]["concentration_70"] is not None
