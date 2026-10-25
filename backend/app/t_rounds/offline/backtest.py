"""模式 A 纸面回测摘要：正 T / 倒 T × 路径 A / B，完成 / 未完成 / 已放弃分列。

**仅供研究，不构成投资建议；执行须人工确认。**

## "模式 A" 具体是什么

同一 `(symbol, 交易日)` 内，按路径标记找**一次**入场 + **一次**出场，构成一轮 T：

| 模式 | 入场（leg1） | 出场（leg2） | 赚的条件 |
|---|---|---|---|
| 正 T (`zheng_t`) | 价格首次进入 **上带**（`price >= vwap*(1+ε)`）→ **卖出** | 之后价格回落到 VWAP 之下 → **买回** | 卖价 > 买价 |
| 倒 T (`dao_t`) | 价格首次进入 **下带**（`price <= vwap*(1-ε)`）→ **买入** | 之后价格回升到 VWAP 之上 → **卖出** | 卖价 > 买价 |

出场条件统一为「价格回到 VWAP 的**另一侧**」，不用固定持仓时长 —— 固定时长会把
「等不到回摆」的情况强行记成成交，而那正是该记成 `incomplete` 的情形。

## 状态桶，以及为什么只能这样分

一轮的结局只有三种，分别对应 `t_rounds` 的三个终态：

| 结局 | `round_status` | 进已实现 KPI |
|---|---|---|
| 两腿都成交 | `completed` | ✅（且必须过 `complete_round()`） |
| 入场了但当日收盘前没等到出场 | `incomplete` | ❌ |
| 入场后触发了放弃条件（下文） | `abandoned` | ❌ |

`count_in_realized_kpi` 只在 `completed` 为 True —— 这条**不是本模块新加的规则**，
而是 `TRoundRecord.__post_init__` 与 `complete_round()` 早就钉死的。本模块只是
不绕过它：所有结算都走 `state_machine.complete_round()`，所以"未完成被算进已实现"
这件事在结构上就不可能发生。

## 正 T 的 leg2 必须是老底仓

正 T 是「先卖后买」。卖出的份额必须是**当日之前就持有**的。本包用虚拟底仓
（`virtual_position_flag=True`）做纸面记录，来源标 `old_lot`；若有人把它标成
`same_day_buy`，`complete_round()` 会抛 `InvalidTRoundTransitionError`，那一轮
进不了 `completed`，只会落在 `incomplete` 里。

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

_EXIT_CUTOFF: Final = time(14, 57)
"""最后出场时点。收盘前 3 分钟不再开新仓也不等出场 —— 集合竞价前的价格不可信，
而 `incomplete` 本来就该把"没等到"记下来，没必要为此借一根尾盘bar。"""


# ---------------------------------------------------------------- 结果结构


@dataclass(frozen=True, slots=True)
class BucketCounts:
    """一个 (模式 × 路径) 格子的分桶计数。"""

    completed: int = 0
    incomplete: int = 0
    abandoned: int = 0
    skipped_quota: int = 0
    """同一世界（模式, 路径）同日多候选时，超出 `MAX_ROUNDS_PER_DAY` 被拒的候选数。

    **不是**一轮 T，只计数不建记录。也**不是**"被别的格子抢走的名额" —— 配额按
    格子隔离，四个格子各跑各的（见模块 docstring「四个格子各是独立世界」）。
    """

    @property
    def rounds(self) -> int:
        """真正建出记录的轮次数（不含被配额拒掉的）。"""
        return self.completed + self.incomplete + self.abandoned

    @property
    def resolution_rate(self) -> float | None:
        """完成率 = completed / rounds。`rounds=0` → None（不是 0）。"""
        return None if self.rounds == 0 else self.completed / self.rounds


@dataclass(frozen=True, slots=True)
class GroupSummary:
    """(模式, 路径) 的摘要。`realized_net_spread` **只含 completed**。"""

    mode: str
    path: str
    counts: BucketCounts
    realized_gross_spread: float
    realized_net_spread: float
    realized_rounds: int
    realized_avg_net_spread: float | None
    """每轮平均净价差。`realized_rounds=0` → None。"""

    @property
    def kpi_realized_only_completed(self) -> bool:
        """写死 True 并进摘要：提醒读者「已实现」这一栏只有 completed。"""
        return True


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    """整次回测的摘要。`as_dict()` 就是写进报告 JSON 的那个结构。"""

    version: str
    epsilon_pct: float
    dates: tuple[date, ...]
    groups: tuple[GroupSummary, ...]
    records: tuple[TRoundRecord, ...] = field(default=(), repr=False)
    """逐轮记录（纸面）。**不进摘要 JSON**，避免报告体积随样本膨胀。"""

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
            "modes": ["zheng_t", "dao_t"],
            "paths": list(PATHS),
            "groups": [
                {
                    "mode": g.mode,
                    "path": g.path,
                    "completed": g.counts.completed,
                    "incomplete": g.counts.incomplete,
                    "abandoned": g.counts.abandoned,
                    "skipped_quota": g.counts.skipped_quota,
                    "rounds": g.counts.rounds,
                    "resolution_rate": g.counts.resolution_rate,
                    "realized_rounds": g.realized_rounds,
                    "realized_gross_spread": g.realized_gross_spread,
                    "realized_net_spread": g.realized_net_spread,
                    "realized_avg_net_spread": g.realized_avg_net_spread,
                    "realized_kpi_only_completed": g.kpi_realized_only_completed,
                }
                for g in self.groups
            ],
            "virtual_lot_assumption": VIRTUAL_LOT_ASSUMPTION,
            "max_rounds_per_day": C.MAX_ROUNDS_PER_DAY,
            "rounds_count_field": C.ROUNDS_CONSUMED_FIELD,
            "cost_bp_tier": COST_DESIGN_PENDING,
        }


# ---------------------------------------------------------------- 内部工具


def _band_column(mode: str, path: str) -> str:
    """模式 × 路径 → 用哪一列当"入场标记"。

    正 T 卖出要卖在均价上方 → 上带; 倒 T 买入要买在均价下方 → 下带。
    路径 A 是**边沿**（某根才为真）; 路径 B 是**首次进带**（当日至多一根）。

    - `zheng_t` + A → `signal_intraday_avg_cross_up`（价格上穿均价 = 卖点候选）
    - `dao_t`   + A → `signal_intraday_avg_cross_down`（价格下穿均价 = 买点候选）
    - `zheng_t` + B → `gate_enter_band_up`
    - `dao_t`   + B → `gate_enter_band_down`

    路径 A 额外要求的确认列（见 `_simulate_one`）由 `_confirm_columns` 给出：
    上穿只说明"此刻在均价上方"，卖点还要求**价格确实离开了均价**（进到 +ε 带），
    否则一次贴着均价抖动的穿越会被记成一笔实际不存在的价差。
    """
    if path == PATH_A:
        return (
            "signal_intraday_avg_cross_up"
            if mode == "zheng_t"
            else "signal_intraday_avg_cross_down"
        )
    if path == PATH_B:
        return "gate_enter_band_up" if mode == "zheng_t" else "gate_enter_band_down"
    raise ValueError(f"未知路径: {path!r}")


def _confirm_columns(mode: str, path: str) -> tuple[str, ...]:
    """路径 A 的"确实离开均价"确认列（侧面由 `entry_side` 决定）。

    为什么路径 A 需要确认：`avg_cross_up` 只比较**相邻两根** price 与 vwap 的相对
    位置，价格只比均价高 0.01% 也成立。把这种边沿直接当成交点，回测会记下一堆
    无法成交的"价差"。所以要求边沿之后价格**继续朝同一侧离开均价**、进到 ε 带。

    - `zheng_t` + A（上穿即卖点候选）→ 同侧 **+ε 带**（`gate_enter_band_up`）
    - `dao_t`   + A（下穿即买点候选）→ 同侧 **−ε 带**（`gate_enter_band_down`）

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
    return ("gate_enter_band_up",) if mode == "zheng_t" else ("gate_enter_band_down",)


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
        # 路径 B 的带列本身已带 ε，且「首次进带」在 gates 里就只标一根，
        # 所以这里通常只有一个候选。仍按列表返回，保持两种路径同一形状。
        return candidates

    ts = day_sorted["datetime"]
    confirmed = pl.all_horizontal(
        [pl.col(c).fill_null(False) for c in confirm]
    ).alias("_confirmed")
    confirmed_ts = day_sorted.select(ts.alias("_ts"), confirmed).filter(
        pl.col("_confirmed")
    )["_ts"]

    if not confirmed_ts.len():
        return []
    confirmed_list = confirmed_ts.to_list()
    return [
        entry_ts
        for entry_ts in candidates
        if any(c > entry_ts for c in confirmed_list)
    ]


