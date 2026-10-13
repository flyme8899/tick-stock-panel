"""前复权日线与不复权分钟价的口径对齐。

enriched 的 open/high/low/close 是前复权，raw_close 是不复权。分钟分区在
`.raw_basis` 标记存在时，仓库读取会把价格和成交额投影到同一复权尺度；标记
不存在时读取原样返回，而当前分钟事实层是不复权价。把复权日线价直接与未投影
的分钟价、或 amount/(volume×100) 的原始均价相除，除权股会产生虚假收益。

当日因子 f = close / raw_close。原始价 = 复权价 / f。标记已开启时读取结果
已经是复权价，不得再乘 f。
"""
from __future__ import annotations

import math
from datetime import date


def _finite_positive(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def adjusted_over_raw(close, raw_close) -> float:
    """当日复权因子 close/raw_close。缺失或非正时为 1，调用方不缩放。"""
    adjusted = _finite_positive(close)
    raw = _finite_positive(raw_close)
    if adjusted is None or raw is None:
        return 1.0
    return adjusted / raw


def raw_from_adjusted(price, close, raw_close) -> float | None:
    """把复权价折回当日原始价尺度。"""
    number = _finite_positive(price)
    if number is None:
        return None
    factor = adjusted_over_raw(close, raw_close)
    if factor == 1.0:
        return number
    return number / factor


def adjusted_from_raw(price, close, raw_close) -> float | None:
    """把原始价折到当日复权价尺度。"""
    number = _finite_positive(price)
    if number is None:
        return None
    factor = adjusted_over_raw(close, raw_close)
    if factor == 1.0:
        return number
    return number * factor


def session_prev_on_raw_scale(prev_adjusted, prev_raw, day_close, day_raw) -> float | None:
    """交易所昨收在原始价尺度。

    与 indicators.pipeline 涨跌停参考价同口径：当日复权因子相对昨日跳变时，
    前复权昨收仍在复权尺度，除以当日因子才回到原始参考价；因子不变时用原始昨收。
    最新日因子为 1 时，除权日该式等于前复权昨收。
    """
    adjusted = _finite_positive(prev_adjusted)
    raw_prev = _finite_positive(prev_raw)
    factor = adjusted_over_raw(day_close, day_raw)
    if adjusted is not None and raw_prev is not None:
        yesterday = adjusted / raw_prev
        if abs(factor - yesterday) > 1e-6:
            return adjusted / factor
        return raw_prev
    if raw_prev is not None and adjusted is None:
        return raw_prev
    if adjusted is None:
        return None
    if factor != 1.0:
        return adjusted / factor
    return adjusted


def minute_prev_close(
    prev_adjusted,
    *,
    day_close=None,
    day_raw=None,
    prev_raw=None,
    reads_adjusted: bool,
) -> float | None:
    """分钟图、做T 和盘中信号使用的昨收，与分钟读取口径一致。"""
    if reads_adjusted:
        adjusted = _finite_positive(prev_adjusted)
        if adjusted is not None:
            return adjusted
        return _finite_positive(prev_raw)
    return session_prev_on_raw_scale(prev_adjusted, prev_raw, day_close, day_raw)


def minute_reads_are_adjusted(data_dir) -> bool:
    """仓库 get_minute* 是否已经投影到前复权。无数据目录视为未投影。"""
    if data_dir is None:
        return False
    from app.services.minute_adjust import minute_basis_is_raw

    try:
        return bool(minute_basis_is_raw(data_dir))
    except OSError:
        return False


def repo_data_dir(repo):
    return getattr(getattr(repo, "store", None), "data_dir", None)


def scale_minute_prices(array, factor: float):
    """把原始分钟 OHLC 与 amount 乘到复权尺度。volume（手）不变，VWAP 跟着价格走。"""
    if array is None or factor == 1.0 or not math.isfinite(factor) or factor <= 0:
        return array
    if getattr(array, "ndim", 0) != 2 or array.shape[1] < 4:
        return array
    import numpy as np

    out = np.array(array, dtype=np.float64, copy=True)
    out[:, 0:4] *= factor
    if out.shape[1] >= 6:
        out[:, 5] *= factor
    return out


def factors_from_rows(rows) -> dict[tuple[str, str], float]:
    """(symbol, YYYY-MM-DD) -> close/raw_close，只保留不等于 1 的交易日。"""
    out: dict[tuple[str, str], float] = {}
    for symbol, day, close, raw in rows:
        factor = adjusted_over_raw(close, raw)
        if abs(factor - 1.0) <= 1e-12:
            continue
        out[(str(symbol), str(day)[:10])] = factor
    return out


def _parse_dates(dates) -> list[date]:
    parsed: list[date] = []
    for item in dates:
        if isinstance(item, date):
            parsed.append(item)
            continue
        try:
            parsed.append(date.fromisoformat(str(item)[:10]))
        except ValueError:
            continue
    return parsed


def _load_daily_factor_frame(repo, symbols: list[str], start: date, end: date, asset_type: str):
    columns = ["symbol", "date", "close", "raw_close"]
    try:
        if asset_type == "stock":
            getter = getattr(repo, "get_daily_batch", None)
            if getter is None:
                return None
            return getter(symbols, start, end, columns=columns)
        getter = getattr(repo, "get_daily_asset", None)
        if getter is None:
            return None
        import polars as pl

        parts = []
        for symbol in symbols:
            part = getter(asset_type, symbol, start, end, columns=["date", "close", "raw_close"])
            if part is None or part.is_empty():
                continue
            if "symbol" not in part.columns:
                part = part.with_columns(pl.lit(symbol).alias("symbol"))
            parts.append(part)
        if not parts:
            return None
        return pl.concat(parts, how="diagonal_relaxed")
    except Exception:  # noqa: BLE001 — 日线因子缺失时不缩放, 保持原成交价
        return None


def lookup_minute_price_factors(repo, symbols, dates, asset_type: str = "stock") -> dict[tuple[str, str], float]:
    """未投影分钟读取要乘上的 close/raw_close。已投影或拿不到原始收盘时为空。"""
    if minute_reads_are_adjusted(repo_data_dir(repo)):
        return {}
    parsed = _parse_dates(dates)
    symbol_list = [str(symbol) for symbol in symbols if symbol]
    if not symbol_list or not parsed:
        return {}
    frame = _load_daily_factor_frame(repo, symbol_list, min(parsed), max(parsed), asset_type)
    if frame is None or frame.is_empty() or "close" not in frame.columns or "raw_close" not in frame.columns:
        return {}
    rows = []
    for row in frame.iter_rows(named=True):
        symbol = row.get("symbol")
        if symbol is None:
            continue
        rows.append((symbol, row.get("date"), row.get("close"), row.get("raw_close")))
    return factors_from_rows(rows)
