"""买卖信号的推送出口(批次 20261025gpush)。

**仅供研究，不构成投资建议；执行须人工确认。**

本模块只做一件事: 把一条**已经人工发布**的信号, 经**既有**通道扇出一次。

通道(写死, 不得扩):
- **SSE**: `QuoteService.push_alerts(list[dict])` → 内部 `_broadcast_alerts` →
  各 `QuoteSubscriber.push_alerts`。不新建路由、不新建总线、不碰 `sse_starlette`。
- **站内**: `alert_store.append_many(data_dir, [event])` → `data_dir/user_data/alerts.jsonl`。

**本批次不做飞书**(下一拍): 这里不 import `webhook_adapter`, 不调
`QuoteService._maybe_send_webhook`, 不 import `app.news.push`。`monitor_rules` 里
那段「落盘 → SSE → 飞书」的三步链条, 本模块**只抄前两步**。

**门控**: `BUY_SELL_SIGNALS_PUSH`,真值集合与 `BUY_SELL_SIGNALS_PERSIST` 完全一致
(`{"1","true","yes","on"}`,大小写与首尾空白归一)。未设 → 本模块所有入口都是空操作,
一行都不写。生产是否置 1 **另等部署口令**, 不得随合入默开。

**与落盘正交**: 落盘(persist)管"记录活得比进程久", 推送(push)管"变更播出去"。
两者各有一个 env, 互不推导 —— 可以只落盘不推、也可以只推不落盘(后者重启即失忆,
不建议, 但代码不拦)。

### 触发条件(全部满足才推)

1. `status == "publishable"` —— 唯一入口仍是 `state_machine.manual_publish()`
2. `push_gate == "armed"`
3. 派生 `push_eligible` 为真(内含"当前时间不在冷却窗口")
4. `event_class != "limit_reopen"` —— 炸板/回封不推, 与状态机双拦一致
5. **状态变化**: `last_pushed_at is None` 或 `last_status_change_at > last_pushed_at`

### 「同状态不推」优先于「冷却期满回 armed」

这两条规则会撞车, 优先级写死如下:

- 冷却期满只把闸门从 `cooldown` **恢复**到 `armed`, 恢复本身**不产生推送**。
- 恢复之后仍要过第 5 条。若这期间状态没变(`last_status_change_at <= last_pushed_at`),
  照样拒推。
- 想让同一条信号再推一次, 必须**先有新的状态变化**(`manual_publish` /
  `transition` 会推 `last_status_change_at`), 再等冷却过点。

理由: 冷却的语义是"别重复播报同一条消息", 不是"时间到了就再播一遍"。反过来做,
用户会在没有任何新信息的情况下周期性收到同一条信号。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from app.market_time import CN_TZ, cn_now

from . import disclaimer as _disclaimer
from .constants import (
    MONITOR_EVENT_TYPE,
    NAMESPACE,
    PUSH_DEDUPE_COOLDOWN_MIN,
    PUSH_ENV_NAME,
    PUSH_PAYLOAD_SCHEMA_VERSION,
)
from .models import SignalRecord
from .state_machine import set_push_gate
from .store import SignalRegistryStore

logger = logging.getLogger(__name__)

# 与 `store._TRUTHY` 同值。刻意各自持有一份而不是互相 import: 落盘与推送正交,
# 将来一方改真值集合不该悄悄改到另一方。
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})

_LIMIT_REOPEN: Final = "limit_reopen"


# ================================================================ 结果类型


@dataclass(frozen=True, slots=True)
class PushResult:
    """一次 `try_push` 的结果。

    `ok=True` 表示**真的扇出过**(SSE 与站内都试过, 见 `channels`)。失败不是异常:
    调用方多半是人工触发的一次动作, 拿一个带 `reason` 的结果去显示, 比 try/except
    更直接。真正的程序性错误(装载器坏了、数据形状不对)仍会抛。
    """

    ok: bool
    reason: str = ""
    record: SignalRecord | None = None
    """推送成功后**已更新**的记录(冷却字段与 `push_gate` 已改)。失败时为 None。"""
    sse_sent: bool = False
    inbox_sent: bool = False
    message: str = ""
    """人读的一行摘要(与推送出去的 message 同文)。"""

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(name for name, hit in (("sse", self.sse_sent), ("inbox", self.inbox_sent)) if hit)


class PushError(Exception):
    """推送失败的基类。"""


class PushDisabled(PushError):
    """`BUY_SELL_SIGNALS_PUSH` 未开。**这不是异常路径, 是默认状态。**"""


class PushRejected(PushError):
    """资格不足: 非 publishable / 炸板 / 闸门未 armed / 冷却中 / 同状态重算。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ================================================================ 门控


