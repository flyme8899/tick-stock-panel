"""入口注册表：未注册 → fail-closed，不做"猜一个默认值"。

**仅供研究，不构成投资建议；执行须人工确认。**

## 为什么注册表是必需的，而不是可选

夹具要回答的问题不是"随便跑一遍两次结果一样吗"，而是"**同一个入口**在只看到
≤ t 的数据时，会不会给出和原来一样的 ≤ t 产出"。这里的"同一个入口、同一套比对键"
必须先被**钉住**，否则每跑一次都可以换一套键去比，总能比出一个"相等"。

注册表钉三件事：

1. `entry_id` —— 调用方说得出自己在审谁；
2. `t_kind` —— 该入口的 `t` 是交易日还是 bar 时戳（口径写死在注册项里，不由调用点决定）；
3. `key_spec` —— 比对键（见 `invariant.KeySpec`）。

未注册 → `UnregisteredEntryError`。**不存在**"未注册就用通用键兜底"的分支：
通用键必然比该入口真正需要的键少，少掉的正是容易出前视的那几个字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .errors import UnregisteredEntryError
from .invariant import KeySpec
from .truncate import T_KIND_DATE, T_KIND_TIMESTAMP

__all__ = [
    "EntrySpec",
    "ENTRY_T_ROUNDS_OFFLINE",
    "T_ROUNDS_OFFLINE_IDENTITY",
    "T_ROUNDS_OFFLINE_COMPARE",
    "register_entry",
    "get_entry",
    "list_entries",
    "clear_registry_for_tests",
]

ENTRY_T_ROUNDS_OFFLINE = "t_rounds.offline"
"""本拍唯一注册的入口：做 T 的离线纸面回测（`run_mode_a_backtest`）。"""

T_ROUNDS_OFFLINE_IDENTITY: tuple[str, ...] = ("symbol", "trade_date", "mode", "path")
"""定位一轮的最小集合。`path` 取自记录的 `metadata.gate_path`。

为什么把 `path` 放进 identity 而不是 compare：A 与 B 是**两格独立的回测**（同一
天同一票在两个格子里可以各开一轮），把它们混成一个键会让两轮的记录在比对时互相
覆盖。`mode` 同理。
"""

T_ROUNDS_OFFLINE_COMPARE: tuple[str, ...] = (
    "leg1_ts",
    "leg2_ts",
    "round_status",
    "leg1_price",
    "leg2_price",
    "gross_spread",
    "net_spread",
)
"""要比的语义字段（不限于"最小集"，只多不少）。

| 字段 | 为什么要比 |
|---|---|
| `leg1_ts` | 成交**时刻**。前视的典型症状就是它往前挪了一根。 |
| `leg2_ts` | 出场时刻；`None` 与"某个时刻"的差别必须暴露。 |
| `round_status` | 同一天同一票，一轮从 `completed` 变成 `unreverted_at_cutoff` 就是结论翻转。 |
| `leg1_price` | 成交价。旧的前视 bug 正出在这：用确认 bar 自己的价而不是下一根的价。 |
| `leg2_price` | 出场价，同上。 |
| `gross_spread` | 毛价差；已结算才有，未结算时为 `None` —— `None == None` 是有效断言。 |
| `net_spread` | 净价差；同上。 |

`t_round_id` 与 `feature_frame_ref` **故意不比对**：它们把 `confirm_ts` 拼进串里
（`_make_record` 的 `tag` 参数），本身不含额外语义 —— 真正的语义在 `leg1_ts` 里，
而那个字段已经比了。把它们加进 compare 只会制造"只差一个字符串"的噪声失败，
让人以为是浮点问题。

`cost_amount` / `cost_bp_tier` **不比对**：本夹具审的是"看到了什么数据"，
成本口径由 5b/5c 闸门管，而本拍零碰 5c 行为。若将来要给成本也上不变性，
那是另一套键，不是往这里塞。
"""


@dataclass(frozen=True, slots=True)
class EntrySpec:
    """一个入口的完整注册项。

    `replay` 是"用给定 run_config + t 重跑一次"的可调用对象。夹具**只**通过它
    拿重跑结果，不自己去调业务代码 —— 这样入口换了实现，夹具不用改。

    签名约定：`replay(run_config: Mapping[str, Any], t: Any) -> list[Mapping[str, Any]]`
    （或返回 `ReplayResult`）。返回的记录必须是**该入口自己的产出形状**，
    夹具不做二次解释。
    """

    entry_id: str
    t_kind: str
    key_spec: KeySpec
    replay: Callable[[Mapping[str, Any], Any], Any]
    note: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)


_REGISTRY: dict[str, EntrySpec] = {}


def register_entry(spec: EntrySpec, *, replace: bool = False) -> EntrySpec:
    """注册一个入口。

    重复注册默认抛错（而不是静默覆盖）：两次注册意味着两套比对键，而"到底按哪套
    比"取决于 import 顺序 —— 那是最难查的一类假绿。
    """
    if not spec.entry_id:
        raise ValueError("entry_id 不能为空")
    if spec.t_kind not in (T_KIND_DATE, T_KIND_TIMESTAMP):
        raise ValueError(f"未知 t_kind={spec.t_kind!r}")
    if spec.entry_id in _REGISTRY and not replace:
        raise ValueError(
            f"入口 {spec.entry_id!r} 已注册；如需覆盖请显式 replace=True，"
            "并确认不是 import 顺序导致的重复注册"
        )
    _REGISTRY[spec.entry_id] = spec
    return spec


def get_entry(entry_id: str) -> EntrySpec:
    """取注册项；未注册抛 `UnregisteredEntryError`（fail-closed）。"""
    spec = _REGISTRY.get(entry_id)
    if spec is None:
        raise UnregisteredEntryError(entry_id, known=tuple(sorted(_REGISTRY)))
    return spec


def list_entries() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def clear_registry_for_tests() -> None:
    """清空注册表 —— **只给测试**用，用来验证"未注册 → fail-closed"。

    生产代码不该调它；调了之后夹具对所有入口都会 fail-closed，症状是全红而不是
    全绿，所以这个方向的误用是安全的。
    """
    _REGISTRY.clear()
