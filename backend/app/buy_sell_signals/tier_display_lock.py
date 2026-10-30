"""T2 展示层字段契约常量锁面（批次 20261030gt2 · P-bss-t2-tier-display · #-A-#12）。

## 这是什么

一组**纯常量 + 纯断言函数**的「T2 展示层字段契约锁」：锁面版本、展示载荷**必填四元组**
（轨 + asof 粒度 + mode + tier 或显式无档）、**禁六词动作指令式**、**禁价量仓单**、
与 T1 `tier_cut_lock` 的**只读交叉**、与 `DIRECTION_LABELS` 正交 —— 在这几个维度上把
**展示层字段契约**钉死。

契约全文见 `docs/buy-sell-signal-tier-display.md`。**文档与模块是同一份契约的两个形态**：
文档给人读，模块给测例验。两者不一致时以测例为准 —— 因为测例会红。

## 它不是什么

- **不是**行情计算器 / 算档实现：没有 I/O，不读行情、不写文件、不碰 `HitStore`、不调推送。
- **不是** hits 写路径：档位**不**落盘，本拍**不**改 hits schema。
- **不是** UI / 钉钉接线：怎么画怎么发另口令；本拍只锁字段契约。
- **不是**放量开关，**不是**观测口开通（#-A-#9 另口令），**不是**重开 T1 规则。

## 字段契约 ≠ 可放量

`TIER_DISPLAY_*` 齐备只说明「**展示字段契约定定了**」。它**不**说明：可放量、
`TRIGGERING` 该开、`COMPLETE` 该翻、schema 已进 hits、真推已上线、算档已接线。
这六件事没有一件能由本模块推出。

一份字段表写得很整齐的文档，最容易被读成「所以这套东西已经能上线了」。不是。

## T1 ≺ T2（T1 已满足）

本模块只锁 **T2**（字段契约）。T1（切档规则，`tier_cut_v1`）已由 `tier_cut_lock` / #259
交付并部署在 tip＝prod 上。

本模块**只读** `import` `tier_cut_lock` 做交叉断言 —— **不**重定义阈值 / 量 / 规则版本，
**不**改其任何常量。T1 那边若漂了，这里的 `assert_t1_cross_lock()` 会红。这就是
"只读接线"的正确形态：**读它、验它、不改它**。

## 与方向正交

档＝弱/中/强（**展示强度**）。方向仍由 `constants.DIRECTION_LABELS` 三值封闭
（`research_buy` / `research_sell` / `research_watch`）。

两条轴互不嵌套。禁止六词动作指令式（可/建议/强烈 + 买入/卖出）；禁止把
买/卖/加仓/减仓/开多/开空 当档名或载荷文案；**禁止删 `research_watch`** ——
观察档不是「最弱的买卖」，它是另一种状态。本模块**不**改 `DIRECTION_LABELS`。

## 无量＝无档（显式）

无连续量 → **显式无档**（`no_tier=True` 且 `tier=None`）。**禁止用 `weak` 顶缺失**。

`weak` 一旦被用作缺失的填充值，这个档名就**再也不能表示"真的弱"** —— 那是不可逆的
语义污染。填充 0 更糟：0 在很多连续量上是真实取值（行业垫底），把「不知道」渲染成
「最差」，页面上看不出任何异常。

## 展示边 ≠ 校准边

本锁＝展示边字段契约，**不占** `BONFERRONI_M = 6`；**非永久豁免**。
本模块**不**改 m / scheme。校准边日后若要占用预算，须新锁 + 重算 / bump，不得在本锁上
顺手加格。

## 措辞检查的两种匹配方式（写在明处）

- **六词长短语**（「建议买入」等）：子串匹配可用，四字短语误报率低；
- **短词**（买 / 卖 / 加仓 / 减仓 / 开多 / 开空）：**只查档名**，不做全文子串扫描——
  中文里「买卖信号」是本项目名，子串扫必然误报。

**这条限制是设计的一部分，不是漏做。** 一个"看起来全覆盖、实际天天误报"的守卫，很快
会被加 `# noqa` 关掉；说清边界比假装全覆盖有用。

批次 20261030gt2 · P-bss-t2-tier-display。
仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。
成本来回 18.2bp。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

from . import tier_cut_lock as _t1
from .constants import BONFERRONI_M, BONFERRONI_SCHEME_ID, DIRECTION_LABELS

__all__ = [
    "EXPECTED_BONFERRONI_M",
    "EXPECTED_BONFERRONI_SCHEME_ID",
    "EXPECTED_DIRECTION_LABELS",
    "EXPECTED_T1_RULE_VERSION",
    "TIER_DISPLAY_FORBIDDEN_ACTION_PHRASES",
    "TIER_DISPLAY_FORBIDDEN_PAYLOAD_FIELDS",
    "TIER_DISPLAY_FORBIDDEN_SHORT_ACTION_WORDS",
    "TIER_DISPLAY_LOCK_NOTE",
    "TIER_DISPLAY_LOCK_VERSION",
    "TIER_DISPLAY_NO_TIER_KEY",
    "TIER_DISPLAY_NO_TIER_REASON_KEY",
    "TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS",
    "assert_direction_labels_untouched",
    "assert_no_forbidden_action_phrases",
    "assert_no_forbidden_payload_fields",
    "assert_payload_quad_or_no_tier",
    "assert_t1_cross_lock",
    "assert_tier_display_lock_invariants",
    "assert_tier_label_is_neutral",
]

# ---------------------------------------------------------------- 锁面版本

TIER_DISPLAY_LOCK_VERSION: Final = "tier_display_v1"
"""可审计字段契约版本。改四元组语义 / 无档形状 / 禁止项 **必须 bump**。"""

EXPECTED_T1_RULE_VERSION: Final = "tier_cut_v1"
"""交叉断言期望的 T1 规则版本。本拍**只读对照**, 不重定义。"""

# ---------------------------------------------------------------- 载荷必填四元组

TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS: Final = (
    *_t1.TIER_DISPLAY_REQUIRED_KEYS,  # ("track", "asof_grain", "mode") —— 由 T1 派生
    "tier",
)
"""展示载荷逻辑键 = T1 三项**派生** + `tier`。

