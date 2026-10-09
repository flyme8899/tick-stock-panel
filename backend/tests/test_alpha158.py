"""Alpha158 实验组：DSL 编译、手算抽检、无未来函数、默认不进入稳定列。"""
from __future__ import annotations

import math
import time
import tracemalloc
from datetime import date, timedelta

import polars as pl
import pytest

from app.factors.alpha158 import ALPHA158_GROUP, ALPHA158_IDS, ALPHA158_SPECS, compute_alpha158
from app.factors.dsl import FACTOR_COLUMN, compile_formula
from app.factors.registry import (
    all_factors,
    factor_columns_view,
    get_factor,
    unregister_factor,
)


def _ohlcv(
    closes: list[float],
    *,
    symbol: str = "AAA",
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
    amounts: list[float] | None = None,
) -> pl.DataFrame:
    rows = []
    for index, close in enumerate(closes):
        volume = 10.0 if volumes is None else volumes[index]
        rows.append({
            "symbol": symbol,
            "date": date(2024, 1, 1) + timedelta(days=index),
            "open": close if opens is None else opens[index],
            "high": close if highs is None else highs[index],
            "low": close if lows is None else lows[index],
            "close": close,
            "volume": volume,
            "amount": (close * volume * 100.0) if amounts is None else amounts[index],
        })
    return pl.DataFrame(rows)


def _eval(formula: str, frame: pl.DataFrame) -> list[float | None]:
    compiled = compile_formula(formula)
    assert compiled.ok, [error.to_dict() for error in compiled.errors]
    assert compiled.frame_transform is not None
    out = compiled.frame_transform(frame)
    assert out is not None
    assert FACTOR_COLUMN in out.columns
    assert not any(name.startswith("__tsfx_") for name in out.columns)
    values: list[float | None] = []
    for value in out[FACTOR_COLUMN].to_list():
        if value is None or (isinstance(value, float) and math.isnan(value)):
            values.append(None)
        else:
            values.append(float(value))
    return values


def test_alpha158_count_and_compile() -> None:
    assert len(ALPHA158_SPECS) == 158
    assert len(set(ALPHA158_IDS)) == 158
    assert all(spec.id.startswith("a158_") for spec in ALPHA158_SPECS)
    assert all(spec.stability == "experimental" for spec in ALPHA158_SPECS)
    assert all(spec.asset_types == frozenset({"stock"}) for spec in ALPHA158_SPECS)
    assert all(spec.group == ALPHA158_GROUP for spec in ALPHA158_SPECS)
    failures: list[str] = []
    for spec in ALPHA158_SPECS:
        compiled = compile_formula(spec.formula_text)
        if not compiled.ok:
            failures.append(f"{spec.id}: {[error.code for error in compiled.errors]}")
    assert failures == []


def test_default_views_exclude_group_until_opt_in() -> None:
    default_ids = [item["id"] for item in factor_columns_view()]
    experimental_ids = [item["id"] for item in factor_columns_view(include_experimental=True)]
    assert not any(item.startswith("a158_") for item in default_ids)
    assert all(item in experimental_ids for item in ALPHA158_IDS)
    assert len(experimental_ids) == len(default_ids) + 158
    assert all_factors(asset_type="etf")
    assert not any(spec.id.startswith("a158_") for spec in all_factors(asset_type="etf"))
    stock_stable = all_factors(asset_type="stock", stable_only=True)
    assert len(stock_stable) == 77
    assert get_factor("a158_kmid") is not None
    with pytest.raises(ValueError, match="内置因子不可注销"):
        unregister_factor("a158_kmid")


def test_future_label_is_rejected() -> None:
    compiled = compile_formula("ts_delay(close, -3) / ts_delay(close, -1) - 1")
    assert not compiled.ok
    assert any(error.code == "E005" for error in compiled.errors)


def test_regression_and_rank_spot_checks() -> None:
    # 窗口 [3, 4, 5]，x = 0, 1, 2。斜率 1，R^2 1，残差 0。
    frame = _ohlcv([1.0, 2.0, 3.0, 4.0, 5.0])
    slope = _eval("ts_slope(close, 3)", frame)
    rsquare = _eval("ts_rsquare(close, 3)", frame)
    resi = _eval("ts_resi(close, 3)", frame)
    assert slope[:2] == [None, None]
    assert slope[2:] == pytest.approx([1.0, 1.0, 1.0])
    assert rsquare[2:] == pytest.approx([1.0, 1.0, 1.0])
    assert resi[2:] == pytest.approx([0.0, 0.0, 0.0])

    # 总体标准差：均值 4，方差 (1+0+1)/3 = 2/3。
    std0 = _eval("ts_std0(close, 3)", frame)
    assert std0[2] == pytest.approx(math.sqrt(2.0 / 3.0))

    # 线性分位 q=0.8。下标 4 的窗口是 [3,4,5]，位置 0.8*(3-1)=1.6 → 4.6。
    qlinear = _eval("ts_qlinear(close, 3, 0.8)", frame)
    assert qlinear[4] == pytest.approx(4.6)

    # 1-based，1 = 最旧。最高在最新一根，最低在最旧一根。
    argmax = _eval("ts_argmax(high, 3)", frame)
    argmin = _eval("ts_argmin(low, 3)", frame)
    assert argmax[2:] == pytest.approx([3.0, 3.0, 3.0])
    assert argmin[2:] == pytest.approx([1.0, 1.0, 1.0])

    # 并列取最先出现：窗口 [5, 9, 9]，高点下标是 2 不是 3。
    ties = _ohlcv([5.0, 9.0, 9.0])
    assert _eval("ts_argmax(high, 3)", ties)[2] == pytest.approx(2.0)

    # percentileofscore(kind='rank')/100。当前值是窗口最大： (2+3+1)/(2*3) = 1。
    rank = _eval("ts_pctrank(close, 3)", frame)
    assert rank[:2] == [None, None]
    assert rank[2] == pytest.approx(1.0)

    # 全相等 n=4：(0+4+1)/(8) = 0.625。前三根窗口未满，为空。
    flat = _ohlcv([2.0, 2.0, 2.0, 2.0])
    flat_rank = _eval("ts_pctrank(close, 4)", flat)
    assert flat_rank[:3] == [None, None, None]
    assert flat_rank[3] == pytest.approx(0.625)


