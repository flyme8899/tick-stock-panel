"""Alpha158 实验因子组。

公式来自 vnpy 的 Alpha158（MIT），vnpy 又移植自微软 Qlib 的 Alpha158（MIT）。
这里不依赖 vnpy 或 qlib，只把表达式写成 TSP 因子 DSL。

Copyright (c) vnpy contributors
Copyright (c) Microsoft Corporation

原始实现:
https://github.com/vnpy/vnpy/blob/master/vnpy/alpha/dataset/datasets/alpha_158.py
https://github.com/microsoft/qlib （Alpha158）

完整 MIT 许可文本见仓库根目录 ``THIRD_PARTY_NOTICES.md`` 的 Alpha158 一节。
使用说明见 ``docs/alpha158.md``。

不移植 vnpy 的标签 ``ts_delay(close, -3) / ts_delay(close, -1) - 1``。
负向 shift 用的是未来收盘价，TSP 的 DSL 也会直接拒绝。

方向不预填，单因子使用时以实测 IC 的符号为准。强因子经常是负 IC：
值越大，接下来的收益往往越低。正向打分会把信号用反。
IC 高也不等于扣费后能赚钱，下单前要看分层多空和换手。
本地日线分区不一定每天都是全市场，小截面日子会把日均 IC 带偏，见 docs/alpha158.md。

和 vnpy 算子对齐、但不改 TSP 原有算子的地方:
- 标准差用 ts_std0（总体标准差，ddof=0）。原 ts_std 仍是样本标准差。
- 分位 qtlu/qtld 用 ts_qlinear（线性插值）。原 ts_quantile 仍是 nearest。
- rank 用 ts_pctrank，对齐 percentileofscore(kind="rank")/100。原 ts_rank 不改。
- 回归、极值位置的时间轴只含当前和过去：x=0 是窗口里最旧的一根。
- 极值下标从 1 开始，1 表示最旧；并列取最先出现的那个。
- enriched 的 open/high/low/close 是前复权，amount 和 volume 是不复权。
  vwap_0 = (amount / (volume * 100)) / raw_close，分子分母都是不复权价格。
  成交量单位仍是手，且不做复权。vma / vstd / vsum* / corr / cord 在送转除权日
  会看到成交量跳变，这是现有日线口径，不是另做了一套复权。
- 1e-12 写成十进制字面量，因为 DSL 不支持科学计数法。
- 窗口未满时结果为空（min_samples=窗口长度）。vnpy 的部分滚动函数在未满窗口时仍出数；
  满窗之后的定义一致。
"""
from __future__ import annotations

import polars as pl

from app.factors.registry import FactorSpec, protect_builtin, register_factor

ALPHA158_GROUP = "Alpha158（实验）"
ALPHA158_TAG = "alpha158"
_EPS = "0.000000000001"
_WINDOWS = (5, 10, 20, 30, 60)
_VOLUME = frozenset({"close", "volume"})
_VWAP = frozenset({"raw_close", "volume", "amount"})
_STOCK = frozenset({"stock"})


def _spec(name: str, label: str, formula: str, deps: frozenset[str], warmup: int) -> FactorSpec:
    return FactorSpec(
        id=f"a158_{name}",
        label=label,
        group=ALPHA158_GROUP,
        formula_text=formula,
        kind="custom",
        dependencies=deps,
        warmup_bars=warmup,
        asset_types=_STOCK,
        stability="experimental",
        tags=(ALPHA158_TAG, "experimental"),
        scale_free=True,
    )


def _bar_specs() -> list[FactorSpec]:
    """K 线形态。ts_greater/ts_less 分别对应 DSL 的 max/min。"""
    rows = [
        ("kmid", "KMID 实体", "(close - open) / open", frozenset({"open", "close"}), 1),
        ("klen", "KLEN 振幅", "(high - low) / open", frozenset({"open", "high", "low"}), 1),
        (
            "kmid_2", "KMID2 实体比",
            f"(close - open) / (high - low + {_EPS})",
            frozenset({"open", "high", "low", "close"}), 1,
        ),
        (
            "kup", "KUP 上影",
            "(high - max(open, close)) / open",
            frozenset({"open", "high", "close"}), 1,
        ),
        (
            "kup_2", "KUP2 上影比",
            f"(high - max(open, close)) / (high - low + {_EPS})",
            frozenset({"open", "high", "low", "close"}), 1,
        ),
        (
            "klow", "KLOW 下影",
            "(min(open, close) - low) / open",
            frozenset({"open", "low", "close"}), 1,
        ),
        (
            "klow_2", "KLOW2 下影比",
            f"(min(open, close) - low) / (high - low + {_EPS})",
            frozenset({"open", "high", "low", "close"}), 1,
        ),
        (
            "ksft", "KSFT 重心",
            "(close * 2 - high - low) / open",
            frozenset({"open", "high", "low", "close"}), 1,
        ),
        (
            "ksft_2", "KSFT2 重心比",
            f"(close * 2 - high - low) / (high - low + {_EPS})",
            frozenset({"open", "high", "low", "close"}), 1,
        ),
        ("open_0", "OPEN0 开盘/收盘", "open / close", frozenset({"open", "close"}), 1),
        ("high_0", "HIGH0 最高/收盘", "high / close", frozenset({"high", "close"}), 1),
        ("low_0", "LOW0 最低/收盘", "low / close", frozenset({"low", "close"}), 1),
        (
            "vwap_0", "VWAP0 均价/不复权收盘",
            "(amount / (volume * 100)) / raw_close",
            _VWAP, 1,
        ),
    ]
    return [_spec(*row) for row in rows]


