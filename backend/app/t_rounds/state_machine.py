"""做 T 轮次状态机: 转移表 + KPI 硬禁 + 正 T 老底仓硬禁。

## 四条硬约束

1. **未完成 / 已放弃 / 过期复核 / 作废 一律不得进已实现 KPI**
   `count_in_realized_kpi` 只能在 `completed` 时为 True。这是整本账的核心: 把没做成
   的轮次混进"已实现", 等于只统计赢的那部分 —— 而这类偏差从汇总数字上完全看不出来。

2. **正 T 第二腿必须是老底仓**
   `zheng_t` 是"先卖后买"。如果第二腿的份额来源是 `same_day_buy`, 那这一轮实际
   变成了"卖掉今天买的票", 违反 T+1。纸面记录里它看不出来, 实盘上就是个废单。

3. **唯一能进 KPI 的入口是 `complete_round()`**
   不允许直接 `transition(..., "completed")` —— 那个入口不知道净价差算没算完。

4. **同 `(symbol, trade_date)` 超过 `max_rounds_per_day` 的轮次拒绝**
   计数口径是"**消耗掉轮次**"的状态: 已开腿的都算(含 completed / incomplete /
   abandoned / expired_review), 只有 `void_data` 不算 —— 数据异常那一轮什么都没发生,
   不该占掉当天的名额。

## 与 `buy_sell_signals.state_machine` 的关系

结构上参照了那个模块与 `mining_jobs` 的"非法转移抛专用异常"模式, 但转移表、状态名、
业务含义**全部独立**, 没有任何 import 关系。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from app.market_time import cn_now

from . import constants as C
from .models import TRoundRecord, TRoundValidationError


class TRoundStateError(RuntimeError):
    """状态机相关错误的基类。"""


class InvalidTRoundTransitionError(TRoundStateError):
    """非法的轮次状态转移。对标 `InvalidMiningStatusTransitionError`。"""

    def __init__(self, *, t_round_id: str, source: str, target: str, detail: str = "") -> None:
        self.t_round_id = t_round_id
        self.source = source
        self.target = target
        self.detail = detail
        message = f"轮次 {t_round_id!r} 不允许从 {source!r} 转移到 {target!r}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class RealizedKpiRejected(TRoundStateError):
    """试图把不该进已实现 KPI 的轮次计进去。"""


class RoundQuotaExceeded(TRoundStateError):
    """同 `(symbol, trade_date)` 的轮次已用满 `max_rounds_per_day`。"""


# ---------------------------------------------------------------------------
# 转移表
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    # 第一腿成交, 等第二腿。可以放弃, 也可以作废; **不能直接 completed**
    # (第二腿还没发生)。
    "open_leg1": frozenset({"open_leg2", "abandoned", "void_data"}),
    # 第二腿成交, 等结算。可以放弃 / 判为未完成 / 作废。
    "open_leg2": frozenset({"incomplete", "abandoned", "void_data"}),
    # 未完成 → 过期复核(等人工看), 或直接作废。
    "incomplete": frozenset({"expired_review", "void_data"}),
    # 终态之间不互相流转。
    "completed": frozenset(),
    "abandoned": frozenset(),
    "expired_review": frozenset(),
    "void_data": frozenset(),
}
"""合法转移表。

注意 `incomplete → expired_review` 是唯一的"终态之后还能再走一步"的边 ——
因为未完成本身已经是一个结论(第二腿没做成), 而过期复核是"这个结论需要人看一眼"。
其余终态都没有出边。
"""

STATUSES_CONSUMING_QUOTA: Final = frozenset(
    {
        "open_leg1",
        "open_leg2",
        "completed",
        "incomplete",
        "abandoned",
        "expired_review",
    }
)
"""消耗当日轮次名额的状态。

