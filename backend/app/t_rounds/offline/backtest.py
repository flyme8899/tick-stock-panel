"""模式 A 纸面回测摘要：先卖后买 / 先买后卖 × 路径 A / B，按结局分列。

**仅供研究，不构成投资建议；执行须人工确认。**

## "模式 A" 具体是什么

同一 `(symbol, 交易日)` 内，按路径标记找**一次**入场 + **一次**出场，构成一轮 T：

| 模式 | 入场（leg1） | 出场（leg2） | 赚的条件 |
|---|---|---|---|
| `sell_first`（先卖后买，旧名 `zheng_t`） | 价格首次进入 **上带**（`price >= vwap*(1+ε)`）→ **卖出** | 之后价格回落到 VWAP 之下 → **买回** | 卖价 > 买价 |
| `buy_first`（先买后卖，旧名 `dao_t`） | 价格首次进入 **下带**（`price <= vwap*(1-ε)`）→ **买入** | 之后价格回升到 VWAP 之上 → **卖出** | 卖价 > 买价 |

出场条件统一为「价格回到 VWAP 的**另一侧**」，不用固定持仓时长 —— 固定时长会把
「等不到回摆」的情况强行记成成交，而那正是该记成 `unreverted_at_cutoff` 的情形。

## 因果：入场用**确认 bar 的下一根**成交，出场只在入场之后找

批次 20261025gtrounds ①/⑤a 修的就是这里。旧实现的路径 A 有两处超前：

1. **入场点取的是「边沿那根」**，而"边沿之后确实进到 ε 带"这件事要到更晚才成立
   —— 于是进场时刻早于它自己的确认依据。
2. **出场搜索从「边沿那根」之后开始**，于是出场可以发生在确认之前。极端情形：
   13:00 才确认，却记了一笔 10:05 的"出场"，一轮不存在的 T 就这么成交了。

现在的规则（两条路径同一口径）：

```
边沿/进带 出现在 bar[i]  ──┐
                          ├─→ 确认 bar = bar[i]（A 取「边沿」与「其后首次进带」中较晚者）
                          │
                          └─→ 成交 bar = bar[i+1]，成交价 = bar[i+1]["price"]
                                  ↓
                            出场只在 bar[i+1] **之后**找
```

- 确认 bar 是当日最后一根 → 没有下一根可成交 → 这一轮**不成立**（不是 completed）。
- 成交价用的是**下一根 bar 的 `price`**，不是确认 bar 的 `price`，也不是 `vwap`。
  两条路径同一口径，A 与 B 的差别因此收敛到"信号怎么定义"，而不是"在哪儿成交"。

### `price` 到底是什么：`price := 该 bar 的 close`

`price` 这个字段不是本包算的，是 `build_feature_frame` 里直接由 `close` 赋过去的
（`app/strategy/intraday_features.py` **L101**：
`cols = {"price": pl.col("close").cast(pl.Float64)}`，**已核**）。

所以"成交价 = bar[i+1]['price']" 这句话的完整读法是：

> **以下一根 bar 的收盘价成交。**

这一点必须写明：`price` 听起来像"成交价"或"中间价"，而它实际是**那根 bar 的收盘价**。
两者在分钟线上差别不大，但在"什么时候能知道自己成交在什么价"上是两件事 ——
收盘价要到那根 bar 走完才知道，而我们的成交假设本来就是"下一根走完才成交"，
两者是自洽的。这个来源**已经核过**，不必再回去怀疑；也不必为它改
`intraday_features.py` 的行为：那边一行既成事实，改的是本包的注释口径。

### A 与 B 的关系：A = B + `require_prior_cross` + 一根延迟

修完之后 A **不是**与 B 平行的第二个信号，而是 B 的一个**更严的变体**：同样是
「进到 ε 带」，只是额外要求"这一带之前价格确实穿越过均价"，并且因为要等穿越确认，
进场比 B 晚一根。所以：

- A 的样本量**必然**少于或等于 B。若 A 比 B 多，说明 causal 又坏了。
- A 的盈亏差不该被解读成"信号优劣"—— 它是**敏感度开关**（对"是否要求先穿越"这一
  建模选择的敏感度），报告里必须这么写。

## 状态桶，以及为什么只能这样分

一轮的结局有五种，对应 `t_rounds` 的五个非进行中终态：

| 结局 | `round_status` | 可计盈亏 | 进已实现 KPI |
|---|---|---|---|
| 两腿都成交 | `completed` | 净价差 | ✅（且必须过 `complete_round()`） |
| 走到 14:57 仍没回摆（数据是全的） | `unreverted_at_cutoff` | **盯市盈亏** | ❌ |
| 14:57 之前数据就断了 | `data_truncated` | 不可计 | ❌ |
| 涨跌停锁死、无法按收盘价平 | `limit_locked` | 留在分母 | ❌ |
| 过复核期 / 数据作废 | `expired_review` / `void_data` | — | ❌ |

`count_in_realized_kpi` 只在 `completed` 为 True —— 这条**不是本模块新加的规则**，
而是 `TRoundRecord.__post_init__` 与 `complete_round()` 早就钉死的。本模块只是
不绕过它：所有结算都走 `state_machine.complete_round()`，所以"未完成被算进已实现"
这件事在结构上就不可能发生。

### 为什么必须给未回摆轮盯市（本批次 ③）

只统计 `completed` 等于**只统计赢的那部分**：轮次一旦不回摆就落进
`unreverted_at_cutoff`，而那个桶此前不算任何盈亏，于是"已实现"永远为正 —— 这是
幸存者偏差，不是策略好。所以未回摆轮按 `ts <= 14:57:00` 的**最后一根** `price`
盯市，把亏的那一半也摆出来。

期望值口径：

```
expectancy = (Σ completed 的净价差 + Σ 可盯市的未回摆轮盯市盈亏) / 实际开出的轮次
```

分母是 `completed + unreverted_at_cutoff + data_truncated + limit_locked`
（**不含** `skipped_quota` —— 它不是一个轮次，只是被配额挡掉的候选）。

### `limit_locked` 留在分母里

涨跌停锁死时那一轮**真实存在过**，只是没法在那个价位平掉。把它从分母里剔除，
等于把"卡在跌停板上出不来的亏损"从账上抹掉 —— 那正好是最该被看见的一类。所以：

- 它**留在分母**；
- 它**不进** `data_problem`（那不是数据问题，是流动性问题）；
- 报告里**不许**出现「剔除」这个词。

## T+1 约束挂在**卖出腿**上，不挂在"第二腿"上

两种模式的**卖出腿**（`sell_first` 的 leg1 / `buy_first` 的 leg2）份额来源都必须是
`old_lot`。本包用虚拟底仓（`virtual_position_flag=True`）做纸面记录，来源标
`old_lot`。旧实现把约束绑在 `zheng_t` 的第二腿 —— 那是**买入**腿 —— 于是
`buy_first` 的卖出腿从来没被检查过。详见 `t_rounds.models.check_sell_leg_source_lot`。

## 一日一票一轮（四个格子各是独立世界）

`MAX_ROUNDS_PER_DAY=1` **钉死不放宽**（与 `InMemoryTRoundRegistryStore.upsert()`
的配额检查同一口径）。同一 (symbol, 交易日) 在同一格里第二个候选不再开轮，计入
`skipped_quota`。

### 配额按「格子」隔离，不跨格子共享

四个格子（正/倒 T × 路径 A/B）是**四个反事实世界**：每个格子各自受一日一轮约束，
彼此不排队、不抢名额。

这一条是本批次明确修正过的。早先的实现让四格共享一个 `(symbol, 交易日)` 名额，
于是「谁先被扫到谁拿走」—— 顺序被写死成「正 T 优先、A 优先」。后果很具体：
路径 A 会在每个同时满足两类条件的日子抢走名额，B 永远拿不到，A 与 B 的对比
自带系统性优势。而任务书要求 A∥B **分列、不混主门禁** —— 带顺序偏置的分列
没有可比性。

改成隔离之后，「硬规则有没有被放宽」这个疑问要分清两件事：

- **没有放宽**：每个世界内部仍然是一日一轮，同一天在同一世界里的第二个候选照旧
  被拒、照旧记 `skipped_quota`。
- **不再互相排队**：不同世界之间本来就不该排队 —— 它们不会同时发生。真实交易
  里你只可能处在其中一种情形下；把四个假设并排摆出来看，是评估的工作方式，
  不是同时开四轮 T。

`skipped_quota` 因此只有一个含义：**同一世界、同一路径、同日多候选撞配额**。
它**不再**表示"被别的格子抢走了名额"。

这不是为了"让数字好看"而做的限制：一天做多轮 T 在纸面回测里几乎总是更好看
（多做几次总能摊到一次成功的），但它要求能在同一个日内区间反复进出，实盘上滑点
和冲击成本会把这点优势吃光。

## 虚拟底仓假设（必须写进报告）

本包的每一轮都建在纸面底仓上：假设账户在一开盘就持有 `LOT_SIZE` 股该标的、
且这些股份是可卖的（当日之前买入）。这不是实盘约束的建模 —— **不得**据此宣称
「实盘可卖老股已通」。报告里的 `virtual_lot_assumption` 字段就是这个说明。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Final

import polars as pl

from app.market_time import CN_TZ

from .. import constants as C
from ..models import TRoundRecord
from ..state_machine import RoundCost, complete_round, transition
from . import costs as costs_mod

LOT_SIZE: Final = 100
"""纸面每轮的股数。1 手，够表达价差又不制造"大仓位"的错觉。"""

COST_DESIGN_PENDING: Final = "design_pending"

PATH_A: Final = "A"
PATH_B: Final = "B"
PATHS: Final = (PATH_A, PATH_B)

VIRTUAL_LOT_ASSUMPTION: Final = (
    "每一轮均建在纸面虚拟底仓上: 假设开盘即持有 LOT_SIZE 股且当日之前买入(可卖), "
    "leg2_source_lot=old_lot; 未建模真实持仓/冻结/T+1 可用量, 不得据此宣称实盘可卖已通"
)

PATH_A_DEFINITION: Final = (
    "路径 A = 进 ε 带 + require_prior_cross(这一带之前价格确实穿越过均价) + 一根延迟。"
    "它不是与 B 平行的第二个信号, 而是 B 的更严变体; A 的样本量必然 <= B。"
    "A 与 B 的盈亏差是**敏感度开关**, 不是信号优劣的结论"
)
"""路径 A 的定义 —— 必须进报告。

