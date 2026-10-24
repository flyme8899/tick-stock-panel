"""买卖信号注册表记录。

一条记录回答的是「这个信号是什么、现在处于什么状态、能不能对外发布」, 不回答
「今天要不要买」。后者是策略层的事, 本包只提供骨架与闸门。

字段分组对应 PHASE1 §2–2.7:

- 身份: 这个信号叫什么、属于哪个家族
- 状态与推送闸: 它现在在哪一档、能不能推
- 人工闸: 谁在什么时候按下发布, 依据是什么
- 噪声分列: 竞价桶与开盘后桶各算各的
- 健康 / 行业 / 环境挂点: 预留下的接口位, 本批次**只落字段不落规则**
- 合规: 三件套

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Final

from app.market_time import CN_TZ, cn_now

from . import disclaimer as _disclaimer
from .constants import (
    AUCTION_RULE_ID_NONE,
    AUCTION_STAT_BUCKET_0925,
    DISCLAIMER_KEY,
    EVENT_CLASSES,
    INDUSTRY_STANDARD_SW_L1,
    NAMESPACE,
    NOISE_MIX_FORBIDDEN,
    POST_OPEN_NOISE_BUCKETS,
    PUSH_DEDUPE_COOLDOWN_MIN,
    SIGNAL_FAMILIES,
    design_version,
)

SIGNAL_STATUSES: Final = frozenset(
    {
        "candidate",       # 刚注册, 还没验证
        "observe",         # 进入观察, 记录但不发布
        "publishable",     # 人工批准后对外发布 —— 唯一入口是 manual_publish()
        "retired",         # 主动下线
        "data_abnormal",   # 数据异常, 需要人工干预
    }
)

PUSH_GATES: Final = frozenset({"blocked", "manual_pending", "armed", "cooldown", "offline"})

LIMIT_REOPEN_INITIAL_STATUS: Final = "observe"
"""`event_class="limit_reopen"`(炸板/回封)的初始状态。

炸板类事件在构造那一刻就已经进入"记录但不发布"的状态, 不需要调用方再显式走一次
`transition(..., "observe")`。理由是: 少一次必须记得做的动作, 就少一次忘记做。