**派生而不是抄字面量**: 抄一份的话, T1 哪天改了键名, 这里会静默地继续用旧名; 派生 +
`assert_t1_cross_lock()` 会在那一刻红。

分层读法: T1 的三项指认「这个档是什么」, 第四项 `tier` 给「值是多少」。
"""

TIER_DISPLAY_NO_TIER_KEY: Final = "no_tier"
"""显式无档标志键。

`no_tier=True` → `tier` 必须为 `None`；`no_tier=False` → `tier` 必须 ∈ `TIER_LABELS`。
**禁止用 `weak` 顶缺失。** 没有第三种形状: 「`tier` 缺键」不是无档, 是**不合规** ——
缺键与"明确没有"必须分开, 否则下游分不清「字段漏了」和「确实没算出来」。
"""

TIER_DISPLAY_NO_TIER_REASON_KEY: Final = "no_tier_reason"
"""无档原因键（可选, 但 `no_tier=True` 时强烈建议填）。

它是给**读者**的解释, 不是给程序的补丁: 「这条没有档」而非「这条算失败了」。
"""

# ---------------------------------------------------------------- 禁止项

TIER_DISPLAY_FORBIDDEN_ACTION_PHRASES: Final = frozenset(
    {
        "可以买入",
        "建议买入",
        "强烈买入",
        "可以卖出",
        "建议卖出",
        "强烈卖出",
    }
)
"""六词动作指令式（及实现侧可扩同义）。禁止出现在档名或载荷文案。

这类短语做**子串匹配**是安全的（四字短语误报率低）。
"""

TIER_DISPLAY_FORBIDDEN_SHORT_ACTION_WORDS: Final = frozenset(
    {"买", "卖", "加仓", "减仓", "开多", "开空"}
)
"""短动作指令词。**只用于档名检查**（`assert_tier_label_is_neutral`），不做全文子串扫描。

为什么分开: 中文里「买卖信号」是本项目名, 全文子串扫必然误报。一个"看起来全覆盖、
实际天天误报"的守卫, 很快会被加 `# noqa` 关掉 —— 说清边界比假装全覆盖有用。
"""

TIER_DISPLAY_FORBIDDEN_PAYLOAD_FIELDS: Final = frozenset(
    {
        "price",
        "quantity",
        "position",
        "order",
        "价格",
        "数量",
        "仓位",
        "订单",
    }
)
"""展示载荷内禁止的价格 / 数量 / 仓位 / 订单字段名（中英）。

