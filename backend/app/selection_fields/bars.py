"""用日 K 推导周线、月线和连涨天数。

不读新的数据源。周、月的涨跌幅是本周期收盘相对上一周期收盘的小数，
和日线 ``change_pct`` 同一口径。MACD 柱是 (DIF-DEA)×2，KDJ 的 J 是 3K-2D。
只有一根 K 时涨跌幅为空，连涨连跌为 0。
"""
from __future__ import annotations

import polars as pl

PERIOD_COLUMNS = (
    "symbol",
    "up_streak",
    "down_streak",
    "weekly_change_pct",
    "monthly_change_pct",
    "macd_dif_w",
    "macd_dea_w",
    "macd_hist_w",
    "kdj_k_w",
    "kdj_d_w",
    "kdj_j_w",
    "macd_dif_m",
    "macd_dea_m",
    "macd_hist_m",
    "kdj_k_m",
    "kdj_d_m",
    "kdj_j_m",
)

_REQUIRED = ("symbol", "date", "open", "high", "low", "close")


def empty_period_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.Utf8,
            "up_streak": pl.Int64,
            "down_streak": pl.Int64,
            "weekly_change_pct": pl.Float64,
            "monthly_change_pct": pl.Float64,
            "macd_dif_w": pl.Float64,
            "macd_dea_w": pl.Float64,
            "macd_hist_w": pl.Float64,
            "kdj_k_w": pl.Float64,
            "kdj_d_w": pl.Float64,
            "kdj_j_w": pl.Float64,
            "macd_dif_m": pl.Float64,
            "macd_dea_m": pl.Float64,
            "macd_hist_m": pl.Float64,
            "kdj_k_m": pl.Float64,
            "kdj_d_m": pl.Float64,
            "kdj_j_m": pl.Float64,
        }
    )


def _ema_alpha(span: int) -> float:
    return 2.0 / (span + 1)


def derive_period_snapshot(df: pl.DataFrame) -> pl.DataFrame:
    """每个 symbol 一行：最近交易日的连涨连跌，以及当前周、月的指标。"""
    if df is None or df.is_empty() or any(name not in df.columns for name in _REQUIRED):
        return empty_period_frame()
    frame = df.select(list(_REQUIRED)).with_columns(pl.col("date").cast(pl.Date, strict=False))
    frame = frame.filter(
        pl.col("symbol").is_not_null()
        & pl.col("date").is_not_null()
        & pl.col("close").is_not_null()
    )
    if frame.is_empty():
        return empty_period_frame()
    frame = frame.sort(["symbol", "date"])
    streaks = _streaks(frame)
    weekly = _resample(frame, "1w", "w", "weekly_change_pct")
    monthly = _resample(frame, "1mo", "m", "monthly_change_pct")
    return (
        streaks.join(weekly, on="symbol", how="left")
        .join(monthly, on="symbol", how="left")
        .select(list(PERIOD_COLUMNS))
    )


def _streaks(frame: pl.DataFrame) -> pl.DataFrame:
    prev = pl.col("close").shift(1).over("symbol")
    work = frame.with_columns(
        [
            (pl.col("close") > prev).fill_null(False).alias("_up"),
            (pl.col("close") < prev).fill_null(False).alias("_down"),
        ]
    )
    work = work.with_columns(
        [
            (pl.col("_up") != pl.col("_up").shift(1).over("symbol"))
            .fill_null(True)
            .cast(pl.Int32)
            .cum_sum()
            .over("symbol")
            .alias("_up_grp"),
            (pl.col("_down") != pl.col("_down").shift(1).over("symbol"))
            .fill_null(True)
            .cast(pl.Int32)
            .cum_sum()
            .over("symbol")
            .alias("_down_grp"),
        ]
    )
    work = work.with_columns(
        [
            pl.col("_up").cast(pl.Int64).cum_sum().over(["symbol", "_up_grp"]).alias("up_streak"),
            pl.col("_down")
            .cast(pl.Int64)
            .cum_sum()
            .over(["symbol", "_down_grp"])
            .alias("down_streak"),
        ]
    )
    return work.group_by("symbol", maintain_order=True).tail(1).select(
        ["symbol", "up_streak", "down_streak"]
    )


def _resample(frame: pl.DataFrame, every: str, suffix: str, change_name: str) -> pl.DataFrame:
    bars = (
        frame.with_columns(pl.col("date").dt.truncate(every).alias("_bucket"))
        .group_by(["symbol", "_bucket"], maintain_order=True)
        .agg(
            [
                pl.col("open").first().alias("open"),
                pl.col("high").max().alias("high"),
                pl.col("low").min().alias("low"),
                pl.col("close").last().alias("close"),
            ]
        )
        .sort(["symbol", "_bucket"])
    )
    bars = bars.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias(change_name)
    )
    bars = _macd_kdj(bars, suffix)
    keep = [
        "symbol",
        change_name,
        f"macd_dif_{suffix}",
        f"macd_dea_{suffix}",
        f"macd_hist_{suffix}",
        f"kdj_k_{suffix}",
        f"kdj_d_{suffix}",
        f"kdj_j_{suffix}",
    ]
    return bars.group_by("symbol", maintain_order=True).tail(1).select(keep)


def _macd_kdj(bars: pl.DataFrame, suffix: str) -> pl.DataFrame:
    dif = f"macd_dif_{suffix}"
    dea = f"macd_dea_{suffix}"
    hist = f"macd_hist_{suffix}"
    work = bars.with_columns(
        [
            pl.col("close")
            .ewm_mean(alpha=_ema_alpha(12), adjust=False)
            .over("symbol")
            .alias("_ema12"),
            pl.col("close")
            .ewm_mean(alpha=_ema_alpha(26), adjust=False)
            .over("symbol")
            .alias("_ema26"),
        ]
    )
    work = work.with_columns((pl.col("_ema12") - pl.col("_ema26")).alias(dif))
    work = work.with_columns(
        pl.col(dif).ewm_mean(alpha=_ema_alpha(9), adjust=False).over("symbol").alias(dea)
    )
    work = work.with_columns(((pl.col(dif) - pl.col(dea)) * 2).alias(hist))

    high_n = pl.col("high").rolling_max(9).over("symbol")
    low_n = pl.col("low").rolling_min(9).over("symbol")
    span = high_n - low_n
    rsv = pl.when(span > 0).then(100 * (pl.col("close") - low_n) / span)
    k_name = f"kdj_k_{suffix}"
    d_name = f"kdj_d_{suffix}"
    j_name = f"kdj_j_{suffix}"
    work = work.with_columns(
        rsv.ewm_mean(alpha=1.0 / 3, adjust=False, ignore_nulls=True).over("symbol").alias(k_name)
    )
    work = work.with_columns(
        pl.col(k_name)
        .ewm_mean(alpha=1.0 / 3, adjust=False, ignore_nulls=True)
        .over("symbol")
        .alias(d_name)
    )
    return work.with_columns((3 * pl.col(k_name) - 2 * pl.col(d_name)).alias(j_name))
