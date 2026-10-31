"""R2-3 独立影子观测口契约常量锁面（批次 20261031gshadow · P-bss-shadow · #-A-#9）。

## 这是什么

一组**纯常量 + 纯断言函数**的「R2-3 影子观测口契约锁」。在锁面版本、默认关、
空态/过期标记、内容不并入 `/hits/scope`、只读交叉 T1/T2 与 P3a 闸、
只读衔接 P3b 三键这几个维度上，把**独立影子观测口契约**钉死。

规则全文见 `docs/buy-sell-signal-shadow-obs.md`。**文档与模块是同一份契约的两个形态**：
文档给人读，模块给测例验。

## 它不是什么

- **不是**行情计算器 / 算档实现：没有写路径 I/O，不读行情、不写 live hits、不调推送。
- **不是** hits schema 改动：本拍**不**改 hits 字段集，**不**把影子写入 live。
- **不是**完整观测 UI / 钉钉接线：怎么画怎么发另口令；本拍只锁观测口契约。
- **不是**放量开关，**不是**部署 #260，**不是**重开 T1/T2 规则，**不是**放宽 P3a。

## 空桶不得报 ``ok``（本契约的关键约束）

零条目只允许 ``closed`` / ``empty``；``stale`` / ``ok`` **必须**有内容。

允许空桶报 ``ok``，观测口就永远显示健康、永远不暴露「根本没数据」这个事实 ——
这与 T1「**无量＝无档**、禁止拿 ``weak`` 顶缺失」是同一个错误的两种形态：
**拿一个「看起来正常」的值去顶「什么都没有」**。

``empty`` 是诚实的坏消息；``ok`` 是不诚实的假好消息。见 ``assert_state_marker_honest``。

## 影子写入 ≠ 观测口 ≠ 放量

P3b ``hits_shadow_store.list_shadow_hits`` 是**核验钩子**，其 docstring 明文写着
「这是核验钩子, 不是 R2-3 观测口」。本模块补齐的是**契约层**（默认关 / 空态过期 /
不并入 scope / 兼容 T1T2）。契约齐备 ≠ 观测 UI 已开 ≠ 可放量 ≠ TRIGGERING 该开。

## 只读交叉的**分层**（为什么三键写死而四元组派生）

- **同层**（T1/T2 锁面、``constants`` 契约常量、``hits_visibility.VisibilityReject``）：
  **只读 import 并对照**。它们本身就是契约层，对照它们是本锁的职责。
- **跨层**（P3b ``hits_shadow_store`` 是**存储实现**）：**不 import**，三键在这里
  **写死字面量**，交叉验证放在测例里做。

理由与 P3b ``SHADOW_MODE`` 的注释同源：**写死的是「这一个具体值」，不是「从某个宽集合里挑一个」**
—— 宽集合哪天多了个近义词就会悄悄放行。同理，锁面若依赖存储实现，「实现改了名字但语义没变」
与「语义被改了」就无法在 import 期区分。分层让本模块保持**纯契约**，且不把 ``config`` /
``Path`` / ``tempfile`` 这些存储侧依赖拉进契约层。

三键与本文件 ``EXPECTED_P3B_BUCKET_KEYS`` 的漂移，由测例
``test_p3b_bucket_keys_match_shadow_store`` 捕获（那里 import 真实 P3b）。

## Bonferroni

本锁＝观测/展示边契约，**不占** ``BONFERRONI_M = 6``；**非永久豁免**。
本模块**不**改 m / scheme。

## 门禁

**路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING**；**P3a 已合 ≠ 可放影子进钉钉**；
**影子写入 ≠ 观测口 ≠ 放量**；**影子 ≠ live 模板**；**字段契约 ≠ 可放量**；
**K8 ≠ 可放量 ≠ TRIGGERING**；**tip ≠ prod**（#260 未部署）；**合入 ≠ 部署**；
**COMPLETE 仍 False ≠ 可翻 COMPLETE**。本拍不部署 #260。

批次 20261031gshadow · P-bss-shadow。
仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。
成本来回 18.2bp。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

from . import tier_cut_lock as _t1
from . import tier_display_lock as _t2
from .constants import BONFERRONI_M, DIRECTION_LABELS
from .hits_visibility import VisibilityReject

__all__ = [
    "EXPECTED_BONFERRONI_M",
    "EXPECTED_DIRECTION_LABELS",
    "EXPECTED_P3A_REJECT_NON_LIVE_MODE",
    "EXPECTED_P3B_BUCKET_KEYS",
    "EXPECTED_P3B_REASON_SHADOW",
    "EXPECTED_T1_RULE_VERSION",
    "EXPECTED_T2_LOCK_VERSION",
    "SHADOW_OBS_CONTENT_MODE",
    "SHADOW_OBS_DEFAULT_ENABLED",
    "SHADOW_OBS_LOCK_NOTE",
    "SHADOW_OBS_LOCK_VERSION",
    "SHADOW_OBS_NONZERO_ITEM_MARKERS",
    "SHADOW_OBS_P3B_BUCKET_KEYS",
    "SHADOW_OBS_SCOPE_ALLOWED_KEYS",
    "SHADOW_OBS_SCOPE_MUST_NOT_CONTAIN",
    "SHADOW_OBS_STATE_MARKERS",
    "SHADOW_OBS_ZERO_ITEM_MARKERS",
    "assert_direction_labels_untouched",
    "assert_no_allow_shadow",
    "assert_p3a_gate_still_green",
    "assert_scope_carries_no_shadow_detail",
    "assert_shadow_obs_lock_invariants",
    "assert_state_marker_honest",
    "assert_t1_t2_cross_lock",
]

# ---------------------------------------------------------------- 锁面版本 / 默认关

SHADOW_OBS_LOCK_VERSION: Final = "shadow_obs_v1"
"""可审计观测口契约版本。