理由: 这是**研究档**的展示载荷。载荷里一出现价量仓单, 下游任何一段代码都可以"顺手"
把它当成下单参数用 —— 而它本来只是一句话的强度描述。**字段存在本身就在邀请越界。**
"""

TIER_DISPLAY_LOCK_NOTE: Final = (
    "T2展示字段锁: version=tier_display_v1 / "
    "四元组=track+asof_grain+mode+tier或显式无档 / "
    "禁六词动作指令式 / 禁价量仓单 / 只读交叉T1 tier_cut_v1 / "
    "展示边Bonferroni不占m=6 / T1≺T2(T1已满足)。"
    "**字段契约 != 可放量**: 不算行情、不写hits、不接线UI/钉钉、不改hits schema; "
    "本拍不翻COMPLETE; 不改DIRECTION; 不改m; 不开观测口。"
)
"""一行摘要, 供日志 / PR / 状态口引用。不是判据, 判据在常量与断言。"""

# ---------------------------------------------------------------- 对照锚（写死期望值）

EXPECTED_DIRECTION_LABELS: Final = frozenset(
    {"research_buy", "research_sell", "research_watch"}
)
"""`DIRECTION_LABELS` 的期望值。写死一份在这里是故意的: 改了它这条断言就红。"""

EXPECTED_BONFERRONI_M: Final = 6
"""`BONFERRONI_M` 的期望值。本拍零碰数值, 只做对照。"""

EXPECTED_BONFERRONI_SCHEME_ID: Final = "independent_family_v1"
"""`BONFERRONI_SCHEME_ID` 的期望值。本拍零碰, 只做对照。"""


# ---------------------------------------------------------------- 断言助手（由测例调用）


def assert_t1_cross_lock() -> None:
    """只读交叉：T1 的 version / labels / display keys / no_measure 未被本拍重定义。

    **这是本模块与 T1 之间唯一的耦合点**, 且只读。五项分开查、分开报错:
    一条大红不如一条指名道姓的红。
    """
    if _t1.TIER_CUT_RULE_VERSION != EXPECTED_T1_RULE_VERSION:
        raise AssertionError(
            f"T1 version drift: {_t1.TIER_CUT_RULE_VERSION!r} "
            f"!= {EXPECTED_T1_RULE_VERSION!r}"
        )
    if _t1.TIER_LABELS != {"weak", "mid", "strong"}:  # noqa: SIM300
        raise AssertionError(f"T1 TIER_LABELS mutated: {_t1.TIER_LABELS!r}")
    if _t1.TIER_DISPLAY_REQUIRED_KEYS != ("track", "asof_grain", "mode"):
        raise AssertionError(
            f"T1 display keys mutated: {_t1.TIER_DISPLAY_REQUIRED_KEYS!r}"
        )
    if _t1.NO_MEASURE_NO_TIER is not True:
        raise AssertionError("T1 NO_MEASURE_NO_TIER must stay True")
    if _t1.TIER_BEFORE_T2 is not True:
        raise AssertionError("T1 TIER_BEFORE_T2 must stay True")


def assert_direction_labels_untouched() -> None:
    """对照锚：`DIRECTION_LABELS` 仍为原三值，且与档名不相交。

    两条分开查: 改了方向的人看到「档名与方向相交」会更迷糊。
    """
    if DIRECTION_LABELS != EXPECTED_DIRECTION_LABELS:
        raise AssertionError(
            f"DIRECTION_LABELS mutated: {DIRECTION_LABELS!r} "
            f"!= {EXPECTED_DIRECTION_LABELS!r}"
        )
    overlap = _t1.TIER_LABELS & DIRECTION_LABELS
    if overlap:
        raise AssertionError(
            f"TIER_LABELS must be disjoint from DIRECTION_LABELS: {sorted(overlap)!r}"
        )


def assert_tier_label_is_neutral(label: str) -> None:
    """档名须中性：不含六词短语、不含短动作指令词。

    短词只在这里查 —— 见模块 docstring「措辞检查的两种匹配方式」。
    """
    for phrase in TIER_DISPLAY_FORBIDDEN_ACTION_PHRASES:
        if phrase in label:
            raise AssertionError(f"tier label carries action phrase {phrase!r}: {label!r}")
    for word in TIER_DISPLAY_FORBIDDEN_SHORT_ACTION_WORDS:
        if word in label:
            raise AssertionError(f"tier label carries action word {word!r}: {label!r}")


def assert_payload_quad_or_no_tier(
    *,
    track: str | None,
    asof_grain: str | None,
    mode: str | None,
    tier: str | None,
    no_tier: bool,
) -> None:
    """四元组 / 显式无档语义助手。

    - 前三项必须落在 T1 取值域（直接引用 T1 集合，不复制成员）；
    - `no_tier=True` → `tier` 必须为 `None`（**禁止用 `weak` 顶**）；
    - `no_tier=False` → `tier` 必须 ∈ `TIER_LABELS`。

    只管"四元组合不合法"，不管载荷里有没有多余字段 —— 那是
    `assert_no_forbidden_payload_fields` 的职责。两个坏法分开报错。
    """
    if track not in _t1.TIER_DISPLAY_TRACKS:
        raise AssertionError(f"track out of domain: {track!r}")
    if asof_grain not in _t1.TIER_DISPLAY_GRAINS:
        raise AssertionError(f"asof_grain out of domain: {asof_grain!r}")
    if mode not in _t1.TIER_DISPLAY_MODES:
        raise AssertionError(f"mode out of domain: {mode!r}")
    if no_tier:
        if tier is not None:
            raise AssertionError(
                "no_tier=True must not carry a tier "
                f"(got {tier!r}; forbid using weak to fill missing)"
            )
        return
    if tier not in _t1.TIER_LABELS:
        raise AssertionError(
            f"has-tier payload requires tier in TIER_LABELS, got {tier!r}"
        )


def assert_no_forbidden_action_phrases(text: str) -> None:
    """文案不得含六词动作指令式（子串匹配）。"""
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__}")
    for phrase in TIER_DISPLAY_FORBIDDEN_ACTION_PHRASES:
        if phrase in text:
            raise AssertionError(f"forbidden action phrase: {phrase!r}")


def assert_no_forbidden_payload_fields(payload: Iterable[str] | Mapping[str, object]) -> None:
    """载荷**键名**里不得出现价量仓单字段。

    收 `Iterable[str]` 或 `Mapping` —— 调用方可能传 dict、`payload.keys()`、字段名列表。
    只查键名，不查值：值里出现 "price" 是文案自由，键名叫 price 才是字段契约问题。
    """
    if isinstance(payload, str):
        raise TypeError("payload must be a mapping or an iterable of keys, not a single string")
    keys = payload.keys() if isinstance(payload, Mapping) else payload
    try:
        present = set(keys)
    except TypeError as exc:
        raise TypeError(f"payload keys must be iterable, got {type(keys).__name__}") from exc
    hit = present & TIER_DISPLAY_FORBIDDEN_PAYLOAD_FIELDS
    if hit:
        raise AssertionError(f"forbidden payload fields present: {sorted(hit)!r}")


def assert_tier_display_lock_invariants() -> None:
    """聚合不变量（由测例调用；模块 import 时**不**硬 raise）。

    若在模块级硬 raise，一个常量写错会让整个包导不进来，报错还指向 import 而不是那一行。
    """
    if not TIER_DISPLAY_LOCK_VERSION:
        raise AssertionError("TIER_DISPLAY_LOCK_VERSION must be non-empty")
    if TIER_DISPLAY_LOCK_VERSION != "tier_display_v1":
        raise AssertionError(
            f"TIER_DISPLAY_LOCK_VERSION must be 'tier_display_v1', "
            f"got {TIER_DISPLAY_LOCK_VERSION!r}"
        )

    # 四元组: 前三项必须与 T1 逐项相同(派生关系), 第四项是 tier
    if TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS != (
        "track",
        "asof_grain",
        "mode",
        "tier",
    ):
        raise AssertionError(
            f"payload required keys mutated: {TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS!r}"
        )
    for key in ("track", "asof_grain", "mode", "tier"):
        if key not in TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS:
            raise AssertionError(f"payload required key missing: {key!r}")

    if TIER_DISPLAY_NO_TIER_KEY != "no_tier":
        raise AssertionError(f"no_tier key mutated: {TIER_DISPLAY_NO_TIER_KEY!r}")

    # 六词: 六个一个都不能少
    expected_phrases = {
        "可以买入",
        "建议买入",
        "强烈买入",
        "可以卖出",
        "建议卖出",
        "强烈卖出",
    }
    if expected_phrases != TIER_DISPLAY_FORBIDDEN_ACTION_PHRASES:
        raise AssertionError(
            f"forbidden action phrases mutated: "
            f"{sorted(TIER_DISPLAY_FORBIDDEN_ACTION_PHRASES)!r}"
        )

    # 价量仓单: 四个中文键必须在(英文键可扩, 中文是硬要求)
    for cn in ("价格", "数量", "仓位", "订单"):
        if cn not in TIER_DISPLAY_FORBIDDEN_PAYLOAD_FIELDS:
            raise AssertionError(f"forbidden payload field missing: {cn!r}")

    assert_t1_cross_lock()
    assert_direction_labels_untouched()

    if BONFERRONI_M != EXPECTED_BONFERRONI_M:
        raise AssertionError(
            f"BONFERRONI_M mutated: {BONFERRONI_M!r} != {EXPECTED_BONFERRONI_M!r}"
        )
    if BONFERRONI_SCHEME_ID != EXPECTED_BONFERRONI_SCHEME_ID:
        raise AssertionError(
            f"BONFERRONI_SCHEME_ID mutated: {BONFERRONI_SCHEME_ID!r} "
            f"!= {EXPECTED_BONFERRONI_SCHEME_ID!r}"
        )