注意这只改了**起点**, 不是改了通路 —— `state_machine` 里禁止 `limit_reopen` 进入
`publishable` 的双重拦截原样保留。初始 observe 不等于"观察一轮之后就能发布"。
"""


class SignalValidationError(ValueError):
    """记录本身不合法(而非状态转移非法)。

    与 `SignalTransitionError` 分开: 前者是"这条记录构造错了", 后者是"这条记录
    想走一条不该走的路"。混成一个异常类型, 调用方就没法区分"我的数据有问题"和
    "我的流程有问题"。
    """


def _require(condition: object, message: str) -> None:
    if not condition:
        raise SignalValidationError(message)


def _is_finite_optional(value: float | None) -> bool:
    return value is None or (isinstance(value, (int, float)) and math.isfinite(float(value)))


@dataclass(frozen=True, slots=True)
class SignalRecord:
    """一条买卖信号注册表记录。

    `frozen=True` 是刻意的: 状态变化必须经过 `state_machine.transition()`, 那里才有
    合法性检查。允许原地改字段, 就等于允许任何人绕开状态机直接写 `status`。
    """

    # ---- 身份 ----------------------------------------------------------
    signal_id: str
    display_name: str
    family: str
    event_class: str
    spec_ref: str
    version: str = field(default_factory=design_version)
    created_at: datetime = field(default_factory=cn_now)
    updated_at: datetime = field(default_factory=cn_now)

    # ---- 状态 / 推送闸 --------------------------------------------------
    status: str = "candidate"
    """当前状态。

    默认 `candidate`。唯一的例外是 `event_class="limit_reopen"`: 见
    `__post_init__`, 这类信号构造出来就是 `observe`。
    """
    push_gate: str = "manual_pending"
    last_status_change_at: datetime = field(default_factory=cn_now)
    last_pushed_at: datetime | None = None
    push_dedupe_cooldown_until: datetime | None = None
    publish_pointer: bool = False

    # ---- 人工闸 --------------------------------------------------------
    manual_publish_required: bool = True
    manual_publisher: str | None = None
    manual_published_at: datetime | None = None
    manual_note: str | None = None
    auto_promote_forbidden: bool = True

    # ---- 竞价 / 开盘后噪声(分列, 不混桶) --------------------------------
    auction_rule_id: str | None = AUCTION_RULE_ID_NONE
    auction_stat_bucket: str | None = None
    post_open_noise_flag: bool = False
    post_open_noise_bucket: str | None = None

    # ---- 健康 / 行业 / 环境挂点(本批次只落字段) --------------------------
    health_veto_rule_version: str = "design_pending"
    health_veto_hit: bool | None = None
    industry_standard: str = INDUSTRY_STANDARD_SW_L1
    regime_caliber_version: str | None = None
    """环境口径版本。**引用** `regime_builder` 的 stamp, 不在这里平行定义 Regime。"""

    # ---- 质量/估值挂点(由 quality_proxy / valuation_stub 填) -------------
    q_missing_flag: bool | None = None
    score_emitted: bool | None = None
    w_quality: float | None = None
    w_value: float | None = None
    weighting_status: str = "design_pending"

    # ---- 合规 ----------------------------------------------------------
    disclaimer_key: str = DISCLAIMER_KEY
    not_investment_advice: bool = _disclaimer.NOT_INVESTMENT_ADVICE
    mcp_trade_forbidden: bool = _disclaimer.MCP_TRADE_FORBIDDEN

    # ---- 自由扩展 ------------------------------------------------------
    metadata: dict[str, Any] = field(default_factory=dict)

    # ================================================================ 校验

    def __post_init__(self) -> None:
        _require(isinstance(self.signal_id, str) and self.signal_id.strip(), "signal_id 不能为空")
        _require(isinstance(self.display_name, str) and self.display_name.strip(), "display_name 不能为空")
        _require(self.family in SIGNAL_FAMILIES, f"未知 family: {self.family!r}")
        _require(self.event_class in EVENT_CLASSES, f"未知 event_class: {self.event_class!r}")

        # 炸板/回封的初始状态强制为 observe(REVIEW_pr200 建议 1)。
        #
        # 放在 status 合法性校验之前, 因为这一步本身就是"把默认值改成合法值":
        # 调用方什么都不传时 status 是 candidate, 而对这类信号 candidate 不是它该在
        # 的地方。用 object.__setattr__ 是因为 frozen dataclass 的字段只能这样改。
        #
        # 只改默认值, 不改调用方的显式意图: 传了非 candidate 的 status 就原样保留,
        # 由下面的校验决定它合法与否 —— 包括显式传 publishable(仍会被
        # "publishable 必须有人签过字" 拒掉)。这样"忘传 status"和"故意传错"不会
        # 被同一条规则吞掉。
        if self.event_class == "limit_reopen" and self.status == "candidate":
            object.__setattr__(self, "status", LIMIT_REOPEN_INITIAL_STATUS)

        _require(self.status in SIGNAL_STATUSES, f"未知 status: {self.status!r}")
        _require(self.push_gate in PUSH_GATES, f"未知 push_gate: {self.push_gate!r}")

        # 恒真三件套。写成断言而不是可配置字段: 它们被改成 False 的那一刻,
        # 整个包的前提就不成立了。
        _require(self.auto_promote_forbidden is True, "auto_promote_forbidden 必须恒为 True")
        _require(self.not_investment_advice is True, "not_investment_advice 必须恒为 True")
        _require(self.mcp_trade_forbidden is True, "mcp_trade_forbidden 必须恒为 True")

        # 竞价桶与开盘后桶必须分列。
        if self.auction_stat_bucket is not None:
            _require(
                self.auction_stat_bucket == AUCTION_STAT_BUCKET_0925,
                f"auction_stat_bucket 只允许 {AUCTION_STAT_BUCKET_0925!r}, "
                f"收到了 {self.auction_stat_bucket!r}",
            )
            _require(
                self.event_class == "auction",
                f"auction_stat_bucket 只属于 event_class='auction', 当前是 {self.event_class!r}",
            )
        if self.post_open_noise_bucket is not None:
            _require(
                self.post_open_noise_bucket in POST_OPEN_NOISE_BUCKETS,
                f"未知 post_open_noise_bucket: {self.post_open_noise_bucket!r}",
            )
            # 唯一的混桶口子就在这里: 一个 auction 类信号同时带开盘后桶, 说明有人
            # 想把两段样本合成一份统计。直接拒, 不给"先合起来再拆"的机会。
            _require(
                not (self.event_class == "auction" and self.post_open_noise_bucket is not None),
                "event_class='auction' 的记录不得携带 post_open_noise_bucket"
                f"(NOISE_MIX_FORBIDDEN={NOISE_MIX_FORBIDDEN})",
            )

        for name in ("created_at", "updated_at", "last_status_change_at"):
            value = getattr(self, name)
            _require(isinstance(value, datetime), f"{name} 必须是 datetime")
            _require(value.tzinfo is not None, f"{name} 必须是带时区的 datetime (Asia/Shanghai)")

        for name in ("manual_published_at", "last_pushed_at", "push_dedupe_cooldown_until"):
            value = getattr(self, name)
            if value is not None:
                _require(isinstance(value, datetime), f"{name} 必须是 datetime 或 None")
                _require(value.tzinfo is not None, f"{name} 必须是带时区的 datetime")

        for name in ("w_quality", "w_value"):
            _require(
                _is_finite_optional(getattr(self, name)),
                f"{name} 必须是有限数或 None",
            )

        # publishable 与人工闸的一致性: 处于 publishable 就必须有人签过字。
        # 这是防线之二 —— 防线之一是状态机根本不放行没有 checklist 的晋升。
        if self.status == "publishable":
            _require(
                self.manual_published_at is not None and self.manual_publisher,
                "status='publishable' 必须已有 manual_published_at 与 manual_publisher",
            )

    # ================================================================ 派生

    @property
    def push_eligible(self) -> bool:
        """能否推送。**派生**字段, 不落库、不手动设置。

        条件全部满足才为 True: 已发布 / 推送闸已 armed / **当前时间**已过去重冷却。

        注意第三条不是"冷却字段为空": 冷却过期后字段里仍留着那个时间戳(它是历史
        事实, 清了就查不到"上次推到什么时候"), 真正要判断的是"现在有没有过点"。
        写成 `is None` 会让任何推过一次的信号永久不可推。
        """
        return (
            self.status == "publishable"
            and self.push_gate == "armed"
            and not self.push_dedupe_cooldown_active
        )

    @property
    def push_dedupe_cooldown_active(self) -> bool:
        """当前时间是否仍在去重冷却窗口内。"""
        until = self.push_dedupe_cooldown_until
        return until is not None and until > cn_now()

    # ================================================================ 变换

    def with_fields(self, **changes: Any) -> SignalRecord:
        """返回改了若干字段的新记录, 顺带把 `updated_at` 推到当前时间。

        不提供原地修改: `frozen=True` 也拦着。状态机用它产出"转移后"的记录。
        """
        changes.setdefault("updated_at", cn_now())
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好的 dict。

        `push_eligible` 是派生字段, 但也写进去了 —— 下游(将来的只读接口)不该为了
        知道"能不能推"而重新实现一遍判定逻辑。
        """
        return {
            "namespace": NAMESPACE,
            "signal_id": self.signal_id,
            "version": self.version,
            "display_name": self.display_name,
            "family": self.family,
            "event_class": self.event_class,
            "spec_ref": self.spec_ref,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "status": self.status,
            "push_eligible": self.push_eligible,
            "push_gate": self.push_gate,
            "last_status_change_at": self.last_status_change_at.isoformat(),
            "last_pushed_at": _iso_or_none(self.last_pushed_at),
            "push_dedupe_cooldown_until": _iso_or_none(self.push_dedupe_cooldown_until),
            "push_dedupe_cooldown_min": PUSH_DEDUPE_COOLDOWN_MIN,
            "publish_pointer": self.publish_pointer,
            "manual_publish_required": self.manual_publish_required,
            "manual_publisher": self.manual_publisher,
            "manual_published_at": _iso_or_none(self.manual_published_at),
            "manual_note": self.manual_note,
            "auto_promote_forbidden": self.auto_promote_forbidden,
            "auction_rule_id": self.auction_rule_id,
            "auction_stat_bucket": self.auction_stat_bucket,
            "post_open_noise_flag": self.post_open_noise_flag,
            "post_open_noise_bucket": self.post_open_noise_bucket,
            "noise_mix_forbidden": NOISE_MIX_FORBIDDEN,
            "health_veto_rule_version": self.health_veto_rule_version,
            "health_veto_hit": self.health_veto_hit,
            "industry_standard": self.industry_standard,
            "regime_caliber_version": self.regime_caliber_version,
            "q_missing_flag": self.q_missing_flag,
            "score_emitted": self.score_emitted,
            "w_quality": self.w_quality,
            "w_value": self.w_value,
            "weighting_status": self.weighting_status,
            "disclaimer_key": self.disclaimer_key,
            "disclaimer": _disclaimer.DISCLAIMER_TEXT,
            "not_investment_advice": self.not_investment_advice,
            "mcp_trade_forbidden": self.mcp_trade_forbidden,
            "metadata": dict(self.metadata),
        }


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def asof_date(value: date | datetime) -> date:
    """把 asof 归一到北京时间下的日期。

    PIT 查询一律以"北京时间的某一天"为单位: 用 UTC 日期做 asof, 在北京时间 08:00
    之前会整体早一天, 而这段时间恰好是盘前。
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise SignalValidationError("asof 若是 datetime 必须带时区")
        return value.astimezone(CN_TZ).date()
    if isinstance(value, date):
        return value
    raise SignalValidationError(f"asof 必须是 date 或 datetime, 收到 {type(value).__name__}")


__all__ = [
    "PUSH_GATES",
    "SIGNAL_STATUSES",
    "SignalRecord",
    "SignalValidationError",
    "asof_date",
]
