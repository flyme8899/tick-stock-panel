"""做 T 轮次记录。

一条记录回答的是「这一轮 T 做到哪一步了、净价差多少、能不能计入已实现 KPI」,
不回答「今天该不该做 T」。后者是触发层与分钟评估的事, 本包只提供骨架与闸门。

字段分组对应 PHASE1 §3–5。**标 [占位]** 的字段本批次只有形状与默认值, 不参与
任何计算 —— 估值/健康/成本这类东西没有实测就不该产出数字。

## 三条硬规则写进了 `__post_init__`, 不是写在文档里

文档里的"应当"没有人执行。所以下面三件事在记录构造时就会**直接抛错**:

1. 非 `completed` 状态不得 `count_in_realized_kpi=True` 或 `net_spread_realized=True`
2. `zheng_t` 的第二腿不允许 `leg2_source_lot="same_day_buy"`
3. `formal_push_allowed` 恒 False, `signal_universe_filtered_by_position` 恒 False

第 1 条是这本账的全部意义: `incomplete` / `abandoned` / `expired_review` 的轮次
如果进了已实现 KPI, 结论一定会偏 —— 因为没做成的那一轮没有净价差, 而做成了的
有; 把它们混进同一个"已实现"里, 等于只统计赢的那部分。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from app.market_time import cn_now

from . import constants as C
from . import disclaimer as _disclaimer


class TRoundValidationError(ValueError):
    """记录本身不合法(而非状态转移非法)。"""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise TRoundValidationError(message)


@dataclass(frozen=True, slots=True)
class LegState:
    """一条腿的状态。

    两腿同构, 用同一个类型: 正 T 与倒 T 的差别只在 `side` 与 `source_lot`,
    不需要两套字段。
    """

    side: str
    status: str = "pending"
    qty: int | None = None
    price: float | None = None
    ts: datetime | None = None

    def __post_init__(self) -> None:
        _require(self.side in C.LEG_SIDES, f"未知 leg side: {self.side!r}")
        _require(self.status in C.LEG_STATUSES, f"未知 leg status: {self.status!r}")
        if self.qty is not None:
            _require(isinstance(self.qty, int) and self.qty > 0, "leg qty 必须是正整数")
        if self.price is not None:
            _require(
                isinstance(self.price, (int, float)) and math.isfinite(float(self.price)) and self.price > 0,
                "leg price 必须是有限正数",
            )
        if self.ts is not None:
            _require(isinstance(self.ts, datetime) and self.ts.tzinfo is not None, "leg ts 必须带时区")

    @property
    def filled(self) -> bool:
        return self.status == "filled"


@dataclass(frozen=True, slots=True)
class TRoundRecord:
    """一轮做 T 的记录。`frozen=True`: 状态只能经 `state_machine` 改。"""

    # ---- 身份 / 模式 ----------------------------------------------------
    t_round_id: str
    symbol: str
    trade_date: date
    mode: str
    trigger_layer_ref: str = ""
    feature_frame_ref: str = ""
    """[占位] 特征帧引用。本批次**不接线** `build_feature_frame`, 留空串。"""
    version: str = field(default_factory=C.design_version)
    created_at: datetime = field(default_factory=cn_now)
    updated_at: datetime = field(default_factory=cn_now)

    max_rounds_per_day: int = C.MAX_ROUNDS_PER_DAY
    """恒 1。不是可配置项 —— 见 constants.MAX_ROUNDS_PER_DAY。"""

    # ---- 腿 -------------------------------------------------------------
    leg1_side: str = "sell"
    leg1_status: str = "pending"
    leg1_qty: int | None = None
    leg1_price: float | None = None
    leg1_ts: datetime | None = None

    leg2_side: str = "buy"
    leg2_status: str = "pending"
    leg2_qty: int | None = None
    leg2_price: float | None = None
    leg2_ts: datetime | None = None
    leg2_source_lot: str | None = None
    leg1_same_feature_frame: bool = True
    """[占位] 两腿是否用同一特征帧。True = 同一帧, 便于事后对齐; 本批次不读帧。"""

    abandoned_reason: str | None = None
    incomplete_reason: str | None = None

    # ---- 轮次状态 -------------------------------------------------------
    round_status: str = "open_leg1"
    last_status_change_at: datetime = field(default_factory=cn_now)

    # ---- 净价差 / KPI ---------------------------------------------------
    gross_spread: float | None = None
    cost_bp_tier: str | None = None
    """[占位] 分档成本。本轮**不做**分档成本实测, 保持 None。"""
    cost_amount: float | None = None
    net_spread: float | None = None
    net_spread_realized: bool = False
    count_in_realized_kpi: bool = False
    incomplete_flag: bool = False
    abandoned_pnl: float | None = None
    liquidity_tier: str | None = None
    threshold_mult_start: float = 2.0
    """设计起点倍率 2.0。这是一个**设计假设**, 不是实测结论。"""
    threshold_bp_locked: float | None = None

    # ---- ε / 第二腿门禁 [占位] -------------------------------------------
    epsilon_definition: str = C.EPSILON_DEFINITION
    epsilon_value: float = C.EPSILON_VALUE_PCT
    epsilon_locked_in_version: bool = C.EPSILON_LOCKED_IN_VERSION
    leg2_gate_path_a: str | None = None
    leg2_gate_path_b: str | None = None
    leg2_gate_mode: str | None = None
    gate_pass_a: bool | None = None
    gate_pass_b: bool | None = None
    day_regime_intraday: str | None = None

    # ---- Regime 挂点(只引用 stamp, 不平行定义) ----------------------------
    regime_stamp_ref: str | None = None
    regime_allow: bool | None = None
    intraday_direction_symbol: str | None = None
    intraday_direction_sector: str | None = None
    zero_cross_ref_only: bool = True
    trend_filter_version: str | None = None

    # ---- T+1 / 可卖 [占位] -----------------------------------------------
    cost_basis_before: float | None = None
    cost_basis_after: float | None = None
    old_lot_qty_available: int | None = None
    same_day_buy_qty: int | None = None
    virtual_position_flag: bool = True
    """离线虚拟底仓。本批次没有接入真实持仓, 所以**默认 True**。

    True 的含义是: 这条记录描述的是纸面底仓上做的 T, 不涉及真实可卖份额。
    **不得**据此宣称"实盘可卖老股已通" —— 那一整套字段(实际持仓、冻结、T+1
    可用量)本批次都没有。
    """
    stamp_tax_price_ref: str = "design_pending"

    # ---- 持仓标签(信号宇宙不过滤) ----------------------------------------
    position_tag: str = "observe_only"
    position_qty: int | None = None
    sellable_old_qty: int | None = None
    tag_reason: str | None = None
    signal_universe_filtered_by_position: bool = C.SIGNAL_UNIVERSE_FILTERED_BY_POSITION

    # ---- 推送占位(默认关) ------------------------------------------------
    formal_push_allowed: bool = C.FORMAL_PUSH_ALLOWED_DEFAULT
    paper_track_only: bool = C.PAPER_TRACK_ONLY_DEFAULT
    t_monitor_event_type: str = C.T_MONITOR_EVENT_TYPE

    # ---- 合规 -----------------------------------------------------------
    not_investment_advice: bool = _disclaimer.NOT_INVESTMENT_ADVICE
    mcp_trade_forbidden: bool = _disclaimer.MCP_TRADE_FORBIDDEN

    metadata: dict[str, Any] = field(default_factory=dict)

    # ================================================================ 校验

    def __post_init__(self) -> None:
        _require(isinstance(self.t_round_id, str) and self.t_round_id.strip(), "t_round_id 不能为空")
        _require(isinstance(self.symbol, str) and self.symbol.strip(), "symbol 不能为空")
        _require(isinstance(self.trade_date, date), "trade_date 必须是 date")
        _require(self.mode in C.T_MODES, f"未知 mode: {self.mode!r}")
        _require(self.round_status in C.ROUND_STATUSES, f"未知 round_status: {self.round_status!r}")
        _require(self.position_tag in C.POSITION_TAGS, f"未知 position_tag: {self.position_tag!r}")
        _require(self.stamp_tax_price_ref in C.STAMP_TAX_PRICE_REFS, f"未知 stamp_tax_price_ref: {self.stamp_tax_price_ref!r}")

        # 恒真 / 恒假三件套
        _require(self.max_rounds_per_day == C.MAX_ROUNDS_PER_DAY, "max_rounds_per_day 恒为 1")
        _require(self.formal_push_allowed is False, "formal_push_allowed 恒为 False(本批次禁止正式推送)")
        _require(
            self.signal_universe_filtered_by_position is False,
            "signal_universe_filtered_by_position 恒为 False(信号宇宙不按持仓过滤)",
        )
        _require(self.not_investment_advice is True, "not_investment_advice 恒为 True")
        _require(self.mcp_trade_forbidden is True, "mcp_trade_forbidden 恒为 True")
        _require(self.zero_cross_ref_only is True, "zero_cross_ref_only 恒为 True(只引用, 不平行定义 Regime)")

        # ε 必须与常量一致: 允许记录上写别的 ε, 等于允许某条记录悄悄换口径。
        _require(
            self.epsilon_value == C.EPSILON_VALUE_PCT,
            f"epsilon_value 必须是 {C.EPSILON_VALUE_PCT}, 收到 {self.epsilon_value}",
        )
        _require(
            self.epsilon_definition == C.EPSILON_DEFINITION,
            f"epsilon_definition 必须是 {C.EPSILON_DEFINITION!r}",
        )

        for name in ("created_at", "updated_at", "last_status_change_at"):
            value = getattr(self, name)
            _require(isinstance(value, datetime) and value.tzinfo is not None, f"{name} 必须是带时区的 datetime")

        for name in ("gross_spread", "net_spread", "cost_amount", "abandoned_pnl", "threshold_bp_locked",
                     "cost_basis_before", "cost_basis_after", "leg1_price", "leg2_price"):
            value = getattr(self, name)
            _require(
                value is None or (isinstance(value, (int, float)) and math.isfinite(float(value))),
                f"{name} 必须是有限数或 None",
            )

        # 硬规则 1: 非 completed 不得进已实现 KPI。
        if self.round_status not in C.REALIZED_KPI_ELIGIBLE_STATUS:
            _require(
                self.count_in_realized_kpi is False,
                f"round_status={self.round_status!r} 不得 count_in_realized_kpi=True",
            )
            _require(
                self.net_spread_realized is False,
                f"round_status={self.round_status!r} 不得 net_spread_realized=True",
            )

        # 硬规则 2: 正 T 第二腿必须是老底仓。
        if self.mode == "zheng_t" and self.leg2_source_lot is not None:
            _require(
                self.leg2_source_lot == "old_lot",
                f"zheng_t 的 leg2_source_lot 必须是 'old_lot', 收到 {self.leg2_source_lot!r}"
                "(same_day_buy 是当日买入份额, 不能当日卖出)",
            )

        # 结构性一致性: incomplete / abandoned 必须带原因, 否则事后没人知道发生了什么。
        if self.round_status == "incomplete":
            _require(bool((self.incomplete_reason or "").strip()), "round_status='incomplete' 必须提供 incomplete_reason")
        if self.round_status == "abandoned":
            _require(bool((self.abandoned_reason or "").strip()), "round_status='abandoned' 必须提供 abandoned_reason")

        # 腿的方向必须与模式匹配。
        expected = _expected_leg_sides(self.mode)
        _require(
            (self.leg1_side, self.leg2_side) == expected,
            f"mode={self.mode!r} 的腿方向必须是 {expected}, 收到 ({self.leg1_side}, {self.leg2_side})",
        )

    # ================================================================ 派生

    @property
    def is_terminal(self) -> bool:
        return self.round_status in C.TERMINAL_STATUSES

    @property
    def kpi_eligible(self) -> bool:
        """能否计入已实现 KPI。派生, 不落库。

        与字段 `count_in_realized_kpi` 的区别: 那个是"这条记录被标记为计入",
        这个是"它**有资格**被计入"。两者都为 True 才真的计入。
        """
        return self.round_status in C.REALIZED_KPI_ELIGIBLE_STATUS

    @property
    def round_key(self) -> tuple[str, date]:
        """轮次计数键。`count_rounds()` 用它统计同日同票的轮次。"""
        return self.symbol, self.trade_date

    # ================================================================ 变换

    def with_fields(self, **changes: Any) -> TRoundRecord:
        changes.setdefault("updated_at", cn_now())
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": C.NAMESPACE,
            "t_round_id": self.t_round_id,
            "version": self.version,
            "symbol": self.symbol,
            "trade_date": self.trade_date.isoformat(),
            "mode": self.mode,
            "max_rounds_per_day": self.max_rounds_per_day,
            "trigger_layer_ref": self.trigger_layer_ref,
            "feature_frame_ref": self.feature_frame_ref,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "leg1": {
                "side": self.leg1_side,
                "status": self.leg1_status,
                "qty": self.leg1_qty,
                "price": self.leg1_price,
                "ts": _iso(self.leg1_ts),
            },
            "leg2": {
                "side": self.leg2_side,
                "status": self.leg2_status,
                "qty": self.leg2_qty,
                "price": self.leg2_price,
                "ts": _iso(self.leg2_ts),
                "source_lot": self.leg2_source_lot,
            },
            "leg1_same_feature_frame": self.leg1_same_feature_frame,
            "abandoned_reason": self.abandoned_reason,
            "incomplete_reason": self.incomplete_reason,
            "round_status": self.round_status,
            "last_status_change_at": self.last_status_change_at.isoformat(),
            "is_terminal": self.is_terminal,
            "kpi_eligible": self.kpi_eligible,
            "gross_spread": self.gross_spread,
            "cost_bp_tier": self.cost_bp_tier,
            "cost_amount": self.cost_amount,
            "net_spread": self.net_spread,
            "net_spread_realized": self.net_spread_realized,
            "count_in_realized_kpi": self.count_in_realized_kpi,
            "incomplete_flag": self.incomplete_flag,
            "abandoned_pnl": self.abandoned_pnl,
            "liquidity_tier": self.liquidity_tier,
            "threshold_mult_start": self.threshold_mult_start,
            "threshold_bp_locked": self.threshold_bp_locked,
            "epsilon_definition": self.epsilon_definition,
            "epsilon_value": self.epsilon_value,
            "epsilon_locked_in_version": self.epsilon_locked_in_version,
            "leg2_gate_path_a": self.leg2_gate_path_a,
            "leg2_gate_path_b": self.leg2_gate_path_b,
            "leg2_gate_mode": self.leg2_gate_mode,
            "gate_pass_a": self.gate_pass_a,
            "gate_pass_b": self.gate_pass_b,
            "day_regime_intraday": self.day_regime_intraday,
            "regime_stamp_ref": self.regime_stamp_ref,
            "regime_allow": self.regime_allow,
            "intraday_direction_symbol": self.intraday_direction_symbol,
            "intraday_direction_sector": self.intraday_direction_sector,
            "zero_cross_ref_only": self.zero_cross_ref_only,
            "trend_filter_version": self.trend_filter_version,
            "cost_basis_before": self.cost_basis_before,
            "cost_basis_after": self.cost_basis_after,
            "old_lot_qty_available": self.old_lot_qty_available,
            "same_day_buy_qty": self.same_day_buy_qty,
            "virtual_position_flag": self.virtual_position_flag,
            "stamp_tax_price_ref": self.stamp_tax_price_ref,
            "position_tag": self.position_tag,
            "position_qty": self.position_qty,
            "sellable_old_qty": self.sellable_old_qty,
            "tag_reason": self.tag_reason,
            "signal_universe_filtered_by_position": self.signal_universe_filtered_by_position,
            "formal_push_allowed": self.formal_push_allowed,
            "paper_track_only": self.paper_track_only,
            "t_monitor_event_type": self.t_monitor_event_type,
            "not_investment_advice": self.not_investment_advice,
            "mcp_trade_forbidden": self.mcp_trade_forbidden,
            "disclaimer": _disclaimer.DISCLAIMER_TEXT,
            "metadata": dict(self.metadata),
        }


def _expected_leg_sides(mode: str) -> tuple[str, str]:
    """模式 → (leg1_side, leg2_side)。

    正 T 先卖后买, 倒 T 先买后卖。写成函数而不是散落的判断, 因为这一条同时被
    `__post_init__` 与 `state_machine` 用到, 两份一定会分叉。
    """
    if mode == "zheng_t":
        return "sell", "buy"
    if mode == "dao_t":
        return "buy", "sell"
    raise TRoundValidationError(f"未知 mode: {mode!r}")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def make_leg(*, side: str, status: str = "pending", qty: int | None = None,
             price: float | None = None, ts: datetime | None = None) -> LegState:
    """构造一条腿的便捷函数。"""
    return LegState(side=side, status=status, qty=qty, price=price, ts=ts)


__all__ = [
    "LegState",
    "TRoundRecord",
    "TRoundValidationError",
    "make_leg",
]
