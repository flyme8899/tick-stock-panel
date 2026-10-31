"""hits schema 真推门禁契约常量锁面（批次 20261031gschema · P-bss-schema-push · #-A-#9）。

## 这是什么

一组**纯常量 + 纯断言函数**的「hits schema 真推门禁契约锁」，把契约落成可验形态：

- 锁面版本 `schema_push_v1`
- `HITS_SCHEMA_VERSION` **仍 `"1"`**（只读断言，不 bump）
- 两处 `*_TRIGGERING_ENABLED` **仍 False**（只读断言，不改取值）
- `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` **仍 False**（不翻）
- **无 `allow_shadow`**
- 只读交叉 `trigger_gate_v1` / `shadow_obs_v1` / T1 `tier_cut_v1` / T2 `tier_display_v1`
- 方向仍三值、`BONFERRONI_M` 仍 6

规则全文见 `docs/buy-sell-signal-schema-push.md`。文档给人读，模块给测例验。

## 它不是什么

- **不是**立刻真推 live：不调钉钉、不写 hits、不动 live 模板
- **不是** bump schema：`HITS_SCHEMA_VERSION` 这里是**只读**的，禁止赋值
- **不是**完整 schema 迁移：没有存量回填、没有兼容层、没有调度
- **不是**授权真推 / 不是授权翻闸 / 不是可放量声明
- **不是**改 `hits_models` 字段集；**不是**改 `build_hit_dingtalk_text`
- **不是**行情计算器 / 调度器；**不是** Opt-A / auction·17-19 / PanWatch

## schema 门禁契约 ≠ 授权真推 live

写完这份契约，真推的门槛**一点都没降低**——只是把门槛写清楚了。
「这九项我核过了」不等于「可以真推」：表是**否决清单**，不是通行证。

基础设施在场（version / models / dingtalk）也只说明「能力存在」，
不说明「门槛已跨」—— 能力存在与授权存在是两件事。

## 为什么用**模块别名**而不是 `from x import CONST`

全模块统一 `from . import constants as _c` 再 `_c.HITS_SCHEMA_VERSION`，
**不是** `from .constants import HITS_SCHEMA_VERSION`。

区别是实质的：`from x import CONST` 在 import 期把值**拷进本模块命名空间**，之后
`monkeypatch.setattr(_c, "HITS_SCHEMA_VERSION", "2")` 改的是源模块属性，本模块的副本
**纹丝不动** —— 断言于是变成**永远为真的守卫**，负控（把 version bump 掉看断言是否发火）
会假绿。

模块别名是**活引用**：每次读都回到源模块。这样「有人 bump 了 schema / 翻了开关」能在断言里当场炸。
同一理由适用于 version 交叉与 `DIRECTION_LABELS` / `BONFERRONI_M`。

## 门禁大字

路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING；
K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING；
讨论 ≠ 派单；影子写入 ≠ 观测口 ≠ 放量；
合入 ≠ 部署（tip = prod）；
**门禁契约 ≠ 授权翻闸**；
**schema 门禁契约 ≠ 授权真推 live**；COMPLETE 仍 False ≠ 可翻 COMPLETE。

批次 20261031gschema · P-bss-schema-push。
仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。
成本来回 18.2bp。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from . import auction_abnormal_stub as _auc
from . import constants as _c
from . import first_seen_audit as _fsa
from . import shadow_obs_lock as _shadow
from . import tier_cut_lock as _t1
from . import tier_display_lock as _t2
from . import trigger_gate_lock as _tg
from . import volume_breakout_stub as _vol

__all__ = [
    "EXPECTED_BONFERRONI_M",
    "EXPECTED_DIRECTION_LABELS",
    "EXPECTED_HITS_SCHEMA_VERSION",
    "EXPECTED_SHADOW_OBS_LOCK_VERSION",
    "EXPECTED_T1_RULE_VERSION",
    "EXPECTED_T2_LOCK_VERSION",
    "EXPECTED_TRIGGER_GATE_LOCK_VERSION",
    "SCHEMA_PUSH_LOCK_NOTE",
    "SCHEMA_PUSH_LOCK_VERSION",
    "SCHEMA_PUSH_REFUSED_ACTION_PHRASES",
    "SCHEMA_PUSH_REFUSED_PAYLOAD_FIELDS",
    "assert_bonferroni_untouched",
    "assert_complete_still_false",
    "assert_direction_labels_untouched",
    "assert_gate_shadow_t1_t2_cross_lock",
    "assert_hits_schema_still_v1",
    "assert_no_allow_shadow",
    "assert_schema_push_lock_invariants",
    "assert_triggering_still_false",
]

# ---------------------------------------------------------------- 锁面版本

SCHEMA_PUSH_LOCK_VERSION: Final = "schema_push_v1"
"""可审计 schema 真推门禁契约版本。

