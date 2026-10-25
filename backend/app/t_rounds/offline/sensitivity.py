"""做 T 成本敏感性 5c · 五档跑表与 Regime 附报（批次 20261025gcost5c）。

**仅供研究，不构成投资建议；执行须人工确认。**

## 跑的是什么

`run_mode_a_backtest()` 会在同一批轮次上产出一份 `BacktestSummary`（`design_pending` +
0，即**不扣成本**）。那是主报告的对照口径，本模块**不改它** —— 改它等于把主口径
偷偷换成扣过成本的口径，而读旧报告的人不会知道。

本模块在**同一批轮次**上重放五档成本（`0bp`/`5bp`/`10bp`/`15bp`/`20bp`），每档都走
`complete_round` 真扣。所以：

- `expectancy_gross` 是**未扣成本**的对照（就是主报告那个 `expectancy`）；
- `expectancy_net` 是扣掉该档成本之后的期望值（**主看**）；
- 两者之差 = 该档成本对每轮期望值的影响，可以直接读。

## 为什么必须重放而不是"拿毛价差减个数"

"期望值 − 平均成本"看起来等价，其实不等价，有两个陷阱：

1. **成本按名义金额扣，名义随成交价变**，而期望值是各轮平均。高价股的一轮与低价股
   的一轮，成本绝对值不同；先平均再减成本，等于给两轮扣了同一个成本。
2. **只有 `completed` 的轮次有已实现净价差可扣。** `unreverted_at_cutoff` 与
   `data_truncated` 的实现价差是 None，不能扣 —— 但它们仍在期望值**分母**里。
   如果拿"总毛价差 − 总成本 / 轮数"来算，就等于也把成本摊给了没成交的轮次。

所以走重放：每个档位下，`completed` 轮按该档扣成本后重新汇总，其余桶原样搬运。
这样 `expectancy_net` 的分母与主口径**逐字一致**（`counts.rounds`），差别只在分子里
`completed` 那一部分的净价差。

## 分母口径（写死，与主报告一致）

```text
n_rounds_opened = completed + unreverted_at_cutoff + data_truncated + limit_locked
```

**含** `limit_locked`（真实开出去了，只是没平掉），**不含** `skipped_quota`（没开出去，
不该进分母）。与本包 `GroupSummary.expectancy` 用的是同一个式子。

## Regime 附报：能算就算，不能算就说 `data_pending`

| 分组键 | 定义 | 本拍数据 |
|---|---|---|
| `regime_day` | 标的当日 close-to-close 收益：`up`(>0.5%) / `flat`([-0.5%,+0.5%]) / `down`(<-0.5%) | 特征表**只有日内分钟数据**，没有前收盘价 → **`data_pending`** |
| `near_limit` | 当日高/低触及或收盘距涨跌停价 ≤1% → `near`，否则 `normal` | **无涨跌停价数据** → **`data_pending`**（继承 `LIMIT_LOCK_SOURCE_PENDING`，不猜） |

**不猜**是这里的硬约束：用日内最高价反推前收盘再算日收益，等于假设"今天没跳空"，
而跳空恰恰是最需要分开看的那类日子；按固定百分比阈值反推涨跌停，会把"刚好跌 10%"
的普通下跌记成锁死。这两种编法在报告上都看不出来，只会让某一组显得样本充足。

所以本拍的 Regime 附报是**结构完整、数值 pending**：列在、键在、组在，`regime_source`
写 `data_pending`，各档 `expectancy_net` 与 `n_rounds` 都是 `None` / `0`。
**不挡主表**（五档全样本跑照做），**不删列**（删了之后接数据的人会以为从没考虑过）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from .. import constants as C
from ..models import TRoundRecord
from . import costs as costs_mod
from .backtest import (
    COST_DESIGN_PENDING,
    BacktestSummary,
)

# ---------------------------------------------------------------- 列名（钉死）

SUMMARY_COL_TIER: Final = "cost_bp_tier"
SUMMARY_COL_RATE: Final = "cost_rate_pct"
SUMMARY_COL_AMOUNT_SUM: Final = "cost_amount_sum"
SUMMARY_COL_EXPECTANCY_GROSS: Final = "expectancy_gross"
SUMMARY_COL_EXPECTANCY_NET: Final = "expectancy_net"
SUMMARY_COL_N_ROUNDS: Final = "n_rounds_opened"
SUMMARY_COL_COMPLETED_RATE: Final = "completed_rate"
SUMMARY_COL_FOOTNOTE: Final = "footnote_retail_assumption"

SENSITIVITY_COLUMNS: Final = (
    SUMMARY_COL_TIER,
    SUMMARY_COL_RATE,
    SUMMARY_COL_AMOUNT_SUM,
    SUMMARY_COL_EXPECTANCY_GROSS,
    SUMMARY_COL_EXPECTANCY_NET,
    SUMMARY_COL_N_ROUNDS,
    SUMMARY_COL_COMPLETED_RATE,
    SUMMARY_COL_FOOTNOTE,
)
"""敏感性跑表的八列（ALIGN §2.3，钉死）。

