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
            "raw_close": close,
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
    assert not any(spec.id.startswith("a158_") for spec in all_factors())
    assert any(spec.id == "a158_kmid" for spec in all_factors(include_experimental=True))
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
    ).with_columns(pl.lit(10.0).alias("raw_close"))
    # (11-10)/10 = 0.1；(12-9)/10 = 0.3；上影、下影、重心都是 0.1。
    assert _eval("(close - open) / open", frame) == pytest.approx([0.1])
    assert _eval("(high - low) / open", frame) == pytest.approx([0.3])
    assert _eval("(high - max(open, close)) / open", frame) == pytest.approx([0.1])
    assert _eval("(min(open, close) - low) / open", frame) == pytest.approx([0.1])
    assert _eval("(close * 2 - high - low) / open", frame) == pytest.approx([0.1])
    # 成交量单位是手。均价 = 11000 / (10 * 100) = 11。
    # close=11 是前复权价，raw_close=10 才是不复权收盘，vwap_0 = 11/10 = 1.1。
    vwap = get_factor("a158_vwap_0")
    assert vwap is not None
    assert "raw_close" in vwap.formula_text
    assert "raw_close" in vwap.dependencies
    assert _eval(vwap.formula_text, frame) == pytest.approx([1.1])


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
                "raw_close": price,
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


def _percentile_rank(window: list[float], score: float) -> float:
    """scipy.stats.percentileofscore(kind='rank') / 100，不引入 scipy。"""
    left = sum(value < score for value in window)
    right = sum(value <= score for value in window)
    plus = 1 if left < right else 0
    return (left + right + plus) / (2 * len(window))


def test_new_operators_match_numpy_reference_including_ties() -> None:
    import numpy as np

    rng = np.random.default_rng(158)
    series = rng.normal(size=90).cumsum() + 20
    series[70:75] = series[70]
    frame = _ohlcv(series.tolist())
    n = 60
    window = series[-n:]
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, window, 1)
    fitted = slope * x + intercept
    ss_res = float(np.sum((window - fitted) ** 2))
    ss_tot = float(np.sum((window - window.mean()) ** 2))
    last = len(series) - 1

    assert _eval(f"ts_std0(close, {n})", frame)[last] == pytest.approx(float(np.std(window, ddof=0)))
    assert _eval(f"ts_qlinear(close, {n}, 0.8)", frame)[last] == pytest.approx(
        float(np.quantile(window, 0.8, method="linear")),
    )
    assert _eval(f"ts_pctrank(close, {n})", frame)[last] == pytest.approx(
        _percentile_rank(window.tolist(), float(window[-1])),
    )
    assert _eval(f"ts_slope(close, {n})", frame)[last] == pytest.approx(float(slope))
    assert _eval(f"ts_rsquare(close, {n})", frame)[last] == pytest.approx(1 - ss_res / ss_tot)
    assert _eval(f"ts_resi(close, {n})", frame)[last] == pytest.approx(
        float(window[-1] - (slope * (n - 1) + intercept)),
    )

    # 并列最高出现在窗口下标 3 和 40（0-based），1-based 取最先的 4。
    tied = np.linspace(1, 10, n)
    tied[3] = 99
    tied[40] = 99
    tied_low = tied.copy()
    tied_low[5] = -5
    tied_low[20] = -5
    tie_frame = _ohlcv(tied.tolist())
    assert _eval(f"ts_argmax(high, {n})", tie_frame)[n - 1] == pytest.approx(4.0)
    low_frame = _ohlcv(tied_low.tolist())
    assert _eval(f"ts_argmin(low, {n})", low_frame)[n - 1] == pytest.approx(6.0)
    assert _eval(f"ts_argmax(high, {n})", tie_frame)[n - 2] is None


def test_window_60_is_populated_after_120_calendar_days() -> None:
    """评审里 max_60 的 IC 接近 0。核对是不是 120 个自然日的预热不够。

    2025-10-09 往前 120 个自然日里，只扣周末也多于 60 个交易日。
    这段没有春节，再扣大约 10 个假日仍够 60 日窗口。
    历史被截到 40 个交易日时，max_60 为空、max_30 仍有数，用来对照真正的预热不足。
    """
    from app.strategy.scoring import materialize_scoring_columns

    eval_start = date(2025, 10, 9)
    warmup_start = eval_start - timedelta(days=120)
    span = [
        warmup_start + timedelta(days=offset)
        for offset in range((eval_start - warmup_start).days + 30)
    ]
    weekdays = [day for day in span if day.weekday() < 5]
    before = [day for day in weekdays if day < eval_start]
    assert len(before) >= 61
    assert len(before) - 10 >= 61

    def _frame(days: list[date]) -> pl.DataFrame:
        rows = []
        for index, day in enumerate(days):
            close = 10 + index * 0.1
            rows.append({
                "symbol": "AAA",
                "date": day,
                "open": close,
                "high": close + (index % 5),
                "low": close - 0.2,
                "close": close,
                "raw_close": close,
                "volume": 100.0,
                "amount": close * 10000,
            })
        return pl.DataFrame(rows)

    full = materialize_scoring_columns(_frame(weekdays), ["a158_max_30", "a158_max_60"])
    on_eval = full.filter(pl.col("date") >= eval_start)
    assert on_eval.height > 0
    assert on_eval["a158_max_60"].null_count() == 0
    assert on_eval["a158_max_30"].null_count() == 0

    short_days = weekdays[:40]
    short = materialize_scoring_columns(_frame(short_days), ["a158_max_30", "a158_max_60"])
    assert short["a158_max_60"].null_count() == short.height
    assert short["a158_max_30"].tail(5).null_count() == 0


