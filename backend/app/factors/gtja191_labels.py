"""gtja191 标签映射（N2）：把**已有**的因子检验结果映射成一个处置标签。

本模块是**薄映射纯函数**，职责单一，不做三件事：

1. **不重算 IC。** 输入是 `FactorBatchItem`（回测已经算好的 `ic_mean` / `ir` /
   `n_dates` / `error` 等），映射只读这些字段。重算会让同一个因子在不同调用点
   得出不同标签 —— 那比没有标签更糟。
2. **不写注册表。** 本模块不 import `unregister_factor`，也不碰任何全局状态。
   标签只是判断，"下线"是人的决定。
3. **不猜。** 字段为 `None` 时一律落到 `error` 或 `underpowered`，绝不用一个
   看起来合理的数字补上去。

## 标签

| 标签 | 含义 | 触发 |
|---|---|---|
| `blocked:<reason>` | 算子缺失 / 算子冲突，根本没算 | `blocked_reason` 非空，优先级最高 |
| `error` | 算了但抛错，或结果自相矛盾 | `item.error` 非空 / `ic_mean` 是 `None` 而 IC 日数 > 0 |
| `degenerate` | 因子值截面上是常数，IC 无定义 | `ic_mean` 是 `None` 且 `n_ic_dates == 0` |
| `underpowered` | IC 日数不够，数值是噪声 | `n_ic_dates < MIN_IC_DATES`（30） |
| `reversed` | 方向反了：IC 与费后多空收益符号相反 | 见下 |
| `dead` | 有样本、方向也对，但费后不过门 | 见下 |
| `alive` | 有样本、方向对、费后过门 | 见下 |

**优先级顺序** `blocked` > `error` > `degenerate` > `underpowered` > `reversed` >
`dead` / `alive`。`underpowered` 压在 `reversed` / `dead` / `alive` 之前是有意的：
29 个 IC 日算出来的 IC 再显著也只是噪声，此时判 `alive` 是把噪声当信号，判 `dead`
是把噪声当结论，两种都错。**唯一正确的处置是承认样本不够。**

### `degenerate` 为什么单独一类

因子值在某天截面上全相等（或整个区间恒定）时，Rank IC 的分母是 0，**IC 无定义**，
`ic_mean` 会是 `None`、`n_dates` 为 0。这种情况：

- 不是 `error` —— 代码没出错，算子也支持；
- 不是 `dead` —— 因子一次都没被真正测过，没有证据说它没用；
- 不是 `underpowered` —— 不是"样本少"，是"样本全是同一个值"。

归成 `error` 会让人去查一个不存在的 bug；归成 `dead` 会误杀。单独一类最诚实：
**这是一种测量失败，需要修因子（或换数据），不是要退役。**


## 为什么 `reversed` 和 `dead` 必须分开

`dead` 是"因子没用"，`reversed` 是"因子有用但用反了"。前者该退役，后者该改方向
再验 —— 处置完全不同。判据是**符号相反**：IC 显著为负（或多空为负）而另一侧为正。

本模块判 `reversed` 用 `ic_mean` 与费后多空收益是否反向。`ic_mean` 与多空收益在
正常因子上同向（IC 高 → 高分组跑赢 → 多空为正），反向说明排序方向与收益方向脱节，
此时按 IC 方向建仓会稳定亏在成本上。

## 成本口径：为什么必须外部传入

`FactorBatchItem.long_short_return` **已经扣过费**，扣的是 `FactorConfig` 的默认
成本（`fees_pct=1e-4`×2 + `slippage_bps=5.0`×2 ≈ **14bp** 来回），不是任务书的
**18.2bp**。这不是 bug，是两套口径：

- 14bp 是 IC 表的默认配置，用来给出可比的相对排名；
- 18.2bp 是本批的成本锚，用于任何"扣费后还行不行"的判断。

所以本模块**不接受**一个已扣费的多空收益当成本门输入，而是要求调用方显式传入
`net_long_short_return` 和 `cost_bp`，并在 `cost_bp` 与 `GTJA191_COST_BP_ROUNDTRIP`
不一致时抛 `ValueError`。传 14bp 进来的结果不是"差不多"，是**系统性偏乐观**：
每天差 4.2bp。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from app.factors.gtja191 import GTJA191_COST_BP_ROUNDTRIP

if TYPE_CHECKING:
    from app.backtest.factor import FactorBatchItem

__all__ = [
    "DELIST_CANDIDATE_LABELS",
    "MIN_ABS_IC",
    "MIN_IC_DATES",
    "MIN_NET_LONG_SHORT",
    "GtjaLabel",
    "LabelVerdict",
    "label_of",
]

GtjaLabel = Literal["alive", "dead", "reversed", "underpowered", "degenerate", "error"]
# ---------------------------------------------------------------- N2 阈值

MIN_IC_DATES: Final = 30
"""IC 有效交易日下限。**低于此数一律 `underpowered`，不得判 `alive`。**

