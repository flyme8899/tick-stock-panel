"""T1 切档规则常量锁面（批次 20261030gt1 · P-bss-t1-tier-cut · #-A-#12）。

## 这是什么

一组**纯常量 + 纯断言函数**的「T1 切档规则锁」：把**展示层切档规则**在下列维度上
钉死 —— 规则版本、档名、日频/事件两族连续量 id、阈值、无量＝无档、展示三项必填、
展示边 Bonferroni、与 `DIRECTION_LABELS` 正交、T1≺T2。

规则全文见 `docs/buy-sell-signal-tier-cut.md`。**本文档与模块是同一份规则的两个形态**：
文档给人读，模块给测例验。两者不一致时以测例为准 —— 因为测例会红。

## 它不是什么

- **不是**行情计算器：没有 I/O，不读行情、不写文件、不碰 `HitStore`、不调推送。
- **不是** hits 写路径：档位**不**落盘，本拍**不**改 hits schema。
- **不是** UI / 钉钉接线：展示层怎么画怎么发是 T2 的事。
- **不是**放量开关，**不是** T2 展示字段设计。

## 文档锁 ≠ 可放量

`TIER_CUT_*` 齐备只说明「**切档规则定了**」。它**不**说明：可放量、`TRIGGERING` 该开、
`COMPLETE` 该翻、schema 已进 hits、真推已上线。这五件事没有一件能由本模块推出。

一份写得整齐的规则文档，最容易被读成「所以这套东西已经能用了」。不是。

## T1 ≺ T2

本模块只锁 **T1**（切档规则）。T2（展示字段设计 / UI 接线 / 观测口 / 算档接线）
另口令。定序写死：**T1 未锁，T2 无从接线**；T2 只消费本模块的取值，不得重定义。

## 与方向正交

档＝弱/中/强（**展示强度**）。方向仍由 `constants.DIRECTION_LABELS` 三值封闭
（`research_buy` / `research_sell` / `research_watch`）。

两条轴互不嵌套：任何把档塞进方向集合、或把方向键当档名的做法都是错的。
**禁止**六词动作指令式（买/卖/加仓/减仓/开多/开空）替代档名；**禁止**删
`research_watch` —— 观察档不是「最弱的买卖」，它是另一种状态。

本模块**不**改 `DIRECTION_LABELS`，只做对照断言。

## 展示边 ≠ 校准边

本锁是**展示边**：不占 `BONFERRONI_M = 6`，且**非永久豁免**。
本模块**不**改 `BONFERRONI_M` / `BONFERRONI_SCHEME_ID`，只做数值对照断言。
校准边若日后要占用预算，须新开一把锁 + 重算 / bump scheme，不得在本锁上顺手加格。

## 无量的处理

**无量＝无档**。缺连续量、缺 rank、无触发连续量、无频率锁 → `tier is None`。
**绝不填 0 冒充最弱档**：0 在多数连续量上是真实取值（行业垫底），把「不知道」渲染成
「最差」会让档位分布在缺失样本上系统性左偏，而这个偏移在页面上看不出来。

另有一个边界事实（见文档 §5.2）：弱档下界是**开**的，故 `measure == 0` 属于
「**有量但无档**」—— 既不是无量，也不匹配任何档位。

批次 20261030gt1 · P-bss-t1-tier-cut。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。
成本来回 18.2bp。
"""

from __future__ import annotations

from typing import Final

from .constants import BONFERRONI_M, BONFERRONI_SCHEME_ID, DIRECTION_LABELS