def _eval_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "eval_alpha158.py"
    spec = importlib.util.spec_from_file_location("eval_alpha158", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_eval_script_gates_and_filters() -> None:
    module = _eval_script()
    assert module.polars_matches_project("1.44.2")
    assert not module.polars_matches_project("1.43.1")
    assert not module.polars_matches_project("1.45.0")
    assert not module.polars_matches_project("2.0.1")
    today = date(2026, 10, 9)
    assert module.latest_closed_trading_day(
        [date(2026, 10, 8), today], today,
    ) == date(2026, 10, 8)
    assert module.latest_closed_trading_day([today], today) is None

    days = [date(2024, 1, 2) + timedelta(days=offset) for offset in range(4)]
    closes = [10.0, 11.0, 12.0, 13.0]
    frame = pl.DataFrame({
        "symbol": ["000001.SZ"] * 4,
        "date": days,
        "close": closes,
    })
    labels = module.qlib_forward_return(frame, lag=1, horizon=1, start=days[0], end=days[-1])
    by_date = dict(zip(labels["date"].to_list(), labels["_qlib_return"].to_list(), strict=True))
    # close(t+2)/close(t+1)-1 = 12/11-1
    assert by_date[days[0]] == pytest.approx(12 / 11 - 1)
    assert by_date[days[-1]] is None

    instruments = pl.DataFrame({
        "symbol": ["000001.SZ", "000002.SZ"],
        "name": ["平安银行", "*ST 测试"],
        "listing_date": [date(2010, 1, 4), date(2024, 1, 2)],
    })
    prices = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ", "000002.SZ"],
        "date": [days[0], days[1], days[0]],
        "raw_close": [10.0, 11.0, 8.0],
        "close": [10.0, 11.0, 8.0],
        "a158_kmid": [0.1, 0.2, 0.3],
    })
    limited = module.apply_row_filters(
        prices.filter(pl.col("symbol") == "000001.SZ"),
        exclude_limit=True,
        exclude_st=False,
        min_listed_days=0,
        instruments=instruments,
    )
    assert limited["date"].to_list() == [days[0]]
    st_only = module.apply_row_filters(
        prices.filter(pl.col("symbol") == "000002.SZ"),
        exclude_limit=False,
        exclude_st=True,
        min_listed_days=0,
        instruments=instruments,
    )
    assert st_only.is_empty()


def test_drop_thin_dates_keeps_only_wide_cross_sections() -> None:
    """横截面门槛: 样本太少的交易日不能进 IC 均值。

    这类日子的 Rank IC 是噪声(只有几只股票时几个点就能算出 ±0.5),
    数量一多就会主导均值, 把因子强弱排反。
    """
    module = _eval_script()
    days = [date(2026, 5, 13), date(2026, 5, 14), date(2026, 5, 15)]
    # 每天样本数: 5 / 2 / 4
    symbols = (
        ["00000%d.SZ" % i for i in range(5)]
        + ["000001.SZ", "000002.SZ"]
        + ["00000%d.SZ" % i for i in range(4)]
    )
    panel = pl.DataFrame({
        "symbol": symbols,
        "date": [d for d, n in zip(days, (5, 2, 4)) for _ in range(n)],
    })

    kept, thick, dropped = module.drop_thin_dates(panel, 3)
    assert dropped == 1
    assert sorted(kept.get_column("date").unique().to_list()) == [days[0], days[2]]
    # 保留下来的表要能报出每天的样本数, 供脚本打印中位/最少
    assert sorted(thick.get_column("_n").to_list()) == [4, 5]

    # 门槛 0 表示不过滤
    kept_all, thick_all, dropped_all = module.drop_thin_dates(panel, 0)
    assert dropped_all == 0
    assert kept_all.height == panel.height


def test_signal_whitelist_excludes_alpha158() -> None:
    from app.strategy.custom_signals import allowed_fields
    from app.strategy.custom_signals_ai import build_messages

    assert "a158_kmid" not in allowed_fields()
    system = build_messages("动量强的票")[0]["content"]
    assert "a158_" not in system