任务书写死 30。一个月 ≈ 21 个交易日，30 个 IC 日约等于 1.5 个月。再短的样本
里 IC 的标准误大到无法区分 0 和 0.05。
"""

MIN_ABS_IC: Final = 0.02
"""|IC| 下限。低于此视为"排序能力不足"，即使费后碰巧为正也判 `dead`。

0.02 是 A 股日频价量因子的常用地板：|IC| < 0.02 的因子在扣 18.2bp 之后基本不可能
稳定留下正收益，能留下的是运气。
"""

MIN_NET_LONG_SHORT: Final = 0.0
"""费后多空累计收益下限（按 18.2bp 重扣之后）。**

必须是**净**值。用毛收益过门等于假装交易不要钱。
"""

DELIST_CANDIDATE_LABELS: Final[frozenset[str]] = frozenset({"dead", "reversed"})
"""可以直接作为「下线候选」提示的标签集合。**只用于提示，不触发任何注销。**"""


@dataclass(frozen=True, slots=True)
class LabelVerdict:
    """映射结果。`reason` 是给人看的一句话，写清为什么得到这个标签。"""

    label: str
    reason: str
    cost_bp: float | None = None
    """本次判定用的成本口径。`blocked:*` / `error` 时为 None（没算，谈不上成本）。"""

    net_long_short_return: float | None = None

    @property
    def is_delist_candidate(self) -> bool:
        """`{dead, reversed}` 且按 N5（18.2bp）**未过门**。

        任务书原文：标签 ∈ `{dead, reversed}` 且费后按 N5 未过门 → 下线候选。

        - `dead` 的定义本身就是"未过门"（无论是 |IC| 不够还是费后 ≤ 0），所以恒为候选；
        - `reversed` 是"方向反了"，按 IC 方向建仓费后必为负 —— 也恒为候选；
        - `alive` 过门了，不是候选；
        - `underpowered` / `degenerate` / `error` / `blocked:*` 是**缺证据**，
          不构成"该下线"的证据。

        这个属性只用于打提示，**不触发任何注销**。本模块不 import `unregister_factor`。
        """
        return self.label in DELIST_CANDIDATE_LABELS


def _f(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def label_of(
    item: FactorBatchItem,
    *,
    n_ic_dates: int,
    net_long_short_return: float | None,
    cost_bp: float,
    blocked_reason: str | None = None,
) -> LabelVerdict:
    """把一次因子检验映射成一个处置标签。**纯函数，不重算 IC。**

    Parameters
    ----------
    item
        回测已经算好的结果。只读 `ic_mean` / `error` / `factor_name`，不读也不改别的。
    n_ic_dates
        参与 IC 的有效交易日数。**显式传入而不是从 `item.n_dates` 取**，因为调用方
        可能对样本做过额外过滤（比如截面门槛），那时 `item.n_dates` 是过滤前的数。
    net_long_short_return
        **按 `cost_bp` 重扣之后**的多空累计收益。传 `None` 表示没有分层结果（样本
        不足以分组），此时不可能过门。
    cost_bp
        本次判定用的来回成本（bp）。必须等于 `GTJA191_COST_BP_ROUNDTRIP`，否则抛错 ——
        见模块文档「成本口径」。
    blocked_reason
        算子缺失/冲突的原因。非空时**优先于一切**，包括 `item.error`。

    Raises
    ------
    ValueError
        `cost_bp` 不等于成本锚 18.2bp。这是一个**口径错误**，不是数据问题，
        所以抛错而不是返回一个偏乐观的标签。
    """
    if cost_bp != GTJA191_COST_BP_ROUNDTRIP:
        raise ValueError(
            f"成本口径错误: 传入 {cost_bp} bp, 但下线候选门必须用 "
            f"{GTJA191_COST_BP_ROUNDTRIP} bp。`FactorBatchItem.long_short_return` "
            "扣的是 14bp（FactorBatchConfig 默认值），直接拿它过门会系统性偏乐观。"
        )

    # ── 1. blocked: 根本没算, 谈不上好坏 ──
    if blocked_reason:
        return LabelVerdict(
            label=f"blocked:{blocked_reason}",
            reason=f"算子不可表达（{blocked_reason}），本批不登记实现",
        )

    # ── 2. error: 算了但抛错 ──
    if item.error:
        return LabelVerdict(label="error", reason=f"计算失败: {item.error}")

    # ── 3. degenerate: 因子值在截面上没有方差, IC 无定义 ──
    # 这不是"算挂了"，也不是"因子没用"：**根本没测过**。判 dead 是拿一次没做成的
    # 测量当结论；判 error 会让人去查一个并不存在的 bug。
    if item.ic_mean is None and n_ic_dates == 0:
        return LabelVerdict(
            label="degenerate",
            reason="全部交易日的因子值在截面上是常数（无方差），Rank IC 无定义",
        )
    if item.ic_mean is None:
        return LabelVerdict(
            label="error",
            reason=f"ic_mean 为 None 但 IC 日数 {n_ic_dates} > 0，结果自相矛盾，需人工查",
            cost_bp=cost_bp,
        )

    # ── 4. underpowered: 样本不够, 数值是噪声 ──
    if n_ic_dates < MIN_IC_DATES:
        return LabelVerdict(
            label="underpowered",
            reason=f"IC 日数 {n_ic_dates} < {MIN_IC_DATES}，样本太薄不判生死",
            cost_bp=cost_bp,
        )

    ic = item.ic_mean
    net = net_long_short_return

    # ── 5. reversed: IC 与费后多空反向 ──
    # 只在两侧都显著且反向时判 reversed。任一侧贴近 0 时不判 —— 噪声的符号没有意义。
    if (
        net is not None
        and abs(ic) >= MIN_ABS_IC
        and abs(net) > MIN_NET_LONG_SHORT
        and (ic > 0) != (net > 0)
    ):
        return LabelVerdict(
            label="reversed",
            reason=(
                f"IC {_f(ic)} 与费后多空 {_f(net)} 符号相反，"
                "排序方向与收益方向脱节（用了会稳定亏在成本上）"
            ),
            cost_bp=cost_bp,
            net_long_short_return=net,
        )

    # ── 5. dead / alive: 方向没问题, 看费后过门 ──
    if abs(ic) < MIN_ABS_IC:
        return LabelVerdict(
            label="dead",
            reason=f"|IC| {abs(ic):.4f} < {MIN_ABS_IC}，排序能力不足",
            cost_bp=cost_bp,
            net_long_short_return=net,
        )
    if net is None:
        return LabelVerdict(
            label="dead",
            reason="没有分层多空结果，无法证明费后为正",
            cost_bp=cost_bp,
        )
    if net <= MIN_NET_LONG_SHORT:
        return LabelVerdict(
            label="dead",
            reason=f"费后多空 {_f(net)} ≤ {MIN_NET_LONG_SHORT}（{cost_bp} bp 已扣）",
            cost_bp=cost_bp,
            net_long_short_return=net,
        )
    return LabelVerdict(
        label="alive",
        reason=f"|IC| {abs(ic):.4f} ≥ {MIN_ABS_IC}，费后多空 {_f(net)} > 0（{cost_bp} bp 已扣）",
        cost_bp=cost_bp,
        net_long_short_return=net,
    )