def _rolling_specs() -> list[FactorSpec]:
    specs: list[FactorSpec] = []
    for window in _WINDOWS:
        warm = window + 1
        specs.extend([
            _spec(f"roc_{window}", f"ROC{window} 滞后价/收盘", f"ts_delay(close, {window}) / close", frozenset({"close"}), warm),
            _spec(f"ma_{window}", f"MA{window} 均线/收盘", f"ts_mean(close, {window}) / close", frozenset({"close"}), warm),
            _spec(f"std_{window}", f"STD{window} 波动/收盘", f"ts_std0(close, {window}) / close", frozenset({"close"}), warm),
            _spec(f"beta_{window}", f"BETA{window} 斜率/收盘", f"ts_slope(close, {window}) / close", frozenset({"close"}), warm),
            _spec(f"rsqr_{window}", f"RSQR{window} 拟合优度", f"ts_rsquare(close, {window})", frozenset({"close"}), warm),
            _spec(f"resi_{window}", f"RESI{window} 残差/收盘", f"ts_resi(close, {window}) / close", frozenset({"close"}), warm),
            _spec(f"max_{window}", f"MAX{window} 最高/收盘", f"ts_max(high, {window}) / close", frozenset({"high", "close"}), warm),
            _spec(f"min_{window}", f"MIN{window} 最低/收盘", f"ts_min(low, {window}) / close", frozenset({"low", "close"}), warm),
            _spec(
                f"qtlu_{window}", f"QTLU{window} 高分位/收盘",
                f"ts_qlinear(close, {window}, 0.8) / close", frozenset({"close"}), warm,
            ),
            _spec(
                f"qtld_{window}", f"QTLD{window} 低分位/收盘",
                f"ts_qlinear(close, {window}, 0.2) / close", frozenset({"close"}), warm,
            ),
            _spec(f"rank_{window}", f"RANK{window} 收盘百分位", f"ts_pctrank(close, {window})", frozenset({"close"}), warm),
            _spec(
                f"rsv_{window}", f"RSV{window} 随机指标",
                f"(close - ts_min(low, {window})) / (ts_max(high, {window}) - ts_min(low, {window}) + {_EPS})",
                frozenset({"high", "low", "close"}), warm,
            ),
            _spec(f"imax_{window}", f"IMAX{window} 高点位置", f"ts_argmax(high, {window}) / {window}", frozenset({"high"}), warm),
            _spec(f"imin_{window}", f"IMIN{window} 低点位置", f"ts_argmin(low, {window}) / {window}", frozenset({"low"}), warm),
            _spec(
                f"imxd_{window}", f"IMXD{window} 高低点间隔",
                f"(ts_argmax(high, {window}) - ts_argmin(low, {window})) / {window}",
                frozenset({"high", "low"}), warm,
            ),
            _spec(
                f"corr_{window}", f"CORR{window} 价量相关",
                f"ts_corr(close, log(volume + 1), {window})",
                _VOLUME, warm,
            ),
            _spec(
                f"cord_{window}", f"CORD{window} 收益与量变相关",
                f"ts_corr(close / ts_delay(close, 1), log(volume / ts_delay(volume, 1) + 1), {window})",
                _VOLUME, warm,
            ),
            _spec(
                f"cntp_{window}", f"CNTP{window} 上涨占比",
                f"ts_mean(close > ts_delay(close, 1), {window})",
                frozenset({"close"}), warm,
            ),
            _spec(
                f"cntn_{window}", f"CNTN{window} 下跌占比",
                f"ts_mean(close < ts_delay(close, 1), {window})",
                frozenset({"close"}), warm,
            ),
            _spec(
                f"cntd_{window}", f"CNTD{window} 涨跌差",
                f"ts_mean(close > ts_delay(close, 1), {window}) - ts_mean(close < ts_delay(close, 1), {window})",
                frozenset({"close"}), warm,
            ),
            _spec(
                f"sump_{window}", f"SUMP{window} 上涨幅度占比",
                (
                    f"ts_sum(max(close - ts_delay(close, 1), 0), {window}) / "
                    f"(ts_sum(abs(close - ts_delay(close, 1)), {window}) + {_EPS})"
                ),
                frozenset({"close"}), warm,
            ),
            _spec(
                f"sumn_{window}", f"SUMN{window} 下跌幅度占比",
                (
                    f"ts_sum(max(ts_delay(close, 1) - close, 0), {window}) / "
                    f"(ts_sum(abs(close - ts_delay(close, 1)), {window}) + {_EPS})"
                ),
                frozenset({"close"}), warm,
            ),
            _spec(
                f"sumd_{window}", f"SUMD{window} 涨跌幅度差",
                (
                    f"(ts_sum(max(close - ts_delay(close, 1), 0), {window}) - "
                    f"ts_sum(max(ts_delay(close, 1) - close, 0), {window})) / "
                    f"(ts_sum(abs(close - ts_delay(close, 1)), {window}) + {_EPS})"
                ),
                frozenset({"close"}), warm,
            ),
            _spec(
                f"vma_{window}", f"VMA{window} 均量/当日量",
                f"ts_mean(volume, {window}) / (volume + {_EPS})",
                frozenset({"volume"}), warm,
            ),
            _spec(
                f"vstd_{window}", f"VSTD{window} 量波动/当日量",
                f"ts_std0(volume, {window}) / (volume + {_EPS})",
                frozenset({"volume"}), warm,
            ),
            _spec(
                f"wvma_{window}", f"WVMA{window} 量加权波动",
                (
                    f"ts_std0(abs(close / ts_delay(close, 1) - 1) * volume, {window}) / "
                    f"(ts_mean(abs(close / ts_delay(close, 1) - 1) * volume, {window}) + {_EPS})"
                ),
                _VOLUME, warm,
            ),
            _spec(
                f"vsump_{window}", f"VSUMP{window} 放量占比",
                (
                    f"ts_sum(max(volume - ts_delay(volume, 1), 0), {window}) / "
                    f"(ts_sum(abs(volume - ts_delay(volume, 1)), {window}) + {_EPS})"
                ),
                frozenset({"volume"}), warm,
            ),
            _spec(
                f"vsumn_{window}", f"VSUMN{window} 缩量占比",
                (
                    f"ts_sum(max(ts_delay(volume, 1) - volume, 0), {window}) / "
                    f"(ts_sum(abs(volume - ts_delay(volume, 1)), {window}) + {_EPS})"
                ),
                frozenset({"volume"}), warm,
            ),
            _spec(
                f"vsumd_{window}", f"VSUMD{window} 量能差",
                (
                    f"(ts_sum(max(volume - ts_delay(volume, 1), 0), {window}) - "
                    f"ts_sum(max(ts_delay(volume, 1) - volume, 0), {window})) / "
                    f"(ts_sum(abs(volume - ts_delay(volume, 1)), {window}) + {_EPS})"
                ),
                frozenset({"volume"}), warm,
            ),
        ])
    return specs