修掉 look-ahead 之后 A 不再是"另一个独立策略"，而是"B 加一个更严的条件"。把这条
写进 JSON 而不是只写在 docstring 里，是因为读报告的人不会去翻源码，然后就会把
"A 比 B 少赚"当成"这个信号不行"，而真实原因只是样本少了一个量级。
"""

EXPECTANCY_DEFINITION: Final = (
    "expectancy = (Σ completed 净价差 + Σ 可盯市未回摆轮净价差) / 实际开出轮次; "
    "分母含 limit_locked, 不含 skipped_quota。"
    "realized_avg_net_spread 只含赢的一部分, 不能当策略成绩"
)
"""期望值口径 —— 必须进报告。两个平均值的分母不同，混用会得出相反结论。"""

_EXIT_CUTOFF: Final = time(14, 57)
"""最后出场时点。收盘前 3 分钟不再开新仓也不等出场 —— 集合竞价前的价格不可信，
而 `unreverted_at_cutoff` 本来就该把"没等到"记下来，没必要为此借一根尾盘bar。"""

SESSION_CLOSE: Final = time(15, 0)
"""收盘时刻。用来判断"数据到底有没有走完"。

`data_truncated` 的判据是**当日最后一根 bar 的时刻早于收盘时段末尾**，而不是
"最后一根早于 14:57"—— 14:57..15:00 之间没有 bar 是正常的（那三分钟本来就
不用于决策），把它当成数据缺失会把每个正常交易日都判成截断。
"""


# ---------------------------------------------------------------- 结果结构


@dataclass(frozen=True, slots=True)
class BucketCounts:
    """一个 (模式 × 路径) 格子的分桶计数。

    字段名与 `constants.ROUND_STATUSES` 一一对应 —— 想加一个桶就要先加一个状态，
    不许在计数器里偷偷合并两个不同的结局。
    """

    completed: int = 0
    unreverted_at_cutoff: int = 0
    """走到 14:57 仍没回摆，数据是完整的 → 可盯市。"""
    data_truncated: int = 0
    """14:57 之前数据就断了 → 不可盯市，先分因。"""
    limit_locked: int = 0
    """涨跌停锁死平不掉 → **留在分母**，不计 mtm，也不算数据问题。"""

    no_fill_bar: int = 0
    """确认 bar 是当日最后一根、没有下一根可成交的候选数。

    **不是**一轮 T，只计数不建记录、不进分母。与 `skipped_quota` 的区别在于原因：
    `skipped_quota` 是配额挡的（本来可以开），`no_fill_bar` 是没机会下单。

    它也不是 `data_truncated`：数据没断，只是信号来得太晚。混进去会让"数据问题"
    这个桶涨起来，而真正的数据缺失反而被埋掉。
    """

    skipped_quota: int = 0
    """同一世界（模式, 路径）同日多候选时，超出 `MAX_ROUNDS_PER_DAY` 被拒的候选数。

    **不是**一轮 T，只计数不建记录。也**不是**"被别的格子抢走的名额" —— 配额按
    格子隔离，四个格子各跑各的（见模块 docstring「四个格子各是独立世界」）。
    """

    @property
    def rounds(self) -> int:
        """**实际开出的轮次数** = 期望值的分母。

        含 `limit_locked`：那一轮真实开出去了，只是没能平掉。把它剔掉等于把
        "卡在板上出不来的亏损"从账上抹掉。不含 `skipped_quota` —— 它不是一个
        轮次，只是被配额挡掉的候选。
        """
        return (
            self.completed
            + self.unreverted_at_cutoff
            + self.data_truncated
            + self.limit_locked
        )

    @property
    def data_problem(self) -> int:
        """**只有** `data_truncated` 算数据问题。

        `limit_locked` 不算 —— 数据是全的，缺的是对手盘。混进来会让"数据补齐后
        这个数就消失了"，恰好把最该保留的亏损洗掉。
        """
        return self.data_truncated

    @property
    def resolution_rate(self) -> float | None:
        """完成率 = completed / rounds。`rounds=0` → None（不是 0）。"""
        return None if self.rounds == 0 else self.completed / self.rounds


@dataclass(frozen=True, slots=True)
class GroupSummary:
    """(模式, 路径) 的摘要。

    `realized_*` 一栏 **只含 completed** —— 它回答"做成了的那几轮赚了多少"。
    这个数**不能**当策略成绩看：没回摆的轮次不在里面，所以它天然偏乐观。
    要看策略本身，用 `expectancy`。
    """

    mode: str
    path: str
    counts: BucketCounts
    realized_gross_spread: float
    realized_net_spread: float
    realized_rounds: int
    realized_avg_net_spread: float | None
    """每轮平均净价差。`realized_rounds=0` → None。"""

    mtm_net_spread: float = 0.0
    """未回摆轮的**盯市**盈亏合计（按 `ts<=14:57:00` 最后一根 `price` 平掉）。

    与 `realized_net_spread` 分开存：它不进已实现 KPI（那不是"做成的交易"），
    但必须可见 —— 否则未回摆的亏损就在账上消失了。
    """

    mtm_rounds: int = 0
    """能盯市的未回摆轮数。`unreverted_at_cutoff` 里有几轮拿到了 14:57 内的价格。"""

    mtm_avg_net_spread: float | None = None
    """未回摆轮的平均盯市盈亏。`mtm_rounds=0` → None。"""

    @property
    def kpi_realized_only_completed(self) -> bool:
        """写死 True 并进摘要：提醒读者「已实现」这一栏只有 completed。"""
        return True

    @property
    def expectancy(self) -> float | None:
        """**期望值** = (Σ completed 净价差 + Σ 可盯市未回摆净价差) / 实际开出轮次。

        分母是 `counts.rounds`，即 `completed + unreverted_at_cutoff +
        data_truncated + limit_locked` —— **不含** `skipped_quota`。

        这是本批次唯一可以拿来谈"这个策略赚不赚"的数。`realized_avg_net_spread`
        不行，因为它只数赢的那部分。`rounds=0` → None（不是 0：没有样本时平均值
        不存在，写 0 会被读成"打平"）。

        注意分母里那两类算不进分子的轮次（`data_truncated`、`limit_locked`）：
        它们让期望值**偏保守**。这是有意的 —— 拿不到价格的轮次不能当成不亏不赚。
        """
        if self.counts.rounds == 0:
            return None
        return (self.realized_net_spread + self.mtm_net_spread) / self.counts.rounds


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    """整次回测的摘要。`as_dict()` 就是写进报告 JSON 的那个结构。"""

    version: str
    epsilon_pct: float
    dates: tuple[date, ...]
    groups: tuple[GroupSummary, ...]
    records: tuple[TRoundRecord, ...] = field(default=(), repr=False)
    """逐轮记录（纸面）。**不进摘要 JSON**，避免报告体积随样本膨胀。"""

    require_prior_cross: bool = True
    """路径 A 是否额外要求"进带之前价格穿越过均价"。

    这是 A 与 B 之间**唯一**的实质差别，所以必须明写进摘要：读者看到 A 与 B 数字
    不同时，要能一眼看出差别来自这个开关，而不是去猜两条路径是不是两个独立策略。
    """

    def group(self, mode: str, path: str) -> GroupSummary:
        for item in self.groups:
            if item.mode == mode and item.path == path:
                return item
        raise KeyError(f"没有 ({mode}, {path}) 这一组")

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "epsilon_value_pct": self.epsilon_pct,
            "epsilon_definition": C.EPSILON_DEFINITION,
            "dates": [d.isoformat() for d in self.dates],
            "modes": sorted(C.T_MODES),
            "mode_aliases": dict(C.T_MODE_ALIASES),
            "paths": list(PATHS),
            "path_a_definition": PATH_A_DEFINITION,
            "require_prior_cross": self.require_prior_cross,
            "groups": [
                {
                    "mode": g.mode,
                    "path": g.path,
                    # 分桶: 一个状态一列，不在报告层合并
                    "completed": g.counts.completed,
                    "unreverted_at_cutoff": g.counts.unreverted_at_cutoff,
                    "data_truncated": g.counts.data_truncated,
                    "limit_locked": g.counts.limit_locked,
                    "skipped_quota": g.counts.skipped_quota,
                    "no_fill_bar": g.counts.no_fill_bar,
                    # 实际开出的轮次数 = 期望值分母（含 limit_locked，不含 skipped_quota）
                    "rounds": g.counts.rounds,
                    "data_problem": g.counts.data_problem,
                    "resolution_rate": g.counts.resolution_rate,
                    # 已实现: 只含 completed
                    "realized_rounds": g.realized_rounds,
                    "realized_gross_spread": g.realized_gross_spread,
                    "realized_net_spread": g.realized_net_spread,
                    "realized_avg_net_spread": g.realized_avg_net_spread,
                    "realized_kpi_only_completed": g.kpi_realized_only_completed,
                    # 盯市: 未回摆轮按 14:57 内最后一根平掉
                    "mtm_rounds": g.mtm_rounds,
                    "mtm_net_spread": g.mtm_net_spread,
                    "mtm_avg_net_spread": g.mtm_avg_net_spread,
                    # 期望值: 上面两栏合起来除以 rounds
                    "expectancy": g.expectancy,
                }
                for g in self.groups
            ],
            "expectancy_definition": EXPECTANCY_DEFINITION,
            "virtual_lot_assumption": VIRTUAL_LOT_ASSUMPTION,
            "max_rounds_per_day": C.MAX_ROUNDS_PER_DAY,
            "rounds_count_field": C.ROUNDS_CONSUMED_FIELD,
            "cost_bp_tier": COST_DESIGN_PENDING,
            "limit_lock_source": C.LIMIT_LOCK_SOURCE_PENDING,
        }


# ---------------------------------------------------------------- 内部工具


def _band_column(mode: str, path: str) -> str:
    """模式 × 路径 → 用哪一列当"入场标记"。

    `sell_first` 卖出要卖在均价上方 → 上带; `buy_first` 买入要买在均价下方 → 下带。

    - `sell_first` + A → `signal_intraday_avg_cross_up`（价格上穿均价 = 卖点边沿）
    - `buy_first`  + A → `signal_intraday_avg_cross_down`（价格下穿均价 = 买点边沿）
    - `sell_first` + B → `gate_enter_band_up`
    - `buy_first`  + B → `gate_enter_band_down`

    路径 A 的**入场列是边沿**，还需要一列"确实离开均价"的确认（`_confirm_columns`）：
    上穿只说明"此刻在均价上方"，卖点还要求价格进到 +ε 带，否则一次贴着均价抖动的
    穿越会被记成一笔实际不存在的价差。确认之后的成交与出场见 `_confirmed_entry_bar`。
    """
    if path == PATH_A:
        return (
            "signal_intraday_avg_cross_up"
            if mode == C.SELL_FIRST
            else "signal_intraday_avg_cross_down"
        )
    if path == PATH_B:
        return "gate_enter_band_up" if mode == C.SELL_FIRST else "gate_enter_band_down"
    raise ValueError(f"未知路径: {path!r}")


def _confirm_columns(mode: str, path: str) -> tuple[str, ...]:
    """路径 A 的"确实离开均价"确认列（侧面由 `entry_side` 决定）。

    为什么路径 A 需要确认：`avg_cross_up` 只比较**相邻两根** price 与 vwap 的相对
    位置，价格只比均价高 0.01% 也成立。把这种边沿直接当成交点，回测会记下一堆
    无法成交的"价差"。所以要求边沿之后价格**继续朝同一侧离开均价**、进到 ε 带。

    - `sell_first` + A（上穿即卖点边沿）→ 同侧 **+ε 带**（`gate_enter_band_up`）
    - `buy_first`  + A（下穿即买点边沿）→ 同侧 **−ε 带**（`gate_enter_band_down`）

    ## 时序关系是「边沿在前、进带在后」，不是「同一根同时成立」

    这两个条件如果要求**同一根 bar 同时成立**，A 路径在结构上就是空集：

    - `cross_up` 成立的那根，price 只比 vwap 高一点点（它刚越过去），
      通常达不到 +ε 带；
    - 等 price 真的到了 +ε 带，`cross_up` 早就过去了。

    所以确认的语义是**因果的**：边沿那根在先，当日之内**之后某一根**进到 ε 带，
    边沿才算数。实现见 `_confirmed_entry_bar()`。这样 A 路径既不会因浮点噪音
    凭空多出成交点，也不会因为两个条件互斥而恒为空。

    路径 B 自身已经带 ε，不需要附加确认。
    """
    if path != PATH_A:
        return ()
    return ("gate_enter_band_up",) if mode == C.SELL_FIRST else ("gate_enter_band_down",)


def _confirmed_entry_bars(
    day_sorted: pl.DataFrame, *, flag: str, confirm: tuple[str, ...]
) -> list[Any]:
    """当日**全部**合格的入场 bar（按时间升序）。

    返回空列表表示当日没有合格入场点（边沿没出现，或出现了但价格至终没离开均价
    —— 后者正是「贴着均价抖动」那种不该记成交的情形）。

    为什么返回**多个**而不是第一个：同一 (symbol, 交易日) 在同一格里可能被价格
    反复穿越（每次穿越且之后进带，都是一次独立的入场候选）。原来的实现只取第一
    个，于是 `MAX_ROUNDS_PER_DAY=1` 永远撞不到，`skipped_quota` 变成死代码。
    现在把候选全列出来，再由格子内配额按一日一轮取前 N 个、其余记 `skipped_quota`
    —— 这才让那个计数器有真实含义。

    路径 A 的确认是**因果的**：只认「边沿之后」的进带。边沿之前就进过带不算数，
    因为那时价格还没穿越，那根带是上一段行情的尾巴。
    """
    hits = day_sorted.filter(pl.col(flag).fill_null(False))
    if hits.is_empty():
        return []
    candidates: list[Any] = hits["datetime"].to_list()
    if not confirm:
        # 路径 B: 带列本身已带 ε 且「首次进带」只标一根。它**自己就是**确认 bar
        # —— 进带那一刻信息已完整，不需要再等别的确认。
        return candidates

    # 路径 A: 边沿只说明"越过均价"，还要等它之后进到 ε 带。**确认 bar 是那一根进带**，
    # 不是边沿那根 —— 这正是本批次修掉的 look-ahead。
    #
    # 相同时刻的取舍（刻意的）：`c > edge_ts` 是**严格**晚于。若同一根 bar 同时
    # 迈过均价又落进 ε 带，这一根既不是"边沿之后"，也就不构成独立确认 ——
    # 返回空、当天不成交。宁可漏一轮，不把"看过这根收盘才知道的事"记到它自己头上。
    # `test_band_on_the_same_bar_as_the_cross_is_not_a_confirmation` 钉住这条。
    ts = day_sorted["datetime"]
    confirmed = pl.all_horizontal(
        [pl.col(c).fill_null(False) for c in confirm]
    ).alias("_confirmed")
    confirmed_ts = day_sorted.select(ts.alias("_ts"), confirmed).filter(
        pl.col("_confirmed")
    )["_ts"]

    if not confirmed_ts.len():
        return []
    confirmed_list = sorted(confirmed_ts.to_list())

    # 每条边沿配"它之后**第一根**进带" —— 取最近的确认 bar, 而不是"只要后面还有
    # 进带就算"。后者会把 09:31 的边沿确认成 14:50 的进带，等于用收盘前的信息做
    # 早盘的决策，是另一种形态的 look-ahead。
    confirms: list[Any] = []
    for edge_ts in candidates:
        later = [c for c in confirmed_list if c > edge_ts]
        if later:
            confirms.append(later[0])
    # 确认 bar 可能被多条边沿共用, 去重后保持时间序。
    seen: set[Any] = set()
    out: list[Any] = []
    for c in confirms:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _confirmed_entry_bar(
    day_sorted: pl.DataFrame, *, flag: str, confirm: tuple[str, ...]
) -> Any | None:
    """当日第一个**确认** bar；没有则 `None`。

    注意返回的是**确认 bar**（见 `_confirmed_entry_bars`），不是成交 bar。成交发生
    在它的下一根 —— 调用方拿这个值再去 `_simulate_one()` 才拿到成交价。
    便捷封装，语义等同列表版的首元素；回测主循环用列表版（它需要知道"还有几个候选
    被配额挡掉了"），这里留给单点查询的调用方。
    """
    found = _confirmed_entry_bars(day_sorted, flag=flag, confirm=confirm)
    return found[0] if found else None


def entry_side(mode: str, path: str) -> str:
    """这一格是「价格朝哪一侧离开了均价才算进场」。

    返回 `"up"` / `"down"`。它决定了两件事：

    1. **成交那一根的所在侧**：`sell_first` 卖在上方、`buy_first` 买在下方。
    2. **出场判定**：从成交价相对 VWAP 的位置出发，出场 = 价格跑到另一侧。

    `sell_first` 先卖后买（卖在均价上方 → 买回在均价下方）→ `"up"`；
    `buy_first` 先买后卖（买在均价下方 → 卖在均价上方）→ `"down"`。
    """
    del path  # 侧面只由模式决定；参数保留是为了调用处读起来完整
    return "up" if mode == C.SELL_FIRST else "down"


def _make_record(symbol: str, trade_date: date, mode: str, path: str, tag: str) -> TRoundRecord:
    """构造一轮的初始记录（leg1 未成交）。"""
    leg1_side, leg2_side = ("sell", "buy") if mode == C.SELL_FIRST else ("buy", "sell")
    return TRoundRecord(
        t_round_id=f"tr.{trade_date.isoformat()}.{symbol}.{mode}.{path}.{tag}",
        symbol=symbol,
        trade_date=trade_date,
        mode=mode,
        trigger_layer_ref=f"offline/{path}",
        feature_frame_ref=f"offline://{path}/{tag}",
        leg1_side=leg1_side,
        leg2_side=leg2_side,
        virtual_position_flag=True,
        position_tag="executable",
        position_qty=LOT_SIZE,
        sellable_old_qty=LOT_SIZE,
        tag_reason="离线虚拟底仓(纸面)",
        metadata={"offline": True, "gate_path": path, "epsilon_pct": C.EPSILON_VALUE_PCT},
    )


def _as_cn(dt: datetime) -> datetime:
    return dt.replace(tzinfo=CN_TZ) if dt.tzinfo is None else dt


def _simulate_one(
    bars: pl.DataFrame,
    *,
    mode: str,
    path: str,
    confirm_ts: datetime,
) -> tuple[str, float | None, datetime | None, float | None, datetime | None, float | None, datetime | None]:
    """在**当日全量已排序** bar 上模拟一轮。

    返回 `(结局, 成交价, 成交时刻, 出场价, 出场时刻, 盯市价, 盯市时刻)`。
    成交时刻由本函数给出，调用方**不要**再自己扫一遍 bar 去推 —— 两个地方各推一次，
    迟早会有一个推错（成交价取这一根、时刻记那一根，审计时根本看不出来）。

    | 结局 | 成交价/时刻 | 出场价/时刻 | 盯市价/时刻 |
    |---|---|---|---|
    | `completed` | ✅ | ✅ | — |
    | `unreverted_at_cutoff` | ✅ | — | ✅ |
    | `data_truncated` | ✅ | — | — |
    | `(无成交)` | None | — | — |

    ## 因果（本批次 ①/⑤a）

    `confirm_ts` 是**确认 bar**（信号的信息在这一根收盘时才齐）。成交不发生在这一根：

    1. **成交 = 确认 bar 的下一根**，成交价取那一根的 `price`。用同一根的价格等于
       假设"看到收盘价的同时就在那个价成交"，是回测里最经典的一类前视。
    2. **出场只在这一根之后找**（`datetime > 成交时刻`）。旧实现从边沿那根之后开始
       找，于是出场可以早于确认 —— 会凭空造出一轮不存在的 T。
    3. 确认 bar 已经是当日最后一根 → 没有下一根可成交 → 这一轮**不成立**，
       返回 `("no_fill", None, ...)`。信号来了却没机会下单，不算一轮 T：它**不建
       记录、不进分母**，只记一个 `no_fill_bar` 计数。

       **不能**记成 `data_truncated`：那样数据没断却被算成"数据问题"，而且会
       稀释期望值的分母（没持仓的轮次除以持仓的轮次）。也**不能**记成
       `unreverted_at_cutoff` —— 那等于凭空造一笔持仓。

    ## 结局判定（本批次 ②）

    - 出场出现在 `_EXIT_CUTOFF` 之前 → `completed`
    - 一路没回摆，且**最后一根 bar 的时刻已过 `_EXIT_CUTOFF`**（交易时段走完了）
      → `unreverted_at_cutoff`（可盯市）
    - 最后一根 bar 的时刻**还没到** `_EXIT_CUTOFF`（数据提前断了）→ `data_truncated`

    旧实现这两个分支是**反的**：docstring 说"没等到出场 = incomplete"，
    代码却把"时段走完"标成 `abandoned`。以代码实际行为为准修名，
    而不是以 docstring 为准 —— 否则改完名字，盯市就会算在数据截断的桶上。
    """
    ordered = bars.sort("datetime")
    ts_col = ordered["datetime"].to_list()
    try:
        confirm_idx = ts_col.index(confirm_ts)
    except ValueError as exc:
        raise ValueError(f"传入序列里没有 {confirm_ts} 这一根; 调用方应先确认确认点存在") from exc

    # ---- 1/2: 成交在确认 bar 的**下一根**
    fill_idx = confirm_idx + 1
    if fill_idx >= len(ts_col):
        return "no_fill", None, None, None, None, None, None
    fill_row = ordered.row(fill_idx, named=True)
    fill_ts = fill_row["datetime"]
    fill_px = float(fill_row["price"])

    # ---- 出场只在成交之后找
    side = entry_side(mode, path)
    for idx in range(fill_idx + 1, len(ts_col)):
        row = ordered.row(idx, named=True)
        ts = row["datetime"]
        if ts.time() > _EXIT_CUTOFF:
            break
        vwap = row["vwap"]
        px = row["price"]
        if vwap is None or px is None or vwap <= 0:
            continue
        offset = (px - float(vwap)) / float(vwap)
        # 出场 = 价格**越到均价的另一侧**，而不是"回到均价这一侧的同侧"。
        #   side="up"  → 成交在均价上方，要 `offset <= 0`（跌到均价及以下）;
        #   side="down"→ 成交在均价下方，要 `offset >= 0`。
        # 不用"相对成交偏移变号"：成交偏移只有 +0.3% 时，价格仍在均价上方
        # (+0.1%) 就已经变号了，那还不算回到另一侧。
        crossed = offset <= 0.0 if side == "up" else offset >= 0.0
        if crossed:
            return "completed", fill_px, fill_ts, float(px), ts, None, None

    # ---- 3: 没回摆 —— 分「走完了」和「断了」
    last_ts = ts_col[-1]
    if last_ts.time() > _EXIT_CUTOFF:
        # 时段走完：按 ts<=14:57:00 的最后一根盯市
        mtm_ts, mtm_px = _mark_price(ordered, ts_col)
        return "unreverted_at_cutoff", fill_px, fill_ts, None, None, mtm_px, mtm_ts
    return "data_truncated", fill_px, fill_ts, None, None, None, None


def _mark_price(
    ordered: pl.DataFrame, ts_col: list[datetime]
) -> tuple[datetime | None, float | None]:
    """盯市用的 `(时刻, 价格)`：`ts <= _EXIT_CUTOFF` 的**最后一根**。

    为什么是最后一根而不是收盘价：14:57 之后是集合竞价前的三分钟，那个价格不可信，
    本包的出场判定本来就不看它。既然决策逻辑不看，盯市也不能看 —— 否则会出现
    "策略在 14:57 认输，账却按 14:59 的价格算"的口径错位。

    找不到合格 bar（全是 14:57 之后）→ `(None, None)`，调用方据此把这一轮记为
    不可盯市，而不是拿一个随便什么价格硬算。
    """
    for idx in range(len(ts_col) - 1, -1, -1):
        if ts_col[idx].time() <= _EXIT_CUTOFF:
            row = ordered.row(idx, named=True)
            px = row["price"]
            if px is None:
                return None, None
            return ts_col[idx], float(px)
    return None, None


def _settle(
    record: TRoundRecord,
    *,
    outcome: str,
    fill_px: float | None,
    exit_px: float | None,
    fill_ts: datetime | None,
    exit_ts: datetime | None,
    mtm_px: float | None = None,
    mtm_ts: datetime | None = None,
    cost_bp_tier: str = COST_DESIGN_PENDING,
) -> TRoundRecord:
    """把一轮推到终态。`completed` 一律经 `complete_round()`，不绕过。

    `fill_ts` 是**成交时刻**（确认 bar 的下一根），不是确认时刻。记录上写的
    `leg1_ts` 必须是真实成交的时刻 —— 写确认时刻会让审计的人以为下单早了一根。

    ## 成本（批次 20261025gcost5c · 5c）

    `cost_bp_tier` 缺省 `design_pending` + `0.0`，即**旧对照路径**：不扣任何成本。
    传敏感性档位（`0bp`/`5bp`/`10bp`/`15bp`/`20bp`）时，金额由
    `costs.cost_amount_for_tier()` 按「两腿均价 × qty」算出，**照样经**
    `complete_round` 登记 —— 这里不自己算 `net = gross - cost`，也不改记录上的
    净价差。理由很直接：绕过 `complete_round` 就等于绕过 5b 闸门（非零档 + 0 的
    silent 0 正是闸门拦下的那个），而"只改报告数字"的假扣成本比不扣更糟 ——
    它看起来像扣过。

    `unreverted_at_cutoff` / `data_truncated` 两个终态**不受**档位影响：它们没有
    实现净价差（`net_spread` 为 None），没有已实现盈亏可扣。未回摆轮的盯市盈亏
    `mtm_pnl` 是"如果按 14:57 价格平掉"的假设值，也**不扣**成本 —— 它不是一次
    真实平仓，扣了会把"假设平仓"和"真实平仓"的成本口径混在一起。这一点在报告的
    `cost_gate_scope` 里明写，别让读者以为 mtm 也扣了。
    """
    if fill_px is None:
        raise ValueError("_settle 不接受无成交的轮次: no_fill 不该建记录(见 _simulate_one docstring)")
    opened = record.with_fields(
        leg1_status="filled",
        leg1_price=fill_px,
        leg1_qty=LOT_SIZE,
        leg1_source_lot="old_lot",
        leg1_ts=_as_cn(fill_ts) if fill_ts is not None else None,
    )

    if outcome == "completed" and exit_px is not None:
        leg2_ok = transition(
            opened,
            "open_leg2",
            leg2_status="filled",
            leg2_source_lot="old_lot",
        ).with_fields(leg2_price=exit_px, leg2_qty=LOT_SIZE)
        # 成本金额: pending 对照走 0.0(旧口径), 敏感性档走费率×名义。
        # 两种情况都**必须**经 complete_round 登记, 由 5b 闸门复核。
        if cost_bp_tier == COST_DESIGN_PENDING:
            cost = RoundCost(cost_amount=0.0, cost_bp_tier=COST_DESIGN_PENDING)
        else:
            cost = RoundCost(
                cost_amount=costs_mod.cost_amount_for_tier(
                    cost_bp_tier, leg1_price=fill_px, leg2_price=exit_px, qty=LOT_SIZE
                ),
                cost_bp_tier=cost_bp_tier,
            )
        return complete_round(
            leg2_ok,
            leg1_price=fill_px,
            leg2_price=exit_px,
            qty=LOT_SIZE,
            # 两条腿都显式传 `old_lot`: `sell_first` 的卖出腿是 leg1, 只传 leg2 等于
            # 卖腿来源留白 —— 那是实盘上根本不可能成立的纸面记录。批次
            # 20261025gtrounds_patch ③ 之后, 漏传会被 complete_round 直接拒绝。
            leg1_source_lot="old_lot",
            leg2_source_lot="old_lot",
            cost=cost,
            leg1_ts=_as_cn(fill_ts) if fill_ts is not None else None,
            leg2_ts=_as_cn(exit_ts) if exit_ts is not None else None,
        )

    if outcome == "unreverted_at_cutoff":
        # 第二腿是**挂单未成交**: 价格从未回到 VWAP 另一侧。先 open_leg2(unfilled)
        # 再转 unreverted_at_cutoff —— 状态机要求走这条边。
        pending = transition(
            opened, "open_leg2", leg2_status="unfilled", leg2_source_lot="old_lot"
        )
        settled = transition(
            pending, "unreverted_at_cutoff", note="走到 14:57 仍未回到 VWAP 另一侧"
        )
        if mtm_px is None:
            # 找不到 14:57 内的价格 → 这一轮不可盯市。**不改状态**（它确实没回摆），
            # 只是 mtm 三个字段留空，摘要里也不会把它算进 mtm。
            return settled
        # 盯市盈亏 = (卖出腿 − 买入腿) × qty，与 complete_round 的方向同一个式子。
        sell_px, buy_px = (
            (fill_px, mtm_px) if record.leg1_side == "sell" else (mtm_px, fill_px)
        )
        return settled.with_fields(
            mtm_pnl=(sell_px - buy_px) * LOT_SIZE,
            mtm_price=mtm_px,
            mtm_ts=_as_cn(mtm_ts) if mtm_ts is not None else None,
        )

    # `data_truncated`: 14:57 之前数据就断了。**不计 mtm** —— 拿不到截点价格，
    # 用断点那根硬算等于把"数据缺失"当成"行情如此"。
    pending = transition(
        opened, "open_leg2", leg2_status="unfilled", leg2_source_lot="old_lot"
    )
    return transition(pending, "data_truncated", note="14:57 之前分钟数据就断了")


# ---------------------------------------------------------------- 主入口


_MODE_ITER: Final = (C.SELL_FIRST, C.BUY_FIRST)
"""遍历顺序固定为「先卖后买 → 先买后卖」。