改前置表口径 / 与 live 模板边界 / 交叉边界 / 禁项**必须** bump。
"""

# ---------------------------------------------------------------- 交叉期望值

EXPECTED_HITS_SCHEMA_VERSION: Final = "1"
"""hits schema 期望版本。本拍**只读** —— bump 是「另口令」的事，不在这里做。"""

EXPECTED_TRIGGER_GATE_LOCK_VERSION: Final = "trigger_gate_v1"
EXPECTED_SHADOW_OBS_LOCK_VERSION: Final = "shadow_obs_v1"
EXPECTED_T1_RULE_VERSION: Final = "tier_cut_v1"
EXPECTED_T2_LOCK_VERSION: Final = "tier_display_v1"

EXPECTED_DIRECTION_LABELS: Final = frozenset(
    {"research_buy", "research_sell", "research_watch"}
)
EXPECTED_BONFERRONI_M: Final = 6

SCHEMA_PUSH_REFUSED_ACTION_PHRASES: Final = (
    "可以买入",
    "建议买入",
    "强烈买入",
    "可以卖出",
    "建议卖出",
    "强烈卖出",
)
"""六词动作指令式（及同义）—— schema 文档与载荷**禁止**出现。

方向是 `research_*` 三值，不是"买入/卖出"指令。把研究结论写成动作指令，
读者会把它当操作建议，而这套东西从来没有那个授权。
"""

SCHEMA_PUSH_REFUSED_PAYLOAD_FIELDS: Final = (
    "price",
    "qty",
    "quantity",
    "position",
    "order",
    "order_id",
    "amount",
    "shares",
)
"""载荷**禁写**字段族：价格 / 数量 / 仓位 / 订单。

