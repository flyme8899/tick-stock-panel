"""gtja191 标签映射（N2）：把**已有**的因子检验结果映射成一个处置标签。

本模块是**薄映射纯函数**，职责单一，不做三件事：

1. **不重算 IC。** 输入是 `FactorBatchItem`（回测已经算好的 `ic_mean` / `ir` /
   `t_newey_west` / `q_value` / `yearly_ic` 等），映射只读这些字段。重算会让同一个
   因子在不同调用点得出不同标签 —— 那比没有标签更糟。
2. **不写注册表。** 本模块不 import `unregister_factor`，也不碰任何全局状态。
   标签只是判断，"下线"是人的决定。
3. **不猜。** 字段为 `None` 时一律落到 `underpowered` / `error`，绝不用一个
   看起来合理的数字补上去。

## 门槛以 FIX 写死表为准

`FIX_TASKS_20261027ggtja191.md`（sha256 `7b5cac07…`）N2 节「标签映射表（自上而下
优先；第一命中即停）」是**写死**的，不是"任意自洽的另一张表"。本模块逐条照抄，
数字一个字不改：

| 优先 | 标签 | 可测条件（FIX 写死） |
|---|---|---|
| 1 | `blocked:data` | 公式声明 deps 含 tip enriched **不存在**的列，或第二批字段被误引入 |
| 2 | `blocked:ops` | 报告原语无法用现有 33 OPERATORS **忠实**表达，且无已标注「TSP 映射」的等价改写 |
| 3 | `error` | `FactorBatchItem.error` 非空（物化/回测异常） |
| 4 | `underpowered` | `n_ic_dates < 30`；**或** `ir` / `t_newey_west` / `q_value` 因样本不足为 `None` 且无法完成显著性判断 |
| 5 | `reversed` | `n_ic_dates >= 30` **且** 前后半窗 `ic_mean` **符号相反**，且两侧 `|ic_mean| >= 0.02` |
| 6 | `alive` | `n_ic_dates >= 30` **且** `|ir| >= 0.50` **且** `|t_newey_west| >= 1.96` **且** `q_value is not None and q_value <= 0.10` **且** 未命中 `reversed` |
| 7 | `dead` | `n_ic_dates >= 30` 且不满足 `alive` / `reversed` |

### 三条容易写错的边界

**`alive` 的显著性门是 IR / NW-t / q，不是 `|IC|`，也不是费后多空。**
`|IC| >= 0.02` 看着很像"显著性"，但它是**效应量**，不是显著性：IC 本身的标准误随
样本天数变化，单看 IC 大小会把 60 天的强 IC 和 600 天的强 IC 判成一样。FIX 选
`|IR| >= 0.50`（IC 均值/IC 标准差）作效应量门、`|NW-t| >= 1.96` 作显著性门、
`q <= 0.10` 作多重检验校正门，三者缺一不可。

**费后多空（N5）只进「下线候选」，不进 `alive`。** 一个因子扣费后不赚钱不等于它
没有排序能力（可能是换手太高）。把费后塞进 `alive` 定义，等于用成本口径置换显著性
口径，会让"高 IC 高换手"的因子被误判为 `dead`。

**`reversed` 判的是**前后半窗**符号相反，不是 IC 与多空反号。**
半窗反号说明因子**在时间上不稳定**（前半段有效、后半段反向），处置是查 regime 变化
或直接放弃。IC 与多空反号是另一回事（可能只是换手吃掉了收益），拿它冒充 `reversed`
会把"高换手"误诊成"方向错"。

## `degenerate`（FIX 枚举外的扩展，需旁注）

FIX 写死标签集合仅 7 个。本模块**额外**定义 `degenerate`，用于一种 FIX 表没有覆盖
的实测情形：因子值在截面上恒定（无方差）→ Rank IC 分母为 0 → 全部日期 IC 无定义 →
`ic_mean` 为 `None` 而 `n_ic_dates` 也为 0。

这不是 `error`（代码没挂、算子也支持），不是 `dead`（一次都没测过），也不是
`underpowered`（不是样本少，是样本全是同一个值）。归 `error` 会让人查一个不存在的
bug，归 `dead` 会误杀。

> **它不绕开 IR/NW/q 门。** `degenerate` 只在 `ic_mean is None and n_ic_dates == 0`
> 时命中，此时 IR/NW/q 本来就全是 `None`，按 FIX 表第 4 行也只会落到 `underpowered`。
> `degenerate` 是把这种情况说得更准，不是给某类因子开后门。
>
> 触发条件比 `underpowered` **更窄**（要求 `ic_mean` 也是 `None`），所以它只切走
> `underpowered` 里"一条有效 IC 都没有"的那一小撮。有 IC、只是天数不够或
> IR/NW/q 缺失的，仍然老老实实是 `underpowered`。

## 成本口径：为什么必须外部传入

`FactorBatchItem.long_short_return` **已经扣过费**，扣的是 `FactorConfig` 的默认
成本（`fees_pct=2e-4`×2 + `slippage_bps=5.0`×2 ≈ **14bp** 来回），不是任务书的
**18.2bp**。这不是 bug，是两套口径：

- 14bp 是 IC 表的默认配置，用来给出可比的相对排名；
- 18.2bp 是本批的成本锚，用于任何"扣费后还行不行"的判断。

所以本模块要求调用方显式传入 `cost_bp`，并在与 `GTJA191_COST_BP_ROUNDTRIP` 不一致时
抛 `ValueError`。传 14bp 进来的结果不是"差不多"，是**系统性偏乐观**：每天差 4.2bp。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from app.factors.gtja191 import GTJA191_COST_BP_ROUNDTRIP

if TYPE_CHECKING:
    from app.backtest.factor import FactorBatchItem

__all__ = [
    "DELIST_CANDIDATE_LABELS",
    "MAX_Q_VALUE",
    "MIN_ABS_IC",
    "MIN_ABS_IR",
    "MIN_ABS_NW_T",
    "MIN_IC_DATES",
    "MIN_NET_LONG_SHORT",
    "GtjaLabel",
    "HalfWindowFlip",
    "LabelVerdict",
    "half_window_of",
    "label_of",
]

GtjaLabel = Literal[
    "alive", "dead", "reversed", "underpowered", "degenerate", "error",
]

# ---------------------------------------------------------------- N2 阈值（FIX 写死）

MIN_IC_DATES: Final = 30
"""IC 有效交易日下限。低于此数**一律 `underpowered`，禁止返回 `alive`**。