顺序在这里**不影响结果**：每个格子有自己的世界（配额按 mode×path×symbol×date
隔离），先扫谁都不会抢走谁的名额。写死顺序只是为了让摘要里四格的排列稳定。
"""

_COUNTED_STATUSES: Final = (
    "completed",
    "unreverted_at_cutoff",
    "data_truncated",
    "limit_locked",
)


def run_mode_a_backtest(
    feature_frame: pl.DataFrame,
    *,
    with_sensitivity: bool = False,
    require_prior_cross: bool = True,
) -> BacktestSummary:
    """在特征表上跑模式 A，返回分列摘要。**纯函数，无 IO、无落盘、无推送。**

    参数:
      feature_frame: `features.build_offline_feature_frame()` 的产物。
      with_sensitivity: 保留参数位；敏感性档**不参与**主口径统计（写死忽略），
        传入 True 只是表明调用方要自行另算，摘要里不含敏感性分栏。
      require_prior_cross: 路径 A 是否额外要求"进带之前价格穿越过均价"。
        默认 True。设成 False 时路径 A 退化成"与 B 同一条件"，用来验证一件事：
        **A 与 B 的差别是否只来自这个开关**。这不是"关掉前视"的开关 ——
        前视已经由成交价取下一根修掉了，与这个参数无关。
    """
    del with_sensitivity  # 敏感性不进主摘要 —— 见 gates 模块 docstring
    if feature_frame is None or feature_frame.is_empty():
        return BacktestSummary(
            version=C.design_version(),
            epsilon_pct=C.EPSILON_VALUE_PCT,
            dates=(),
            groups=tuple(_empty_group(m, p) for m in _MODE_ITER for p in PATHS),
            require_prior_cross=require_prior_cross,
        )

    records: list[TRoundRecord] = []
    counters: dict[tuple[str, str], dict[str, int]] = {
        (m, p): dict.fromkeys((*_COUNTED_STATUSES, "skipped_quota", "no_fill_bar"), 0)
        for m in _MODE_ITER
        for p in PATHS
    }
    realized: dict[tuple[str, str], list[float]] = {
        (m, p): [] for m in _MODE_ITER for p in PATHS
    }
    realized_gross: dict[tuple[str, str], float] = {
        (m, p): 0.0 for m in _MODE_ITER for p in PATHS
    }
    mtm: dict[tuple[str, str], list[float]] = {
        (m, p): [] for m in _MODE_ITER for p in PATHS
    }

    # 配额（`MAX_ROUNDS_PER_DAY = 1`）按 (模式, 路径, symbol, 交易日) 计数 ——
    # **四个格子各是独立世界**。
    #
    # 为什么不共享一个 `(symbol, 交易日)` 名额：那样四个格子就会互相抢，抢的顺序
    # （谁先扫到谁拿走）直接写进 A/B 的比较里，等于给路径 A 一个系统性优势。而
    # 任务书要求 A∥B **分列、不混主门禁** —— 带顺序偏置的分列没有可比性。
    #
    # 反事实口径：把每个格子看成"如果只按这一格的条件交易，会怎样"，四个答案
    # 各自独立地受 `MAX_ROUNDS_PER_DAY=1` 约束。**硬规则没有被放宽** —— 每个
    # 世界内部仍然一天一轮；放宽的是"不同世界之间要不要互相排队"，而它们本来就
    # 不该排队：它们不会同时发生（同一时刻只会有一个条件先满足）。
    used: dict[tuple[str, str, str, date], int] = {}

    for (symbol, trade_date), day in feature_frame.group_by(["symbol", "date"], maintain_order=True):
        symbol = str(symbol)
        if not isinstance(trade_date, date):
            raise TypeError(f"date 列必须是 pl.Date, 收到 {type(trade_date)!r}")
        day_sorted = day.sort("datetime")
        for mode in _MODE_ITER:
            for path in PATHS:
                col = _band_column(mode, path)
                if col not in day_sorted.columns:
                    continue
                # 路径 A 要「边沿在前、进带在后」的同日确认；路径 B 直接取进带标记。
                confirm = _confirm_columns(mode, path) if require_prior_cross else ()
                candidates = _confirmed_entry_bars(day_sorted, flag=col, confirm=confirm)
                if not candidates:
                    continue

                # 格子内配额：缺口径与 `Store.count_rounds` 一致——`void_data`
                # 不占名额。纸面模拟不产生 `void_data`, 所以这里等价于"已开过几轮",
                # 但保留同一判据, 免得将来加了 `void_data` 分支后两处口径分叉。
                world = (mode, path, symbol, trade_date)
                allowed = max(0, C.MAX_ROUNDS_PER_DAY - used.get(world, 0))
                taken = candidates[:allowed]
                # 当天在同一世界里还有候选、但配额已满 → 记 `skipped_quota`。
                # 这是 `skipped_quota` 的**唯一**含义：同世界同路径同日多候选撞配额。
                for _ in candidates[len(taken):]:
                    counters[(mode, path)]["skipped_quota"] += 1
                if not taken:
                    continue

                for confirm_ts in taken:
                    used[world] = used.get(world, 0) + 1
                    record = _make_record(symbol, trade_date, mode, path, str(confirm_ts))
                    (outcome, fill_px, fill_ts, exit_px, exit_ts,
                     mtm_px, mtm_ts) = _simulate_one(
                        day_sorted, mode=mode, path=path, confirm_ts=confirm_ts
                    )
                    if fill_px is None:
                        # 确认 bar 在最后一根 → 没有下一根可成交。这一轮**不建记录**:
                        # 没有持仓就没有轮次, 也不进期望值分母。只计数, 让报告能解释
                        # "为什么今天的信号数比轮次数多"。
                        counters[(mode, path)]["no_fill_bar"] += 1
                        continue
                    settled = _settle(
                        record,
                        outcome=outcome,
                        fill_px=fill_px,
                        exit_px=exit_px,
                        fill_ts=fill_ts,
                        exit_ts=exit_ts,
                        mtm_px=mtm_px,
                        mtm_ts=mtm_ts,
                    )
                    records.append(settled)
                    counters[(mode, path)][settled.round_status] += 1
                    if settled.round_status == "completed" and settled.net_spread is not None:
                        realized[(mode, path)].append(float(settled.net_spread))
                        realized_gross[(mode, path)] += float(settled.gross_spread or 0.0)
                    if settled.mtm_pnl is not None:
                        mtm[(mode, path)].append(float(settled.mtm_pnl))

    groups: list[GroupSummary] = []
    for mode in _MODE_ITER:
        for path in PATHS:
            key = (mode, path)
            nets = realized[key]
            mtms = mtm[key]
            counts = counters[key]
            groups.append(
                GroupSummary(
                    mode=mode,
                    path=path,
                    counts=BucketCounts(
                        completed=counts["completed"],
                        unreverted_at_cutoff=counts["unreverted_at_cutoff"],
                        data_truncated=counts["data_truncated"],
                        limit_locked=counts["limit_locked"],
                        skipped_quota=counts["skipped_quota"],
                        no_fill_bar=counts["no_fill_bar"],
                    ),
                    realized_gross_spread=realized_gross[key],
                    realized_net_spread=sum(nets),
                    realized_rounds=len(nets),
                    realized_avg_net_spread=(sum(nets) / len(nets)) if nets else None,
                    mtm_net_spread=sum(mtms),
                    mtm_rounds=len(mtms),
                    mtm_avg_net_spread=(sum(mtms) / len(mtms)) if mtms else None,
                )
            )

    dates = tuple(sorted({d for d in feature_frame["date"].to_list()}))
    return BacktestSummary(
        version=C.design_version(),
        epsilon_pct=C.EPSILON_VALUE_PCT,
        dates=dates,
        groups=tuple(groups),
        records=tuple(records),
        require_prior_cross=require_prior_cross,
    )


def _empty_group(mode: str, path: str) -> GroupSummary:
    return GroupSummary(
        mode=mode,
        path=path,
        counts=BucketCounts(),
        realized_gross_spread=0.0,
        realized_net_spread=0.0,
        realized_rounds=0,
        realized_avg_net_spread=None,
        mtm_net_spread=0.0,
        mtm_rounds=0,
        mtm_avg_net_spread=None,
    )


__all__ = [
    "COST_DESIGN_PENDING",
    "EXPECTANCY_DEFINITION",
    "LOT_SIZE",
    "PATHS",
    "PATH_A",
    "PATH_A_DEFINITION",
    "PATH_B",
    "SESSION_CLOSE",
    "VIRTUAL_LOT_ASSUMPTION",
    "BacktestSummary",
    "BucketCounts",
    "CostSensitivityRow",
    "GroupSummary",
    "RegimeCell",
    "RegimeReport",
    "build_cost_sensitivity",
    "build_regime_report",
    "run_mode_a_backtest",
]