__all__ = [
    "DAILY_TIER_MEASURE_ID",
    "EVT_TIER_MEASURE_ID",
    "EXPECTED_BONFERRONI_M",
    "EXPECTED_BONFERRONI_SCHEME_ID",
    "EXPECTED_DIRECTION_LABELS",
    "NO_MEASURE_NO_TIER",
    "TIER_BEFORE_T2",
    "TIER_CUT_BONFERRONI_DISPLAY_EDGE",
    "TIER_CUT_DOES_NOT_CONSUME_BONFERRONI_M",
    "TIER_CUT_LOCK_NOTE",
    "TIER_CUT_MID_HI",
    "TIER_CUT_RULE_VERSION",
    "TIER_CUT_WEAK_HI",
    "TIER_DISPLAY_GRAINS",
    "TIER_DISPLAY_MODES",
    "TIER_DISPLAY_REQUIRED_KEYS",
    "TIER_DISPLAY_TRACKS",
    "TIER_LABELS",
    "TIER_LABEL_ZH",
    "assert_direction_labels_untouched",
    "assert_display_keys_complete",
    "assert_measure_ids_distinct",
    "assert_no_measure_no_tier",
    "assert_thresholds_ordered",
    "assert_tier_cut_lock_invariants",
]

# ---------------------------------------------------------------- 规则版本

TIER_CUT_RULE_VERSION: Final = "tier_cut_v1"
"""可审计规则版本。改量 / 改阈值 / 改映射**必须 bump**；禁止静默改阈值不 bump。

档位一旦展示出去, 历史截图 / 群消息 / 报告里的「强」都绑定着当时那一版阈值。
不 bump 版本, 两批样本会被当成同一口径比较 —— 这种错**不会报错**, 只会让结论悄悄漂移。
"""

# ---------------------------------------------------------------- 档名（英文键 + 对外文案）

TIER_LABELS: Final = frozenset({"weak", "mid", "strong"})
"""展示层档名封闭集（英文键）。与方向正交，且与 `DIRECTION_LABELS` 不相交。"""

TIER_LABEL_ZH: Final = {
    "weak": "弱",
    "mid": "中",
    "strong": "强",
}
"""对外展示文案。**只做文案**, 不要拿中文当键 —— 一处改文案就成了改键。

禁止用六词动作指令式（买 / 卖 / 加仓 / 减仓 / 开多 / 开空）替代档名:
动作指令读起来是「已决策」, 而档位只回答「这条信号有多强」。
"""

# ---------------------------------------------------------------- 两族连续量 id（必须不同）

DAILY_TIER_MEASURE_ID: Final = "q_rank_within_ind"
"""日频族: 申万一级**行业内**质量相对分位, ∈[0,1], 越高越好。

来源 `quality_rank`（本拍零碰, 只作量锚）。缺 rank / `q_rank_missing` /
`singleton_industry` → **无档**, 绝不填 0。
"""

EVT_TIER_MEASURE_ID: Final = "evt_excess_vs_freq_cross_section_q"
"""事件族: 触发连续量相对 FreqLock / 设计频率的**超额倍数**, 再取同
`signal_id` × 同交易日**截面分位**。

不取截面分位则各 `signal_id` 之间量纲不可比, 阈值 ⅓/⅔ 会退化成「对某些信号永远弱、
对另一些永远强」。无触发连续量 / 无频率锁 → **无档**。

**规则已锁；算档接线属 T2 / 观测口另口令。** 本拍不实现算子、不实现 UI / 推送。
"""

# ---------------------------------------------------------------- 阈值（N3 已拍板）

TIER_CUT_WEAK_HI: Final = 1.0 / 3.0
"""弱档上界（**闭**）：弱 ∈ (0, ⅓]。

下界是**开**的 —— 故 `measure == 0` 有量但无档（不匹配任何档位）。
这不是 bug, 也不是「无量」: 0 是真实的百分位, 只是不落在任何切档区间里。
"""

TIER_CUT_MID_HI: Final = 2.0 / 3.0
"""中档上界（**闭**）：中 ∈ (⅓, ⅔]；强 ∈ (⅔, 1]。

恰落在 ⅓ 上的归弱, 恰落在 ⅔ 上的归中 —— 右闭的直接后果, 两处实现不得各写一种。
"""

# ---------------------------------------------------------------- 无量＝无档 / 展示三项 / Bonferroni / 定序