FIX 写死 30。一个月 ≈ 21 个交易日，30 个 IC 日约等于 1.5 个月。再短的样本里 IC
的标准误大到无法区分 0 和 0.05。
"""

MIN_ABS_IR: Final = 0.50
"""`|ir|` 下限（FIX 写死）。`ir` 是 ICIR = IC 均值 / IC 标准差，是**效应量**门。"""

MIN_ABS_NW_T: Final = 1.96
"""`|t_newey_west|` 下限（FIX 写死）。NW HAC 稳健 t（lag=1），是**显著性**门。"""

MAX_Q_VALUE: Final = 0.10
"""`q_value` 上限（FIX 写死）。BH-FDR q 值，是**多重检验校正**门。"""

MIN_ABS_IC: Final = 0.02
"""半窗反号的幅度门（FIX 写死）。**只用于 `reversed` 判定**，不用于 `alive`。

FIX 表第 5 行原文：「两侧 `|ic_mean| >= 0.02`」。这是为了让"一侧接近 0 的符号翻转"
不算反号 —— 噪声的符号没有意义。
"""

MIN_NET_LONG_SHORT: Final = 0.0
"""费后多空累计收益下限（按 18.2bp 重扣之后）。**只用于「下线候选」，不用于 `alive`。**

FIX 把 N5 费后写在「下线候选」的 AND 条件里（N2 节末「下线候选提示」），不是
`alive` 的定义。
"""

DELIST_CANDIDATE_LABELS: Final[frozenset[str]] = frozenset({"dead", "reversed"})
"""可以作为「下线候选」的标签集合。**还要再过 N5 费后门才算候选。只提示，不注销。**"""


@dataclass(frozen=True, slots=True)
class HalfWindowFlip:
    """前后半窗的 IC 均值与符号翻转判定。`half_window_of` 的产物。"""

    first_mean: float
    second_mean: float
    first_days: int
    second_days: int

    @property
    def flipped(self) -> bool:
        """两半符号相反。任一侧在数值上是 0 时不算翻转（符号无意义）。"""
        return (self.first_mean > 0) != (self.second_mean > 0) and (
            self.first_mean != 0 and self.second_mean != 0
        )

    def significant_flip(self, floor: float = MIN_ABS_IC) -> bool:
        """FIX 表第 5 行的完整判据：符号相反 **且** 两侧 `|ic_mean| >= 0.02`。"""
        return (
            self.flipped
            and abs(self.first_mean) >= floor
            and abs(self.second_mean) >= floor
        )


def half_window_of(ic_series: list[dict] | None) -> HalfWindowFlip | None:
    """把 `ic_series` 按日期一分为二，各算 IC 均值。

    `ic_series` 是 `FactorResult.ic_series` 的形态：`[{"date": "2026-01-05", "ic": 0.03}, …]`，
    已按日期升序、已过滤掉 null 与非有限值。

    前半窗 = 前 `n // 2` 个交易日，后半窗 = 剩下的。`n < 4` 时返回 `None` —— 每半窗
    不到 2 个点，均值没有意义。

    这里只做**算术平均**，不重算 IC：输入就是算好的 IC 序列，不是因子值和收益。
    """
    if not ic_series or len(ic_series) < 4:
        return None
    values = [
        float(row["ic"]) for row in ic_series
        if row.get("ic") is not None
    ]
    if len(values) < 4:
        return None
    ordered = sorted(
        ((str(row.get("date", "")), float(row["ic"]))
         for row in ic_series if row.get("ic") is not None),
        key=lambda pair: pair[0],
    )
    values = [value for _, value in ordered]
    split = len(values) // 2
    first, second = values[:split], values[split:]
    if not first or not second:
        return None
    return HalfWindowFlip(
        first_mean=sum(first) / len(first),
        second_mean=sum(second) / len(second),
        first_days=len(first),
        second_days=len(second),
    )


def _f(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


@dataclass(frozen=True, slots=True)
class LabelVerdict:
    """映射结果。`reason` 是给人看的一句话，写清为什么得到这个标签。"""

    label: str
    reason: str
    cost_bp: float | None = None
    """本次判定用的成本口径。`blocked:*` / `error` 时为 None（没算，谈不上成本）。"""

    net_long_short_return: float | None = None
    """按 `cost_bp` 重扣后的多空累计收益。**只用于下线候选，不参与 `alive` 判定。**"""

    half_window: HalfWindowFlip | None = None

    @property
    def is_delist_candidate(self) -> bool:
        """FIX N2 节末「下线候选提示」的三条 AND 条件。

        1. 标签 ∈ `{dead, reversed}`；
        2. N5 费后路径下多空净收益未过门（`<= 0`，或拿不到费后值）；
        3. 仅输出报告字段 —— 由调用方保证，本模块不 import `unregister_factor`。

        `reversed` 属于"按当前方向做会亏"，`dead` 属于"费后不成立"，两者都进候选；
        `alive` 过门了不是候选；`underpowered` / `degenerate` / `error` / `blocked:*`
        是**缺证据**，不构成"该下线"的证据。
        """
        if self.label not in DELIST_CANDIDATE_LABELS:
            return False
        if self.net_long_short_return is None:
            return True  # 拿不到费后值，不能证明过门
        return self.net_long_short_return <= MIN_NET_LONG_SHORT


def label_of(
    item: FactorBatchItem,
    *,
    n_ic_dates: int,
    net_long_short_return: float | None,
    cost_bp: float,
    blocked_reason: str | None = None,
    ic_series: list[dict] | None = None,
) -> LabelVerdict:
    """把一次因子检验映射成一个处置标签。**纯函数，不重算 IC。**

    判定顺序严格照抄 FIX N2 写死表「自上而下优先；第一命中即停」。

    Parameters
    ----------
    item
        回测已经算好的结果。只读 `ic_mean` / `ir` / `t_newey_west` / `q_value` /
        `error`，不读也不改别的。
    n_ic_dates
        参与 IC 的有效交易日数（**不是** `FactorResult.n_dates`，那是面板交易日数）。
    net_long_short_return
        **按 `cost_bp` 重扣之后**的多空累计收益。只用于下线候选，不参与 `alive`。
    cost_bp
        本次判定用的来回成本（bp）。必须等于 `GTJA191_COST_BP_ROUNDTRIP`。
    blocked_reason
        `blocked:data` 或 `blocked:ops`。非空时**优先于一切**，包括 `item.error`。
    ic_series
        `FactorResult.ic_series`。给了才能判前后半窗反号；不给则 `reversed` 不可能命中。

    Raises
    ------
    ValueError
        `cost_bp` 不等于成本锚 18.2bp。这是**口径错误**，不是数据问题，
        所以抛错而不是返回一个偏乐观的标签。
    """
    if cost_bp != GTJA191_COST_BP_ROUNDTRIP:
        raise ValueError(
            f"成本口径错误: 传入 {cost_bp} bp, 但下线候选门必须用 "
            f"{GTJA191_COST_BP_ROUNDTRIP} bp。`FactorBatchItem.long_short_return` "
            "扣的是 14bp（FactorBatchConfig 默认值），直接拿它过门会系统性偏乐观。"
        )

    # ── 1/2. blocked: 根本没算, 谈不上好坏（FIX 表第 1、2 行） ──
    if blocked_reason:
        return LabelVerdict(
            label=f"blocked:{blocked_reason}",
            reason=f"不可表达（{blocked_reason}），本批不登记实现 / 拒算",
        )

    # ── 3. error: 物化或回测异常（FIX 表第 3 行） ──
    if item.error:
        return LabelVerdict(label="error", reason=f"计算失败: {item.error}")

    # ── 4. underpowered: 样本不够，或显著性判不了（FIX 表第 4 行） ──
    # 全部日期都没有有效 IC（因子值截面恒定）时，先归 `degenerate` —— 这是
    # FIX 枚举外的扩展，用于把"测量失败"和"样本不足"分开。它不绕开任何门：
    # 这种情况 IR/NW/q 本来就全是 None，按 FIX 表也只能落到 underpowered。
    if item.ic_mean is None and n_ic_dates == 0:
        return LabelVerdict(
            label="degenerate",
            reason="全部交易日的因子值在截面上是常数（无方差），Rank IC 无定义",
            cost_bp=cost_bp,
            net_long_short_return=net_long_short_return,
        )
    if n_ic_dates < MIN_IC_DATES:
        return LabelVerdict(
            label="underpowered",
            reason=f"IC 日数 {n_ic_dates} < {MIN_IC_DATES}，样本太薄不判生死",
            cost_bp=cost_bp,
            net_long_short_return=net_long_short_return,
        )
    # FIX 表第 4 行后半句：「ir / t_newey_west / q_value 因样本不足为 None
    # 且无法完成显著性判断」→ underpowered。
    missing = [
        name for name, value in (
            ("ir", item.ir),
            ("t_newey_west", item.t_newey_west),
            ("q_value", item.q_value),
        )
        if value is None
    ]
    if missing:
        return LabelVerdict(
            label="underpowered",
            reason=f"显著性字段缺失（{', '.join(missing)}），无法完成显著性判断",
            cost_bp=cost_bp,
            net_long_short_return=net_long_short_return,
        )

    # ── 5. reversed: 前后半窗 IC 均值符号相反，且两侧都够大（FIX 表第 5 行） ──
    half = half_window_of(ic_series)
    if half is not None and half.significant_flip():
        return LabelVerdict(
            label="reversed",
            reason=(
                f"前后半窗 IC 均值符号相反"
                f"（前 {_f(half.first_mean)} / 后 {_f(half.second_mean)}，"
                f"各 {half.first_days}/{half.second_days} 日），因子在时间上不稳定"
            ),
            cost_bp=cost_bp,
            net_long_short_return=net_long_short_return,
            half_window=half,
        )

    # ── 6. alive: IR + NW-t + q 三门全过（FIX 表第 6 行） ──
    ir = item.ir
    nw_t = item.t_newey_west
    q_value = item.q_value
    assert ir is not None and nw_t is not None and q_value is not None  # 上面已排除
    if abs(ir) >= MIN_ABS_IR and abs(nw_t) >= MIN_ABS_NW_T and q_value <= MAX_Q_VALUE:
        return LabelVerdict(
            label="alive",
            reason=(
                f"|IR| {abs(ir):.4f} ≥ {MIN_ABS_IR}，"
                f"|NW-t| {abs(nw_t):.4f} ≥ {MIN_ABS_NW_T}，"
                f"q {q_value:.4f} ≤ {MAX_Q_VALUE}"
            ),
            cost_bp=cost_bp,
            net_long_short_return=net_long_short_return,
            half_window=half,
        )

    # ── 7. dead: 有样本但不满足 alive / reversed（FIX 表第 7 行） ──
    shortfalls = []
    if abs(ir) < MIN_ABS_IR:
        shortfalls.append(f"|IR| {abs(ir):.4f} < {MIN_ABS_IR}")
    if abs(nw_t) < MIN_ABS_NW_T:
        shortfalls.append(f"|NW-t| {abs(nw_t):.4f} < {MIN_ABS_NW_T}")
    if q_value > MAX_Q_VALUE:
        shortfalls.append(f"q {q_value:.4f} > {MAX_Q_VALUE}")
    return LabelVerdict(
        label="dead",
        reason="未过显著性门: " + "；".join(shortfalls),
        cost_bp=cost_bp,
        net_long_short_return=net_long_short_return,
        half_window=half,
    )
