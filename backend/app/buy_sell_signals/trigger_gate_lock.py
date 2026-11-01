"""触发/放量门禁契约常量锁面（批次 20261031gtrig · P-bss-trigger · #-A-#9）。

## 这是什么

一组**纯常量 + 纯断言函数**的「触发/放量门禁契约锁」，把门禁契约落成可验形态：

- 锁面版本 `trigger_gate_v1`
- 两处 `*_TRIGGERING_ENABLED` **仍 False**（只读断言，不改取值）
- `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` **仍 False**（不翻）
- **无 `allow_shadow`**
- 只读交叉 `shadow_obs_v1` / T1 `tier_cut_v1` / T2 `tier_display_v1`
- 两族证据缺口**仍开着**：已清 1 / 剩 7（批次 20261101gevid 清了 VOLUME 的
  `trigger_evidence_table_data_pending`，**清零一刀 ≠ 可翻闸**）

规则全文见 `docs/buy-sell-signal-trigger-gate.md`。文档给人读，模块给测例验。

## 它不是什么

- **不是**触发引擎：没有量比计算、没有突破判定、没有窗口逻辑、没有调度
- **不是**授权翻闸：本模块**禁止**把任一 `*_TRIGGERING_ENABLED` 赋 True
- **不是**可放量声明；**不是**改 stub 取值
- **不是** schema 进 hits；**不是**真推；**不是** Opt-A / auction·17-19 / PanWatch

## 门禁契约 ≠ 授权翻闸

写完这份契约，翻闸的门槛**一点都没降低**——只是把门槛写清楚了。写清楚门槛不等于跨过门槛。

## 为什么用**模块别名**而不是 `from x import CONST`

全模块统一 `from . import volume_breakout_stub as _vol` 再 `_vol.VOLUME_...`，
**不是** `from .volume_breakout_stub import VOLUME_...`。

区别是实质的：`from x import CONST` 在 import 期把值**拷进本模块命名空间**，之后
`monkeypatch.setattr(_vol, "CONST", True)` 改的是源模块属性，本模块的副本**纹丝不动** ——
断言于是变成**永远为真的守卫**，负控（把开关改成 True 看断言是否发火）会假绿。

模块别名是**活引用**：每次读都回到源模块。这样「有人翻了开关」能在断言里当场炸。
同一理由适用于 version 交叉与 `DIRECTION_LABELS` / `BONFERRONI_M`。

## 证据缺口是 8 条，不是一句「待补」

`TRIGGER_GATE_EVIDENCE_PENDING_COUNT` 由两族 `*_EVIDENCE_PENDING` **只读求和**，
不写 `8` 这个字面量。手抄的数字会在缺口被补掉后继续骗人。

`assert_evidence_pending_still_open()` 断了**不代表改坏了**，代表**文档 §4.1 该更新了** ——
缺口补掉是好事，但契约描述必须跟着动，否则表就成了「已经补完了」的假象。

## 门禁大字

路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING；
K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING；
讨论 ≠ 派单；影子写入 ≠ 观测口 ≠ 放量；
**门禁契约 ≠ 授权翻闸**；COMPLETE 仍 False ≠ 可翻 COMPLETE；
合入 ≠ 部署（tip = prod）。

批次 20261031gtrig · P-bss-trigger。
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
from . import volume_breakout_stub as _vol

__all__ = [
    "EXPECTED_BONFERRONI_M",
    "EXPECTED_DIRECTION_LABELS",
    "EXPECTED_SHADOW_OBS_LOCK_VERSION",
    "EXPECTED_T1_RULE_VERSION",
    "EXPECTED_T2_LOCK_VERSION",
    "TRIGGER_GATE_EVIDENCE_PENDING_COUNT",
    "TRIGGER_GATE_EVIDENCE_STATUS_EXPECTED",
    "TRIGGER_GATE_LOCK_NOTE",
    "TRIGGER_GATE_LOCK_VERSION",
    "TRIGGER_GATE_REFUSED_ACTION_PHRASES",
    "assert_complete_still_false",
    "assert_direction_labels_untouched",
    "assert_evidence_pending_still_open",
    "assert_no_allow_shadow",
    "assert_shadow_t1_t2_cross_lock",
    "assert_trigger_gate_lock_invariants",
    "assert_triggering_still_false",
]

# ---------------------------------------------------------------- 锁面版本

TRIGGER_GATE_LOCK_VERSION: Final = "trigger_gate_v1"
"""可审计门禁契约版本。