NO_MEASURE_NO_TIER: Final = True
"""**无量＝无档**硬门。

缺连续量不得填 0 冒充最弱档。0 是真实取值（行业垫底）, 拿它顶缺失等于把「不知道」
渲染成「最差」—— 用户看到的是确定结论, 而真实情况是没有结论。且档位分布会在缺失
样本上系统性左偏, 而这件事在页面上看不出来。
"""

TIER_DISPLAY_REQUIRED_KEYS: Final = ("track", "asof_grain", "mode")
"""展示必填三项逻辑键。**缺一不合规。**

三项一起才是「这个档是什么」的完整指认: 同一个 `strong`, `shadow` 和 `live` 的
可信度完全不同; 同一个 `weak`, 日频/日粒度 与 事件/分钟粒度 是两件事。
"""

TIER_DISPLAY_TRACKS: Final = frozenset({"daily_fundamental", "minute_event"})
"""轨取值域。与 `constants.FREQ_LOCK_CADENCES` **同集合**（本拍零碰该集合, 只对齐取值）。"""

TIER_DISPLAY_GRAINS: Final = frozenset({"day", "minute"})
"""asof 粒度取值域。粒度 ≠ 节拍: 节拍描述「多久评估一次」, 粒度描述「截到哪一刻」。"""

TIER_DISPLAY_MODES: Final = frozenset({"shadow", "observation", "live"})
"""模式取值域。门禁顺序写死: shadow → observation → live（真推另口令）。"""

TIER_CUT_BONFERRONI_DISPLAY_EDGE: Final = True
"""本锁是**展示边**, 不是校准边。"""

TIER_CUT_DOES_NOT_CONSUME_BONFERRONI_M: Final = True
"""展示边**不占** `BONFERRONI_M=6`。**非永久豁免**: 校准边若日后要占用, 须新锁 +
重算 / bump scheme, 不得在本锁上顺手加格。"""

TIER_BEFORE_T2: Final = True
"""定序: **T1 ≺ T2**。T1 未锁, T2 无从接线。"""

TIER_CUT_LOCK_NOTE: Final = (
    "T1切档锁: version=tier_cut_v1 / 弱中强 / "
    "日频=q_rank_within_ind / 事件=evt_excess_vs_freq_cross_section_q / "
    "阈值(0,1/3](1/3,2/3](2/3,1] / 无量=无档 / 展示三项必填 / "
    "展示边Bonferroni不占m=6 / T1≺T2。"
    "**文档锁 != 可放量**: 不算行情、不写hits、不接线UI/钉钉; "
    "本拍不翻COMPLETE; 不改DIRECTION; 不改m; 不开T2。"
)
"""一行摘要, 供日志 / PR / 状态口引用。不是判据, 判据在常量与断言。"""

# ---------------------------------------------------------------- 对照锚（写死期望值）

EXPECTED_DIRECTION_LABELS: Final = frozenset(
    {"research_buy", "research_sell", "research_watch"}
)
"""`DIRECTION_LABELS` 的期望值。**写死一份在这里是故意的**: 若有人改了它,
`assert_direction_labels_untouched()` 会红, 于是「改方向」这件事不会悄悄发生。"""

EXPECTED_BONFERRONI_M: Final = 6
"""`BONFERRONI_M` 的期望值。本拍零碰数值, 只做对照。"""

EXPECTED_BONFERRONI_SCHEME_ID: Final = "independent_family_v1"
"""`BONFERRONI_SCHEME_ID` 的期望值。本拍零碰, 只做对照。"""


# ---------------------------------------------------------------- 断言助手（由测例调用）


