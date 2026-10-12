"""筹码日表：窗口、停牌、缺换手、可续跑的回补，以及盘后旁路失败不外抛。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.chip.algorithm import LOOKBACK_BARS, compute_window
from app.chip.jobs import backfill, summarize, write_date
from app.chip.store import partition_file, progress_file
from app.jobs.daily_pipeline import _refresh_chip


def _days(count: int, end: date = date(2026, 10, 9)) -> list[date]:
    found: list[date] = []
    cursor = end
    while len(found) < count:
        if cursor.weekday() < 5:
            found.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(found))


def _write_enriched(data_dir: Path, rows: list[dict]) -> None:
    frame = pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))
    for day, group in frame.group_by("date", maintain_order=True):
        trade_day = day[0] if isinstance(day, tuple) else day
        folder = data_dir / "kline_daily_enriched" / f"date={trade_day.isoformat()}"
        folder.mkdir(parents=True, exist_ok=True)
        group.write_parquet(folder / "part.parquet")


def _bar(symbol: str, day: date, price: float, turnover) -> dict:
    return {
        "symbol": symbol,
        "date": day,
        "open": price,
        "high": price + 0.4,
        "low": price - 0.3,
        "close": price + 0.1,
        "turnover_rate": turnover,
    }


def test_write_date_skips_suspended_and_nulls_missing_turnover(tmp_path: Path):
    days = _days(4)
    rows = []
    for day in days:
        rows.append(_bar("300829.SZ", day, 12.0, 5.0))
        rows.append(_bar("000001.SZ", day, 8.0, None))
    # 002212 停牌：目标日没有这根 K。
    for day in days[:-1]:
        rows.append(_bar("002212.SZ", day, 9.0, 4.0))
    _write_enriched(tmp_path, rows)

    count = write_date(tmp_path, days[-1])
    table = pl.read_parquet(partition_file(tmp_path, days[-1]))
    assert count == 2
    assert table["symbol"].to_list() == ["000001.SZ", "300829.SZ"]
    halted = table.filter(pl.col("symbol") == "000001.SZ").row(0, named=True)
    assert halted["benefit_ratio"] is None
    assert halted["avg_cost"] is None
    alive = table.filter(pl.col("symbol") == "300829.SZ").row(0, named=True)
    assert alive["benefit_ratio"] is not None
    assert alive["avg_cost"] is not None
    assert alive["avg_cost_bias"] is not None


def test_lookback_ignores_the_bar_before_210(tmp_path: Path):
    days = _days(LOOKBACK_BARS + 1)
    rows = [_bar("300829.SZ", days[0], 500.0, 20.0)]
    rows.extend(_bar("300829.SZ", day, 12.0, 5.0) for day in days[1:])
    _write_enriched(tmp_path, rows)
    write_date(tmp_path, days[-1])
    stored = pl.read_parquet(partition_file(tmp_path, days[-1])).row(0, named=True)

    window = pl.DataFrame(rows[1:]).sort("date")
    expected = compute_window(
        window["open"].to_numpy(),
        window["high"].to_numpy(),
        window["low"].to_numpy(),
        window["close"].to_numpy(),
        window["turnover_rate"].to_numpy(),
    )
    assert stored["avg_cost"] == expected.avg_cost
    assert stored["benefit_ratio"] == expected.benefit_ratio


def test_backfill_resumes_after_a_deleted_partition(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.chip.jobs.lower_priority", lambda: None)
    days = _days(2)
    rows = [_bar("300829.SZ", day, 12.0, 6.0) for day in days]
    _write_enriched(tmp_path, rows)

    first = backfill(tmp_path, priority=True)
    assert first["written"] == 2
    assert first["skipped"] == 0
    second_path = partition_file(tmp_path, days[1])
    second_path.unlink()

    second = backfill(tmp_path, priority=True)
    assert second["written"] == 1
    assert second["skipped"] == 1
    assert second_path.exists()
    progress = progress_file(tmp_path).read_text(encoding="utf-8")
    assert days[1].isoformat() in progress


def test_refresh_chip_swallows_errors(tmp_path: Path, monkeypatch):
    def boom(_data_dir):
        raise RuntimeError("chip down")

    monkeypatch.setattr("app.chip.jobs.refresh_latest", boom)
    result = _refresh_chip(tmp_path)
    assert result["rows"] == 0
    assert result["error"] == "failed"


def test_summarize_drops_symbol_without_the_target_bar():
    day = date(2026, 10, 9)
    earlier = date(2026, 10, 8)
    frame = pl.DataFrame([
        _bar("300829.SZ", day, 12.0, 5.0),
        _bar("002212.SZ", earlier, 9.0, 4.0),
    ]).with_columns(pl.col("date").cast(pl.Date))
    table = summarize(frame, day)
    assert table["symbol"].to_list() == ["300829.SZ"]