改默认关 / 空态过期语义 / 不并入 scope / 交叉边界 / 三键**必须** bump。
"""

SHADOW_OBS_DEFAULT_ENABLED: Final = False
"""默认关闭。开启须**显式**。

本拍只钉常量，不实现生产开关注入（环境变量 / 开关路由 ＝ 另口令）。
与两处 ``*_TRIGGERING_ENABLED = False`` 同一个取向：默认关 + 出事是「明确开过」，
责任链可追；默认开 + 出事是「忘了关」，追不到。
"""

# ---------------------------------------------------------------- 交叉期望值（只读对照）

EXPECTED_T1_RULE_VERSION: Final = "tier_cut_v1"
EXPECTED_T2_LOCK_VERSION: Final = "tier_display_v1"

EXPECTED_P3A_REJECT_NON_LIVE_MODE: Final = "NON_LIVE_MODE"
"""P3a ``VisibilityReject.NON_LIVE_MODE`` 的期望值。

注意：``VisibilityReject`` 是**普通类属性字符串**容器，不是 ``Enum``
（见 ``hits_visibility.py:103`` 的说明「用字符串而不是枚举」）。
故对照用**字符串直比**，**不是** ``.value``。
"""

EXPECTED_P3B_REASON_SHADOW: Final = "shadow_not_pushable"
"""P3a 门 1b 拒因。``allow_shadow`` **没有**也不许将来加。

本模块不 import ``hits_dingtalk``（推送实现），期望值在此写死，
由测例去比对真实 ``REASON_SHADOW``。
"""

EXPECTED_P3B_BUCKET_KEYS: Final = (
    "signal_id",
    "definition_version",
    "freq_lock_version",
)
"""P3b 分桶三键。写死字面量（理由见模块 docstring「只读交叉的分层」）。

漂移由测例 ``test_p3b_bucket_keys_match_shadow_store`` 捕获。
"""

EXPECTED_DIRECTION_LABELS: Final = frozenset(
    {"research_buy", "research_sell", "research_watch"}
)
EXPECTED_BONFERRONI_M: Final = 6

# ---------------------------------------------------------------- 空态 / 过期

SHADOW_OBS_STATE_MARKERS: Final = frozenset({"closed", "empty", "stale", "ok"})
"""空态 / 过期标记枚举（四值）。

- ``closed``  观测口**未开启**（默认态或显式关闭）
- ``empty``   **已开启**，但分桶内无任何影子条目
- ``stale``   有内容，但**已过期**（超出新鲜窗口）
- ``ok``      有内容，且**未过期**（≠ 可放量）
"""

SHADOW_OBS_ZERO_ITEM_MARKERS: Final = frozenset({"closed", "empty"})
"""**零条目**时**只允许**的标记。