def _confirmed_entry_bar(
    day_sorted: pl.DataFrame, *, flag: str, confirm: tuple[str, ...]
) -> Any | None:
    """当日第一个合格入场 bar；没有则 `None`。

    便捷封装，语义等同 `_confirmed_entry_bars(...)` 的首元素。回测主循环用列表版
    （它需要知道"还有几个候选被配额挡掉了"），这里留给单点查询的调用方。
    """
    found = _confirmed_entry_bars(day_sorted, flag=flag, confirm=confirm)
    return found[0] if found else None


def entry_side(mode: str, path: str) -> str:
    """这一格是「价格朝哪一侧离开了均价才算进场」。

    返回 `"up"` / `"down"`。它决定了两件事：

    1. **入场点**：路径 A 下"信号与 ε 带同一根成立"的那一根；路径 B 下就是进带
       那一根本身。
    2. **出场判定**：从入场那根的**收盘价相对 VWAP 的位置**出发，出场 = 价格跑到
       另一侧（`pct_vs_vwap` 变号）。用"相对位置"而不是"价格绝对值低于 VWAP"，
       是因为 VWAP 是累计均价，会被后段行情拖向现价；只在绝对意义上比较，可能
       在价格明明还高着的时候就判"回到均价下方"，那不是回摆而是均线在追价。

    `zheng_t` 先卖后买（卖在均价上方 → 买回在均价下方）→ `"up"`；
    `dao_t` 先买后卖（买在均价下方 → 卖在均价上方）→ `"down"`。
    """
    del path  # 侧面只由模式决定；参数保留是为了调用处读起来完整
    return "up" if mode == "zheng_t" else "down"


