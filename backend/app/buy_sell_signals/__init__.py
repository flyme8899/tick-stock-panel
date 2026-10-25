"""买卖信号注册表与状态机骨架 (批次 20261024g / 20261025g2 / 20261025gp)。

模块边界:

- `constants`  元数据 / Bonferroni 口径 / 与提醒层分家的冷却字段名
- `models`     `SignalRecord` 及其校验
- `state_machine` 转移表 + 人工发布闸 + 硬禁自动晋升
- `store`      `SignalRegistryStore` 协议 + 内存实现 + 落盘实现(env 门控) + 工厂
- `sw_pit`     申万一级行业 PIT 只读查询(history × l1_map, valid_from/valid_to)
- `quality_proxy` 日线质量代理(财务按 announce_date 做 PIT)
- `quality_rank`  申万一级行业内的质量相对分位 + 白名单**试跑**判定(g2 窄化)
- `rebalance_snapshot` 月度再平衡快照(纯内存, 不调度不写盘)
- `valuation_stub` / `health_veto_stub` 估值与健康接口桩, 返回 pending

**g2 窄化做了什么**: `limit_reopen` 构造即 `observe`; 行业内相对分位; 白名单试跑
`p40` 并明确**未锁定**。**没做**: 估值腿实装、健康规则引擎、权重标定、p30/p50
敏感性、正式推送。

**gp 做了什么**: `DiskSignalRegistryStore` 真的能落盘了 —— 但只在
`BUY_SELL_SIGNALS_PERSIST` 为真值时, 经 `build_signal_registry_store()` 工厂开启;
默认仍是内存实现, 进程退出即消失。落盘写 `data/buy_sell_signals/registry.json`,
不碰 `partition_writer` 的生产分区。**gp 没做**: 推送(SSE / 飞书 / `news.push`)、
路由、日管道自动 upsert —— **落盘 ≠ 推送**。

估值与健康**未测通**, 只返回 `data_pending` / `design_pending`; `health_veto_hit`
保持 `None`(不是 `False` —— `False` 是一个"查过、没命中"的结论)。

本包与 `app.services.trade_marks`、`app.news.push` **没有** import 关系, 也不共用
任何提醒层常量(`VWAP_BAND` / `RANGE_BAND` / `T_COOLDOWN_MIN`); 与 `app.t_rounds`
零符号耦合。理由见 `constants`。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from .constants import (
    BONFERRONI_M,
    BONFERRONI_SCHEME_ID,
    DISCLAIMER_KEY,
    NAMESPACE,
    PERSIST_ENV_NAME,
    PERSIST_SUBDIR,
    REGISTRY_FILENAME,
    SIGNAL_DESIGN_PIN,
    design_version,
)
from .disclaimer import DISCLAIMER_TEXT
from .models import SignalRecord, SignalValidationError
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
    "PersistDisabledError",
    "PublishPointerConflict",
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
    "build_rebalance_snapshot",
    "build_signal_registry_store",
    "design_version",
    "make_rebalance_id",
    "manual_publish",
    "percentile_rank",
    "rank_within_industry",
    "transition",
]
