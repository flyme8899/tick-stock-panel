"""买卖信号注册表与状态机骨架 (批次 20261024g)。

模块边界:

- `constants`  元数据 / Bonferroni 口径 / 与提醒层分家的冷却字段名
- `models`     `SignalRecord` 及其校验
- `state_machine` 转移表 + 人工发布闸 + 硬禁自动晋升
- `store`      `SignalRegistryStore` 协议 + 内存实现 + (默认不可用的)磁盘空壳
- `sw_pit`     申万一级行业 PIT 只读查询(history × l1_map, valid_from/valid_to)
- `quality_proxy` 日线质量代理(财务按 announce_date 做 PIT)
- `valuation_stub` / `health_veto_stub` 估值与健康接口桩, 返回 pending

**本批次不做**: 正式推送、默认落盘、竞价回测、做 T(旁支 h)轮次、完整健康规则引擎。
估值与健康**未测通**, 只返回 `data_pending` / `design_pending`。

本包与 `app.services.trade_marks`、`app.news.push` **没有** import 关系, 也不共用
任何提醒层常量(`VWAP_BAND` / `RANGE_BAND` / `T_COOLDOWN_MIN`)。理由见 `constants`。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from .constants import (
    BONFERRONI_M,
    BONFERRONI_SCHEME_ID,
    NAMESPACE,
    SIGNAL_DESIGN_PIN,
    design_version,
)
from .disclaimer import DISCLAIMER_TEXT
from .models import SignalRecord, SignalValidationError
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
    SignalRegistryError,
    SignalRegistryStore,
)

__all__ = [
    "BONFERRONI_M",
    "BONFERRONI_SCHEME_ID",
    "DISCLAIMER_TEXT",
    "DiskSignalRegistryStore",
    "InMemorySignalRegistryStore",
    "MANUAL_PUBLISH_CHECKLIST",
    "ManualPublishRejected",
    "NAMESPACE",
    "PersistDisabledError",
    "PublishPointerConflict",
    "SIGNAL_DESIGN_PIN",
    "SignalRecord",
    "SignalRegistryError",
    "SignalRegistryStore",
    "SignalStateError",
    "SignalTransitionError",
    "SignalValidationError",
    "design_version",
    "manual_publish",
    "transition",
]
