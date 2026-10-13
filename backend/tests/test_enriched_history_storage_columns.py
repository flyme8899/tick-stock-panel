"""内存历史缓存必须带走 enriched 存储列。

换手率与连板数不能从 OHLCV 回推。刷新缓存时如果读列白名单漏掉它们,
且没有 instruments 重算路径, 选股叠加条件与涨停基因矩阵会报列不存在。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.tickflow.repository import DataStore, KlineRepository


def test_refresh_history_cache_keeps_stored_turnover_and_limit_counts(tmp_path) -> None:
    day = date(2026, 9, 1)
    part = tmp_path / "kline_daily_enriched" / f"date={day.isoformat()}"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "date": [day, day],
        "open": [10.0, 20.0],
        "high": [11.0, 21.0],
        "low": [9.0, 19.0],
        "close": [10.5, 20.5],
        "volume": [100.0, 200.0],
        "amount": [1050.0, 4100.0],
        "raw_close": [10.5, 20.5],
        "raw_high": [11.0, 21.0],
        "raw_low": [9.0, 19.0],
        "turnover_rate": [7.5, 1.2],
        # 与 ENRICHED_STORAGE_SCHEMA 一致; Int64 会被 scan 拒绝
        "consecutive_limit_ups": pl.Series([3, 0], dtype=pl.UInt32),
        "consecutive_limit_downs": pl.Series([0, 1], dtype=pl.UInt32),
    }).write_parquet(part / "part.parquet")

    repo = KlineRepository(DataStore(tmp_path))
    # 空维表: 没有换手率/连板数的重算路径, 列只能来自 parquet 透传
    repo._instruments_cache = pl.DataFrame()
    repo._refresh_enriched_impl()

    hist = repo._enriched_history_cache
    assert hist is not None and not hist.is_empty()
    assert "turnover_rate" in hist.columns
    assert "consecutive_limit_ups" in hist.columns
    row = hist.filter(pl.col("symbol") == "600000.SH").row(0, named=True)
    assert row["turnover_rate"] == 7.5
    assert int(row["consecutive_limit_ups"]) == 3
