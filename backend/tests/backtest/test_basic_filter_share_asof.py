"""回测基础过滤的价格和总市值不能用前复权价或当前股本。

价格带对比的是当日不复权收盘。总市值 = 不复权收盘 × 当天已经公开的总股本。
股本来自 financials/shares, 可用日是 period_end 与 announce_date 中较晚的那个。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import pyarrow.dataset as pads

from app.backtest.matrix import (
    MarketDataMatrix,
    _build_basic_filter_mask_uncached,
    _instrument_axis_values,
    _resolve_matrix_storage_fields,
    load_market_data_matrix_from_parquet,
)


def _market(
    close: list,
    *,
    raw_close: list | None = None,
    total_shares: list | None = None,
    symbols: tuple[str, ...] | None = None,
) -> MarketDataMatrix:
    prices = np.asarray(close, dtype=np.float32)
    if prices.ndim == 1:
        prices = prices.reshape(-1, 1)
    rows, assets = prices.shape
    if symbols is None:
        symbols = tuple(f"S{index}" for index in range(assets))
    fields: dict[str, np.ndarray] = {}
    if raw_close is not None:
        fields["raw_close"] = np.asarray(raw_close, dtype=np.float32).reshape(rows, assets)
    if total_shares is not None:
        fields["total_shares"] = np.asarray(total_shares, dtype=np.float32).reshape(rows, assets)
    return MarketDataMatrix(
        timestamps=np.arange(rows, dtype=np.int64),
        timestamp_labels=tuple(f"d{index}" for index in range(rows)),
        session_ids=np.arange(rows, dtype=np.int32),
        symbols=symbols,
        names=symbols,
        open=prices,
        high=prices,
        low=prices,
        close=prices,
        volume=np.ones((rows, assets), dtype=np.float32),
        tradable=np.ones((rows, assets), dtype=np.uint8),
        limit_up_locked=np.zeros((rows, assets), dtype=np.uint8),
        limit_down_locked=np.zeros((rows, assets), dtype=np.uint8),
        fields=fields,
    )


def test_price_filter_keeps_stock_whose_raw_price_is_above_three() -> None:
    """前复权价低于 3、当日实际价高于 3 时, 不能被价格带误杀。"""
    market = _market(
        close=[[2.5, 10.0], [1.0, 10.0]],
        raw_close=[[10.0, 2.5], [100.0, 400.0]],
        symbols=("LOW_ADJ.SZ", "LOW_RAW.SZ"),
    )
    mask = _build_basic_filter_mask_uncached(
        market,
        {"enabled": True, "price_min": 3, "price_max": 300},
    )
    assert mask.tolist() == [[True, False], [True, False]]


def test_price_filter_without_raw_close_still_uses_close() -> None:
    market = _market(close=[[2.0], [4.0]])
    mask = _build_basic_filter_mask_uncached(
        market,
        {"enabled": True, "price_min": 3},
    )
    assert mask[:, 0].tolist() == [False, True]


def test_market_cap_uses_old_shares_before_change_and_raw_price() -> None:
    """股本变更后: 变更前用旧股本, 变更后用新股本; 价格用当日实际价。"""
    market = _market(
        close=[[2.0], [2.0]],
        raw_close=[[10.0], [10.0]],
        total_shares=[[100.0], [300.0]],
        symbols=("000001.SZ",),
    )
    mask = _build_basic_filter_mask_uncached(
        market,
        {"enabled": True, "market_cap_min": 2000},
    )
    # 10×100=1000 不够; 10×300=3000 通过。前复权 2 元无论哪档股本都到不了 2000。
    assert mask[:, 0].tolist() == [False, True]


def test_total_shares_resolved_as_daily_matrix(tmp_path: Path) -> None:
    pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [date(2024, 4, 15)],
        "open": [10.0],
        "high": [10.0],
        "low": [10.0],
        "close": [10.0],
        "volume": [1.0],
        "raw_close": [10.0],
    }).write_parquet(tmp_path / "part.parquet")
    dataset = pads.dataset(str(tmp_path / "part.parquet"), format="parquet")
    instruments = pl.DataFrame({
        "symbol": ["000001.SZ"],
        "total_shares": [100.0],
        "float_shares": [80.0],
    })
    _parquet_fields, matrix_fields, vector_fields = _resolve_matrix_storage_fields(
        dataset,
        frozenset({"total_shares", "float_shares", "raw_close"}),
        instruments,
    )
    assert "total_shares" in matrix_fields
    assert "total_shares" not in vector_fields
    assert vector_fields == ["float_shares"]


def test_share_asof_boundaries_use_later_of_period_and_announce() -> None:
    dates = [
        date(2024, 4, 12),
        date(2024, 4, 15),
        date(2024, 4, 16),
        date(2024, 6, 28),
        date(2024, 7, 1),
        date(2024, 8, 13),
        date(2024, 8, 14),
    ]
    symbols = ["AAA.SZ", "BBB.SZ", "CCC.SZ", "DDD.SZ", "EEE.SZ", "FFF.SZ"]
    instruments = pl.DataFrame({
        "symbol": symbols,
        "name": symbols,
        "total_shares": [999.0, 50.0, 999.0, 999.0, 999.0, 999.0],
    })
    history = pl.DataFrame({
        "symbol": [
            "AAA.SZ", "AAA.SZ", "AAA.SZ",
            "CCC.SZ", "CCC.SZ",
            "DDD.SZ", "DDD.SZ", "DDD.SZ",
            "EEE.SZ", "EEE.SZ",
            "FFF.SZ",
        ],
        "period_end": [
            date(2023, 12, 31), date(2024, 6, 30), date(2024, 3, 31),
            date(2023, 12, 31), date(2024, 3, 31),
            date(2023, 9, 30), date(2024, 1, 31), date(2024, 3, 31),
            date(2023, 12, 31), date(2024, 4, 15),
            date(2024, 12, 31),
        ],
        "announce_date": [
            date(2024, 4, 15), date(2024, 4, 20), date(2024, 8, 14),
            date(2024, 1, 1), date(2024, 4, 13),
            date(2024, 1, 1), date(2024, 4, 15), date(2024, 4, 15),
            date(2024, 1, 1), None,
            date(2024, 12, 31),
        ],
        "total_shares": [
            100.0, 180.0, 250.0,
            40.0, 70.0,
            1.0, 5.0, 10.0,
            30.0, 80.0,
            80.0,
        ],
    })

    _names, fields, _limits = _instrument_axis_values(
        symbols,
        {"total_shares"},
        instruments,
        time_count=len(dates),
        trading_dates=dates,
        share_history=history,
    )
    shares = fields["total_shares"]
    assert [symbol for symbol in symbols] == list(symbols)
    # 最早公告 4/15。4/12 早于任何记录, 用最早已知 100, 不用当前 999。
    # 6/30 是周日, 且晚于 4/20 的公告, 所以 6/28 仍是 100, 7/1 才是 180。
    # 8/14 公告当天才换成 250。
    np.testing.assert_array_equal(
        shares[:, 0],
        np.array([100, 100, 100, 100, 180, 180, 250], dtype=np.float32),
    )
    # 财务表没有 BBB, 整列退回 instruments 当前股本。
    np.testing.assert_array_equal(shares[:, 1], np.full(len(dates), 50, dtype=np.float32))
    # 4/13 是周六。周五仍用旧股本, 下周一才用新股本。
    np.testing.assert_array_equal(
        shares[:, 2],
        np.array([40, 70, 70, 70, 70, 70, 70], dtype=np.float32),
    )
    # 同一可用日两条记录, 报告期更晚的 10 覆盖 5。
    np.testing.assert_array_equal(
        shares[:, 3],
        np.array([1, 10, 10, 10, 10, 10, 10], dtype=np.float32),
    )
    # 公告日为空时, 可用日就是报告期 4/15。
    np.testing.assert_array_equal(
        shares[:, 4],
        np.array([30, 80, 80, 80, 80, 80, 80], dtype=np.float32),
    )
    # 窗口内全部早于唯一记录: 整段用最早已知 80, 不用当前 999。
    np.testing.assert_array_equal(shares[:, 5], np.full(len(dates), 80, dtype=np.float32))


def test_share_asof_accepts_string_dates() -> None:
    history = pl.DataFrame({
        "symbol": ["AAA.SZ", "AAA.SZ"],
        "period_end": ["2023-12-31", "2024-03-31"],
        "announce_date": ["2024-04-12", "2024-04-16"],
        "total_shares": [100.0, 200.0],
    })
    dates = [date(2024, 4, 12), date(2024, 4, 15), date(2024, 4, 16)]
    instruments = pl.DataFrame({
        "symbol": ["AAA.SZ"],
        "name": ["AAA"],
        "total_shares": [999.0],
    })
    _names, fields, _limits = _instrument_axis_values(
        ["AAA.SZ"],
        {"total_shares"},
        instruments,
        time_count=len(dates),
        trading_dates=dates,
        share_history=history,
    )
    np.testing.assert_array_equal(
        fields["total_shares"][:, 0],
        np.array([100, 100, 200], dtype=np.float32),
    )


def test_loader_filters_with_raw_close_and_announced_shares(tmp_path: Path) -> None:
    root = tmp_path / "kline_daily_enriched"
    days = [date(2024, 4, 12), date(2024, 4, 15)]
    for current in days:
        partition = root / f"date={current.isoformat()}"
        partition.mkdir(parents=True)
        pl.DataFrame({
            "symbol": ["000001.SZ"],
            "date": [current],
            "open": [2.5],
            "high": [2.5],
            "low": [2.5],
            "close": [2.5],
            "volume": [1000.0],
            "raw_close": [10.0],
        }).write_parquet(partition / "part.parquet")
    share_dir = tmp_path / "financials" / "shares"
    share_dir.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "period_end": ["2023-12-31", "2024-03-31"],
        "announce_date": ["2024-01-10", "2024-04-15"],
        "total_shares": [100.0, 400.0],
    }).write_parquet(share_dir / "part.parquet")
    instruments = pl.DataFrame({
        "symbol": ["000001.SZ"],
        "name": ["测试"],
        "total_shares": [999.0],
        "float_shares": [999.0],
    })

    market = load_market_data_matrix_from_parquet(
        root,
        days[0],
        days[-1],
        field_columns={"raw_close", "total_shares"},
        instruments=instruments,
    )

    np.testing.assert_array_equal(
        market.field("total_shares")[:, 0],
        np.array([100, 400], dtype=np.float32),
    )
    mask = _build_basic_filter_mask_uncached(
        market,
        {"enabled": True, "price_min": 3, "market_cap_min": 2000},
    )
    # 实际价 10 元过价格带。10×100 不够 2000, 10×400 够。
    # 若误用前复权 2.5, 两天都会被价格带或市值线去掉。
    assert mask[:, 0].tolist() == [False, True]
