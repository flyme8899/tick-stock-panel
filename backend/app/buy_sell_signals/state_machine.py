"""信号状态机: 合法转移表 + 硬禁自动晋升。

## 为什么状态机要独立成模块而不是写成几个 if

因为这里的三条硬约束都不是"通常不会这么写"级别的约定, 而是**一旦破了就没人
发现**的那种:

1. `candidate|observe` 不能直接变 `publishable`。中间少了这一步, 效果是某天
   开始一批没有任何人工签字的信号悄悄出现在对外列表里。
2. `limit_reopen`(炸板/回封)永远不许变 `publishable`。这类事件的样本量极不稳定,
   且回封后的走势与统计口径强相关; 在没有专门验证之前, 让它具备发布资格就等于
   允许一条没验证过的规则对外生效。
3. 进了 `data_abnormal` 之后不许"顺手改回来"。数据异常常常意味着前一步的判定
   依据本身是脏的, 静默回到 `candidate` 会把脏数据带入下一轮。

写成表 + 专用异常之后, 违反它们会**当场抛错**, 而不是生成一条看起来正常的记录。

## 与 `mining_jobs` 的关系

参照了 `MiningRunStore` 的"非法转移抛专用异常"模式(`InvalidMiningStatusTransitionError`),
但转移表、状态名、业务含义全部独立, 没有复用那个模块的任何符号。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.market_time import cn_now

from .models import SIGNAL_STATUSES, PUSH_GATES, SignalRecord, SignalValidationError


class SignalStateError(RuntimeError):
    """状态机相关错误的基类。

    继承 `RuntimeError`: 调用方通常是"流程推到这里才发现的错", 而不是输入校验错误。
    """


class SignalTransitionError(SignalStateError):
    """非法的状态转移。

    对标 `mining_jobs.InvalidMiningStatusTransitionError`。消息里带上 from / to /
    signal_id, 不要只写"转移失败" —— 这类错误出现在日志里时, 最需要的就是这三个值。
    """

    def __init__(self, *, signal_id: str, source: str, target: str, detail: str = "") -> None:
        self.signal_id = signal_id
        self.source = source
        self.target = target
        self.detail = detail
        message = f"信号 {signal_id!r} 不允许从 {source!r} 转移到 {target!r}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class ManualPublishRejected(SignalStateError):
    """人工发布被拒(典型原因: checklist 没全勾)。"""


# ---------------------------------------------------------------------------
# 转移表
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    # candidate 只能进观察或数据异常。没有一条通往 publishable 的边。
    "candidate": frozenset({"observe", "retired", "data_abnormal"}),
    # observe 同理: 想发布必须走 manual_publish(), 那不是一个"转移", 是一次带签字的动作。
    "observe": frozenset({"retired", "data_abnormal"}),
    # publishable 可以退回 observe(发现问题先撤下), 也可以退役。
    "publishable": frozenset({"observe", "retired", "data_abnormal"}),
    # 数据异常只能由人工重跑入口处理; 状态机层面只允许退役, 不给"回到 candidate"的边。
    "data_abnormal": frozenset({"retired"}),
    "retired": frozenset(),
}
"""合法转移表。

`publishable` 的入边**故意为空**: 本表里没有任何状态能直接转移到 publishable。
唯一入口是 `manual_publish()`。这样即使有人写 `transition(rec, "publishable")`,
也会拿到一条明确的报错, 而不是"碰巧成功"。
"""

RERUN_STATUS: Final = "candidate"
"""数据异常后人工重跑的目标状态。