改前置表口径 / 缺口披露方式 / 交叉边界 / 禁项**必须** bump。
"""

# ---------------------------------------------------------------- 交叉期望值

EXPECTED_SHADOW_OBS_LOCK_VERSION: Final = "shadow_obs_v1"
EXPECTED_T1_RULE_VERSION: Final = "tier_cut_v1"
EXPECTED_T2_LOCK_VERSION: Final = "tier_display_v1"

EXPECTED_DIRECTION_LABELS: Final = frozenset(
    {"research_buy", "research_sell", "research_watch"}
)
EXPECTED_BONFERRONI_M: Final = 6

TRIGGER_GATE_EVIDENCE_STATUS_EXPECTED: Final = "data_pending"
"""两族评估状态的期望值。

`data_pending` = 这次评估**没有结论**。这是诚实的：连一条可信证据都没有，
不该是「已可触发」。
"""

TRIGGER_GATE_EVIDENCE_PENDING_COUNT: Final = len(
    _vol.VOLUME_BREAKOUT_EVIDENCE_PENDING
) + len(_auc.AUCTION_ABNORMAL_EVIDENCE_PENDING)
"""两族待补证据**总条数**（只读求和，不写死 8 / 7）。

清掉一条缺口，这个数字**自己就会变小**（批次 20261101gevid 清了 VOLUME 的
触发表标签，8 → 7，没改这一行代码）。这正是它派生的意义：手抄的数字会在缺口
被补掉后继续骗人。

**但它不会替你把文档改好。** `assert_evidence_pending_still_open` 只在
「某一族被清空」或「`*_STATUS` 漂移」时才炸；清个一条两条，两族都还非空、
STATUS 还是 `data_pending`，断言照过。所以"清了一条"这件事**必须靠人同步
文档 §4.1 / 锁面 NOTE / 测例期望**，断言兜不住 —— 别把它当成自动提醒。
"""

TRIGGER_GATE_REFUSED_ACTION_PHRASES: Final = (
    "可以买入",
    "建议买入",
    "强烈买入",
    "可以卖出",
    "建议卖出",
    "强烈卖出",
)
"""六词动作指令式（及同义）—— 门禁文档与载荷**禁止**出现。

