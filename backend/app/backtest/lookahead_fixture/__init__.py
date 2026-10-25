"""lookahead 偏差审计夹具（批次 20261025glookahead · -A-#3 P1）。

**仅供研究，不构成投资建议；执行须人工确认。**

## 它做什么

给定「入口 id + 冻结的 run_config + 截断时点 t」，用**仅含 ≤ t 的数据**重跑同一
入口，然后把原跑里「时点 ≤ t」的产出与重跑结果做**结构化逐字段**比对。不一致
→ 抛 `LookaheadInvariantError`。

这是**必要条件检验**：通过只代表"未检出跨截断点的前视"，不代表"已无前视"。
未注册的入口、未列入 compare 的字段、未选中的 t，都在它的视野之外。

## 它不做什么

- **不**替代 walkforward。walkforward 切的是 OOS 折（训练/验证的时间隔离），
  本夹具审的是"某一次跑有没有用到 t 之后的信息"。两者互补：walkforward 覆盖
  "样本外是否还有效"，本夹具覆盖"样本内的每个时点是否只用了当时已知的信息"。
  用其中一个冒充另一个，两者都不会被真正验证。
- **不**在 MCP `run_backtest` 热路径里跑。主门禁是**仓内 CI**（pytest 节点），
  不是每次 API 调用时现算。
- **不**拷贝 freqtrade / Fincept 任何源码（GPL 零集成）。本模块只借了
  "截断重放 + 逐字段比对"这个思路，实现全部原创。

## 模块地图

| 模块 | 职责 |
|---|---|
| `api.py` | `run_truncated_replay` / `build_baseline` / `assert_invariant` / `assert_causal_order` |
| `truncate.py` | 裁到 ≤ t，并**复核**裁没裁干净 |
| `invariant.py` | 规范化 + 逐字段比对 + 差集白名单 |
| `registry.py` | 入口注册；未注册 → fail-closed |
| `entries.py` | `t_rounds.offline` 适配器（只读既有字段，零碰 5c） |
| `gate.py` | 对外前置查询桩（恒不放行） |
| `errors.py` | 专用错误类型 |

## fail-closed 的三处

1. 入口未注册 → `UnregisteredEntryError`，不猜默认键；
2. 截断没生效（仍有 > t 的行 / t 口径不明）→ `TruncationNotAppliedError`；
3. 比对窗内两边都是 0 行 → `LookaheadInvariantError`，**不**当作"集合相等即通过"。
"""

from __future__ import annotations

from .api import (
    ReplayResult,
    assert_causal_order,
    assert_invariant,
    build_baseline,
    ensure_default_entries_registered,
    run_truncated_replay,
)
from .entries import build_run_config, record_to_row, records_to_rows
from .errors import (
    KeySpecError,
    LookaheadFixtureError,
    LookaheadInvariantError,
    TruncationNotAppliedError,
    UnregisteredEntryError,
)
from .gate import (
    GATE_STATUS_PENDING,
    LookaheadGateUnavailable,
    gate_status,
    is_cleared_for_external_claim,
)
from .invariant import KeySpec, normalize_records, normalize_value
from .registry import (
    ENTRY_T_ROUNDS_OFFLINE,
    T_ROUNDS_OFFLINE_COMPARE,
    T_ROUNDS_OFFLINE_IDENTITY,
    EntrySpec,
    clear_registry_for_tests,
    get_entry,
    list_entries,
    register_entry,
)
from .truncate import (
    T_KIND_DATE,
    T_KIND_TIMESTAMP,
    assert_truncated,
    truncate_frame,
)

__all__ = [
    "ENTRY_T_ROUNDS_OFFLINE",
    "GATE_STATUS_PENDING",
    "T_KIND_TIMESTAMP",
    "T_ROUNDS_OFFLINE_COMPARE",
    "T_ROUNDS_OFFLINE_IDENTITY",
    "EntrySpec",
    "KeySpec",
    "KeySpecError",
    "LookaheadFixtureError",
    "LookaheadGateUnavailable",
    "LookaheadInvariantError",
    "ReplayResult",
    "TruncationNotAppliedError",
    "UnregisteredEntryError",
    "assert_causal_order",
    "assert_invariant",
    "assert_truncated",
    "build_baseline",
    "build_run_config",
    "clear_registry_for_tests",
    "ensure_default_entries_registered",
    "gate_status",
    "get_entry",
    "is_cleared_for_external_claim",
    "list_entries",
    "normalize_records",
    "normalize_value",
    "record_to_row",
    "records_to_rows",
    "register_entry",
    "run_truncated_replay",
    "truncate_frame",
]