def push_env_enabled() -> bool:
    """推送门是否开。只认 `PUSH_ENV_NAME` 这一个名字。"""
    return os.environ.get(PUSH_ENV_NAME, "").strip().lower() in _TRUTHY


# ================================================================ 载荷


def build_push_payload(record: SignalRecord, *, now: datetime | None = None) -> dict[str, Any]:
    """组一条告警事件。SSE 与站内**共用**同一份 dict(多余字段两处都无害)。

    合规脚注 `disclaimer` 是必带字段, 不是可选装饰 —— 推到用户眼前的每个字都要能被
    追溯到"这不是投资建议"这句话。
    """
    stamp = now or cn_now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=CN_TZ)
    message = f"买卖信号状态变更: {record.display_name} ({record.signal_id}) → {record.status}"
    return {
        "monitor_event_type": MONITOR_EVENT_TYPE,
        "schema_version": PUSH_PAYLOAD_SCHEMA_VERSION,
        "namespace": NAMESPACE,
        "source": NAMESPACE,
        "type": MONITOR_EVENT_TYPE,
        "severity": "info",
        "ts": int(stamp.timestamp() * 1000),
        "message": message,
        "disclaimer": _disclaimer.DISCLAIMER_TEXT,
        # 定位用字段。不含价格/仓位/买卖方向 —— 本包不产出交易指令。
        "signal_id": record.signal_id,
        "version": record.version,
        "display_name": record.display_name,
        "status": record.status,
        "push_gate": record.push_gate,
        "event_class": record.event_class,
        "family": record.family,
        "spec_ref": record.spec_ref,
    }


def build_sse_alert(payload: dict[str, Any]) -> dict[str, Any]:
    """从共用载荷挑出 SSE 侧用的字段。

    SSE 订阅者的队列容量有限(见 `QuoteSubscriber`), 载荷越大越容易在拥堵时被丢最旧。
    这里保留判定与展示必需的部分, 丢掉 `spec_ref` 这类长字符串。
    """
    keep = (
        "source",
        "type",
        "monitor_event_type",
        "severity",
        "message",
        "disclaimer",
        "signal_id",
        "version",
        "display_name",
        "status",
        "push_gate",
        "schema_version",
        "namespace",
    )
    return {key: payload[key] for key in keep if key in payload}


# ================================================================ 资格判定


def _same_status_as_last_push(record: SignalRecord) -> bool:
    """本次状态是否与上次推送时相同。

    `last_pushed_at is None` → 从未推过, 不算同状态。
    否则比较 `last_status_change_at` 与 `last_pushed_at`: 前者不晚于后者, 说明这次
    没有新的状态变化, 只是把同一份记录又算了一遍。
    """
    if record.last_pushed_at is None:
        return False
    return record.last_status_change_at <= record.last_pushed_at