def _make_record(symbol: str, trade_date: date, mode: str, path: str, tag: str) -> TRoundRecord:
    """构造一轮的初始记录（leg1 未成交）。"""
    leg1_side, leg2_side = ("sell", "buy") if mode == "zheng_t" else ("buy", "sell")
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
    entry_ts: datetime,
) -> tuple[str, float | None, float | None, datetime | None]:
    """在**当日全量已排序** bar 上模拟一轮。

    返回 `(结局, 入场价, 出场价, 出场时刻)`；未成交的结局后两项为 `None`。

    入场 bar 由调用方用 `_confirmed_entry_bar()` 定好后**显式传入** `entry_ts`。
    这里不再自己去序列里找"第一根标记为真的 bar" —— A 路径的入场条件是"边沿在前、
    进带在后"的两根关系，压不回单列标记；让 `_simulate_one` 再去猜第一根，等于把
    已经判好的语义丢掉重算一遍。

    出场 = 入场之后第一根价格**跑到均价的另一侧**的 bar，且不晚于 `_EXIT_CUTOFF`。
    """
    at_entry = bars.filter(pl.col("datetime") == entry_ts)
    if at_entry.is_empty():
        raise ValueError(f"传入序列里没有 {entry_ts} 这一根; 调用方应先确认入场点存在")

    entry = at_entry.row(0, named=True)
    entry_px = float(entry["price"])
    # 入场时价格在均价的哪一侧。后面出场就找"越到另一侧"。
    side = entry_side(mode, path)

    rest = bars.filter(pl.col("datetime") > entry_ts)
    for row in rest.iter_rows(named=True):
        ts = row["datetime"]
        if ts.time() > _EXIT_CUTOFF:
            break
        vwap = row["vwap"]
        px = row["price"]
        if vwap is None or px is None or vwap <= 0:
            continue
        offset = (px - float(vwap)) / float(vwap)
        # 出场 = 价格**越到均价的另一侧**，而不是"回到均价这一侧的同侧"。
        #   side="up"  → 入场在均价上方，要 `offset <= 0`（跌到均价及以下）;
        #   side="down"→ 入场在均价下方，要 `offset >= 0`。
        # 不用"相对入场偏移变号"：入场偏移只有 +0.3% 时，价格仍在均价上方
        # (+0.1%) 就已经变号了，那还不算回到另一侧。
        crossed = offset <= 0.0 if side == "up" else offset >= 0.0
        if crossed:
            return "completed", entry_px, float(px), ts

    last = bars["datetime"].max()
    if last is not None and last.time() > _EXIT_CUTOFF:
        return "abandoned", entry_px, None, None
    return "incomplete", entry_px, None, None


def _settle(
    record: TRoundRecord,
    *,
    outcome: str,
    entry_px: float | None,
    exit_px: float | None,
    entry_ts: datetime | None,
    exit_ts: datetime | None,
) -> TRoundRecord:
    """把一轮推到终态。`completed` 一律经 `complete_round()`，不绕过。"""
    if outcome == "completed" and entry_px is not None and exit_px is not None:
        opened = transition(record, "open_leg2", leg2_source_lot="old_lot")
        return complete_round(
            opened,
            leg1_price=entry_px,
            leg2_price=exit_px,
            qty=LOT_SIZE,
            leg2_source_lot="old_lot",
            cost=RoundCost(cost_amount=0.0, cost_bp_tier=COST_DESIGN_PENDING),
            leg1_ts=_as_cn(entry_ts) if entry_ts is not None else None,
            leg2_ts=_as_cn(exit_ts) if exit_ts is not None else None,
        )
    if outcome == "abandoned":
        opened = record.with_fields(leg1_status="filled", leg1_price=entry_px, leg1_qty=LOT_SIZE)
        return transition(opened, "abandoned", note="尾盘未等到出场, 主动放弃(离线纸面)")
    # `incomplete` 在状态机里只能从 `open_leg2` 进入 —— 这不是障碍, 而是状态机在
    # 要求记录"第二腿的状态"。本包纸面模拟里第二腿是**挂单未成交**: 价格从未回到
    # VWAP 另一侧, 所以先 `open_leg2(leg2_status=unfilled)` 再转 `incomplete`。
    # 走这条边而不是 `abandoned`, 才能把"入场了但没等到出场"和"主动放弃"分开。
    opened = record.with_fields(leg1_status="filled", leg1_price=entry_px, leg1_qty=LOT_SIZE)
    pending = transition(
        opened, "open_leg2", leg2_status="unfilled", leg2_source_lot="old_lot"
    )
    return transition(pending, "incomplete", note="当日收盘前未回到 VWAP 另一侧")