方向是 `research_*` 三值，不是"买入/卖出"指令。把研究结论写成动作指令，
读者会把它当操作建议，而这套东西从来没有那个授权。
"""

# ---------------------------------------------------------------- 备注

TRIGGER_GATE_LOCK_NOTE: Final = (
    "触发/放量门禁锁: version=trigger_gate_v1 / "
    "两TRIGGERING Final=False非env / COMPLETE仍False / 无allow_shadow / "
    "只读交叉 shadow_obs_v1 + T1 tier_cut_v1 + T2 tier_display_v1 / "
    "证据缺口已清1/剩7(触发表契约; 清零一刀!=可翻闸; 表PRESENT!=可开TRIGGERING; "
    "strongly_negative!=阈值已锁) / 门禁边Bonferroni不占m=6非永久豁免。"
    "**路径已通!=calib!=可放量!=TRIGGERING**; "
    "**K8 PASS!=可放量!=可开TRIGGERING**; **讨论!=派单**; "
    "**影子写入!=观测口!=放量**; **门禁契约!=授权翻闸**; "
    "**清零一刀!=可翻闸**; "
    "**COMPLETE仍False!=可翻COMPLETE**; **合入!=部署**; tip=prod。"
    "本拍不翻闸、不算行情、不调度、不写hits、不真推、不改stub取值。"
)


# ---------------------------------------------------------------- 断言助手（由测例调用）


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


def assert_evidence_pending_still_open() -> None:
    """两族证据缺口**仍开着**，且评估状态仍 `data_pending`。

    **这条断了不代表改坏了** —— 缺口被补掉是好事。它断的含义是：
    **文档 §4.1 的缺口清单与缺口条数描述已经过时**，必须同步更新。

    **但它抓不住"只清了一两条"。** 判定只有三件事：某族 pending 为空、STATUS 漂移、
    COUNT 与派生不一致。批次 20261101gevid 清掉 VOLUME 的触发表标签后，VOLUME 仍有
    3 条、STATUS 仍 `data_pending`、COUNT 仍是派生值 —— 这条断言**照过**。

    所以「清了 1 条、8 变 7」这件事，是靠**人**同步文档/锁面 NOTE/测例期望的，
    不是靠这条断言提醒。把它当成自动兜底，就会在真清空之前一直读到过期的"0/8"。
    """
    for family, pending, status in (
        ("volume_breakout", _vol.VOLUME_BREAKOUT_EVIDENCE_PENDING, _vol.VOLUME_BREAKOUT_STATUS),
        ("auction_abnormal", _auc.AUCTION_ABNORMAL_EVIDENCE_PENDING, _auc.AUCTION_ABNORMAL_STATUS),
    ):
        if not pending:
            raise AssertionError(
                f"{family} EVIDENCE_PENDING is empty — 缺口已清零?"
                " 若确已补完, 请同步更新 docs/buy-sell-signal-trigger-gate.md §4.1"
            )
        if status != TRIGGER_GATE_EVIDENCE_STATUS_EXPECTED:
            raise AssertionError(
                f"{family} status drifted: {status!r} != "
                f"{TRIGGER_GATE_EVIDENCE_STATUS_EXPECTED!r}; "
                "契约文档 §4.1 需同步"
            )
    expected_total = len(_vol.VOLUME_BREAKOUT_EVIDENCE_PENDING) + len(
        _auc.AUCTION_ABNORMAL_EVIDENCE_PENDING
    )
    if expected_total != TRIGGER_GATE_EVIDENCE_PENDING_COUNT:
        raise AssertionError(
            "TRIGGER_GATE_EVIDENCE_PENDING_COUNT drifted from live pending sets: "
            f"{TRIGGER_GATE_EVIDENCE_PENDING_COUNT} != {expected_total}"
        )


def assert_no_allow_shadow(names: Iterable[str]) -> None:
    """断言给定命名空间里**没有** `allow_shadow` 符号。

    参数是 `dir(module)` 之类的名字集合 —— 刻意不做 `sys.modules` 自省：
    自省只能查自己，查不了推送那处（「不许加」的本体）。

    影子命中与生产命中形状完全一致，一旦有开关，就有「打开的那天」。
    """
    banned = {"allow_shadow", "ALLOW_SHADOW", "AllowShadow"}
    present = banned & {name for name in names}
    if present:
        raise AssertionError(
            f"allow_shadow forbidden near trigger gate (found {sorted(present)}); "
            "有开关就有打开的那天"
        )


def assert_shadow_t1_t2_cross_lock() -> None:
    """只读交叉：`shadow_obs_v1` / T1 `tier_cut_v1` / T2 `tier_display_v1` 未被重定义。"""
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


def assert_trigger_gate_lock_invariants() -> None:
    """聚合不变量（由测例调用；模块 import 时**不**硬 raise）。"""
    if not TRIGGER_GATE_LOCK_VERSION:
        raise AssertionError("TRIGGER_GATE_LOCK_VERSION must be non-empty")
    if TRIGGER_GATE_LOCK_VERSION != "trigger_gate_v1":
        raise AssertionError(
            "TRIGGER_GATE_LOCK_VERSION must be 'trigger_gate_v1', "
            f"got {TRIGGER_GATE_LOCK_VERSION!r}"
        )
    assert_triggering_still_false()
    assert_complete_still_false()
    assert_shadow_t1_t2_cross_lock()
    assert_direction_labels_untouched()
    assert_bonferroni_untouched()
    assert_evidence_pending_still_open()
    assert_no_allow_shadow(dir(_vol))
    assert_no_allow_shadow(dir(_auc))
    if not TRIGGER_GATE_REFUSED_ACTION_PHRASES:
        raise AssertionError("TRIGGER_GATE_REFUSED_ACTION_PHRASES must be non-empty")
    if len(TRIGGER_GATE_REFUSED_ACTION_PHRASES) != 6:
        raise AssertionError(
            "six-word action phrases must be exactly 6, "
            f"got {len(TRIGGER_GATE_REFUSED_ACTION_PHRASES)}"
        )