def assert_direction_labels_untouched() -> None:
    """对照锚：`DIRECTION_LABELS` 仍为原三值，且与档名不相交。

    两条分开查、分开报错：混在一条里，改了方向的人看到「档名与方向相交」会更迷糊。

    本函数**不在 import 时执行** —— 断言由测例调用。若在模块级硬 raise，一个常量
    写错会让整个包导不进来，而报错指向 import 而不是那一行。
    """
    if DIRECTION_LABELS != EXPECTED_DIRECTION_LABELS:
        raise AssertionError(
            f"DIRECTION_LABELS mutated: {DIRECTION_LABELS!r} "
            f"!= {EXPECTED_DIRECTION_LABELS!r}"
        )
    overlap = TIER_LABELS & DIRECTION_LABELS
    if overlap:
        raise AssertionError(
            f"tier labels must not overlap direction labels: {sorted(overlap)!r}"
        )


def assert_no_measure_no_tier(*, has_measure: bool, tier: str | None) -> None:
    """无量＝无档语义助手：`has_measure=False` 时 `tier` 必须为 `None`。

    只管「无量→无档」这一个方向。反向（有量一定有档）**不**成立：
    §5.2 的 `measure == 0` 是有量但落在切档区间外，仍属无档。
    """
    if not NO_MEASURE_NO_TIER:
        raise AssertionError("NO_MEASURE_NO_TIER must stay True")
    # 档名本身先验一遍: 一个拼错的档名（"midium"）在「有量」时也该被抓到,
    # 否则无量/有量两条路都会把它放过去。
    if tier is not None and tier not in TIER_LABELS:
        raise AssertionError(f"unknown tier label: {tier!r}")
    if not has_measure and tier is not None:
        raise AssertionError(f"no measure must yield no tier, got tier={tier!r}")


def assert_measure_ids_distinct() -> None:
    """日频 measure id 必须 ≠ 事件 measure id，且两者非空。"""
    if not DAILY_TIER_MEASURE_ID or not EVT_TIER_MEASURE_ID:
        raise AssertionError("tier measure ids must be non-empty")
    if DAILY_TIER_MEASURE_ID == EVT_TIER_MEASURE_ID:
        raise AssertionError(
            "daily and evt tier measure ids must differ: "
            f"both are {DAILY_TIER_MEASURE_ID!r}"
        )


def assert_display_keys_complete(keys: object) -> None:
    """展示三项必填检查：`keys` 须同时含 `track` / `asof_grain` / `mode`。

    `keys` 收 `object` 而非 `Mapping`：调用方可能传 dict、dataclass、pydantic 模型,
    甚至一个已经拼好的字段名列表。这里只关心「三项在不在」。
    """
    if isinstance(keys, str):
        raise TypeError("keys must be a collection of keys, not a single string")
    try:
        present = set(keys)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(f"keys must be iterable, got {type(keys).__name__}") from exc
    missing = [key for key in TIER_DISPLAY_REQUIRED_KEYS if key not in present]
    if missing:
        raise AssertionError(f"tier display missing required keys: {missing!r}")


def assert_thresholds_ordered() -> None:
    """阈值形状：0 < WEAK_HI < MID_HI ≤ 1，且两段等宽（⅓ / ⅔）。"""
    if not (0.0 < TIER_CUT_WEAK_HI < TIER_CUT_MID_HI <= 1.0):
        raise AssertionError(
            "tier thresholds must satisfy 0 < WEAK_HI < MID_HI <= 1: "
            f"WEAK_HI={TIER_CUT_WEAK_HI!r}, MID_HI={TIER_CUT_MID_HI!r}"
        )
    if abs(TIER_CUT_WEAK_HI - 1.0 / 3.0) > 1e-12:
        raise AssertionError(f"TIER_CUT_WEAK_HI must stay 1/3, got {TIER_CUT_WEAK_HI!r}")
    if abs(TIER_CUT_MID_HI - 2.0 / 3.0) > 1e-12:
        raise AssertionError(f"TIER_CUT_MID_HI must stay 2/3, got {TIER_CUT_MID_HI!r}")


