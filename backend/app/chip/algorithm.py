"""东财筹码分布 CYQCalculator 的 numpy 移植。

口径对齐 akshare ``stock_cyq_em`` 内嵌的网页 JS：``this.range`` 未设置，
因此用调用方传入的全部 K 线（日任务取最近 210 根前复权日 K）。价格分成 150 档，
精度不低于 0.01。每一天先把存量筹码乘 ``(1 - 换手率)``，再把当日换手按三角形
铺到 ``[low, high]``，顶点在 ``(open+close+high+low)/4``。一字板单独堆一档。

获利比例、平均成本和 90/70 成本是估算，不是交易所持仓成本。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SOURCE_LABEL = "本地计算（东财算法）"
PRICE_BINS = 150
LOOKBACK_BARS = 210
_PRECISION = 12


@dataclass
class ChipResult:
    computable: bool
    close: float | None = None
    benefit_ratio: float | None = None
    avg_cost: float | None = None
    avg_cost_bias: float | None = None
    cost_90_low: float | None = None
    cost_90_high: float | None = None
    concentration_90: float | None = None
    cost_70_low: float | None = None
    cost_70_high: float | None = None
    concentration_70: float | None = None
    accuracy: float | None = None
    min_price: float | None = None
    bins: list[dict] = field(default_factory=list)
    trapped_zones: list[dict] = field(default_factory=list)


def _js_to_precision(value: float, digits: int = _PRECISION) -> float:
    """JS ``Number.toPrecision`` 再转回数字。读筹码时东财用 12 位有效数字。"""
    if value == 0.0 or not math.isfinite(value):
        return 0.0
    return float(format(value, f".{digits}g"))


def _js_to_fixed(value: float, digits: int = 2) -> float:
    """JS ``toFixed``：先落到 IEEE 双精度，再四舍五入（0.5 远离 0）。"""
    if not math.isfinite(value):
        return 0.0
    scale = 10 ** digits
    scaled = value * scale
    rounded = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
    return rounded / scale


def _as_float_array(values) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1:
        raise ValueError("chip inputs must be 1-d")
    return array


def compute_window(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    turnover_pct: np.ndarray,
    *,
    with_bins: bool = False,
) -> ChipResult:
    """对已经按日期排好、且最后一根就是目标日的窗口计算筹码。

    ``turnover_pct`` 用 enriched 的百分数口径（5 表示 5%）。缺失、非有限和
    负数按 0 处理：当天不衰减、也不铺新筹码。窗口里从未出现正换手时不可算，
    返回空指标，不用 0 冒充获利比例。
    """
    open_ = _as_float_array(open_)
    high = _as_float_array(high)
    low = _as_float_array(low)
    close = _as_float_array(close)
    turnover_pct = _as_float_array(turnover_pct)
    n = close.shape[0]
    if not (open_.shape[0] == high.shape[0] == low.shape[0] == turnover_pct.shape[0] == n):
        raise ValueError("chip inputs must have the same length")
    if n == 0 or not np.isfinite(close[-1]) or close[-1] <= 0:
        return ChipResult(computable=False)

    valid = (
        np.isfinite(open_)
        & np.isfinite(high)
        & np.isfinite(low)
        & np.isfinite(close)
        & (high >= low)
        & (low > 0)
    )
    if not valid[-1] or not valid.any():
        return ChipResult(computable=False, close=float(close[-1]))

    # 无效价不参与高低点，否则一档脏数据会把 150 档拉满。
    use_high = high[valid]
    use_low = low[valid]
    max_price = float(use_high.max())
    min_price = float(use_low.min())
    if not (math.isfinite(max_price) and math.isfinite(min_price)) or max_price < min_price:
        return ChipResult(computable=False, close=float(close[-1]))

    accuracy = max(0.01, (max_price - min_price) / (PRICE_BINS - 1))
    chips = np.zeros(PRICE_BINS, dtype=np.float64)
    bin_price = min_price + accuracy * np.arange(PRICE_BINS, dtype=np.float64)
    turnover = np.zeros(n, dtype=np.float64)
    finite_turn = np.isfinite(turnover_pct) & (turnover_pct > 0)
    turnover[finite_turn] = np.minimum(turnover_pct[finite_turn] / 100.0, 1.0)
    if not finite_turn.any():
        return ChipResult(computable=False, close=float(close[-1]), accuracy=accuracy, min_price=min_price)

    for i in range(n):
        if not valid[i]:
            continue
        rate = float(turnover[i])
        if rate:
            chips *= 1.0 - rate
        hi = float(high[i])
        lo = float(low[i])
        avg = (float(open_[i]) + float(close[i]) + hi + lo) / 4.0
        if hi == lo:
            if rate:
                idx = math.floor((avg - min_price) / accuracy)
                if 0 <= idx < PRICE_BINS:
                    chips[idx] += (PRICE_BINS - 1) * rate / 2.0
            continue
        if not rate:
            continue
        upper = math.floor((hi - min_price) / accuracy)
        lower = math.ceil((lo - min_price) / accuracy)
        left = max(lower, 0)
        right = min(upper, PRICE_BINS - 1)
        if left > right:
            continue
        height = 2.0 / (hi - lo)
        current = bin_price[left:right + 1]
        weight = np.empty(right - left + 1, dtype=np.float64)
        on_left = current <= avg
        if abs(avg - lo) < 1e-8:
            weight[on_left] = height * rate
        else:
            weight[on_left] = (current[on_left] - lo) / (avg - lo) * height * rate
        on_right = ~on_left
        if abs(hi - avg) < 1e-8:
            weight[on_right] = height * rate
        else:
            weight[on_right] = (hi - current[on_right]) / (hi - avg) * height * rate
        chips[left:right + 1] += weight

    rounded = np.array([_js_to_precision(float(item)) for item in chips], dtype=np.float64)
    total = float(rounded.sum())
    last_close = float(close[-1])
    if total == 0.0:
        return ChipResult(computable=False, close=last_close, accuracy=accuracy, min_price=min_price)

    benefit = _benefit(rounded, total, last_close, min_price, accuracy)
    raw_avg = _cost_at(rounded, total * 0.5, min_price, accuracy)
    avg_cost = _js_to_fixed(raw_avg, 2)
    low90, high90, conc90 = _percent_band(rounded, total, 0.9, min_price, accuracy)
    low70, high70, conc70 = _percent_band(rounded, total, 0.7, min_price, accuracy)
    bias = (last_close / avg_cost - 1.0) if avg_cost else None
    bins: list[dict] = []
    if with_bins:
        for index, amount in enumerate(rounded):
            if amount <= 0:
                continue
            price = float(bin_price[index])
            bins.append({
                "price": price,
                "ratio": float(amount / total),
                "profit": last_close >= price,
            })
    return ChipResult(
        computable=True,
        close=last_close,
        benefit_ratio=benefit,
        avg_cost=avg_cost,
        avg_cost_bias=bias,
        cost_90_low=low90,
        cost_90_high=high90,
        concentration_90=conc90,
        cost_70_low=low70,
        cost_70_high=high70,
        concentration_70=conc70,
        accuracy=accuracy,
        min_price=min_price,
        bins=bins,
        trapped_zones=trapped_zones(bins, last_close, accuracy=accuracy) if with_bins else [],
    )


def trapped_zones(bins: list[dict], close: float, *, accuracy: float | None = None, limit: int = 3) -> list[dict]:
    """现价上方、筹码比周围更密的连续价格带。最多 3 段，按筹码占比从高到低。"""
    above = [item for item in bins if float(item.get("price") or 0) > close and float(item.get("ratio") or 0) > 0]
    if not above:
        return []
    ratios = [float(item["ratio"]) for item in above]
    peak = max(ratios)
    cutoff = max(peak * 0.35, sum(ratios) / len(ratios))
    step = accuracy if accuracy and accuracy > 0 else _bin_step(above)
    groups: list[list[dict]] = []
    current: list[dict] = []
    previous: float | None = None
    for item in above:
        price = float(item["price"])
        dense = float(item["ratio"]) >= cutoff
        separated = previous is not None and step > 0 and price - previous > step * 1.5
        if current and (not dense or separated):
            groups.append(current)
            current = []
        if dense:
            current.append(item)
        previous = price
    if current:
        groups.append(current)
    ranked = sorted(groups, key=lambda group: -sum(float(item["ratio"]) for item in group))
    zones = []
    for group in ranked[:limit]:
        prices = [float(item["price"]) for item in group]
        zones.append({
            "low": round(min(prices), 2),
            "high": round(max(prices), 2),
            "share": float(sum(float(item["ratio"]) for item in group)),
        })
    return zones


def _bin_step(bins: list[dict]) -> float:
    if len(bins) < 2:
        return 0.01
    gaps = [float(bins[i + 1]["price"]) - float(bins[i]["price"]) for i in range(len(bins) - 1)]
    positive = [gap for gap in gaps if gap > 0]
    return min(positive) if positive else 0.01


def _benefit(chips: np.ndarray, total: float, price: float, min_price: float, accuracy: float) -> float:
    if total == 0:
        return 0.0
    below = 0.0
    for index, amount in enumerate(chips):
        if price >= min_price + index * accuracy:
            below += float(amount)
    return below / total


def _cost_at(chips: np.ndarray, target: float, min_price: float, accuracy: float) -> float:
    """堆叠筹码第一次严格超过 ``target`` 的那一档下沿。够不到时为 0，与 JS 相同。"""
    total = 0.0
    for index, amount in enumerate(chips):
        if total + float(amount) > target:
            return min_price + index * accuracy
        total += float(amount)
    return 0.0


def _percent_band(
    chips: np.ndarray,
    total: float,
    percent: float,
    min_price: float,
    accuracy: float,
) -> tuple[float, float, float]:
    low_q = (1.0 - percent) / 2.0
    high_q = (1.0 + percent) / 2.0
    raw_low = _cost_at(chips, total * low_q, min_price, accuracy)
    raw_high = _cost_at(chips, total * high_q, min_price, accuracy)
    concentration = 0.0 if raw_low + raw_high == 0 else (raw_high - raw_low) / (raw_low + raw_high)
    return _js_to_fixed(raw_low, 2), _js_to_fixed(raw_high, 2), concentration
