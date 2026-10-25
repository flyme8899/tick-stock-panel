"""离线特征派生：同尺帧 → `build_feature_frame` → 路径 A 边沿 + 路径 B 进带标记。

**仅供研究，不构成投资建议；执行须人工确认。**

## 为什么直接调 `build_feature_frame`，而不是在这里再写一遍公式

那个函数是四条消费路径（监控评估 / 分钟策略执行 / 分钟回测 / 回放验证）的**单一
口径源**。离线回测若自己实现一份 VWAP / 放量比，即便"看起来一样"，也会在某次上游
改动后悄悄分叉，而分叉的表现是"回测结论与实盘不一致"——这种问题排查起来要命。

所以本模块的工作只有三件：

1. **进之前**确认同尺（`loader` 做），**洗完**再交给 `build_feature_frame`
2. 路径 A：调 `intraday_signals` 的同一套边沿机制产出 `signal_intraday_avg_cross_up/down`
3. 路径 B：调 `gates.build_band_columns` 产出首次进带标记

## `sanitize_minute_amount` 的位置是刻意的

`build_feature_frame` 内部会调 `sanitize_minute_amount`（L108）。那个函数在**混尺**
时会把"对不上收盘价"的额改成 `close × volume × 100` —— 于是脏数据被"修好"了，
ratio 变回 1，问题消失得无影无踪。

所以本模块**要求调用方先对齐**（`LoadedMinutes` 保证这点），并在进入前再断言一次
口径。断言失败直接抛，不"帮它洗一洗"。

## 可复现性

同一帧 + 同一 ε + 同一 prev_close → 同一特征表。本模块不做任何随机、不做采样、
不依赖系统时间。浮点比较的容差写在 `docs/t-rounds-offline-hfeat.md`（1e-9 相对）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import polars as pl

from ...strategy import custom_signals
from ...strategy.intraday_features import INTRADAY_FEATURES, build_feature_frame
from ...strategy.intraday_signals import _legacy_builtin_definitions
from .. import constants as C
from . import gates
from .loader import LoadedMinutes, assert_no_market_open_impersonation, assess_caliber

# 路径 A 的两列（与 `intraday_signals.INTRADAY_SIGNAL_LABELS` 同名，列名写死）
PATH_A_COLUMNS: tuple[str, ...] = (
    "signal_intraday_avg_cross_up",
    "signal_intraday_avg_cross_down",
)

_META_COLUMNS: tuple[str, ...] = ("symbol", "date", "datetime")


@dataclass(frozen=True, slots=True)
class OfflineFeatureResult:
    """特征派生结果。`frame` 即特征表（未写盘）。"""

    frame: pl.DataFrame
    dates: tuple[Any, ...]
    bars: int
    path_a_hits: dict[str, int]
    """路径 A 各列命中数（按 `symbol/date` 去重前的逐 bar 计数）。"""
    path_b_hits: dict[str, int]
    """路径 B 各列命中数。"""

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(self.frame.columns)


def _assert_caliber(loaded: LoadedMinutes) -> None:
    """进特征前最后一次确认口径。

    `LoadedMinutes` 本应已对齐，但它是可以被手工拼出来的（测试、脚本）。在这里
    再判一次的成本是一次分组聚合，换来的是"永远不会静默拿混尺数据算特征"。
    """
    report = assess_caliber(loaded.frame)
    if not report.all_single:
        raise ValueError(
            f"进入特征前仍有 {len(report.mixed_days)} 个混尺 (symbol,交易日); "
            "请先用 loader.align_minute_frame 对齐(见 VWAP_SAME_BASIS_NOTE)"
        )


def _path_a(frame: pl.DataFrame) -> pl.DataFrame:
    """路径 A：价格 vs VWAP 的穿越边沿。

    定义**直接取自** `intraday_signals._legacy_builtin_definitions()` —— 不在这里
    重写 `cross_up` 语义。取其中 `avg_cross` 两项，编译与求值都走
    `custom_signals` 的同一套函数（与监控/实盘/回测/回放同口径）。
    """
    defs = [d for d in _legacy_builtin_definitions() if d["id"] in PATH_A_COLUMNS]
    if len(defs) != len(PATH_A_COLUMNS):
        raise RuntimeError(
            "intraday_signals 的内置 avg_cross 定义变了; 离线口径必须与之保持一致, "
            "请同步更新 features.PATH_A_COLUMNS"
        )
    exprs = custom_signals.build_intraday_expressions(defs)
    # 内置信号在监控侧会保留历史列名(不加 csgi_ 前缀), 这里同样去掉前缀,
    # 让离线特征表的列名与监控规则里的字段名一致 —— 报告可以直接互相引用。
    renamed = {custom_signals.intraday_column_name(d["id"]): d["id"] for d in defs}
    exprs = {renamed.get(k, k): v for k, v in exprs.items()}
    missing = [c for c in PATH_A_COLUMNS if c not in exprs]
    if missing:
        raise RuntimeError(f"路径 A 表达式编译失败: {missing}")
    return custom_signals.apply_intraday_edges(frame, exprs)


def build_offline_feature_frame(
    loaded: LoadedMinutes,
    *,
    prev_close: Mapping[str, float] | None = None,
    with_sensitivity: bool = False,
    epsilon_pct: float | None = None,
) -> OfflineFeatureResult:
    """同尺帧 → 特征表（含路径 A / B 标记）。

    参数:
      loaded: `loader.load_minute_export` 的产物（已对齐）。
      prev_close: symbol → 昨收，**必须与分钟价同尺**（前复权就一起前复权）。
                  缺标的 → `pct_vs_prev_close` 为 null，不填 0。
      with_sensitivity: 是否附敏感性进带列（`_sens_` 前缀，不进主判定）。
      epsilon_pct: 主钉 ε 覆盖值；`None` = `constants.EPSILON_VALUE_PCT`。
                   **不允许**用它来"试"其他档位 —— 敏感性要走 `with_sensitivity`。

    返回 `OfflineFeatureResult`；`frame` 至少含
    `symbol, date, datetime` + `INTRADAY_FEATURES` + 路径 A 两列 + 路径 B 两列。
    """
    if loaded.frame is None or loaded.frame.is_empty():
        empty = pl.DataFrame(
            schema={"symbol": pl.Utf8, "date": pl.Date, "datetime": pl.Datetime}
            | {name: pl.Float64 for name in INTRADAY_FEATURES}
            | {name: pl.Boolean for name in (*PATH_A_COLUMNS, "gate_enter_band_up", "gate_enter_band_down")}
        )
        return OfflineFeatureResult(
            frame=empty, dates=(), bars=0, path_a_hits={}, path_b_hits={}
        )

    _assert_caliber(loaded)
    assert_no_market_open_impersonation(loaded.frame)

    frame = build_feature_frame(loaded.frame, prev_close=dict(prev_close) if prev_close else None)
    if frame.is_empty():
        raise ValueError("build_feature_frame 返回空帧; 输入可能缺 open/high/low/close 之一")

    frame = _path_a(frame)
    frame = gates.build_band_columns(
        frame, epsilon_pct=epsilon_pct, with_sensitivity=with_sensitivity
    )

    ordered = [*_META_COLUMNS, *INTRADAY_FEATURES.keys(), *PATH_A_COLUMNS, "gate_enter_band_up", "gate_enter_band_down"]
    if with_sensitivity:
        ordered.extend(gates.sensitivity_columns())
    frame = frame.select([c for c in ordered if c in frame.columns])

    return OfflineFeatureResult(
        frame=frame,
        dates=tuple(sorted(set(frame["date"].to_list()))),
        bars=frame.height,
        path_a_hits={c: int(frame[c].sum() or 0) for c in PATH_A_COLUMNS if c in frame.columns},
        path_b_hits={
            c: int(frame[c].sum() or 0)
            for c in ("gate_enter_band_up", "gate_enter_band_down")
            if c in frame.columns
        },
    )


def assert_version_matches_pin() -> str:
    """报告 `version` 字段的唯一来源。返回 `design_pin_v1+eps_pct:0.30`。

    写成函数而不是让调用方各拼一遍，是为了让"报告里写的版本"与"常量里钉的版本"
    不可能不一致 —— 那种不一致会让整份报告的口径无从追溯。
    """
    return C.design_version()


__all__ = [
    "PATH_A_COLUMNS",
    "OfflineFeatureResult",
    "assert_version_matches_pin",
    "build_offline_feature_frame",
]