`void_data` **不在**里面: 数据异常那一轮什么都没发生(可能第一腿都没真成交就被判
脏了), 不该占掉当天唯一的名额。其余只要是"真的开过腿"的, 无论成败都算用掉一轮 ——
否则"一轮失败就重来"会让单日轮次上限形同虚设。
"""


def assert_transition_allowed(record: TRoundRecord, target: str) -> None:
    """只检查不执行。"""
    if target not in C.ROUND_STATUSES:
        raise TRoundValidationError(f"未知目标状态: {target!r}")

    if target == "completed":
        raise InvalidTRoundTransitionError(
            t_round_id=record.t_round_id,
            source=record.round_status,
            target=target,
            detail="completed 只能由 complete_round() 进入(那里才检查净价差与正 T 老底仓)",
        )

    allowed = ALLOWED_TRANSITIONS.get(record.round_status, frozenset())
    if target not in allowed:
        raise InvalidTRoundTransitionError(
            t_round_id=record.t_round_id,
            source=record.round_status,
            target=target,
            detail=f"合法目标: {sorted(allowed) or '无(终态)'}",
        )


def transition(
    record: TRoundRecord,
    target: str,
    *,
    note: str | None = None,
    leg1_status: str | None = None,
    leg2_status: str | None = None,
    leg2_source_lot: str | None = None,
) -> TRoundRecord:
    """推进到目标状态, 返回**新**记录。

    `note` 用于 `abandoned` / `incomplete` / `void_data`: 这三类都必须留下原因,
    否则下次看到这条记录的人不知道当时发生了什么。
    """
    assert_transition_allowed(record, target)

    if target in ("abandoned", "incomplete", "void_data") and not (note and note.strip()):
        raise TRoundValidationError(f"转入 {target} 必须提供 note 说明原因")

    changes: dict[str, object] = {"round_status": target, "last_status_change_at": cn_now()}
    if target == "abandoned":
        changes["abandoned_reason"] = note.strip()
    if target == "incomplete":
        changes["incomplete_reason"] = note.strip()
        changes["incomplete_flag"] = True
    if note and target in ("void_data",):
        meta = dict(record.metadata)
        meta["last_void_reason"] = note.strip()
        changes["metadata"] = meta

    if leg1_status is not None:
        if leg1_status not in C.LEG_STATUSES:
            raise TRoundValidationError(f"未知 leg1_status: {leg1_status!r}")
        changes["leg1_status"] = leg1_status
    if leg2_status is not None:
        if leg2_status not in C.LEG_STATUSES:
            raise TRoundValidationError(f"未知 leg2_status: {leg2_status!r}")
        changes["leg2_status"] = leg2_status
    if leg2_source_lot is not None:
        _check_source_lot(record.mode, leg2_source_lot)
        changes["leg2_source_lot"] = leg2_source_lot

    # 任何非 completed 的目标都必须把 KPI 标记清掉, 不能靠"反正构造时校验过"。
    # 一条记录可能是从 completed 之外的分支拼出来的, 清一遍最省事也最安全。
    if target != "completed":
        changes["count_in_realized_kpi"] = False
        changes["net_spread_realized"] = False

    return record.with_fields(**changes)


def _check_source_lot(mode: str, source_lot: str) -> None:
    if source_lot not in C.SOURCE_LOTS:
        raise TRoundValidationError(f"未知 leg2_source_lot: {source_lot!r}")
    if mode == "zheng_t" and source_lot != "old_lot":
        raise InvalidTRoundTransitionError(
            t_round_id="<pending>",
            source="open_leg2",
            target="completed",
            detail=(
                f"zheng_t 的第二腿份额来源必须是 'old_lot', 收到 {source_lot!r}"
                " —— same_day_buy 是当日买入份额, 当日卖出违反 T+1"
            ),
        )


# ---------------------------------------------------------------------------
# 唯一完成入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoundCost:
    """一轮的成本明细。

    **[占位]** 本批次不做分档成本实测, 所以 `cost_bp_tier` 只能填
    `design_pending`。要填真实分档, 得先有分档成本实测数据。
    """

    cost_amount: float = 0.0
    cost_bp_tier: str = "design_pending"


def complete_round(
    record: TRoundRecord,
    *,
    leg1_price: float,
    leg2_price: float,
    qty: int,
    leg2_source_lot: str,
    cost: RoundCost | None = None,
    leg1_ts: datetime | None = None,
    leg2_ts: datetime | None = None,
) -> TRoundRecord:
    """唯一能把轮次推进到 `completed` 并计入已实现 KPI 的入口。

    四道检查:

    1. 当前状态必须是 `open_leg2`(两腿都已成交才谈得上完成)
    2. `zheng_t` 的 `leg2_source_lot` 必须是 `old_lot`
    3. 两腿价格 / 数量必须有限且为正
    4. 成本只能是 `design_pending`(本批次没有实测分档)

    净价差按**方向**算, 不是简单相减:

    - 正 T (先卖后买): `(leg1_price - leg2_price) * qty` 才是赚的
    - 倒 T (先买后卖): `(leg2_price - leg1_price) * qty` 才是赚的

    写错方向的后果是全部符号反过来 —— 一个永远亏的策略看起来很诚实, 只是没人会
    注意到它其实是反的。
    """
    if record.round_status != "open_leg2":
        raise InvalidTRoundTransitionError(
            t_round_id=record.t_round_id,
            source=record.round_status,
            target="completed",
            detail="只有 open_leg2(两腿均已成交)才能结算完成",
        )

    _check_source_lot(record.mode, leg2_source_lot)

    for name, value in (("leg1_price", leg1_price), ("leg2_price", leg2_price)):
        if not isinstance(value, (int, float)) or value <= 0:
            raise TRoundValidationError(f"{name} 必须是正数")
    if not isinstance(qty, int) or qty <= 0:
        raise TRoundValidationError("qty 必须是正整数")

    cost = cost or RoundCost()
    # 本批次没有分档成本实测: 允许的档位只有 design_pending。真要填实测档位,
    # 得先有成本实测数据, 并在 constants 里登记合法取值 —— 不是随便写个字符串。
    if cost.cost_bp_tier != "design_pending":
        raise TRoundValidationError(
            f"cost_bp_tier 本批次只允许 'design_pending', 收到 {cost.cost_bp_tier!r}"
            "(分档成本实测本轮不做)"
        )
    if not isinstance(cost.cost_amount, (int, float)) or cost.cost_amount < 0:
        raise TRoundValidationError("cost_amount 必须是非负数")

    if record.mode == "zheng_t":
        gross = (float(leg1_price) - float(leg2_price)) * qty
    else:
        gross = (float(leg2_price) - float(leg1_price)) * qty
    net = gross - float(cost.cost_amount)

    now = cn_now()
    return record.with_fields(
        round_status="completed",
        leg1_status="filled",
        leg2_status="filled",
        leg1_price=float(leg1_price),
        leg2_price=float(leg2_price),
        leg1_qty=qty,
        leg2_qty=qty,
        leg1_ts=leg1_ts or record.leg1_ts or now,
        leg2_ts=leg2_ts or record.leg2_ts or now,
        leg2_source_lot=leg2_source_lot,
        gross_spread=gross,
        cost_amount=float(cost.cost_amount),
        cost_bp_tier=cost.cost_bp_tier,
        net_spread=net,
        net_spread_realized=True,
        count_in_realized_kpi=True,
        incomplete_flag=False,
        last_status_change_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# 轮次上限
# ---------------------------------------------------------------------------


def check_round_quota(existing: list[TRoundRecord], symbol: str, trade_date: date) -> None:
    """检查同 `(symbol, trade_date)` 是否还有轮次名额。超了就抛 `RoundQuotaExceeded`。

    `existing` 由调用方(Store)给出, 这样可以避免这里反向依赖 Store。
    """
    used = sum(
        1
        for rec in existing
        if rec.symbol == symbol
        and rec.trade_date == trade_date
        and rec.round_status in STATUSES_CONSUMING_QUOTA
    )
    if used >= C.MAX_ROUNDS_PER_DAY:
        raise RoundQuotaExceeded(
            f"{symbol} 在 {trade_date.isoformat()} 已用掉 {used} 轮"
            f"(max_rounds_per_day={C.MAX_ROUNDS_PER_DAY}); 同票同日不允许多轮"
        )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidTRoundTransitionError",
    "RealizedKpiRejected",
    "RoundCost",
    "RoundQuotaExceeded",
    "STATUSES_CONSUMING_QUOTA",
    "TRoundStateError",
    "assert_transition_allowed",
    "check_round_quota",
    "complete_round",
    "transition",
]