def alpha158_specs() -> tuple[FactorSpec, ...]:
    return tuple(_bar_specs() + _rolling_specs())


ALPHA158_SPECS: tuple[FactorSpec, ...] = alpha158_specs()
ALPHA158_IDS: tuple[str, ...] = tuple(spec.id for spec in ALPHA158_SPECS)


def register_alpha158() -> None:
    """注册实验组。重复导入时版本相同会被注册表拒绝，因此只注册尚未存在的 id。"""
    from app.factors.registry import get_factor

    for spec in ALPHA158_SPECS:
        protect_builtin(spec.id)
        if get_factor(spec.id) is None:
            register_factor(spec)


def compute_alpha158(frame: pl.DataFrame, *, chunk_symbols: int = 400) -> pl.DataFrame:
    """按股票分块计算整组，避免全市场同时展开 158 列的中间表达式。

    调用方需已按 symbol、date 排好，或至少每个分块内部可排序。
    """
    from app.strategy.scoring import materialize_scoring_columns

    if frame.is_empty() or "symbol" not in frame.columns:
        return frame
    symbols = frame.get_column("symbol").unique(maintain_order=True).to_list()
    names = list(ALPHA158_IDS)
    if chunk_symbols < 1:
        raise ValueError("chunk_symbols 必须 ≥ 1")
    if len(symbols) <= chunk_symbols:
        ordered = frame.sort(["symbol", "date"]) if "date" in frame.columns else frame
        return materialize_scoring_columns(ordered, names)
    parts: list[pl.DataFrame] = []
    for offset in range(0, len(symbols), chunk_symbols):
        batch = symbols[offset:offset + chunk_symbols]
        part = frame.filter(pl.col("symbol").is_in(batch))
        if "date" in part.columns:
            part = part.sort(["symbol", "date"])
        parts.append(materialize_scoring_columns(part, names))
    return pl.concat(parts, how="vertical")