def assert_tier_cut_lock_invariants() -> None:
    """一把梭：本锁面的全部写死期望值。

    测例调这一个就够，但**每个子项仍然单独暴露**（上面那几个函数）—— 一条大红
    不如一条指名道姓的红：「DIRECTION_LABELS mutated」比「invariants failed」有用得多。
    """
    # 先查非空再查取值: 反过来的话空串会先被"取值不对"抓走, 那条"必须非空"永远
    # 不可达 —— 一条永不触发的守卫等于没写。
    if not TIER_CUT_RULE_VERSION:
        raise AssertionError("TIER_CUT_RULE_VERSION must be non-empty")
    if TIER_CUT_RULE_VERSION != "tier_cut_v1":
        raise AssertionError(
            f"TIER_CUT_RULE_VERSION must be 'tier_cut_v1', got {TIER_CUT_RULE_VERSION!r}"
        )

    if TIER_LABELS != {"weak", "mid", "strong"}:  # noqa: SIM300
        raise AssertionError(f"TIER_LABELS mutated: {TIER_LABELS!r}")
    if set(TIER_LABEL_ZH) != set(TIER_LABELS):
        raise AssertionError(
            f"TIER_LABEL_ZH keys must match TIER_LABELS: {sorted(TIER_LABEL_ZH)!r}"
        )
    # 用 .get 而不是下标: 若键被改掉, 下标会抛 KeyError(看起来像崩溃),
    # .get 会走到下面那句带明确信息的 AssertionError。
    if (TIER_LABEL_ZH.get("weak"), TIER_LABEL_ZH.get("mid"), TIER_LABEL_ZH.get("strong")) != (
        "弱",
        "中",
        "强",
    ):
        raise AssertionError(f"TIER_LABEL_ZH mutated: {TIER_LABEL_ZH!r}")

    if DAILY_TIER_MEASURE_ID != "q_rank_within_ind":
        raise AssertionError(
            f"DAILY_TIER_MEASURE_ID must be 'q_rank_within_ind', "
            f"got {DAILY_TIER_MEASURE_ID!r}"
        )
    assert_measure_ids_distinct()

    assert_thresholds_ordered()

    if NO_MEASURE_NO_TIER is not True:
        raise AssertionError("NO_MEASURE_NO_TIER must be True")
    if TIER_BEFORE_T2 is not True:
        raise AssertionError("TIER_BEFORE_T2 must be True")
    if TIER_CUT_BONFERRONI_DISPLAY_EDGE is not True:
        raise AssertionError("TIER_CUT_BONFERRONI_DISPLAY_EDGE must be True")
    if TIER_CUT_DOES_NOT_CONSUME_BONFERRONI_M is not True:
        raise AssertionError("TIER_CUT_DOES_NOT_CONSUME_BONFERRONI_M must be True")

    assert_display_keys_complete(TIER_DISPLAY_REQUIRED_KEYS)
    if TIER_DISPLAY_TRACKS != {"daily_fundamental", "minute_event"}:  # noqa: SIM300
        raise AssertionError(f"TIER_DISPLAY_TRACKS mutated: {TIER_DISPLAY_TRACKS!r}")
    if TIER_DISPLAY_GRAINS != {"day", "minute"}:  # noqa: SIM300
        raise AssertionError(f"TIER_DISPLAY_GRAINS mutated: {TIER_DISPLAY_GRAINS!r}")
    if TIER_DISPLAY_MODES != {"shadow", "observation", "live"}:  # noqa: SIM300
        raise AssertionError(f"TIER_DISPLAY_MODES mutated: {TIER_DISPLAY_MODES!r}")

    if BONFERRONI_M != EXPECTED_BONFERRONI_M:
        raise AssertionError(
            f"BONFERRONI_M mutated: {BONFERRONI_M!r} != {EXPECTED_BONFERRONI_M!r}"
        )
    if BONFERRONI_SCHEME_ID != EXPECTED_BONFERRONI_SCHEME_ID:
        raise AssertionError(
            f"BONFERRONI_SCHEME_ID mutated: {BONFERRONI_SCHEME_ID!r} "
            f"!= {EXPECTED_BONFERRONI_SCHEME_ID!r}"
        )

    assert_direction_labels_untouched()