`data_abnormal → candidate` 不走 `transition()`, 只能走 `rerun_from_abnormal()`,
后者要求显式的操作者与原因。状态表里没有这条边是刻意的: 让它成为"必须开口说
一句话"的动作。
"""


def assert_transition_allowed(record: SignalRecord, target: str) -> None:
    """只检查不执行。供测试与调用方预检用。"""
    if target not in SIGNAL_STATUSES:
        raise SignalValidationError(f"未知目标状态: {target!r}")

    if target == "publishable":
        # 唯一允许进入 publishable 的入口是 manual_publish()。
        raise SignalTransitionError(
            signal_id=record.signal_id,
            source=record.status,
            target=target,
            detail="publishable 只能由 manual_publish() 进入, 状态机不存在自动晋升路径",
        )

    allowed = ALLOWED_TRANSITIONS.get(record.status, frozenset())
    if target not in allowed:
        raise SignalTransitionError(
            signal_id=record.signal_id,
            source=record.status,
            target=target,
            detail=f"合法目标: {sorted(allowed) or '无(终态)'}",
        )

    # limit_reopen 在任何路径上都不许触及 publishable。上面那条已经拦住了, 这里
    # 再拦一次是刻意的重复: 如果将来有人给 ALLOWED_TRANSITIONS 加上 publishable
    # 的入边, 这条仍然守着炸板类信号。
    if record.event_class == "limit_reopen" and target == "publishable":
        raise SignalTransitionError(
            signal_id=record.signal_id,
            source=record.status,
            target=target,
            detail="event_class='limit_reopen' 禁止转入 publishable",
        )


def transition(record: SignalRecord, target: str, *, note: str | None = None) -> SignalRecord:
    """把记录推进到目标状态, 返回**新**记录(原记录不变)。

    `note` 只用于 `data_abnormal`: 进异常时必须留下原因, 否则下次看到这条记录的人
    不知道当时发生了什么。
    """
    assert_transition_allowed(record, target)
    if target == "data_abnormal" and not (note and note.strip()):
        raise SignalValidationError("转入 data_abnormal 必须提供 note 说明原因")

    now = cn_now()
    gate = record.push_gate
    if target == "publishable":
        gate = "armed"
    elif target in ("retired", "data_abnormal"):
        # 一旦不可发布, 推送闸必须同步落到 blocked/offline。留着 armed 会出现
        # "status 已经不是 publishable 但 push_gate 还是 armed"的自相矛盾记录。
        gate = "blocked" if target == "data_abnormal" else "offline"
    elif target == "observe":
        gate = "manual_pending"

    changes: dict[str, object] = {
        "status": target,
        "push_gate": gate,
        "last_status_change_at": now,
        "updated_at": now,
    }
    if target != "publishable":
        # 离开 publishable 时撤回指针: 指针只应指向"当前可发布"的那一版。
        changes["publish_pointer"] = False
    if note:
        meta = dict(record.metadata)
        meta["last_transition_note"] = note
        changes["metadata"] = meta
    return record.with_fields(**changes)


# ---------------------------------------------------------------------------
# 人工发布闸
# ---------------------------------------------------------------------------

MANUAL_PUBLISH_CHECKLIST: Final[tuple[str, ...]] = (
    "design_reviewed",      # 设计口径已评审
    "backtest_done",        # 有可复现的回测
    "no_lookahead",         # 无未来函数(含 PIT 口径已核)
    "sample_adequate",      # 样本量达标
    "risk_reviewed",        # 风险已评审
    "disclaimer_ack",       # 已确认合规脚注
)
"""发布前必须逐项确认的清单。

**缺任何一项都拒绝**。这里用"必须显式传 True"而不是"默认为 True, 有则覆盖":
默认放行会让漏传参数变成一个静默的通过。

