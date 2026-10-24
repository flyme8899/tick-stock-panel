"""做 T 轮次注册表与状态机骨架 (批次 20261024h)。

模块边界:

- `constants`       ε=0.30% / 轮次上限 / 模式与状态枚举 / 六条软提醒的常量形式
- `models`          `TRoundRecord`、`LegState` 及其构造期硬校验
- `state_machine`   转移表 + `complete_round()` 唯一完成入口 + 轮次配额检查
- `store`           `TRoundRegistryStore` 协议 + 内存实现 + 默认不可用的磁盘空壳
- `position_tags`   持仓三态(信号宇宙不按持仓过滤)
- `disclaimer`      合规脚注

**与 `app.buy_sell_signals` 全面隔离**: 不 import、不共享 Store、不共用命名空间。
本包记录**不得**写入买卖信号表或那个包的 Store。

**与提醒层分家**: 不 import / 拷贝 `VWAP_BAND` / `RANGE_BAND` / `T_COOLDOWN_MIN`;
轮次上限字段叫 `max_rounds_per_day` / `rounds_consumed_on_trade_date`, 不叫
`push_dedupe_cooldown_until`, 也不叫 `cooldown`。

**本批次不做**: 真实分钟评估接线、`build_feature_frame` 在线路径、`minute_replay`
生产评估、全量特征回测、ε 重标定、分档成本实测、可卖老股实盘字段、正式推送、
默认落盘、竞价回测。

ε = **0.30%**(`pct_of_vwap_or_mean`, PARAM_NAIL `design_pin_v1`)**只入常量与
version 元数据**, 本批次不跑门禁。特征回测 / ε 门禁 / 可卖老股实盘**均未测通**。

六条软提醒(详见 `constants` 模块 docstring):
同尺后再 VWAP · 禁拷提醒常量 · 冷却分列 · virtual 占位 · 研究窗 ≥2025-07-25 ·
禁冒充竞价。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from .constants import (
    EPSILON_DEFINITION,
    EPSILON_VALUE_PCT,
    MAX_ROUNDS_PER_DAY,
    NAMESPACE,
    RESEARCH_WINDOW_START,
    T_ROUND_DESIGN_PIN,
    design_version,
)
from .disclaimer import DISCLAIMER_TEXT
from .models import LegState, TRoundRecord, TRoundValidationError
from .position_tags import PositionTag, tag_position
from .state_machine import (
    InvalidTRoundTransitionError,
    RoundCost,
    RoundQuotaExceeded,
    TRoundStateError,
    complete_round,
    transition,
)
from .store import (
    DiskTRoundRegistryStore,
    InMemoryTRoundRegistryStore,
    PersistDisabledError,
    TRoundRegistryError,
    TRoundRegistryStore,
)

__all__ = [
    "DISCLAIMER_TEXT",
    "DiskTRoundRegistryStore",
    "EPSILON_DEFINITION",
    "EPSILON_VALUE_PCT",
    "InMemoryTRoundRegistryStore",
    "InvalidTRoundTransitionError",
    "LegState",
    "MAX_ROUNDS_PER_DAY",
    "NAMESPACE",
    "PersistDisabledError",
    "PositionTag",
    "RESEARCH_WINDOW_START",
    "RoundCost",
    "RoundQuotaExceeded",
    "T_ROUND_DESIGN_PIN",
    "TRoundRecord",
    "TRoundRegistryError",
    "TRoundRegistryStore",
    "TRoundStateError",
    "TRoundValidationError",
    "complete_round",
    "design_version",
    "tag_position",
    "transition",
]