def test_candle_and_vwap_spot_checks() -> None:
    frame = _ohlcv(
        [11.0],
        opens=[10.0],
        highs=[12.0],
        lows=[9.0],
        volumes=[10.0],
        amounts=[11000.0],
    )
    # (11-10)/10 = 0.1；(12-9)/10 = 0.3；上影、下影、重心都是 0.1。
    assert _eval("(close - open) / open", frame) == pytest.approx([0.1])
    assert _eval("(high - low) / open", frame) == pytest.approx([0.3])
    assert _eval("(high - max(open, close)) / open", frame) == pytest.approx([0.1])
    assert _eval("(min(open, close) - low) / open", frame) == pytest.approx([0.1])
    assert _eval("(close * 2 - high - low) / open", frame) == pytest.approx([0.1])
    # 成交量单位是手：均价 = 11000 / (10 * 100) = 11，再除以收盘 = 1。
    assert _eval("(amount / (volume * 100)) / close", frame) == pytest.approx([1.0])


def test_nested_bool_mean_and_no_lookahead() -> None:
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    frame = _ohlcv(closes)
    # 最后三根都上涨，上涨占比 = 1。第一根没有昨收，窗口未满为空。
    cntp = _eval("ts_mean(close > ts_delay(close, 1), 3)", frame)
    assert cntp[0] is None
    assert cntp[1] is None
    assert cntp[4] == pytest.approx(1.0)

    future = [*closes[:-1], 100.0]
    changed = _ohlcv(future)
    before = _eval("ts_slope(close, 3)", frame)
    after = _eval("ts_slope(close, 3)", changed)
    assert before[2] == pytest.approx(after[2])
    assert before[3] == pytest.approx(after[3])
    assert after[4] != pytest.approx(before[4])

    rank_before = _eval("ts_pctrank(close, 3)", frame)
    rank_after = _eval("ts_pctrank(close, 3)", changed)
    assert rank_before[3] == pytest.approx(rank_after[3])

    arg_before = _eval("ts_argmax(high, 3)", frame)
    arg_after = _eval("ts_argmax(high, 3)", changed)
    assert arg_before[3] == pytest.approx(arg_after[3])


def test_group_compute_ignores_future_bars() -> None:
    left = _ohlcv([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0])
    right = _ohlcv([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 80.0])
    names = ["a158_kmid", "a158_roc_5", "a158_beta_5", "a158_rank_5", "a158_cntp_5"]
    from app.strategy.scoring import materialize_scoring_columns

    a = materialize_scoring_columns(left, names)
    b = materialize_scoring_columns(right, names)
    for name in names:
        assert a[name].to_list()[:-1] == pytest.approx(b[name].to_list()[:-1], nan_ok=True)


def test_compute_chunk_matches_single_pass() -> None:
    frames = [
        _ohlcv([float(i + 1) for i in range(12)], symbol="AAA"),
        _ohlcv([float(20 - i) for i in range(12)], symbol="BBB"),
        _ohlcv([3.0] * 12, symbol="CCC"),
    ]
    frame = pl.concat(frames)
    names = ["a158_ma_5", "a158_std_5", "a158_vwap_0"]
    from app.strategy.scoring import materialize_scoring_columns

    whole = materialize_scoring_columns(frame.sort(["symbol", "date"]), names).sort(["symbol", "date"])
    # 公开入口会算完全部 158 列；这里只核对分块拼接不会串股票。
    chunked = compute_alpha158(frame, chunk_symbols=2).sort(["symbol", "date"])
    for name in names:
        assert chunked[name].to_list() == pytest.approx(whole[name].to_list(), nan_ok=True)
    assert set(chunked["symbol"].unique().to_list()) == {"AAA", "BBB", "CCC"}


def test_full_group_sample_stays_bounded() -> None:
    """合成样本，不是全 A 股。用来防止整组计算在小样本上失控。"""
    n_symbols = 40
    n_days = 70
    start = date(2024, 1, 1)
    rows: list[dict] = []
    for symbol_index in range(n_symbols):
        price = 10.0 + symbol_index * 0.01
        for day in range(n_days):
            price = max(1.0, price * (1.0 + (((symbol_index + day) % 7) - 3) * 0.004))
            volume = 100.0 + (day % 11) * 3
            rows.append({
                "symbol": f"S{symbol_index:04d}",
                "date": start + timedelta(days=day),
                "open": price * 0.99,
                "high": price * 1.02,
                "low": price * 0.98,
                "close": price,
                "volume": volume,
                "amount": price * volume * 100.0,
            })
    frame = pl.DataFrame(rows)
    tracemalloc.start()
    t0 = time.perf_counter()
    out = compute_alpha158(frame, chunk_symbols=20)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert out.height == n_symbols * n_days
    assert set(ALPHA158_IDS) <= set(out.columns)
    assert elapsed < 90
    assert peak < 800 * 1024 * 1024