``stale`` / ``ok`` 必须**有内容**：没东西谈不上过期，也谈不上可读。
"""

SHADOW_OBS_NONZERO_ITEM_MARKERS: Final = frozenset({"closed", "stale", "ok"})
"""**有内容**时**只允许**的标记。

``empty`` 不在其中：明明有内容却报「空」，是**撒谎**。
``closed`` 仍在其中：关着的时候，有没有内容都是 ``closed``。
"""

# ---------------------------------------------------------------- 不并入 scope

SHADOW_OBS_SCOPE_ALLOWED_KEYS: Final = frozenset({"non_live_mode_rejected"})
"""``/hits/scope`` 载荷中与影子相关的**唯一**允许项：P3a 报警**计数**。

常态 **0**。非 0 意味着**有影子命中漏进了生产可见层** —— 那是 P3a 闸被绕过的信号，
不是「影子用得挺多」的进度条。**计数可以进，明细不可以进。**
"""

SHADOW_OBS_SCOPE_MUST_NOT_CONTAIN: Final = frozenset(
    {
        "shadow_hits",
        "shadow_obs_items",
        "shadow_bucket_payload",
        "shadow_observation_rows",
        "shadow_detail",
        "shadow_entries",
    }
)
"""``/hits/scope`` 载荷**不得**出现的影子明细键名（示例集；测例可扩）。

锁的是**明细**，不是计数。见 ``SHADOW_OBS_SCOPE_ALLOWED_KEYS``。
"""

# ---------------------------------------------------------------- P3b 只读衔接

SHADOW_OBS_P3B_BUCKET_KEYS: Final = EXPECTED_P3B_BUCKET_KEYS
"""只读衔接 P3b 三键。**不**扩 live、**不**改写入语义、**不**写 live ``hits.json``。"""

SHADOW_OBS_CONTENT_MODE: Final = "shadow"
"""影子内容的来源模式（P3b ``SHADOW_MODE`` 同一具体值）。