def _record_is_eligible_at(record: SignalRecord, now: datetime) -> bool:
    """按**给定时刻**判断能否推送。

    不直接用 `record.push_eligible`: 那个属性内部读的是 `cn_now()`(系统时钟)。正常
    运行时两者一致, 但 `try_push(now=...)` 是允许注入时钟的(测试、补推、回放都要),
    此时若还用系统时钟, 就会出现"入口说冷却已过、派生量说还在冷却"的自相矛盾。

    这里按 `push_eligible` 的同一套条件重写一遍, 只是把时钟换成参数。**任何一条
    条件的增删都必须同步改 `models.SignalRecord.push_eligible`** —— 两处不一致时以
    models 为准的语义会漂, 所以下面这条断言般的注释是契约:
    publishable / armed / 冷却未过期。
    """
    return (
        record.status == "publishable"
        and record.push_gate == "armed"
        and (record.push_dedupe_cooldown_until is None or record.push_dedupe_cooldown_until <= now)
    )


def evaluate_push(record: SignalRecord, *, now: datetime | None = None) -> str:
    """返回拒推原因; 空字符串表示可推。**纯函数, 只读 `now`。**

    判定顺序是刻意排的, 为的是**报最具体的那条原因**:

    1. 炸板 / 非 publishable —— 状态层面的硬事实
    2. **冷却中** —— 排在 `push_gate` 之前。冷却中的记录 `push_gate` 必然是
       `cooldown`, 若先查闸门只会得到「不是 armed」, 让人去拨闸门(拨了也没用,
       还在冷却里)。报"冷却中直至某时刻"才指向真正该等的东西。
    3. 闸门不是 armed —— 到这一步说明不是冷却, 而是没拨
    4. 派生 `push_eligible` 兜底 —— 走到这里说明还有别的组合不满足
    5. **状态未变化** —— 放最后。冷却期内本来就不该推, 这时报"状态未变化"会误导
       排查方向(让人以为改时间戳就能推, 其实还得等冷却)。
    """
    if record.event_class == _LIMIT_REOPEN:
        return "event_class=limit_reopen 不推(炸板/回封只记录)"
    if record.status != "publishable":
        return f"status={record.status} 不是 publishable"

    stamp = now or cn_now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=CN_TZ)
    until = record.push_dedupe_cooldown_until
    if until is not None and until > stamp:
        return f"冷却中, 直至 {until.isoformat()}"

    if record.push_gate != "armed":
        return f"push_gate={record.push_gate} 不是 armed"
    if not _record_is_eligible_at(record, stamp):  # 兜底: 派生量的其余条件不满足
        return "push_eligible=false(派生条件不满足)"
    if _same_status_as_last_push(record):
        return "状态未变化, 同一条信号不重复播报"
    return ""


def _restore_armed_if_cooldown_over(record: SignalRecord, now: datetime) -> SignalRecord:
    """冷却期满 → 把闸门从 `cooldown` 恢复成 `armed`。

    只在 `try_push` 入口做, **不另开 cron / 定时任务** —— 多一个后台任务就多一处
    需要解释"它什么时候跑、跑漏了怎么办"。闸门是个纯展示态, 在真正要推的那一刻
    修正它最省事。

    恢复**不产生推送**: 恢复后仍要过 `evaluate_push`(尤其"同状态不推")。
    """
    if record.push_gate != "cooldown":
        return record
    until = record.push_dedupe_cooldown_until
    if until is not None and until > now:
        return record  # 还在冷却里, 不动
    try:
        return set_push_gate(record, "armed")
    except Exception:  # noqa: BLE001 - 非 publishable 不允许 armed, 状态机已拦, 这里只兜底
        return record


# ================================================================ 入口


