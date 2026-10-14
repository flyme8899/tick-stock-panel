"""分钟 amount 脏数据：隐含价对不上高低价或收盘时，VWAP 不得被放大。"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix, matrix_feature
from app.factors.dsl import FACTOR_COLUMN, compile_formula
from app.factors.registry import get_factor
from app.news.push import session_signal_sets, t_conditions
from app.services.amount_guard import (
    CLOSE_DEVIATION,
    sanitize_amount_array,
    sanitize_bar_amount,
    sanitize_minute_amount,
    vwap_outside_day_range,
)
from app.services.price_basis import scale_minute_prices
from app.strategy.intraday_features import build_feature_frame
from app.strategy.scoring import materialize_scoring_columns


def _bar(close: float, volume: float = 100.0, *, low=None, high=None, amount=None) -> dict:
    low = close if low is None else low
    high = close if high is None else high
    if amount is None:
        amount = close * volume * 100.0
    return {
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
    }


def test_clean_bar_amount_is_unchanged():
    bar = _bar(10.2, 80.0, low=10.0, high=10.4)
    assert sanitize_bar_amount(
        bar["amount"],
        bar["volume"],
        low=bar["low"],
        high=bar["high"],
        close=bar["close"],
    ) == pytest.approx(bar["amount"])


def test_amount_outside_bar_band_is_replaced_with_close():
    volume = 1200.0
    close, low, high = 8.52, 8.40, 8.60
    dirty = close * volume * 100.0 * 10.0
    cleaned = sanitize_bar_amount(dirty, volume, low=low, high=high, close=close)
    assert cleaned == pytest.approx(close * volume * 100.0)
    implied = cleaned / (volume * 100.0)
    assert low <= implied <= high


def test_band_edges_keep_amount_and_just_outside_replaces():
    volume = 100.0
    close = low = high = 10.0
    shares = volume * 100.0
    inside = low * 0.98 * shares
    outside = low * 0.96 * shares
    assert sanitize_bar_amount(inside, volume, low=low, high=high, close=close) == pytest.approx(
        inside
    )
    assert sanitize_bar_amount(outside, volume, low=low, high=high, close=close) == pytest.approx(
        close * shares
    )
    above = high * 1.04 * shares
    assert sanitize_bar_amount(above, volume, low=low, high=high, close=close) == pytest.approx(
        close * shares
    )


def test_close_deviation_replaces_even_inside_a_wide_band():
    volume = 50.0
    low, high, close = 10.0, 14.0, 10.0
    shares = volume * 100.0
    mild = close * (1 + CLOSE_DEVIATION - 0.01) * shares
    far = close * (1 + CLOSE_DEVIATION + 0.01) * shares
    assert sanitize_bar_amount(mild, volume, low=low, high=high, close=close) == pytest.approx(mild)
    assert sanitize_bar_amount(far, volume, low=low, high=high, close=close) == pytest.approx(
        close * shares
    )


def test_missing_amount_or_volume_is_left_alone():
    assert sanitize_bar_amount(None, 100.0, low=10, high=10, close=10) is None
    assert sanitize_bar_amount(1_000_000.0, 0.0, low=10, high=10, close=10) == pytest.approx(
        1_000_000.0
    )
    assert sanitize_bar_amount(float("nan"), 100.0, low=10, high=10, close=10) is None


def test_frame_and_array_match_scalar_rules():
    frame = pl.DataFrame(
        [
            _bar(10.0),
            _bar(8.52, 1200.0, low=8.40, high=8.60, amount=8.52 * 1200.0 * 100.0 * 10.0),
            _bar(11.0, low=10.0, high=14.0, amount=11.0 * 100.0 * 100.0 * 1.25),
        ]
    )
    cleaned = sanitize_minute_amount(frame)
    scalar = [
        sanitize_bar_amount(
            row["amount"], row["volume"], low=row["low"], high=row["high"], close=row["close"]
        )
        for row in frame.to_dicts()
    ]
    assert cleaned["amount"].to_list() == pytest.approx(scalar)
    array = sanitize_amount_array(
        frame["amount"].to_numpy(),
        frame["volume"].to_numpy(),
        low=frame["low"].to_numpy(),
        high=frame["high"].to_numpy(),
        close=frame["close"].to_numpy(),
    )
    assert array.tolist() == pytest.approx(scalar)
    assert cleaned["volume"].to_list() == frame["volume"].to_list()


def test_adjustment_keeps_price_and_amount_on_one_basis():
    """复权把 OHLC 与 amount 同乘。清洗必须用缩放后的价格，不能退回原始价。"""
    volume = 100.0
    raw = np.array([[20.0, 20.4, 19.6, 20.0, volume, 20.0 * volume * 100.0]], dtype=np.float64)
    scaled = scale_minute_prices(raw, 0.5)
    clean = _frame_from_scaled(scaled)
    assert sanitize_minute_amount(clean)["amount"][0] == pytest.approx(float(scaled[0, 5]))

    dirty = raw.copy()
    dirty[0, 5] *= 10.0
    scaled_dirty = scale_minute_prices(dirty, 0.5)
    fixed = sanitize_minute_amount(_frame_from_scaled(scaled_dirty))
    # 缩放后 close = 10，回填额 = 10 × 手 × 100，而不是原始 close 20。
    assert fixed["amount"][0] == pytest.approx(10.0 * volume * 100.0)
    assert fixed["amount"][0] / (volume * 100.0) == pytest.approx(fixed["close"][0])


def _frame_from_scaled(array: np.ndarray) -> pl.DataFrame:
    row = array[0]
    return pl.DataFrame(
        {
            "open": [row[0]],
            "high": [row[1]],
            "low": [row[2]],
            "close": [row[3]],
            "volume": [row[4]],
            "amount": [row[5]],
        }
    )


def test_bj_dirty_bar_vwap_stays_inside_day_range():
    """920045.BJ 这类 10 倍 amount：累计 VWAP 必须落回当日高低价之间。"""
    symbol = "920045.BJ"
    prices = [8.40, 8.52, 8.60]
    volumes = [800.0, 1200.0, 900.0]
    amount = [price * volume * 100.0 for price, volume in zip(prices, volumes, strict=True)]
    amount[1] *= 10.0
    start = datetime(2026, 10, 13, 9, 31)
    frame = pl.DataFrame(
        {
            "symbol": [symbol] * 3,
            "datetime": [start, start.replace(minute=32), start.replace(minute=33)],
            "open": prices,
            "high": [price + 0.05 for price in prices],
            "low": [price - 0.05 for price in prices],
            "close": prices,
            "volume": volumes,
            "amount": amount,
        }
    )
    features = build_feature_frame(frame)
    day_low = min(frame["low"].to_list())
    day_high = max(frame["high"].to_list())
    vwaps = features["vwap"].to_list()
    assert vwaps[1] == pytest.approx((8.40 * 800.0 + 8.52 * 1200.0) / (800.0 + 1200.0))
    for value in vwaps:
        assert day_low <= value <= day_high


def test_clean_feature_vwap_is_unchanged():
    prices = [10.0, 10.2, 9.8]
    volumes = [100.0, 100.0, 100.0]
    start = datetime(2026, 9, 4, 9, 30)
    frame = pl.DataFrame(
        {
            "symbol": ["000001.SZ"] * 3,
            "datetime": [start, start.replace(minute=31), start.replace(minute=32)],
            "open": prices,
            "high": [price * 1.01 for price in prices],
            "low": [price * 0.99 for price in prices],
            "close": prices,
            "volume": volumes,
            "amount": [
                price * volume * 100.0 for price, volume in zip(prices, volumes, strict=True)
            ],
        }
    )
    features = build_feature_frame(frame)
    assert features["vwap"][2] == pytest.approx(sum(prices) / 3)


def test_t_conditions_skip_vwap_when_average_is_outside_the_day_range():
    # 实时报价只有日累计。10 倍成交额会把均价抬到 100，现价 10 会误报低于均价。
    signals, _pct = t_conditions(
        {
            "close": 10.0,
            "high": 10.2,
            "low": 9.8,
            "volume": 1000.0,
            "amount": 10.0 * 1000.0 * 100.0 * 10.0,
            "prev_close": 10.0,
        }
    )
    assert "above_vwap" not in signals
    assert "below_vwap" not in signals
    assert vwap_outside_day_range(100.0, 9.8, 10.2)
    assert not vwap_outside_day_range(10.0, 9.8, 10.2)

    healthy, _pct = t_conditions(
        {
            "close": 103.0,
            "high": 104.0,
            "low": 100.0,
            "volume": 1000.0,
            "amount": 1000.0 * 100.0 * 100.0,
            "prev_close": 100.0,
        }
    )
    assert "above_vwap" in healthy


def test_session_replay_repairs_one_dirty_bar_without_flipping_vwap_side():
    bars = [
        _bar(10.0, 100.0),
        _bar(10.2, 100.0, low=10.0, high=10.2, amount=10.2 * 100.0 * 100.0 * 10.0),
        _bar(10.5, 20.0, low=10.4, high=10.5),
    ]
    sets = session_signal_sets(bars, mode="t", symbol="920045.BJ", prev_close=10.0)
    assert "below_vwap" not in sets[1]
    assert "above_vwap" in sets[2]


def test_daily_vwap_bias_repairs_dirty_amount_on_raw_prices():
    close = 12.0
    raw_close = 12.0
    volume = 200.0
    panel = pl.DataFrame(
        {
            "symbol": ["688001.SH"],
            "date": [date(2026, 10, 13)],
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "raw_close": [raw_close],
            "raw_high": [12.4],
            "raw_low": [11.6],
            "volume": [volume],
            "amount": [raw_close * volume * 100.0 * 10.0],
        }
    )
    frame = materialize_scoring_columns(panel, {"vwap_bias"})
    assert frame["vwap_bias"][0] == pytest.approx(0.0)
    market = build_market_data_matrix(
        panel,
        field_columns={"amount", "raw_close", "raw_high", "raw_low"},
    )
    assert float(matrix_feature(market, "vwap_bias")[0, 0]) == pytest.approx(0.0)

    honest = panel.with_columns(pl.lit(raw_close * volume * 100.0).alias("amount"))
    kept = materialize_scoring_columns(honest, {"vwap_bias"})
    assert kept["vwap_bias"][0] == pytest.approx(0.0)


def test_alpha158_vwap_replaces_tenfold_amount_and_keeps_small_gap():
    spec = get_factor("a158_vwap_0")
    assert spec is not None
    compiled = compile_formula(spec.formula_text)
    assert compiled.ok, [error.to_dict() for error in compiled.errors]

    def evaluate(amount: float, raw_close: float = 10.0) -> float:
        frame = pl.DataFrame(
            {
                "symbol": ["688001.SH"],
                "date": [date(2026, 10, 13)],
                "volume": [10.0],
                "amount": [amount],
                "raw_close": [raw_close],
            }
        )
        out = compiled.frame_transform(frame)
        assert out is not None
        return float(out[FACTOR_COLUMN][0])

    # 均价 11、不复权收盘 10，偏离 10%，保持原公式。
    assert evaluate(11.0 * 10.0 * 100.0) == pytest.approx(1.1)
    # 10 倍成交额回填后，均价等于不复权收盘。
    assert evaluate(11.0 * 10.0 * 100.0 * 10.0) == pytest.approx(1.0)
