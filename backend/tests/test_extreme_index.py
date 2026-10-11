"""ts_argmax / ts_argmin 与旧 rolling_map 语义对齐。

旧实现每个窗口回调一次 Python，全市场会占满 polars 线程池。
新实现每个 symbol 序列只进一次 numpy。这里用现有 Alpha158 因子和
手写边界（NaN、null、窗口不足、并列极值）锁定结果不变。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.factors import dsl
from app.factors.dsl import FACTOR_COLUMN, compile_formula
from app.strategy.scoring import materialize_scoring_columns


def _legacy_index(frame: pl.DataFrame, column: str, n: int, *, high: bool) -> list[float | None]:
    """旧 rolling_map 口径，只留在测试里当对照。"""

    def _reduce(values: pl.Series) -> float | None:
        if values.len() != n or values.null_count() > 0:
            return None
        index = values.arg_max() if high else values.arg_min()
        if index is None:
            return None
        return float(index) + 1.0

    series = frame.select(
        pl.col(column).rolling_map(_reduce, window_size=n, min_samples=n).over("symbol").alias("v")
    )["v"]
    return [_none_if_nan(value) for value in series.to_list()]


def _none_if_nan(value: object) -> float | None:
    if value is None:
        return None
    number = float(value)  # type: ignore[arg-type]
    if math.isnan(number):
        return None
    return number


def _eval(formula: str, frame: pl.DataFrame) -> list[float | None]:
    compiled = compile_formula(formula)
    assert compiled.ok, [error.to_dict() for error in compiled.errors]
    assert compiled.frame_transform is not None
    out = compiled.frame_transform(frame)
    assert out is not None
    return [_none_if_nan(value) for value in out[FACTOR_COLUMN].to_list()]


def _assert_close(actual: list[float | None], expected: list[float | None]) -> None:
    assert len(actual) == len(expected)
    for got, want in zip(actual, expected, strict=True):
        if want is None:
            assert got is None
        else:
            assert got == pytest.approx(want)


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows).with_columns(
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
    )


def _bars(symbol: str, highs: list[float | None], lows: list[float | None] | None = None) -> list[dict]:
    rows = []
    for index, high in enumerate(highs):
        low = high if lows is None else lows[index]
        close = high if high is not None else low
        rows.append({
            "symbol": symbol,
            "date": date(2024, 1, 1) + timedelta(days=index),
            "open": close,
            "high": high,
            "low": low,
            "close": close if close is not None else 1.0,
            "volume": 10.0,
            "amount": 1000.0,
            "raw_close": close if close is not None else 1.0,
        })
    return rows


def test_dsl_no_longer_calls_rolling_map() -> None:
    text = Path(dsl.__file__).read_text(encoding="utf-8")
    assert "rolling_map(" not in text
    assert "map_batches(" in text


def test_argmax_argmin_match_legacy_on_boundaries() -> None:
    highs = [1.0, 9.0, 9.0, float("nan"), 2.0, None, 4.0, 4.0, float("-inf"), 3.0]
    lows = [5.0, 1.0, 1.0, float("nan"), 4.0, None, 0.0, 0.0, float("inf"), 2.0]
    frame = _frame(_bars("AAA", highs, lows))

    for n in (2, 3, 5):
        _assert_close(_eval(f"ts_argmax(high, {n})", frame), _legacy_index(frame, "high", n, high=True))
        _assert_close(_eval(f"ts_argmin(low, {n})", frame), _legacy_index(frame, "low", n, high=False))

    # 窗口不足：整列为空。
    short = _frame(_bars("AAA", [1.0, 2.0, 3.0]))
    assert _eval("ts_argmax(high, 5)", short) == [None, None, None]
    assert _eval("ts_argmin(low, 5)", short) == [None, None, None]

    # 并列最高取最先出现。窗口 [5, 9, 9] 的 1-based 下标是 2，不是 3。
    ties = _frame(_bars("AAA", [5.0, 9.0, 9.0], [9.0, 1.0, 1.0]))
    assert _eval("ts_argmax(high, 3)", ties)[2] == pytest.approx(2.0)
    assert _eval("ts_argmin(low, 3)", ties)[2] == pytest.approx(2.0)

    # 整窗 NaN：polars arg_max 返回 0，1-based 为 1。null 则整窗为空。
    nan_window = _frame(_bars("AAA", [float("nan"), float("nan"), float("nan")]))
    assert _eval("ts_argmax(high, 3)", nan_window)[2] == pytest.approx(1.0)
    null_window = _frame(_bars("AAA", [None, None, None]))
    assert _eval("ts_argmax(high, 3)", null_window)[2] is None


def test_interleaved_symbols_do_not_share_windows() -> None:
    frame = _frame(
        _bars("BBB", [3.0, 1.0, 4.0, 4.0, 0.0])
        + _bars("AAA", [1.0, 2.0, 2.0, 0.0, 5.0])
    )
    # 打乱物理顺序。两边都按 over("symbol") 的出现顺序，结果仍要一致。
    frame = frame.sample(fraction=1.0, shuffle=True, seed=7)
    _assert_close(_eval("ts_argmax(high, 3)", frame), _legacy_index(frame, "high", 3, high=True))
    _assert_close(_eval("ts_argmin(low, 3)", frame), _legacy_index(frame, "low", 3, high=False))


def test_existing_alpha158_position_factors_match_legacy() -> None:
    rng = np.random.default_rng(158)
    rows: list[dict] = []
    for symbol_index, symbol in enumerate(("AAA", "BBB")):
        highs = rng.normal(size=80).cumsum() + 20
        highs[10:14] = highs[10]
        highs[30] = np.nan
        lows = highs - rng.random(80)
        lows[40] = np.nan
        if symbol_index == 1:
            lows[5] = -np.inf
            highs[6] = np.inf
        for index in range(len(highs)):
            high = None if np.isnan(highs[index]) else float(highs[index])
            low = None if np.isnan(lows[index]) else float(lows[index])
            # 上面把 NaN 收成了 null。这里再写回真正的 NaN，覆盖两种空值。
            if index in {30, 31}:
                high = float("nan")
            if index == 40:
                low = float("nan")
            close = 10.0 if high is None or (isinstance(high, float) and math.isnan(high)) else high
            rows.append({
                "symbol": symbol,
                "date": date(2024, 1, 1) + timedelta(days=index),
                "open": close,
                "high": high,
                "low": low if low is not None else close,
                "close": close,
                "volume": 100.0,
                "amount": close * 10000.0,
                "raw_close": close,
            })
    frame = _frame(rows).sort(["symbol", "date"])
    names = ["a158_imax_20", "a158_imin_20", "a158_imxd_20", "a158_imax_60", "a158_imin_60"]
    got = materialize_scoring_columns(frame, names)

    argmax_20 = _legacy_index(frame, "high", 20, high=True)
    argmin_20 = _legacy_index(frame, "low", 20, high=False)
    argmax_60 = _legacy_index(frame, "high", 60, high=True)
    argmin_60 = _legacy_index(frame, "low", 60, high=False)

    def _div(values: list[float | None], window: int) -> list[float | None]:
        return [None if value is None else value / window for value in values]

    def _gap(highs: list[float | None], lows: list[float | None], window: int) -> list[float | None]:
        out = []
        for high, low in zip(highs, lows, strict=True):
            out.append(None if high is None or low is None else (high - low) / window)
        return out

    _assert_close([_none_if_nan(v) for v in got["a158_imax_20"].to_list()], _div(argmax_20, 20))
    _assert_close([_none_if_nan(v) for v in got["a158_imin_20"].to_list()], _div(argmin_20, 20))
    _assert_close([_none_if_nan(v) for v in got["a158_imxd_20"].to_list()], _gap(argmax_20, argmin_20, 20))
    _assert_close([_none_if_nan(v) for v in got["a158_imax_60"].to_list()], _div(argmax_60, 60))
    _assert_close([_none_if_nan(v) for v in got["a158_imin_60"].to_list()], _div(argmin_60, 60))