def try_push(
    record: SignalRecord,
    *,
    quote_service: Any = None,
    data_dir: Path | str | None = None,
    store: SignalRegistryStore | None = None,
    now: datetime | None = None,
) -> PushResult:
    """尝试把一条已人工发布的信号推出去。**显式调用, 没有任何自动触发。**

    调用链: 资格门 → 组载荷 → 站内 append_many → SSE push_alerts → 写回冷却。

    顺序说明: **先站内后 SSE**。站内是持久化的(alerts.jsonl), SSE 是易丢的(订阅者
    队列满会丢最旧)。先写不会被丢的那份, 再发会丢的那份 —— 反过来的结果是
    "SSE 看到了、刷新页面就没了"。

    **写回时机**: 两个通道都发过之后才写 `last_pushed_at` / 冷却 / `push_gate`。
    若任一通道抛错, 不写回 —— 宁可下一次重复推一次, 也不要出现"标记为已推、其实
    没人收到"。失败会记 warning 日志, 且返回 `ok=False`。

    Args:
        record: 已 `manual_publish` 的记录。
        quote_service: 鸭子类型, 只需要有 `push_alerts(list[dict])`。`None` → 跳过 SSE。
        data_dir: 站内告警根目录(`alerts.jsonl` 落在这里)。`None` → 跳过站内。
        store: 传入则成功后 `upsert` 更新后的记录。`None` → 只在返回值里给新记录。
        now: 注入时钟, 测试用。默认 `cn_now()`。
    """
    stamp = now or cn_now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=CN_TZ)

    if not push_env_enabled():
        return PushResult(ok=False, reason=f"{PUSH_ENV_NAME} 未开, 推送为关")

    record = _restore_armed_if_cooldown_over(record, stamp)
    rejection = evaluate_push(record, now=stamp)
    if rejection:
        return PushResult(ok=False, reason=rejection)

    payload = build_push_payload(record, now=stamp)
    if data_dir is not None and quote_service is None:
        # 只配了一半: 仍然允许推(站内也是真实通道), 但留痕提醒配置不完整。
        logger.info("buy_sell_signals 推送: 未提供 quote_service, 只发站内告警")

    inbox_sent = False
    sse_sent = False
    try:
        if data_dir is not None:
            from app.services import alert_store  # 局部 import: 本模块被 main 的 lifespan 调用

            alert_store.append_many(Path(data_dir), [dict(payload)])
            inbox_sent = True
        if quote_service is not None:
            quote_service.push_alerts([build_sse_alert(payload)])
            sse_sent = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "buy_sell_signals 推送扇出失败 (signal_id=%s): %s", record.signal_id, exc
        )
        return PushResult(
            ok=False,
            reason=f"扇出失败: {exc}",
            sse_sent=sse_sent,
            inbox_sent=inbox_sent,
        )

    updated = record.with_fields(
        last_pushed_at=stamp,
        push_dedupe_cooldown_until=stamp + timedelta(minutes=PUSH_DEDUPE_COOLDOWN_MIN),
    )
    try:
        updated = set_push_gate(updated, "cooldown")
    except Exception:  # noqa: BLE001 - 理论上不会发生(status 仍 publishable), 退化也要留住冷却
        logger.warning("buy_sell_signals 推送后置 cooldown 失败 (signal_id=%s)", record.signal_id)

    if store is not None:
        try:
            store.upsert(updated)
        except Exception as exc:  # noqa: BLE001
            # 已经推完了, 落库失败不能让调用方以为没推。留痕并返回 ok=True + 说明。
            logger.warning("buy_sell_signals 推送后落库失败 (signal_id=%s): %s", record.signal_id, exc)
            return PushResult(
                ok=True,
                reason=f"已推送, 但落库失败: {exc}",
                record=updated,
                sse_sent=sse_sent,
                inbox_sent=inbox_sent,
                message=payload["message"],
            )

    logger.info(
        "buy_sell_signals 已推送 signal_id=%s channels=%s cooldown_until=%s",
        record.signal_id,
        ",".join(n for n, hit in (("sse", sse_sent), ("inbox", inbox_sent)) if hit) or "none",
        updated.push_dedupe_cooldown_until,
    )
    return PushResult(
        ok=True,
        record=updated,
        sse_sent=sse_sent,
        inbox_sent=inbox_sent,
        message=payload["message"],
    )


__all__ = [
    "PushDisabled",
    "PushError",
    "PushRejected",
    "PushResult",
    "build_push_payload",
    "build_sse_alert",
    "evaluate_push",
    "push_env_enabled",
    "try_push",
]
