"""成交量参与率与一字板判定。

成交量单位沿用全库口径: 手 (1 手 = 100 股)。参与率默认关闭。关闭只表示不按
成交量裁剪股数; 一字板放行、除权参考价和收盘跌停卖出仍会改变默认定价结果。

用哪一根 K 线的成交量由调用方决定 (见 ``uses_prior_bar_volume``):
- ``close_t`` 且没有盘中覆盖价: 收盘才成交, 用当日成交量。
- 开盘 (``open_t+1``)、下一分钟、止损/止盈覆盖价: 全日成交量尚未发生,
  用上一交易日成交量。没有上一根、或上一根不是同一标的时 fail-closed。
集合竞价通常只占全天成交量的一小部分, 开盘参与率建议从 5%–10% 起。

未成交余量 (仅 ``volume_limit`` 开启时):
- 买入: 当根能买多少买多少, 不足 1 手则整笔跳过。余量丢弃, 不跨日挂单,
  也不会在当天改派给其他候选 (省下的现金留到之后的交易日)。
- 卖出 (仓位模式): 当根按参与率部分成交, 剩余仓位保留并记待卖, 下一交易日
  继续。多次部分成交在仓位归零时合并成一条往返, 不按切片各记一笔。
  期末最后一根也不绕过参与率, 卖不完的仓位留在权益市值里, 不记完成交易。
- 卖出 (全量独立样本): 样本固定 1 手, 不拆成多笔。当日可参与量不足 1 手则
  整笔顺延; 期末 ``force`` 平仓仍绕过参与率, 保证样本落账。
- 整手按 100 股取整。科创板/北交所 200 股最小申报单位未单独建模。
"""
from __future__ import annotations

import math

import numpy as np

SHARES_PER_LOT = 100.0
# 成交量不足 1 手视为近似无量: 买不进也卖不出一整手。
NEAR_ZERO_LOTS = 1.0
FLAT_REL_TOLERANCE = 1e-4
FLAT_ABS_TOLERANCE = 0.01  # 1 分, A 股最小报价


def normalize_volume_limit(value: float | None) -> float | None:
    """``None`` 或 ``0`` 关闭。合法开区间为 ``(0, 1]`` (占参考成交量的比例)。"""
    if value is None:
        return None
    try:
        limit = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("volume_limit 必须是 0 到 1 之间的小数") from exc
    if not math.isfinite(limit) or limit < 0 or limit > 1:
        raise ValueError("volume_limit 必须在 [0, 1] 内, 0 表示关闭")
    if limit == 0:
        return None
    return limit


def prices_flat(prices: tuple[float, ...] | list[float]) -> bool:
    """四价 (或高低收) 价差不超过 1 分 / 相对 1e-4, 视为全天同一价格。"""
    values = []
    for price in prices:
        try:
            number = float(price)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(number) or number <= 0:
            return False
        values.append(number)
    if len(values) < 2:
        return False
    close = values[-1]
    return max(values) - min(values) <= max(abs(close) * FLAT_REL_TOLERANCE, FLAT_ABS_TOLERANCE)


def volume_near_zero(volume: float) -> bool:
    """成交量缺失、非有限或不足 1 手。"""
    try:
        lots = float(volume)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(lots):
        return True
    return lots < NEAR_ZERO_LOTS


def is_one_price_limit(*, locked: bool, flat: bool, near_zero_volume: bool) -> bool:
    """收盘封在涨跌停, 且全天同一价格或近似无量 → 该方向全天不可成交。

    尾盘封板但盘中有价差、且成交量不少于 1 手, 不是一字板: 开盘口径仍可成交,
    收盘口径由调用方另行拦截。
    """
    return bool(locked) and (flat or near_zero_volume)


def is_volume_halt(*, flat: bool, volume: float, one_price_up: bool, one_price_down: bool) -> bool:
    """零成交且四价合一是停牌; 一字板除外, 以便只封锁一个方向。"""
    try:
        lots = float(volume)
    except (TypeError, ValueError):
        lots = math.nan
    if not (math.isfinite(lots) and lots <= 0 and flat):
        return False
    return not (one_price_up or one_price_down)