schema 存的是「研究了什么、结论方向、时点」，不是「去下什么单」。
"""

# ---------------------------------------------------------------- 备注

SCHEMA_PUSH_LOCK_NOTE: Final = (
    "hits schema 真推门禁锁: version=schema_push_v1 / "
    "HITS_SCHEMA_VERSION仍\"1\"不bump / 两TRIGGERING Final=False非env / "
    "COMPLETE仍False / 无allow_shadow / "
    "只读交叉 trigger_gate_v1 + shadow_obs_v1 + T1 tier_cut_v1 + T2 tier_display_v1 / "
    "方向三值 + m=6不动 / 门禁边Bonferroni不占m=6非永久豁免。"
    "**路径已通!=calib!=可放量!=TRIGGERING**; "
    "**K8 PASS!=可放量!=可开TRIGGERING**; **讨论!=派单**; "
    "**影子写入!=观测口!=放量**; **门禁契约!=授权翻闸**; "
    "**schema 门禁契约!=授权真推live**; **COMPLETE仍False!=可翻COMPLETE**; "
    "**合入!=部署**; tip=prod。"
    "本拍不真推、不bump schema、不算行情、不调度、不写hits、不改models/模板。"
)


# ---------------------------------------------------------------- 断言助手（由测例调用）


def assert_hits_schema_still_v1() -> None:
    """`HITS_SCHEMA_VERSION` 必须**仍是**字符串 `"1"`。

    用 `!=` 比较字符串而不是比数值：`1`（int）与 `"1"`（str）在业务上是两种东西，
    契约钉的是**字面量**，不是"能解析成 1 就行"。
    """
    if _c.HITS_SCHEMA_VERSION != EXPECTED_HITS_SCHEMA_VERSION:
        raise AssertionError(
            f"HITS_SCHEMA_VERSION must stay {EXPECTED_HITS_SCHEMA_VERSION!r}, "
            f"got {_c.HITS_SCHEMA_VERSION!r}; "
            "bump schema 当真推须所有者明示口令 + 另 ALIGN/FIX"
        )


def assert_triggering_still_false() -> None:
    """两处 `*_TRIGGERING_ENABLED` 必须**仍是** `False`。

    用 `is not False` 而不是 `not x`：`0` / `""` / `None` 在布尔语境也"假"，
    但契约说的是「恒 False」这个**布尔事实**，不是"假值即可"。
    """
    if _vol.VOLUME_BREAKOUT_TRIGGERING_ENABLED is not False:
        raise AssertionError(
            "VOLUME_BREAKOUT_TRIGGERING_ENABLED must stay False, "
            f"got {_vol.VOLUME_BREAKOUT_TRIGGERING_ENABLED!r}; "
            "翻闸须所有者明示口令 + 另 ALIGN/FIX"
        )
    if _auc.AUCTION_ABNORMAL_TRIGGERING_ENABLED is not False:
        raise AssertionError(
            "AUCTION_ABNORMAL_TRIGGERING_ENABLED must stay False, "
            f"got {_auc.AUCTION_ABNORMAL_TRIGGERING_ENABLED!r}; "
            "翻闸须所有者明示口令 + 另 ALIGN/FIX"
        )


def assert_complete_still_false() -> None:
    """`EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` 必须**仍是** `False`（A2 完成门仍关）。"""
    if _fsa.EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is not False:
        raise AssertionError(
            "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE must stay False, "
            f"got {_fsa.EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE!r}; "
            "翻 COMPLETE 须另口令"
        )


def assert_no_allow_shadow(names: Iterable[str]) -> None:
    """断言给定命名空间里**没有** `allow_shadow` 符号。

    参数是 `dir(module)` 之类的名字集合 —— 刻意不做 `sys.modules` 自省：
    自省只能查自己，查不了推送那处（「不许加」的本体）。

    影子命中与生产命中形状完全一致，一旦有开关，就有「打开的那天」。
    schema 真推门禁尤其不能有这个旁路：它会绕过 P3a 闸直通 live 模板。
    """
    banned = {"allow_shadow", "ALLOW_SHADOW", "AllowShadow"}
    present = banned & {name for name in names}
    if present:
        raise AssertionError(
            f"allow_shadow forbidden near schema push gate (found {sorted(present)}); "
            "有开关就有打开的那天"
        )


def assert_gate_shadow_t1_t2_cross_lock() -> None:
    """只读交叉：`trigger_gate_v1` / `shadow_obs_v1` / T1 / T2 未被重定义。

    本锁面**不重定义**这些 version —— 漂移说明上游契约换了版本，
    schema 侧的前置条件表必须跟着复核，不能假装没发生。
    """
    if _tg.TRIGGER_GATE_LOCK_VERSION != EXPECTED_TRIGGER_GATE_LOCK_VERSION:
        raise AssertionError(
            "trigger_gate version drift: "
            f"{_tg.TRIGGER_GATE_LOCK_VERSION!r} != {EXPECTED_TRIGGER_GATE_LOCK_VERSION!r}"
        )
    if _shadow.SHADOW_OBS_LOCK_VERSION != EXPECTED_SHADOW_OBS_LOCK_VERSION:
        raise AssertionError(
            "shadow_obs version drift: "
            f"{_shadow.SHADOW_OBS_LOCK_VERSION!r} != {EXPECTED_SHADOW_OBS_LOCK_VERSION!r}"
        )
    if _shadow.SHADOW_OBS_DEFAULT_ENABLED is not False:
        raise AssertionError(
            "shadow_obs default-enabled drifted: "
            f"{_shadow.SHADOW_OBS_DEFAULT_ENABLED!r}; 观测口默认关取向须一致"
        )
    if _t1.TIER_CUT_RULE_VERSION != EXPECTED_T1_RULE_VERSION:
        raise AssertionError(
            f"T1 version drift: {_t1.TIER_CUT_RULE_VERSION!r} != {EXPECTED_T1_RULE_VERSION!r}"
        )
    if _t2.TIER_DISPLAY_LOCK_VERSION != EXPECTED_T2_LOCK_VERSION:
        raise AssertionError(
            "T2 version drift: "
            f"{_t2.TIER_DISPLAY_LOCK_VERSION!r} != {EXPECTED_T2_LOCK_VERSION!r}"
        )


def assert_direction_labels_untouched() -> None:
    """对照锚：`DIRECTION_LABELS` 仍为原三值（本锁面不改之，`research_watch` 仍在）。"""
    if _c.DIRECTION_LABELS != EXPECTED_DIRECTION_LABELS:
        raise AssertionError(
            "DIRECTION_LABELS mutated: "
            f"{_c.DIRECTION_LABELS!r} != {EXPECTED_DIRECTION_LABELS!r}"
        )


def assert_bonferroni_untouched() -> None:
    """对照锚：`BONFERRONI_M` 仍 6（门禁边不占 m，但也不许顺手改 m）。"""
    if _c.BONFERRONI_M != EXPECTED_BONFERRONI_M:
        raise AssertionError(
            f"BONFERRONI_M mutated: {_c.BONFERRONI_M!r} != {EXPECTED_BONFERRONI_M!r}"
        )


def assert_schema_push_lock_invariants() -> None:
    """聚合不变量（由测例调用；模块 import 时**不**硬 raise）。"""
    if not SCHEMA_PUSH_LOCK_VERSION:
        raise AssertionError("SCHEMA_PUSH_LOCK_VERSION must be non-empty")
    if SCHEMA_PUSH_LOCK_VERSION != "schema_push_v1":
        raise AssertionError(
            "SCHEMA_PUSH_LOCK_VERSION must be 'schema_push_v1', "
            f"got {SCHEMA_PUSH_LOCK_VERSION!r}"
        )
    assert_hits_schema_still_v1()
    assert_triggering_still_false()
    assert_complete_still_false()
    assert_gate_shadow_t1_t2_cross_lock()
    assert_direction_labels_untouched()
    assert_bonferroni_untouched()
    assert_no_allow_shadow(dir(_vol))
    assert_no_allow_shadow(dir(_auc))
    assert_no_allow_shadow(dir(_c))
    if len(SCHEMA_PUSH_REFUSED_ACTION_PHRASES) != 6:
        raise AssertionError(
            "six-word action phrases must be exactly 6, "
            f"got {len(SCHEMA_PUSH_REFUSED_ACTION_PHRASES)}"
        )
    if not SCHEMA_PUSH_REFUSED_PAYLOAD_FIELDS:
        raise AssertionError("SCHEMA_PUSH_REFUSED_PAYLOAD_FIELDS must be non-empty")
