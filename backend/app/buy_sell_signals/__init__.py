"""买卖信号注册表与状态机骨架 (批次 g / g2 / gp / gpush)。

模块边界:

- `constants`  元数据 / Bonferroni 口径 / 与提醒层分家的冷却字段名
- `models`     `SignalRecord` 及其校验
- `state_machine` 转移表 + 人工发布闸 + 硬禁自动晋升
- `store`      `SignalRegistryStore` 协议 + 内存实现 + 落盘实现(env 门控) + 工厂
- `push`       推送出口: env 门控的 `try_push`, 通道 = SSE + 站内告警(gpush) + 钉钉(gding)
- `sw_pit`     申万一级行业 PIT 只读查询(history × l1_map, valid_from/valid_to)
- `quality_proxy` 日线质量代理(财务按 announce_date 做 PIT)
- `quality_rank`  申万一级行业内的质量相对分位 + 白名单**试跑**判定(g2 窄化)
- `rebalance_snapshot` 月度再平衡快照(纯内存, 不调度不写盘)
- `valuation_stub` / `health_veto_stub` 估值与健康接口桩, 返回 pending

**g2 窄化做了什么**: `limit_reopen` 构造即 `observe`; 行业内相对分位; 白名单试跑
`p40` 并明确**未锁定**。**没做**: 估值腿实装、健康规则引擎、权重标定、p30/p50
敏感性。

**gp 做了什么**: `DiskSignalRegistryStore` 真的能落盘了 —— 但只在
`BUY_SELL_SIGNALS_PERSIST` 为真值时, 经 `build_signal_registry_store()` 工厂开启;
默认仍是内存实现, 进程退出即消失。落盘写 `data/buy_sell_signals/registry.json`,
不碰 `partition_writer` 的生产分区。**gp 没做**: 推送、路由、日管道自动 upsert
—— **落盘 ≠ 推送**。

**gpush 做了什么**: 推送出口 `push.try_push()` —— 但只在 `BUY_SELL_SIGNALS_PUSH`
为真值时真实扇出, 默认关。通道写死为 **SSE(`QuoteService.push_alerts`) + 站内
(`alert_store.append_many` → `alerts.jsonl`)**, 复用既有通道, 不新建总线、不新建
SSE 路由。触发 = 状态变化 且 `publishable` 且 `push_gate=armed` 且 `push_eligible`;
成功后写 `last_pushed_at` + 30 分钟冷却 + `push_gate=cooldown`。**gpush 没做**:
`news.push`、日管道自动扫晋升、任何自动 `publishable`。

**gding 做了什么**: 在同一个 `try_push` 的资格门之后、写回冷却之前, 接上**既有**
钉钉 webhook —— 条件是 `BUY_SELL_SIGNALS_PUSH` 开 **且** `BUY_SELL_SIGNALS_DINGTALK`
开 **且** `settings.dingtalk_webhook_url` 非空(三者**与**关系, 钉钉门默认关)。
发送只调既有的 `app.news.dingtalk.send_markdown`, 与资讯/ETF/价位提醒同一个机器人,
不新建 webhook、不新建机器人、不复制加签实现。同一次状态变化可以 SSE + 站内 + 钉钉
同时发, 但 `last_pushed_at` / `push_dedupe_cooldown_until` / `push_gate=cooldown`
**只写一次**。钉钉是 best-effort: 发送失败只 warning, 不回滚已成功的站内/SSE,
仍写一次冷却。**gding 没做**: 飞书(**已取消, 从路线图去掉, 不再排期**)、
钉钉独立冷却、钉钉独立触发时钟、日管道自动扫晋升。

现行口径: **落盘已开 · 推送已开(SSE/站内) · 钉钉可选(env) · 飞书已取消 · 竞价/放量事件仍未接**。

估值与健康**未测通**, 只返回 `data_pending` / `design_pending`; `health_veto_hit`
保持 `None`(不是 `False` —— `False` 是一个"查过、没命中"的结论)。

本包与 `app.services.trade_marks`、`app.news.push` **没有** import 关系, 也不共用
任何提醒层常量(`VWAP_BAND` / `RANGE_BAND` / `T_COOLDOWN_MIN`); 与 `app.t_rounds`
零符号耦合。理由见 `constants`。`push.py` 复用 `QuoteService.push_alerts` 与
`alert_store` 这两个**通道**、`app.news.dingtalk.send_markdown` 这个**发送函数**,
但不 import 它们的节流参数, 也不走飞书支路(已取消)。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from .constants import (
    BONFERRONI_M,
    BONFERRONI_SCHEME_ID,
    DINGTALK_ENV_NAME,
    DISCLAIMER_KEY,
    NAMESPACE,
    PERSIST_ENV_NAME,
    PERSIST_SUBDIR,
    PUSH_ENV_NAME,
    REGISTRY_FILENAME,
    SIGNAL_DESIGN_PIN,
    design_version,
)
from .disclaimer import DISCLAIMER_TEXT
from .models import SignalRecord, SignalValidationError
from .push import (
    PushDisabled,
    PushError,
    PushRejected,
    PushResult,
    build_dingtalk_text,
    dingtalk_env_enabled,
    dingtalk_webhook_ready,
    push_env_enabled,
    try_push,
)
from .quality_rank import (
    WHITELIST_PERCENTILE_LOCKED,
    WHITELIST_PERCENTILE_TRIAL,
    WHITELIST_THRESHOLD_LOCKED,
    IndustryRankResult,
    percentile_rank,
    rank_within_industry,
)
from .rebalance_snapshot import (
    RebalanceSnapshot,
    build_rebalance_snapshot,
    make_rebalance_id,
)
from .state_machine import (
    MANUAL_PUBLISH_CHECKLIST,
    ManualPublishRejected,
    SignalStateError,
    SignalTransitionError,
    manual_publish,
    transition,
)
from .store import (
    DiskSignalRegistryStore,
    InMemorySignalRegistryStore,
    PersistDisabledError,
    PublishPointerConflict,
    SignalRegistryCorruptError,
    SignalRegistryError,
    SignalRegistryStore,
    build_signal_registry_store,
)

__all__ = [
    "BONFERRONI_M",
    "BONFERRONI_SCHEME_ID",
    "DINGTALK_ENV_NAME",
    "DISCLAIMER_KEY",
    "DISCLAIMER_TEXT",
    "DiskSignalRegistryStore",
    "InMemorySignalRegistryStore",
    "IndustryRankResult",
    "MANUAL_PUBLISH_CHECKLIST",
    "ManualPublishRejected",
    "NAMESPACE",
    "PERSIST_ENV_NAME",
    "PERSIST_SUBDIR",
    "PUSH_ENV_NAME",
    "PersistDisabledError",
    "PublishPointerConflict",
    "PushDisabled",
    "PushError",
    "PushRejected",
    "PushResult",
    "REGISTRY_FILENAME",
    "RebalanceSnapshot",
    "SIGNAL_DESIGN_PIN",
    "SignalRecord",
    "SignalRegistryCorruptError",
    "SignalRegistryError",
    "SignalRegistryStore",
    "SignalStateError",
    "SignalTransitionError",
    "SignalValidationError",
    "WHITELIST_PERCENTILE_LOCKED",
    "WHITELIST_PERCENTILE_TRIAL",
    "WHITELIST_THRESHOLD_LOCKED",
    "build_dingtalk_text",
    "build_rebalance_snapshot",
    "build_signal_registry_store",
    "design_version",
    "dingtalk_env_enabled",
    "dingtalk_webhook_ready",
    "make_rebalance_id",
    "manual_publish",
    "percentile_rank",
    "push_env_enabled",
    "rank_within_industry",
    "transition",
    "try_push",
]
