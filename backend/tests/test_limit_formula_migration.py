"""涨跌停公式版本戳: 已有分区落后时, 增量调用改为全量重建。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.indicators import pipeline


def _write_daily(tmp_path, rows: pl.DataFrame) -> None:
    for frame in rows.partition_by("date"):
        out = tmp_path / "kline_daily" / f"date={frame['date'][0]}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(out)


def _sample(tmp_path) -> pl.DataFrame:
    rows = []
    for day in range(4):
        for symbol in ("600000.SH", "600001.SH"):
            close = 10.0 + day
            rows.append({
                "symbol": symbol,
                "date": date(2024, 1, 2) + timedelta(days=day),
                "open": close,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 1000.0,
                "amount": 10000.0,
                "quote_ts": 0,
            })
    frame = pl.DataFrame(rows)
    _write_daily(tmp_path, frame)
    instruments = pl.DataFrame({
        "symbol": ["600000.SH", "600001.SH"],
        "name": ["普通股", "普通股"],
        "float_shares": [1_000_000.0, 1_000_000.0],
    })
    out = tmp_path / "instruments" / "all.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    instruments.write_parquet(out)
    return frame


def test_stale_stamp_upgrades_incremental_call_to_full_rebuild(tmp_path, monkeypatch) -> None:
    frame = _sample(tmp_path)
    seeded = tmp_path / "kline_daily_enriched" / f"date={frame['date'][0]}" / "part.parquet"
    seeded.parent.mkdir(parents=True)
    frame.filter(pl.col("date") == frame["date"][0]).write_parquet(seeded)
    # 其余日期也先放一份, 使「无版本戳」之外的增量路径无新日期可写。
    for part in frame.partition_by("date"):
        out = tmp_path / "kline_daily_enriched" / f"date={part['date'][0]}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            part.write_parquet(out)
    pipeline.write_limit_formula_version(tmp_path)
    stamp = pipeline.limit_formula_stamp_path(tmp_path)
    stamp.write_text("1\n", encoding="utf-8")

    calls: list[int] = []
    original = pipeline.compute_enriched

    def spy(raw, **kwargs):
        calls.append(raw.height)
        return original(raw, **kwargs)

    monkeypatch.setattr(pipeline, "compute_enriched", spy)
    monkeypatch.setattr(pipeline, "_adaptive_sym_batch", lambda default, rows: 2)
    written = pipeline.run_pipeline(tmp_path, new_dates_only=True)
    assert written > 0
    assert calls, "落后的公式版本必须重算已有分区, 不能直接返回"
    assert pipeline.read_limit_formula_version(tmp_path) == pipeline.LIMIT_FORMULA_VERSION
    stored = pl.read_parquet(str(tmp_path / "kline_daily_enriched" / "**" / "*.parquet"))
    assert "consecutive_limit_ups" in stored.columns
    assert "signal_limit_up" not in stored.columns

    def reject(raw, **kwargs):
        raise AssertionError("当前版本不应再全量重算")

    monkeypatch.setattr(pipeline, "compute_enriched", reject)
    assert pipeline.run_pipeline(tmp_path, new_dates_only=True) == 0


def test_missing_enriched_incremental_does_not_pretend_history_was_rebuilt(tmp_path, monkeypatch) -> None:
    _sample(tmp_path)
    monkeypatch.setattr(pipeline, "_load_recent_history", lambda *args, **kwargs: pl.DataFrame())
    monkeypatch.setattr(pipeline, "_adaptive_sym_batch", lambda default, rows: 8)
    written = pipeline.run_pipeline(tmp_path, new_dates_only=True)
    assert written > 0
    assert pipeline.read_limit_formula_version(tmp_path) == pipeline.LIMIT_FORMULA_VERSION
