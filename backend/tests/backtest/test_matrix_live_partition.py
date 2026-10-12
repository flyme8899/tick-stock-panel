"""盘中日 K 分区每几秒被替换时，矩阵扫描不能把半截 parquet 读进来。"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.backtest import matrix as matrix_module
from app.backtest.matrix import (
    _live_rewritten_partition_date,
    load_market_data_matrix_from_parquet,
)


def _write_bar(root, day: date, symbol: str, close: float) -> None:
    partition = root / f"date={day.isoformat()}"
    partition.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [symbol],
        "date": [day],
        "open": [close],
        "high": [close],
        "low": [close],
        "close": [close],
        "volume": [1_000.0],
    }).write_parquet(partition / "part.parquet")


def _tear_parquet(path) -> None:
    """Shrink a valid parquet so a scan raises OSError: Couldn't deserialize thrift."""
    data = path.read_bytes()
    footer_len = int.from_bytes(data[-8:-4], "little")
    path.write_bytes(data[:80] + data[-(footer_len + 8):])


def test_live_rewrite_window_is_beijing_weekday_quote_flush() -> None:
    monday = datetime(2026, 10, 12, 0, 0)
    assert monday.weekday() == 0
    assert _live_rewritten_partition_date(monday.replace(hour=9, minute=14)) is None
    assert _live_rewritten_partition_date(monday.replace(hour=9, minute=15)) == monday.date()
    assert _live_rewritten_partition_date(monday.replace(hour=12, minute=0)) == monday.date()
    assert _live_rewritten_partition_date(monday.replace(hour=15, minute=30)) == monday.date()
    assert _live_rewritten_partition_date(monday.replace(hour=15, minute=31)) is None
    saturday = monday + timedelta(days=5)
    assert saturday.weekday() == 5
    assert _live_rewritten_partition_date(saturday.replace(hour=10, minute=0)) is None
    # 02:00 UTC is 10:00 in Beijing.
    utc = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)
    assert _live_rewritten_partition_date(utc) == monday.date()


def test_trading_hours_skip_torn_today_partition(tmp_path, monkeypatch) -> None:
    root = tmp_path / "kline_daily_enriched"
    yesterday = date(2024, 5, 6)
    today = date(2024, 5, 7)
    _write_bar(root, yesterday, "000001.SZ", 10.0)
    _write_bar(root, today, "000002.SZ", 20.0)
    _tear_parquet(root / f"date={today.isoformat()}" / "part.parquet")
    monkeypatch.setattr(
        matrix_module,
        "_live_rewritten_partition_date",
        lambda now=None: today,
    )

    market = load_market_data_matrix_from_parquet(
        root,
        yesterday,
        today,
        field_columns=set(),
        cache_root=None,
    )

    assert market.timestamp_labels == (yesterday.isoformat(),)
    assert market.symbols == ("000001.SZ",)
    assert market.close[0, 0] == pytest.approx(10.0)


def test_only_live_partition_reports_empty_data(tmp_path, monkeypatch) -> None:
    root = tmp_path / "kline_daily_enriched"
    today = date(2024, 5, 7)
    _write_bar(root, today, "000001.SZ", 10.0)
    monkeypatch.setattr(
        matrix_module,
        "_live_rewritten_partition_date",
        lambda now=None: today,
    )

    with pytest.raises(ValueError, match="本地指标数据为空"):
        load_market_data_matrix_from_parquet(
            root,
            today,
            today,
            field_columns=set(),
            cache_root=None,
        )


def test_outside_trading_hours_includes_today(tmp_path, monkeypatch) -> None:
    root = tmp_path / "kline_daily_enriched"
    yesterday = date(2024, 5, 6)
    today = date(2024, 5, 7)
    _write_bar(root, yesterday, "000001.SZ", 10.0)
    _write_bar(root, today, "000001.SZ", 12.0)
    monkeypatch.setattr(
        matrix_module,
        "_live_rewritten_partition_date",
        lambda now=None: None,
    )

    market = load_market_data_matrix_from_parquet(
        root,
        yesterday,
        today,
        field_columns=set(),
        cache_root=None,
    )

    assert market.timestamp_labels == (yesterday.isoformat(), today.isoformat())
    assert market.close[:, 0].tolist() == pytest.approx([10.0, 12.0])


def test_live_partition_rewrite_does_not_invalidate_matrix_cache(tmp_path, monkeypatch) -> None:
    root = tmp_path / "kline_daily_enriched"
    yesterday = date(2024, 5, 6)
    today = date(2024, 5, 7)
    _write_bar(root, yesterday, "000001.SZ", 10.0)
    _write_bar(root, today, "000002.SZ", 20.0)
    monkeypatch.setattr(
        matrix_module,
        "_live_rewritten_partition_date",
        lambda now=None: today,
    )
    cache_root = tmp_path / "matrix_cache"

    first = load_market_data_matrix_from_parquet(
        root,
        yesterday,
        today,
        field_columns=set(),
        cache_root=cache_root,
    )
    _write_bar(root, today, "000002.SZ", 99.0)
    second = load_market_data_matrix_from_parquet(
        root,
        yesterday,
        today,
        field_columns=set(),
        cache_root=cache_root,
    )

    assert first.cache_status == "built"
    assert second.cache_status == "exact"
    assert second.cache_path == first.cache_path
    assert second.timestamp_labels == (yesterday.isoformat(),)


def test_torn_partition_outside_window_retries_once_then_raises(tmp_path, monkeypatch) -> None:
    root = tmp_path / "kline_daily_enriched"
    today = date(2024, 5, 7)
    _write_bar(root, today, "000001.SZ", 10.0)
    _tear_parquet(root / f"date={today.isoformat()}" / "part.parquet")
    monkeypatch.setattr(
        matrix_module,
        "_live_rewritten_partition_date",
        lambda now=None: None,
    )
    calls = {"n": 0}
    original = matrix_module._load_market_data_matrix_from_parquet_once

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(matrix_module, "_load_market_data_matrix_from_parquet_once", counted)

    with pytest.raises(OSError):
        load_market_data_matrix_from_parquet(
            root,
            today,
            today,
            field_columns=set(),
            cache_root=None,
        )

    assert calls["n"] == 2


def test_oserror_during_scan_is_retried_once(tmp_path, monkeypatch) -> None:
    root = tmp_path / "kline_daily_enriched"
    day = date(2024, 5, 6)
    _write_bar(root, day, "000001.SZ", 10.0)
    monkeypatch.setattr(
        matrix_module,
        "_live_rewritten_partition_date",
        lambda now=None: None,
    )
    calls = {"n": 0}
    original = matrix_module._scan_matrix_values

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("Couldn't deserialize thrift: TProtocolException: Invalid data")
        return original(*args, **kwargs)

    monkeypatch.setattr(matrix_module, "_scan_matrix_values", flaky)

    market = load_market_data_matrix_from_parquet(
        root,
        day,
        day,
        field_columns=set(),
        cache_root=None,
    )

    assert calls["n"] == 2
    assert market.close[0, 0] == pytest.approx(10.0)
