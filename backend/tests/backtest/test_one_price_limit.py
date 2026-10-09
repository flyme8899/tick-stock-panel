"""一字板方向封锁, 以及除权后涨跌停参考价回到原始价尺度。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import _limit_lock_matrices, build_market_data_matrix
from app.indicators import pipeline


def _locks(symbol: str, name: str, dates: list[date], close, raw):
    close_arr = np.asarray(close, dtype=np.float64).reshape(-1, 1)
    raw_arr = np.asarray(raw, dtype=np.float64).reshape(-1, 1)
    seen = np.ones(close_arr.shape, dtype=bool)
    return _limit_lock_matrices(
        close_arr,
        raw_arr,
        seen,
        dates,
        [symbol],
        [name],
        {"limit_up": np.array([np.nan]), "limit_down": np.array([np.nan])},
        apply_latest_limits=False,
    )


def test_ex_rights_reference_does_not_false_lock() -> None:
    """昨收前复权 5、当日因子 0.5 时, 原始参考价是 10 而不是 5。"""
    dates = [date(2024, 1, 2), date(2024, 1, 3)]
    up, _down = _locks("600001.SH", "普通股", dates, [5.0, 5.25], [20.0, 10.50])
    assert up[1, 0] == 0
    up_locked, _down = _locks("600001.SH", "普通股", dates, [5.0, 5.50], [20.0, 11.00])
    assert up_locked[1, 0] == 1


def test_pipeline_limit_signal_matches_raw_ex_rights_reference() -> None:
    frame = pl.DataFrame({
        "symbol": ["600001.SH", "600001.SH"],
        "date": [date(2024, 1, 2), date(2024, 1, 3)],
        "open": [5.0, 5.25],
        "high": [5.0, 5.25],
        "low": [5.0, 5.25],
        "close": [5.0, 5.25],
        "raw_close": [20.0, 10.50],
        "raw_high": [20.0, 10.50],
        "raw_low": [20.0, 10.50],
        "change_pct": [0.0, 0.0],
        "vol_ratio_5d": [1.0, 1.0],
        "volume": [1000.0, 1000.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"],
        "name": ["普通股"],
        "listing_date": [date(2010, 1, 4)],
    })
    result = pipeline.compute_limit_signals(frame, instruments, needed={"signal_limit_up"})
    assert result["signal_limit_up"][1] is False


def test_board_limit_pcts_and_st_cutoff() -> None:
    dates = [date(2024, 6, 3), date(2024, 6, 4)]
    up, _ = _locks("300001.SZ", "创业板", dates, [10.0, 12.0], [10.0, 12.0])
    assert up[1, 0] == 1
    up, _ = _locks("300001.SZ", "创业板", dates, [10.0, 11.0], [10.0, 11.0])
    assert up[1, 0] == 0
    up, _ = _locks("688001.SH", "科创板", dates, [10.0, 12.0], [10.0, 12.0])
    assert up[1, 0] == 1
    up, _ = _locks("830001.BJ", "北交所", dates, [10.0, 13.0], [10.0, 13.0])
    assert up[1, 0] == 1
    up, _ = _locks("830001.BJ", "北交所", dates, [10.0, 12.0], [10.0, 12.0])
    assert up[1, 0] == 0

    before = [date(2026, 7, 2), date(2026, 7, 3)]
    up, _ = _locks("600001.SH", "*ST示例", before, [10.0, 10.50], [10.0, 10.50])
    assert up[1, 0] == 1
    up, _ = _locks("600001.SH", "*ST示例", before, [10.0, 10.80], [10.0, 10.80])
    assert up[1, 0] == 1

    after = [date(2026, 7, 6), date(2026, 7, 7)]
    up, _ = _locks("600001.SH", "*ST示例", after, [10.0, 10.80], [10.0, 10.80])
    assert up[1, 0] == 0
    up, _ = _locks("600001.SH", "*ST示例", after, [10.0, 11.00], [10.0, 11.00])
    assert up[1, 0] == 1


def _trade_panel() -> pl.DataFrame:
    start = date(2024, 1, 1)
    rows = []
    for i in range(4):
        price = 10.0
        volume = 100_000.0
        limit_up = False
        if i == 2:
            price = 11.0
            volume = 0.0
            limit_up = True
        rows.append({
            "symbol": "A",
            "name": "A",
            "date": start + timedelta(days=i),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": volume,
            "score": 1.0,
            "signal_limit_up": limit_up,
            "signal_limit_down": False,
        })
    return pl.DataFrame(rows)


def test_zero_volume_one_price_blocks_only_that_direction() -> None:
    panel = _trade_panel()
    market = build_market_data_matrix(panel)
    assert int(market.tradable[2, 0]) == 1

    halted = panel.with_columns(pl.lit(False).alias("signal_limit_up"))
    halted_market = build_market_data_matrix(halted)
    assert int(halted_market.tradable[2, 0]) == 0

    base = date(2024, 1, 1)
    entry = []
    exit_ = []
    for row in panel.iter_rows(named=True):
        day = (row["date"] - base).days
        entry.append(day == 0)
        exit_.append(day == 1)
    result = BacktestEngine(repo=None).simulate_portfolio(
        panel,
        pl.Series(entry),
        pl.Series(exit_),
        MatcherConfig(matching="open_t+1", fees_pct=0, slippage_bps=0, max_positions=1, initial_capital=100_000),
    )
    assert result.stats["execution"]["buy_suspended"] == 0
    assert len(result.trades) == 1
    assert result.trades[0].exit_date == "2024-01-03"

    buy_panel = panel.with_columns([
        pl.when(pl.col("date") == base + timedelta(days=1))
        .then(pl.lit(True))
        .otherwise(pl.lit(False))
        .alias("signal_limit_up"),
        pl.when(pl.col("date") == base + timedelta(days=1))
        .then(pl.lit(0.0))
        .otherwise(pl.col("volume"))
        .alias("volume"),
        pl.when(pl.col("date") == base + timedelta(days=1))
        .then(pl.lit(11.0))
        .otherwise(pl.col("open"))
        .alias("open"),
    ])
    buy_panel = buy_panel.with_columns([
        pl.col("open").alias("high"),
        pl.col("open").alias("low"),
        pl.col("open").alias("close"),
    ])
    buy = BacktestEngine(repo=None).simulate_portfolio(
        buy_panel,
        pl.Series([day == 0 for day in range(4)]),
        pl.Series([False] * 4),
        MatcherConfig(matching="open_t+1", fees_pct=0, slippage_bps=0, max_positions=1),
    )
    assert buy.trades == []
    assert buy.stats["execution"]["buy_limit_up"] == 1
    assert buy.stats["execution"]["buy_suspended"] == 0


def test_portfolio_close_t_defers_non_one_price_limit_down() -> None:
    start = date(2024, 1, 1)
    rows = []
    for i in range(5):
        price = 10.0
        high = 10.2
        low = 9.8
        limit_down = False
        if i == 2:
            price = 9.0
            high = 9.4
            low = 9.0
            limit_down = True
        rows.append({
            "symbol": "A",
            "name": "A",
            "date": start + timedelta(days=i),
            "open": 10.0 if i != 2 else 9.2,
            "high": high,
            "low": low,
            "close": price,
            "volume": 10_000.0,
            "signal_limit_up": False,
            "signal_limit_down": limit_down,
        })
    panel = pl.DataFrame(rows)
    result = BacktestEngine(repo=None).simulate_portfolio(
        panel,
        pl.Series([i == 0 for i in range(5)]),
        pl.Series([i == 2 for i in range(5)]),
        MatcherConfig(
            matching="close_t",
            entry_fill="close_t",
            exit_fill="close_t",
            fees_pct=0,
            slippage_bps=0,
            max_positions=1,
            initial_capital=100_000,
        ),
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_date == "2024-01-04"
    assert result.stats["execution"]["sell_limit_down"] >= 1