清单内容本身可变更, 但每加一项都要同步 `docs/buy-sell-signals-skeleton.md`。
"""


@dataclass(frozen=True, slots=True)
class PublishDecision:
    """一次人工发布的签字记录。"""

    publisher: str
    note: str
    published_at: datetime
    checklist: dict[str, bool]


def manual_publish(
    record: SignalRecord,
    *,
    publisher: str,
    checklist: dict[str, bool],
    note: str,
) -> SignalRecord:
    """唯一能把信号推进到 `publishable` 的入口。

    三道检查按顺序:

    1. 记录本身允许被发布(`limit_reopen` 直接拒)
    2. checklist 六项**全部显式为 True**
    3. 当前状态是 `observe` —— `candidate` 必须先观察一轮再发布

    `publisher` / `note` 为空也拒。发布是需要问责的动作, 匿名发布等于没有发布。
    """
    if record.event_class == "limit_reopen":
        raise ManualPublishRejected(
            f"信号 {record.signal_id!r} 的 event_class 为 'limit_reopen', 本类信号禁止发布"
        )

    if not isinstance(publisher, str) or not publisher.strip():
        raise ManualPublishRejected("manual_publish 必须提供 publisher")
    if not isinstance(note, str) or not note.strip():
        raise ManualPublishRejected("manual_publish 必须提供 note(发布依据)")

    missing = [item for item in MANUAL_PUBLISH_CHECKLIST if checklist.get(item) is not True]
    if missing:
        raise ManualPublishRejected(
            f"信号 {record.signal_id!r} 的 checklist 未全部确认, 缺: {missing}"
        )
    extra = [key for key in checklist if key not in MANUAL_PUBLISH_CHECKLIST]
    if extra:
        # 不认识的键多半是拼错了名字 —— 拼错的时候那一项其实没被确认, 但看起来
        # 像个完整的 checklist。所以直接拒, 而不是忽略。
        raise ManualPublishRejected(
            f"checklist 含未知项 {extra}; 允许项: {list(MANUAL_PUBLISH_CHECKLIST)}"
        )

    if record.status == "candidate":
        raise ManualPublishRejected(
            f"信号 {record.signal_id!r} 仍处于 candidate, 须先进入 observe 并观察一轮"
        )
    if record.status != "observe":
        raise ManualPublishRejected(
            f"信号 {record.signal_id!r} 当前状态为 {record.status!r}, 只有 observe 可发布"
        )

    now = cn_now()
    return record.with_fields(
        status="publishable",
        push_gate="armed",
        publish_pointer=True,
        manual_publish_required=True,
        manual_publisher=publisher.strip(),
        manual_published_at=now,
        manual_note=note.strip(),
        last_status_change_at=now,
        updated_at=now,
    )


def rerun_from_abnormal(
    record: SignalRecord,
    *,
    operator: str,
    reason: str,
) -> SignalRecord:
    """数据异常 → 重新跑一遍的入口。

    这不是 `transition("candidate")`: 状态表里没有那条边。要求 operator + reason,
    是因为"数据异常"往往意味着上游脏了, 谁在什么情况下判定可以重跑, 后面要能查。
    """
    if record.status != "data_abnormal":
        raise SignalTransitionError(
            signal_id=record.signal_id,
            source=record.status,
            target=RERUN_STATUS,
            detail="rerun_from_abnormal 只接受当前状态为 data_abnormal 的记录",
        )
    if not isinstance(operator, str) or not operator.strip():
        raise SignalValidationError("rerun 必须提供 operator")
    if not isinstance(reason, str) or not reason.strip():
        raise SignalValidationError("rerun 必须提供 reason")

    now = cn_now()
    meta = dict(record.metadata)
    meta["last_rerun"] = {"operator": operator.strip(), "reason": reason.strip(), "at": now.isoformat()}
    return record.with_fields(
        status=RERUN_STATUS,
        push_gate="manual_pending",
        publish_pointer=False,
        last_status_change_at=now,
        updated_at=now,
        metadata=meta,
    )


def set_push_gate(record: SignalRecord, gate: str) -> SignalRecord:
    """调整推送闸。

    闸门只影响"要不要推", 不影响"能不能发布" —— 所以这里不检查 status。但反过来
    有一条: 非 `publishable` 的记录不许被置为 `armed`, 否则会出现"没发布却已就绪
    推送"的记录, 而 `push_eligible` 又要求 status == publishable, 两者永远矛盾。
    """
    if gate not in PUSH_GATES:
        raise SignalValidationError(f"未知 push_gate: {gate!r}")
    if gate == "armed" and record.status != "publishable":
        raise SignalStateError(
            f"信号 {record.signal_id!r} 状态为 {record.status!r}, 不能置为 armed"
        )
    return record.with_fields(push_gate=gate)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "MANUAL_PUBLISH_CHECKLIST",
    "ManualPublishRejected",
    "PublishDecision",
    "RERUN_STATUS",
    "SignalStateError",
    "SignalTransitionError",
    "assert_transition_allowed",
    "manual_publish",
    "rerun_from_abnormal",
    "set_push_gate",
    "transition",
]