def uses_prior_bar_volume(fill_mode: str | None, price_override: float | None) -> bool:
    """开盘、下一分钟和盘中覆盖价不能用当日全天成交量。

    仅 ``close_t`` 且 ``price_override is None`` 时成交发生在收盘, 返回 False。
    """
    return not (price_override is None and fill_mode == "close_t")


def participation_fill(
    requested_shares: float,
    volume_lots: float,
    volume_limit: float | None,
) -> tuple[str, float]:
    """按当根成交量裁剪股数。

    返回 ``(status, shares)``:
    - ``off``: 未启用, ``shares`` 等于请求量
    - ``full``: 请求量未超过参与率上限
    - ``partial``: 裁到上限 (整手)
    - ``none``: 不足 1 手, 或成交量缺失/为负 (开启后 fail-closed, 不假装无限流动性)
    """
    try:
        requested = float(requested_shares)
    except (TypeError, ValueError):
        return "none", 0.0
    if not math.isfinite(requested) or requested <= 0:
        return "none", 0.0
    if volume_limit is None:
        return "off", requested
    try:
        lots = float(volume_lots)
    except (TypeError, ValueError):
        return "none", 0.0
    if not math.isfinite(lots) or lots < 0:
        return "none", 0.0
    max_lots = math.floor(lots * float(volume_limit) + 1e-9)
    max_shares = max_lots * SHARES_PER_LOT
    if max_shares + 1e-9 >= requested:
        return "full", requested
    if requested < SHARES_PER_LOT:
        if max_lots >= 1:
            return "full", requested
        return "none", 0.0
    if max_shares < SHARES_PER_LOT:
        return "none", 0.0
    return "partial", max_shares


# 按时间分块, 避免在全市场 time×asset 上同时摊开 6 块临时数组。
RELEASE_CHUNK_ROWS = 256


def release_one_price_boards(
    tradable: np.ndarray,
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    limit_up_locked: np.ndarray,
    limit_down_locked: np.ndarray,
    *,
    chunk_rows: int = RELEASE_CHUNK_ROWS,
) -> None:
    """零成交一字板恢复为可交易, 由撮合按方向拦截。

    停牌 (零成交、四价合一、但并非涨跌停) 保持不可交易。前复权四价乘同一因子,
    价差是否为 0 与原始价一致; 涨跌停标记本身在原始价上计算。按时间行分块,
    临时数组只覆盖一个块。
    """
    if chunk_rows < 1:
        raise ValueError("chunk_rows 必须 >= 1")
    row_count = tradable.shape[0]
    for start in range(0, row_count, chunk_rows):
        end = min(start + chunk_rows, row_count)
        _release_one_price_rows(
            tradable[start:end],
            open_[start:end],
            high[start:end],
            low[start:end],
            close[start:end],
            volume[start:end],
            limit_up_locked[start:end],
            limit_down_locked[start:end],
        )


def _release_one_price_rows(
    tradable: np.ndarray,
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    limit_up_locked: np.ndarray,
    limit_down_locked: np.ndarray,
) -> None:
    finite = (
        np.isfinite(open_)
        & np.isfinite(high)
        & np.isfinite(low)
        & np.isfinite(close)
        & (close > 0)
    )
    max_price = np.maximum(np.maximum(open_, high), np.maximum(low, close))
    min_price = np.minimum(np.minimum(open_, high), np.minimum(low, close))
    tolerance = np.maximum(np.abs(close) * np.float32(FLAT_REL_TOLERANCE), np.float32(FLAT_ABS_TOLERANCE))
    flat = (max_price - min_price) <= tolerance
    near_zero = ~np.isfinite(volume) | (volume < np.float32(NEAR_ZERO_LOTS))
    locked = limit_up_locked.astype(bool) | limit_down_locked.astype(bool)
    mask = finite & locked & (flat | near_zero)
    if np.any(mask):
        tradable[mask] = np.uint8(1)