与 ``observation`` **不是同义词**：``shadow`` 是**内容**的来源属性；
``observation`` 是**读取态**标记。混用会让「这条内容是哪来的」失去答案。
"""

# ---------------------------------------------------------------- 备注

SHADOW_OBS_LOCK_NOTE: Final = (
    "R2-3影子观测口锁: version=shadow_obs_v1 / 默认关=False / "
    "空态过期=closed|empty|stale|ok / 空桶只许closed|empty / 不并入scope / "
    "只读P3b三键 / 只读交叉T1 tier_cut_v1 + T2 tier_display_v1 + P3a NON_LIVE_MODE / "
    "无allow_shadow / 展示观测边Bonferroni不占m=6。"
    "**路径已通!=calib!=可放量!=TRIGGERING**; "
    "**P3a已合!=可放影子进钉钉**; **影子写入!=观测口!=放量**; **影子!=live模板**; "
    "**字段契约!=可放量**; **tip!=prod(#260未部署)**; **合入!=部署**; "
    "本拍不算档、不写hits、不完整UI、不真推、不部署#260、不翻COMPLETE。"
)


# ---------------------------------------------------------------- 断言助手（由测例调用）


def assert_t1_t2_cross_lock() -> None:
    """只读交叉：T1/T2 version 未被本拍重定义。"""
    if _t1.TIER_CUT_RULE_VERSION != EXPECTED_T1_RULE_VERSION:
        raise AssertionError(
            "T1 version drift: "
            f"{_t1.TIER_CUT_RULE_VERSION!r} != {EXPECTED_T1_RULE_VERSION!r}"
        )
    if _t2.TIER_DISPLAY_LOCK_VERSION != EXPECTED_T2_LOCK_VERSION:
        raise AssertionError(
            "T2 version drift: "
            f"{_t2.TIER_DISPLAY_LOCK_VERSION!r} != {EXPECTED_T2_LOCK_VERSION!r}"
        )


def assert_p3a_gate_still_green() -> None:
    """只读交叉：P3a ``NON_LIVE_MODE`` 仍在且未被改写。

    ``VisibilityReject`` 是**普通类属性字符串**容器（非 ``Enum``），故用字符串直比。
    """
    if not hasattr(VisibilityReject, "NON_LIVE_MODE"):
        raise AssertionError("P3a VisibilityReject.NON_LIVE_MODE missing")
    actual = VisibilityReject.NON_LIVE_MODE
    if actual != EXPECTED_P3A_REJECT_NON_LIVE_MODE:
        raise AssertionError(
            "P3a NON_LIVE_MODE mutated: "
            f"{actual!r} != {EXPECTED_P3A_REJECT_NON_LIVE_MODE!r}"
        )


def assert_no_allow_shadow(names: Iterable[str]) -> None:
    """断言给定命名空间里**没有** ``allow_shadow`` 符号。

    参数是 ``dir(module)`` 之类的名字集合 —— 刻意不做 ``sys.modules`` 自省：
    自省只能查自己，查不了 ``hits_dingtalk``（那里才是「不许加」的本体）。

    影子命中与生产命中形状完全一致，一旦有开关，就有「打开的那天」。
    """
    banned = {"allow_shadow", "ALLOW_SHADOW", "AllowShadow"}
    present = banned & {name for name in names}
    if present:
        raise AssertionError(
            f"allow_shadow must not exist (found {sorted(present)}); "
            "影子命中与生产命中形状一致, 有开关就有打开的那天"
        )


def assert_state_marker_honest(*, marker: str, item_count: int) -> None:
    """空态诚实性：**空桶不得报** ``ok`` / ``stale``。

    双向约束：

    - ``item_count == 0`` → 只允许 ``{closed, empty}``
    - ``item_count > 0``  → 只允许 ``{closed, stale, ok}``
    - ``closed`` 两边都在：关着的时候，有没有内容都是 ``closed``

    禁止「空桶 → ok」是这条的核心。理由见模块 docstring：那是**拿一个看起来正常的值
    去顶什么都没有**，与 T1「无量＝无档、禁止 weak 顶缺失」同一个错误。
    """
    if marker not in SHADOW_OBS_STATE_MARKERS:
        raise AssertionError(
            f"unknown state marker {marker!r}; "
            f"must be one of {sorted(SHADOW_OBS_STATE_MARKERS)}"
        )
    if item_count < 0:
        raise AssertionError(f"item_count must be >= 0, got {item_count!r}")
    allowed = (
        SHADOW_OBS_ZERO_ITEM_MARKERS
        if item_count == 0
        else SHADOW_OBS_NONZERO_ITEM_MARKERS
    )
    if marker not in allowed:
        raise AssertionError(
            f"state marker {marker!r} is dishonest for item_count={item_count}; "
            f"allowed={sorted(allowed)}. "
            "空桶只许 closed|empty; stale|ok 必须有内容"
        )


def assert_scope_carries_no_shadow_detail(
    scope_payload: Iterable[str] | Mapping[str, object],
) -> None:
    """``/hits/scope`` 载荷**不得**含影子明细键；计数项须在允许集内。

    允许：``non_live_mode_rejected``（报警计数，常态 0）。
    禁止：任何影子明细键（``SHADOW_OBS_SCOPE_MUST_NOT_CONTAIN``）。
    """
    # 对 Mapping 迭代即键，对 Iterable[str] 迭代即元素 —— 两种入参同一个表达式。
    keys = {str(k) for k in scope_payload}
    leaked = keys & SHADOW_OBS_SCOPE_MUST_NOT_CONTAIN
    if leaked:
        raise AssertionError(
            f"/hits/scope must not carry shadow detail (found {sorted(leaked)}); "
            "scope 只保留 non_live_mode_rejected 报警计数"
        )
    shadowish = {k for k in keys if k.startswith("shadow")}
    unexpected = shadowish - SHADOW_OBS_SCOPE_ALLOWED_KEYS
    if unexpected:
        raise AssertionError(
            f"/hits/scope carries unexpected shadow-prefixed key(s): {sorted(unexpected)}; "
            f"only {sorted(SHADOW_OBS_SCOPE_ALLOWED_KEYS)} may appear"
        )


def assert_direction_labels_untouched() -> None:
    """对照锚：``DIRECTION_LABELS`` 仍为原三值（本锁面不改之）。"""
    if DIRECTION_LABELS != EXPECTED_DIRECTION_LABELS:
        raise AssertionError(
            f"DIRECTION_LABELS mutated: {DIRECTION_LABELS!r} != {EXPECTED_DIRECTION_LABELS!r}"
        )


def assert_shadow_obs_lock_invariants() -> None:
    """聚合不变量（由测例调用；模块 import 时**不**硬 raise）。"""
    if not SHADOW_OBS_LOCK_VERSION:
        raise AssertionError("SHADOW_OBS_LOCK_VERSION must be non-empty")
    if SHADOW_OBS_LOCK_VERSION != "shadow_obs_v1":
        raise AssertionError(
            "SHADOW_OBS_LOCK_VERSION must be 'shadow_obs_v1', "
            f"got {SHADOW_OBS_LOCK_VERSION!r}"
        )
    if SHADOW_OBS_DEFAULT_ENABLED is not False:
        raise AssertionError(
            f"SHADOW_OBS_DEFAULT_ENABLED must be False, got {SHADOW_OBS_DEFAULT_ENABLED!r}"
        )
    if frozenset({"closed", "empty", "stale", "ok"}) != SHADOW_OBS_STATE_MARKERS:
        raise AssertionError(
            f"SHADOW_OBS_STATE_MARKERS unexpected: {SHADOW_OBS_STATE_MARKERS!r}"
        )
    # 空态诚实性：两个集合必须覆盖全部标记，且 empty 只能在零条目侧
    covered = SHADOW_OBS_ZERO_ITEM_MARKERS | SHADOW_OBS_NONZERO_ITEM_MARKERS
    if covered != SHADOW_OBS_STATE_MARKERS:
        raise AssertionError(
            "zero/nonzero marker sets must cover all state markers; "
            f"got {sorted(SHADOW_OBS_ZERO_ITEM_MARKERS)} | "
            f"{sorted(SHADOW_OBS_NONZERO_ITEM_MARKERS)}"
        )
    if "empty" in SHADOW_OBS_NONZERO_ITEM_MARKERS:
        raise AssertionError("'empty' must not be allowed for non-empty buckets")
    if {"ok", "stale"} & SHADOW_OBS_ZERO_ITEM_MARKERS:
        raise AssertionError(
            "'ok'/'stale' must not be allowed for empty buckets "
            "(空桶不许报 ok —— 那是拿正常值顶什么都没有)"
        )
    if "closed" not in SHADOW_OBS_ZERO_ITEM_MARKERS:
        raise AssertionError("'closed' must be allowed regardless of item count")
    if "closed" not in SHADOW_OBS_NONZERO_ITEM_MARKERS:
        raise AssertionError("'closed' must be allowed regardless of item count")
    # 三键
    if SHADOW_OBS_P3B_BUCKET_KEYS != EXPECTED_P3B_BUCKET_KEYS:
        raise AssertionError(
            f"P3b bucket keys drifted: {SHADOW_OBS_P3B_BUCKET_KEYS!r}"
        )
    if len(EXPECTED_P3B_BUCKET_KEYS) != 3:
        raise AssertionError("P3b bucket must be exactly 3 keys")
    # scope 明细禁集非空，且不得与允许集重叠
    if not SHADOW_OBS_SCOPE_MUST_NOT_CONTAIN:
        raise AssertionError("SHADOW_OBS_SCOPE_MUST_NOT_CONTAIN must be non-empty")
    if SHADOW_OBS_SCOPE_MUST_NOT_CONTAIN & SHADOW_OBS_SCOPE_ALLOWED_KEYS:
        raise AssertionError(
            "scope allowed/forbidden key sets must be disjoint"
        )
    # 内容模式必须在 T1 展示值域内（只读交叉）
    if SHADOW_OBS_CONTENT_MODE not in _t1.TIER_DISPLAY_MODES:
        raise AssertionError(
            f"content mode {SHADOW_OBS_CONTENT_MODE!r} "
            f"not in T1 TIER_DISPLAY_MODES {sorted(_t1.TIER_DISPLAY_MODES)}"
        )
    assert_t1_t2_cross_lock()
    assert_p3a_gate_still_green()
    assert_direction_labels_untouched()
    if BONFERRONI_M != EXPECTED_BONFERRONI_M:
        raise AssertionError(f"BONFERRONI_M mutated: {BONFERRONI_M!r}")
