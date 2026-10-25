"""门禁路径 A ∥ B 与 ε 主钉 / 敏感性分列（批次 20261025hfeat）。

**仅供研究，不构成投资建议；执行须人工确认。**

## 两条路径，永不合并成一个「通过」

| 路径 | 定义 | 这一版干什么 |
|---|---|---|
| **A** | 分时价格与 VWAP 的**穿越**边沿（上穿/下穿） | 复用 `intraday_signals._legacy_builtin_definitions` 的同一套表达式机制，口径逐字节一致 |
| **B** | 价格**首次进入** VWAP ± ε 带（正 T 侧重 +ε 侧，倒 T 侧重 −ε 侧），分钟收盘确认 | 本包新写，仅此处 |

两条路径回答的问题不一样：A 问「价格刚从均价下方穿到上方了吗」，B 问「价格是否
已经离开均价到足够远、且还在继续往外」。把 A 或 B 合成一个「门禁通过」的布尔值，
就等于放弃了这个区分，而那正是研究要看的。

所以：**特征表里两列各写各的，摘要里两栏各统计各的，报告里各写各的**。
不存在 `gate_pass` 这种合并字段。

## ε 只有一个主角，其余都是配角

`EPSILON_PCT_MAIN` 直接读 `constants.EPSILON_VALUE_PCT`（0.30），不在本模块重写数值。
敏感性档位（0.20 / 0.40 / 0.50 / ATR×0.5）**只出现在 `_sens_` 前缀的列里**，
**不参与**任何主门禁判定，也不进摘要的主口径。

这条写死的原因：一旦敏感性档"顺手"进了主判定，报告的结论就不再对应
`design_pin_v1` 那个 ε，而 version 串还写着 0.30 —— 事后没人分得清。

## ε 的方向

- **正 T（先卖后买）**：卖出要卖在均价**上方**才有价差 → 关注 **+ε 侧**（上带）
- **倒 T（先买后卖）**：买入要买在均价**下方** → 关注 **−ε 侧**（下带）

所以 B 路径天然是两列：`gate_enter_band_up` / `gate_enter_band_down`。
汇总时正 T 读 up 列，倒 T 读 down 列，**不互相替代**。
"""
from __future__ import annotations

from typing import Final

import polars as pl

from .. import constants as C

GATE_PATH_A: Final = "A"
GATE_PATH_B: Final = "B"

EPSILON_PCT_MAIN: Final = C.EPSILON_VALUE_PCT
"""主钉 ε，单位 %。**直接引用常量**，不在这里写 `0.30` 字面量。"""

SENSITIVITY_EPSILON_PCT: Final = (0.20, 0.40, 0.50)
"""敏感性档位（单位 %）。**分列、不进主判定**。

放在这里的唯一理由是让"有哪些档位"只有一个定义处，好让报告和测试都引用它 ——
而不是各写一份然后慢慢漂。"""

SENSITIVITY_PREFIX: Final = "_sens_"
"""敏感性列前缀。

带下划线前缀是有意的：让它一眼看出「这不是主口径」，也让它排在主列后面。
`features` 的列选择逻辑会把 `_` 开头的列排除在 `INTRADAY_FEATURES` 之外。
"""

_BAND_COL_UP: Final = "gate_enter_band_up"
_BAND_COL_DOWN: Final = "gate_enter_band_down"
_EDGE_GROUP: Final = ["symbol", "date"]


def epsilon_fraction(pct: float | None = None) -> float:
    """把「百分比」的 ε 变成「小数」的带半宽。

    `EPSILON_VALUE_PCT = 0.30`（0.30%）→ `0.003`（0.3%）。

    这个换算只在这里做一次。散在各处 `/100` 的结果是：总有一处忘了除。
    """
    value = EPSILON_PCT_MAIN if pct is None else float(pct)
    if value <= 0:
        raise ValueError(f"ε 必须为正, 收到 {value}")
    return value / 100.0


def sensitivity_columns() -> tuple[str, ...]:
    """敏感性档位对应的列名。报告与测试都从这里取，不手抄。"""
    return tuple(
        f"{SENSITIVITY_PREFIX}gate_enter_band_{side}_{round(pct * 100)}bp"
        for pct in SENSITIVITY_EPSILON_PCT
        for side in ("up", "down")
    )


def build_band_columns(
    frame: pl.DataFrame,
    *,
    epsilon_pct: float | None = None,
    with_sensitivity: bool = False,
) -> pl.DataFrame:
    """在特征帧上追加两个进带标记列（+ 可选敏感性列）。

    前置条件：帧里已有 `vwap`、`price`、`symbol`、`date`、`datetime`。

    定义（写死）：
      - 上带 = `close >= vwap * (1 + ε)`，下带 = `close <= vwap * (1 - ε)`
      - **首次进入** = 条件当日第一次为真（前一根不满足或不存在）
      - `vwap` 为 null（当日累计量为 0）→ 该根两个标记都是 `False`，不当成 0 处理

    「首次进入」而不是「每次进入」：做 T 的第二腿是一个**一次性**动作，同一带内
    反复穿越不构成新的入场机会。用 `cum_sum` 计数保证当日只标一根。
    """
    if frame is None or frame.is_empty():
        return frame
    need = {"symbol", "datetime", "date", "price", "vwap"}
    if not need.issubset(frame.columns):
        raise ValueError(f"进带标记需要列 {sorted(need)}, 收到 {sorted(frame.columns)}")

    frac = epsilon_fraction(epsilon_pct)
    base = frame.with_row_index("_row")
    out = base
    for suffix, expr in (
        (
            _BAND_COL_UP,
            pl.col("vwap").is_not_null() & (pl.col("price") >= pl.col("vwap") * (1.0 + frac)),
        ),
        (
            _BAND_COL_DOWN,
            pl.col("vwap").is_not_null() & (pl.col("price") <= pl.col("vwap") * (1.0 - frac)),
        ),
    ):
        out = out.with_columns(expr.fill_null(False).alias("_hit"))
        # 首次进带 = 这一根命中 ∧ 之前（同一 symbol/date 内）一次都没命中。
        out = out.with_columns(
            (pl.col("_hit") & (pl.col("_hit").cum_sum().over(_EDGE_GROUP) == 1)).alias(suffix)
        )

    if with_sensitivity:
        for pct in SENSITIVITY_EPSILON_PCT:
            sub = epsilon_fraction(pct)
            tag = round(pct * 100)
            for side, expr in (
                ("up", pl.col("vwap").is_not_null() & (pl.col("price") >= pl.col("vwap") * (1.0 + sub))),
                ("down", pl.col("vwap").is_not_null() & (pl.col("price") <= pl.col("vwap") * (1.0 - sub))),
            ):
                out = out.with_columns(expr.fill_null(False).alias("_hit"))
                out = out.with_columns(
                    (pl.col("_hit") & (pl.col("_hit").cum_sum().over(_EDGE_GROUP) == 1)).alias(
                        f"{SENSITIVITY_PREFIX}gate_enter_band_{side}_{tag}bp"
                    )
                )

    return out.drop(["_hit", "_row"])


__all__ = [
    "EPSILON_PCT_MAIN",
    "GATE_PATH_A",
    "GATE_PATH_B",
    "SENSITIVITY_EPSILON_PCT",
    "SENSITIVITY_PREFIX",
    "build_band_columns",
    "epsilon_fraction",
    "sensitivity_columns",
]
