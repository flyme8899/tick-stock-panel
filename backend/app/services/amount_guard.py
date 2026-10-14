"""分钟成交额脏数据防护。

分钟 K 与日线的 volume 都是手，amount 是元。隐含价 = amount / (volume × 100)。
部分分钟 bar（主要是 688* 与 920045.BJ）的 amount 偏大约 10 倍，累计 VWAP
会被抬到当日高低价之外。

复权投影（``price_basis.scale_minute_prices``、``minute_adjust.apply_minute_adjustment``）
把 OHLC 与 amount 乘同一个因子，volume 不动。检查必须用同一口径的价格：
传入的 low/high/close 要和 amount 一起已经复权，或一起仍是原始价。
日线 enriched 的 high/low/close 是前复权，amount 是不复权，日线只能拿
raw_high / raw_low / raw_close 来判断，不能拿复权高低价。
"""

from __future__ import annotations

import math

import polars as pl

# 1 手 = 100 股。VWAP = amount / (volume_手 × 100)。
SHARES_PER_LOT = 100.0
# 隐含价相对本根高低价的容差。区间外视为成交额与价格对不上。
LOW_FACTOR = 0.97
HIGH_FACTOR = 1.03
# 相对收盘偏离超过该比例也视为脏数据。「超过」是严格大于。
CLOSE_DEVIATION = 0.20
# 累计 VWAP 贴着当日高低价时，浮点误差不算出界。
DAY_RANGE_REL_EPS = 1e-6


def _finite(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def sanitize_bar_amount(amount, volume, *, low, high, close) -> float | None:
    """单根 bar 的成交额。volume 为手。价格与 amount 必须同一口径。

    隐含价落在 [low×0.97, high×1.03] 之外，或相对 close 偏离超过 20% 时，
    用 close×volume×100 替换。无法判断时原样返回有限的 amount；amount 缺失返回 None。
    """
    amount_f = _finite(amount)
    if amount_f is None:
        return None
    volume_f = _finite(volume)
    close_f = _finite(close)
    if volume_f is None or volume_f <= 0 or close_f is None or close_f <= 0:
        return amount_f
    shares = volume_f * SHARES_PER_LOT
    implied = amount_f / shares
    if not math.isfinite(implied):
        return amount_f
    dirty = abs(implied / close_f - 1.0) > CLOSE_DEVIATION
    low_f = _finite(low)
    high_f = _finite(high)
    if (
        low_f is not None
        and high_f is not None
        and low_f > 0
        and high_f > 0
        and high_f >= low_f
        and (implied < low_f * LOW_FACTOR or implied > high_f * HIGH_FACTOR)
    ):
        dirty = True
    if not dirty:
        return amount_f
    return close_f * shares


def sanitized_amount_expr(
    amount: pl.Expr,
    volume: pl.Expr,
    *,
    low: pl.Expr | None,
    high: pl.Expr | None,
    close: pl.Expr,
) -> pl.Expr:
    """与 :func:`sanitize_bar_amount` 同一规则的向量表达式。low/high 缺失时只做收盘偏离检查。"""
    amount_f = amount.cast(pl.Float64, strict=False)
    volume_f = volume.cast(pl.Float64, strict=False)
    close_f = close.cast(pl.Float64, strict=False)
    shares = volume_f * SHARES_PER_LOT
    implied = amount_f / shares
    usable = (
        amount_f.is_finite()
        & volume_f.is_finite()
        & (volume_f > 0)
        & close_f.is_finite()
        & (close_f > 0)
        & implied.is_finite()
    )
    far = ((implied / close_f - 1.0).abs() > CLOSE_DEVIATION).fill_null(False)
    dirty = usable.fill_null(False) & far
    if low is not None and high is not None:
        low_f = low.cast(pl.Float64, strict=False)
        high_f = high.cast(pl.Float64, strict=False)
        band_ok = (
            low_f.is_finite() & (low_f > 0) & high_f.is_finite() & (high_f > 0) & (high_f >= low_f)
        ).fill_null(False)
        outside = ((implied < low_f * LOW_FACTOR) | (implied > high_f * HIGH_FACTOR)).fill_null(
            False
        )
        dirty = dirty | (usable.fill_null(False) & band_ok & outside)
    return pl.when(dirty).then(close_f * shares).otherwise(amount_f)


def sanitize_minute_amount(
    frame: pl.DataFrame,
    *,
    amount: str = "amount",
    volume: str = "volume",
    low: str = "low",
    high: str = "high",
    close: str = "close",
) -> pl.DataFrame:
    """把帧里对不上价格的 amount 换成 close×volume×100。缺列时原样返回。

    volume 为手。low/high/close 必须与 amount 同一复权口径。
    """
    if frame.is_empty() or not {amount, volume, close}.issubset(frame.columns):
        return frame
    low_expr = pl.col(low) if low in frame.columns else None
    high_expr = pl.col(high) if high in frame.columns else None
    if low_expr is None or high_expr is None:
        low_expr = None
        high_expr = None
    return frame.with_columns(
        sanitized_amount_expr(
            pl.col(amount),
            pl.col(volume),
            low=low_expr,
            high=high_expr,
            close=pl.col(close),
        ).alias(amount)
    )


def sanitize_amount_array(amount, volume, *, low, high, close):
    """NumPy 版，供日线矩阵与评分保持同一口径。volume 为手。low/high 可缺。"""
    import numpy as np

    amount_a = np.asarray(amount, dtype=np.float64)
    volume_a = np.asarray(volume, dtype=np.float64)
    close_a = np.asarray(close, dtype=np.float64)
    out = np.array(amount_a, dtype=np.float64, copy=True)
    shares = volume_a * SHARES_PER_LOT
    usable = (
        np.isfinite(amount_a)
        & np.isfinite(volume_a)
        & (volume_a > 0)
        & np.isfinite(close_a)
        & (close_a > 0)
        & np.isfinite(shares)
        & (shares > 0)
    )
    implied = np.zeros(amount_a.shape, dtype=np.float64)
    np.divide(amount_a, shares, out=implied, where=usable)
    ratio = np.ones(amount_a.shape, dtype=np.float64)
    np.divide(implied, close_a, out=ratio, where=usable)
    dirty = usable & (np.abs(ratio - 1.0) > CLOSE_DEVIATION)
    if low is not None and high is not None:
        low_a = np.asarray(low, dtype=np.float64)
        high_a = np.asarray(high, dtype=np.float64)
        band_ok = (
            np.isfinite(low_a)
            & (low_a > 0)
            & np.isfinite(high_a)
            & (high_a > 0)
            & (high_a >= low_a)
        )
        outside = (implied < low_a * LOW_FACTOR) | (implied > high_a * HIGH_FACTOR)
        dirty = dirty | (usable & band_ok & outside)
    np.copyto(out, close_a * shares, where=dirty)
    return out


def vwap_outside_day_range(vwap, low, high) -> bool:
    """累计 VWAP 是否落在当日 [low, high] 之外。

    高低价缺失或非正时返回 False：无法判断就不丢掉 VWAP 条件。
    """
    vwap_f = _finite(vwap)
    low_f = _finite(low)
    high_f = _finite(high)
    if vwap_f is None or low_f is None or high_f is None or low_f <= 0 or high_f <= 0:
        return False
    return vwap_f < low_f * (1.0 - DAY_RANGE_REL_EPS) or vwap_f > high_f * (1.0 + DAY_RANGE_REL_EPS)