# ---------------------------------------------------------------- 主入口


def run_mode_a_backtest(
    feature_frame: pl.DataFrame,
    *,
    with_sensitivity: bool = False,
) -> BacktestSummary:
    """在特征表上跑模式 A，返回分列摘要。**纯函数，无 IO、无落盘、无推送。**

    参数:
      feature_frame: `features.build_offline_feature_frame()` 的产物。
      with_sensitivity: 保留参数位；敏感性档**不参与**主口径统计（写死忽略），
        传入 True 只是表明调用方要自行另算，摘要里不含敏感性分栏。
    """
    del with_sensitivity  # 敏感性不进主摘要 —— 见 gates 模块 docstring
    if feature_frame is None or feature_frame.is_empty():
        return BacktestSummary(
            version=C.design_version(),
            epsilon_pct=C.EPSILON_VALUE_PCT,
            dates=(),
            groups=tuple(_empty_group(m, p) for m in ("zheng_t", "dao_t") for p in PATHS),
        )

    records: list[TRoundRecord] = []
    counters: dict[tuple[str, str], dict[str, int]] = {
        (m, p): {"completed": 0, "incomplete": 0, "abandoned": 0, "skipped_quota": 0}
        for m in ("zheng_t", "dao_t")
        for p in PATHS
    }
    realized: dict[tuple[str, str], list[float]] = {
        (m, p): [] for m in ("zheng_t", "dao_t") for p in PATHS
    }
    realized_gross: dict[tuple[str, str], float] = {
        (m, p): 0.0 for m in ("zheng_t", "dao_t") for p in PATHS
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
        for mode in ("zheng_t", "dao_t"):
            for path in PATHS:
                col = _band_column(mode, path)
                if col not in day_sorted.columns:
                    continue
                # 路径 A 要「边沿在前、进带在后」的同日确认；路径 B 直接取进带标记。
                confirm = _confirm_columns(mode, path)
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

                for entry_ts in taken:
                    used[world] = used.get(world, 0) + 1
                    record = _make_record(symbol, trade_date, mode, path, str(entry_ts))
                    outcome, entry_px, exit_px, exit_ts = _simulate_one(
                        day_sorted, mode=mode, path=path, entry_ts=entry_ts
                    )
                    settled = _settle(
                        record,
                        outcome=outcome,
                        entry_px=entry_px,
                        exit_px=exit_px,
                        entry_ts=entry_ts,
                        exit_ts=exit_ts,
                    )
                    records.append(settled)
                    counters[(mode, path)][settled.round_status] += 1
                    if settled.round_status == "completed" and settled.net_spread is not None:
                        realized[(mode, path)].append(float(settled.net_spread))
                        realized_gross[(mode, path)] += float(settled.gross_spread or 0.0)

    groups: list[GroupSummary] = []
    for mode in ("zheng_t", "dao_t"):
        for path in PATHS:
            key = (mode, path)
            nets = realized[key]
            counts = counters[key]
            groups.append(
                GroupSummary(
                    mode=mode,
                    path=path,
                    counts=BucketCounts(
                        completed=counts["completed"],
                        incomplete=counts["incomplete"],
                        abandoned=counts["abandoned"],
                        skipped_quota=counts["skipped_quota"],
                    ),
                    realized_gross_spread=realized_gross[key],
                    realized_net_spread=sum(nets),
                    realized_rounds=len(nets),
                    realized_avg_net_spread=(sum(nets) / len(nets)) if nets else None,
                )
            )

    dates = tuple(sorted({d for d in feature_frame["date"].to_list()}))
    return BacktestSummary(
        version=C.design_version(),
        epsilon_pct=C.EPSILON_VALUE_PCT,
        dates=dates,
        groups=tuple(groups),
        records=tuple(records),
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
    )


__all__ = [
    "COST_DESIGN_PENDING",
    "LOT_SIZE",
    "PATHS",
    "PATH_A",
    "PATH_B",
    "VIRTUAL_LOT_ASSUMPTION",
    "BacktestSummary",
    "BucketCounts",
    "GroupSummary",
    "run_mode_a_backtest",
]