**不允许同义另名并行。** 报告里叫 `expectancy_net`、图里叫 `net_expectancy`、
文档里叫"净期望值"，读者就得自己判断这三个是不是同一个数 —— 而它们一旦真的不同
（比如一个扣了成本一个没扣），也没有任何地方会报错。

这八列是**验收口径**：少一列跑表不成立。测试逐列断言。
"""

REGIME_KEY_DAY: Final = "regime_day"
REGIME_KEY_NEAR_LIMIT: Final = "near_limit"
REGIME_KEYS: Final = (REGIME_KEY_DAY, REGIME_KEY_NEAR_LIMIT)

REGIME_DAY_BUCKETS: Final = ("up", "flat", "down")
REGIME_NEAR_LIMIT_BUCKETS: Final = ("near", "normal")

REGIME_SOURCE_DAY: Final = "data_pending"
"""日收益来源。特征表只有日内分钟数据，没有前收盘价 —— 算不出 close-to-close。"""

REGIME_SOURCE_NEAR_LIMIT: Final = "data_pending"
"""涨跌停价来源。继承 `constants.LIMIT_LOCK_SOURCE_PENDING`，同一条理由：不猜。"""


@dataclass(frozen=True, slots=True)
class CostSensitivityRow:
    """一档成本的一行跑表。字段名与 `SENSITIVITY_COLUMNS` 一一对应。"""

    cost_bp_tier: str
    cost_rate_pct: float
    cost_amount_sum: float
    """该档合计扣减额（绝对额，元）。只统计 `completed` 轮 —— 其余终态没有实现价差可扣。"""
    expectancy_gross: float | None
    """未扣成本的期望值（对照）。与主报告 `expectancy` 同口径、同值。"""
    expectancy_net: float | None
    """扣该档成本后的期望值（**主看**）。"""
    n_rounds_opened: int
    completed_rate: float | None
    """`completed / n_rounds_opened`。`n_rounds_opened=0` → None（不是 0）。"""
    footnote_retail_assumption: str = costs_mod.FOOTNOTE_RETAIL_ASSUMPTION

    def as_row(self) -> dict[str, Any]:
        """按 `SENSITIVITY_COLUMNS` 的顺序导出。顺序固定，测试可逐列比。"""
        return {
            SUMMARY_COL_TIER: self.cost_bp_tier,
            SUMMARY_COL_RATE: self.cost_rate_pct,
            SUMMARY_COL_AMOUNT_SUM: self.cost_amount_sum,
            SUMMARY_COL_EXPECTANCY_GROSS: self.expectancy_gross,
            SUMMARY_COL_EXPECTANCY_NET: self.expectancy_net,
            SUMMARY_COL_N_ROUNDS: self.n_rounds_opened,
            SUMMARY_COL_COMPLETED_RATE: self.completed_rate,
            SUMMARY_COL_FOOTNOTE: self.footnote_retail_assumption,
        }


@dataclass(frozen=True, slots=True)
class RegimeCell:
    """Regime 一个分桶、一档成本的数。

    `expectancy_net` / `n_rounds` 在本拍恒为 `None` / `0` —— 分组键本身还没有数据来源。
    结构先建好，接上数据就能直接填，不必改报告结构。
    """

    bucket: str
    cost_bp_tier: str
    expectancy_net: float | None
    n_rounds: int


@dataclass(frozen=True, slots=True)
class RegimeReport:
    """Regime 附报。**缺数据不挡主表**，但也**不许删列**。"""

    regime_day_source: str
    near_limit_source: str
    cells: tuple[RegimeCell, ...]
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            REGIME_KEY_DAY: {
                "regime_source": self.regime_day_source,
                "buckets": list(REGIME_DAY_BUCKETS),
                "cells": [
                    {
                        "bucket": c.bucket,
                        "cost_bp_tier": c.cost_bp_tier,
                        "expectancy_net": c.expectancy_net,
                        "n_rounds": c.n_rounds,
                    }
                    for c in self.cells
                    if c.bucket in REGIME_DAY_BUCKETS
                ],
            },
            REGIME_KEY_NEAR_LIMIT: {
                "regime_source": self.near_limit_source,
                "buckets": list(REGIME_NEAR_LIMIT_BUCKETS),
                "cells": [
                    {
                        "bucket": c.bucket,
                        "cost_bp_tier": c.cost_bp_tier,
                        "expectancy_net": c.expectancy_net,
                        "n_rounds": c.n_rounds,
                    }
                    for c in self.cells
                    if c.bucket in REGIME_NEAR_LIMIT_BUCKETS
                ],
            },
            "note": self.note,
        }


# ---------------------------------------------------------------- 重放


def _replay_completed_net(
    record: TRoundRecord, tier: str
) -> tuple[float, float]:
    """把一轮 `completed` 记录按该档成本重算 `(净价差, 扣减额)`。

    用记录上的**原始**毛价差减该档金额，而不是反解原净价差 —— 反解需要先知道原来
    扣了多少，而那一轮原本扣的是 0：

    ```text
    net_tier = gross_spread - cost_amount_for_tier(tier, leg1, leg2, qty)
    ```

    这与 `complete_round` 里的 `net = gross - cost_amount` 是同一个式子，只是换了个
    金额。**注意这里没有调用 `complete_round`** —— 重放用的是已经结算好的记录，
    再走一遍状态机会因为「已经是 completed」被拒。真正的登记（证明该档合法）由
    `_settle` 走闸门完成；本函数只负责把已登记的那个金额应用到汇总上。

    两条腿的价格与数量必须齐全。缺了说明记录不完整，直接报错而不是当 0 处理 ——
    当 0 会让那一轮的成本凭空消失。
    """
    if record.gross_spread is None:
        raise ValueError(f"completed 记录缺 gross_spread: {record.t_round_id}")
    leg1, leg2, qty = record.leg1_price, record.leg2_price, record.leg1_qty
    if leg1 is None or leg2 is None or qty is None:
        raise ValueError(f"completed 记录缺腿价/数量: {record.t_round_id}")
    amount = costs_mod.cost_amount_for_tier(tier, leg1_price=leg1, leg2_price=leg2, qty=qty)
    return float(record.gross_spread) - amount, amount


def build_cost_sensitivity(summary: BacktestSummary) -> tuple[CostSensitivityRow, ...]:
    """在**全样本**上产出五档敏感性跑表（每档一行，跨模式跨路径合计）。

    「全样本合计」而不是分 (模式, 路径) 四格：本表的用途是"成本这个假设变化时，
    整体期望值怎么走"。四格分列已经在主报告的 `groups` 里了，这里再分一遍会让表
    变成 20 行，而读者真正要看的对照（毛 vs 净、档位之间）会被稀释。四格分列仍可
    从 `groups` + 本表的档位金额重建，不丢信息。

    `expectancy_gross` 对每一档都填**同一个值**（主摘要的毛期望值）。不同档填不同数
    才是错的：毛就是毛，与假设扣多少成本无关。重复填是为了让每一行都能自证
    "这行的净比毛少了多少"，不必去表外找基准。

    记录为空（没有任何轮次）→ 每档仍出**一行**，数值为 None / 0。**表不能空**：
    空表会让报告读者以为"这一步没跑"，而不是"跑了，样本是零"。
    """
    records = summary.records
    completed = [r for r in records if r.round_status == "completed"]
    mtm_total = sum(float(r.mtm_pnl) for r in records if r.mtm_pnl is not None)
    n_rounds = sum(g.counts.rounds for g in summary.groups)
    n_completed = sum(g.counts.completed for g in summary.groups)
    gross_net_sum = sum(
        float(r.net_spread) for r in completed if r.net_spread is not None
    )

    rows: list[CostSensitivityRow] = []
    for tier in costs_mod.COST_TIERS_SENSITIVITY:
        net_sum = 0.0
        amount_sum = 0.0
        for rec in completed:
            net, amount = _replay_completed_net(rec, tier)
            net_sum += net
            amount_sum += amount
        # 毛期望值: 与 `GroupSummary.expectancy` **同一个式子**
        # (Σcompleted净 + Σ盯市) / 分母。0bp 档的净期望值必然与它相等 —— 这条
        # 恒等式是跑表正确性的第一道自检(单测直接断言), 因为 0bp 扣的是 0。
        gross_expectancy = (
            (gross_net_sum + mtm_total) / n_rounds if n_rounds else None
        )
        rows.append(
            CostSensitivityRow(
                cost_bp_tier=tier,
                cost_rate_pct=costs_mod.COST_RATE_PCT_BY_TIER[tier],
                cost_amount_sum=amount_sum,
                # 毛期望值每档相同 —— 它就是主报告那个数, 不随成本假设变。
                expectancy_gross=gross_expectancy,
                # 净期望值: 分子换成该档扣过成本的 completed 净价差之和, **盯市部分
                # 原样搬运**(mtm 不扣成本, 见 _settle docstring); 分母 n_rounds 与
                # 主口径逐字一致。写成一整段的 Σ 而不是传列表, 是因为这里把
                # completed 的净价差先逐轮加总了, 避免再分配一个等长列表。
                expectancy_net=(
                    (net_sum + mtm_total) / n_rounds if n_rounds else None
                ),
                n_rounds_opened=n_rounds,
                completed_rate=(n_completed / n_rounds) if n_rounds else None,
            )
        )
    return tuple(rows)


def build_regime_report() -> RegimeReport:
    """Regime 附报。本拍两个分组键都**没有数据来源**，整组 `data_pending`。

    `cells` 仍然按「分组键 × 分桶 × 五档」铺满 —— 结构先立住。将来接上日收益与
    涨跌停价，只需填 `expectancy_net` / `n_rounds` 并把 `regime_source` 换成实际来源
    标签，报告结构与消费方都不用改。
    """
    cells: list[RegimeCell] = []
    for key, buckets in (
        (REGIME_KEY_DAY, REGIME_DAY_BUCKETS),
        (REGIME_KEY_NEAR_LIMIT, REGIME_NEAR_LIMIT_BUCKETS),
    ):
        del key
        for bucket in buckets:
            for tier in costs_mod.COST_TIERS_SENSITIVITY:
                cells.append(
                    RegimeCell(
                        bucket=bucket,
                        cost_bp_tier=tier,
                        expectancy_net=None,
                        n_rounds=0,
                    )
                )
    return RegimeReport(
        regime_day_source=REGIME_SOURCE_DAY,
        near_limit_source=REGIME_SOURCE_NEAR_LIMIT,
        cells=tuple(cells),
        note=(
            "Regime 附报结构已建, 但本拍两个分组键都没有数据来源: "
            "regime_day 需要前收盘价(特征表只有日内分钟数据), "
            "near_limit 需要涨跌停价(继承 LIMIT_LOCK_SOURCE_PENDING)。"
            "整组标 data_pending, 计数 0, 不编造; 不挡主跑表, 也不删列。"
        ),
    )


def build_sensitivity_payload(summary: BacktestSummary) -> dict[str, Any]:
    """组进报告 JSON 的 `cost_sensitivity` 段（跑表 + Regime + 口径声明）。"""
    rows = build_cost_sensitivity(summary)
    regime = build_regime_report()
    return {
        "rows": [r.as_row() for r in rows],
        "columns": list(SENSITIVITY_COLUMNS),
        "regime": regime.as_dict(),
        "retail_assumption_tiers": list(costs_mod.RETAIL_ASSUMPTION_TIERS),
        "footnote_retail_assumption": costs_mod.FOOTNOTE_RETAIL_ASSUMPTION,
        "notional_definition": (
            "notional = ((leg1_price + leg2_price) / 2.0) * qty; "
            "cost_amount = (cost_rate_pct / 100.0) * notional"
        ),
        "cost_design_pending_tier": COST_DESIGN_PENDING,
        "applied_to": (
            "只对 completed 轮扣减(有已实现价差); unreverted_at_cutoff 的盯市盈亏 "
            f"mtm_pnl 不扣成本(那不是一次真实平仓); {COST_DESIGN_PENDING} 为"
            "不扣成本的对照口径, 非本表行"
        ),
    }


__all__ = [
    "REGIME_DAY_BUCKETS",
    "REGIME_KEYS",
    "REGIME_KEY_DAY",
    "REGIME_KEY_NEAR_LIMIT",
    "REGIME_NEAR_LIMIT_BUCKETS",
    "REGIME_SOURCE_DAY",
    "REGIME_SOURCE_NEAR_LIMIT",
    "SENSITIVITY_COLUMNS",
    "SUMMARY_COL_AMOUNT_SUM",
    "SUMMARY_COL_COMPLETED_RATE",
    "SUMMARY_COL_EXPECTANCY_GROSS",
    "SUMMARY_COL_EXPECTANCY_NET",
    "SUMMARY_COL_FOOTNOTE",
    "SUMMARY_COL_N_ROUNDS",
    "SUMMARY_COL_RATE",
    "SUMMARY_COL_TIER",
    "CostSensitivityRow",
    "RegimeCell",
    "RegimeReport",
    "build_cost_sensitivity",
    "build_regime_report",
    "build_sensitivity_payload",
]
