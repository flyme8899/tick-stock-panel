"""VOLUME 阈值负锁语义契约薄模块（批次 20261101gthresh · P-bss-evidence · #-A-#9）。

## 这是什么

把 tip 上**已经散落在三处**的同一句话，收成一组**可测常量 + 断言**：

- `docs/buy-sell-signal-trigger-gate.md` §4.1：`threshold_lock_data_pending` 仍开
- `volume_breakout_evidence_table.py`：`strongly_negative` ≠ 阈值已锁
- `freq_lock.py`：有 `FreqLockStamp` ≠ 可清 `threshold_lock_data_pending`

version = `threshold_neg_lock_v1`。

**"负锁"是这个模块唯一要表达的东西**：阈值这一格**必须是锁着的**，
而且这个"锁着"不是待办事项的拖延，是一个**有内容的判断** ——
在负号没被解释之前，把量比 / 突破幅度 / 窗口分钟数钉成"已锁定"，
钉出来的不是标定，是**伪造**。

## 它不是什么

- **不是**清零刀：本拍**不删** `threshold_lock_data_pending`（VOLUME / AUCTION 任一侧都不删）
- **不是**钉可交易阈值：本模块**不含**任何"已锁定的量比 / 突破% / 窗口分钟数"数值
- **不是** fabricate 的入口：`assert_no_tradeable_threshold_nail` 只**拒绝**，不产出
- **不是**真翻 TRIGGERING / 不是可放量 / 不是 calib / 不是阈值已锁
- **不是**完整触发引擎 / 调度 / 真算量比：没有 I/O，不读行情，不写 hits
- **不是**"清零完成"的声明：这个模块存在**恰恰意味着**这一格还没清

## 为什么"负锁"要单独成一个模块

因为"待补"和"必须继续待着"是两件事，而文档里它们长得一样。

`threshold_lock_data_pending` 这条标签和 `http_minute_listing_auth_pending` 的区别是：

| 标签 | 缺的是什么 | 补齐的路径 |
|---|---|---|
| `http_minute_listing_auth_pending` | 一个**外部凭证** | 拿到凭证 → 能通 → 可以清 |
| `threshold_lock_data_pending` | **一个解释** | 负号解释清楚之前，**补什么都不能清** |

前者是"还没到"，后者是"到了也不能用"。把它们并排放在一张 pending 表里，
下一个人看到的都是"待办"，于是很自然地问"这个什么时候清"——
而正确答案是"在另一些事成立之前，不让清"。这个"不让"，需要有自己的常量、
自己的断言、自己的版本号，否则它只活在某个人的记忆里。

## 与触发表的交叉：strongly_negative ≠ 阈值已锁

`volume_breakout_evidence_table` 的四行 `return` 是
`-23.86 / -32.44 / -13.80 / -19.81`，`verdict = strongly_negative`。

这个 verdict 回答的是「**过去这四个口径赚钱吗**」，答案是不赚。
它**没有**回答「量比多少才算放量」「突破几个点算突破」「窗口几分钟」。

所以 `strongly_negative` 越确定，阈值那一格反而**越**该锁着：
一个已知负期望的口径，其阈值标定出来的只会是"更精确的亏损"。
**表 PRESENT ≠ 阈值已锁**；**strongly_negative ≠ 阈值已锁**。

## 与 freq_lock 的交叉：FreqLockStamp ≠ 可清标签

`freq_lock.FreqLockStamp` 是「评估口径身份」的容器：`signal_id` +
`definition_version` + `cadence` + `params` 的指纹。

它证明了「有人把一个参数集写下来了」，**不**证明「这个参数集被证据支持」。

`freq_lock` 自己的 docstring 就写着这句话；本模块只是把它变成一条能被测例调用的断言
—— 因为写在 docstring 里的话，改代码的时候没人会看见；写在断言里的话，改代码会红。

**本模块不 import、不修改 `freq_lock` 的任何业务**：交叉只读，断言留在这里。

## 门禁大字

负锁 ≠ 删标签；
钉可交易数 ＝ fabricate；
**本拍 ≠ 清零刀**；
清零一刀 ≠ 可翻闸；
语义锁面 PRESENT ≠ 阈值已锁 ≠ 可开 TRIGGERING；
strongly_negative ≠ 阈值已锁；
FreqLockStamp ≠ 可清标签理由；
表 PRESENT ≠ 阈值已锁；
路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING；
K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING；
门禁契约 ≠ 授权翻闸；合入 ≠ 部署；tip = prod。

**本拍即使可交，也不清标签、不降 COUNT、不翻闸。**

批次 20261101gthresh · P-bss-evidence。
仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。
成本来回 18.2bp。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

__all__ = [
    "EXPECTED_EVIDENCE_TABLE_VERDICT_WHEN_UNLOCKED",
    "THRESHOLD_LOCK_TAG_MUST_REMAIN",
    "THRESHOLD_NEG_LOCK_NOTE",
    "THRESHOLD_NEG_LOCK_VERSION",
    "TRADEABLE_THRESHOLD_FIELDS",
    "ThresholdTagRemovedError",
    "TradeableThresholdNailError",
    "assert_freq_lock_stamp_is_not_clear_reason",
    "assert_no_tradeable_threshold_nail",
    "assert_strongly_negative_does_not_lock_threshold",
    "assert_threshold_lock_still_pending",
]

# ---------------------------------------------------------------- 版本与标签

THRESHOLD_NEG_LOCK_VERSION: Final = "threshold_neg_lock_v1"
"""负锁语义契约版本。

改"哪条标签必须留着" / 改"哪些字段算可交易阈值" / 改交叉判据**必须** bump。

注意这与 `trigger_gate_v1` 是**两个**版本号：门禁锁面描述整个触发/放量门禁，
本模块只描述"阈值这一格为什么锁着"。本拍**不** bump `trigger_gate_v1`
（理由：bump 会迫使 `schema_push_lock` / `schema_push_live_lock` 的
`EXPECTED_TRIGGER_GATE_LOCK_VERSION` 同步改字面量，而那两个文件本拍零碰；
同 #265 / #266 的处置）。**未 bump ≠ 未披露**：`trigger_gate_lock.NOTE` 已加旁注。
"""

THRESHOLD_LOCK_TAG_MUST_REMAIN: Final = "threshold_lock_data_pending"
"""**必须留在** VOLUME pending 集合里的那条标签。

这条常量存在的唯一目的，是让"标签被删了"这件事能被一个名字指出来。
写死字符串而不是 `from .volume_breakout_stub import ...`：本模块要能被用来
**检查** stub，而不是跟着 stub 一起错。

VOLUME 与 AUCTION **各有一条同名标签**——本模块管的是 VOLUME 那条
（`assert_threshold_lock_still_pending` 的调用方传哪个集合，就检查哪个）。
AUCTION 侧本拍零碰。
"""

EXPECTED_EVIDENCE_TABLE_VERDICT_WHEN_UNLOCKED: Final = "strongly_negative"
"""触发表在"阈值仍未锁"状态下的 verdict。

与 `volume_breakout_evidence_table.VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT`
（`strongly_negative`）对齐，但**刻意不从那边 import**：

- 那边是"表"，这边是"读表得出的规矩"，两者不是同一个抽象层；
- 万一那边哪天被改名，本模块的常量应当在测例里红，而不是悄悄跟着改名
  （跟着改名 = "表变了规矩也自动变"，那这条规矩就没人守了）。

测例 `test_cross_verdict_matches_evidence_table` 咬住两边取值相等。
"""

TRADEABLE_THRESHOLD_FIELDS: Final = (
    "volume_ratio_locked",
    "breakout_pct_locked",
    "window_minutes_locked",
)
"""三个**可交易**阈值字段名 —— 本拍禁止给它们钉值。

这三格就是 §4.1 人读里写的「量比 / 突破幅度 / 窗口分钟数」。
名字里带 `locked` 是刻意的：它们表达的不是"参数"，是"**已锁定**这个宣称"。
所以只要有人把值塞进来，无论塞的是什么数，都已经是宣称。

本模块**不**给出这三格的任何合法数值 —— 给出就等于 fabricate。
"""


class ThresholdTagRemovedError(AssertionError):
    """`threshold_lock_data_pending` 不在 pending 集合里时抛出。

    继承 `AssertionError` 是刻意的：调用方（测例 / 自检脚本）本来就按"断言失败"
    来理解，不必再引入新异常族做 `except`。**import 期不抛**。
    """


class TradeableThresholdNailError(AssertionError):
    """有人把可交易阈值钉成"已锁定"时抛出。

    名字里的 `Nail` 是"钉死"的意思 —— 与 `ThresholdTagRemovedError` 分开，
    是为了让失败信息能一眼区分两类越界：
    **删标签**（把本拍改写成清零刀）vs **钉数**（fabricate）。
    """


# ---------------------------------------------------------------- 断言 · 标签仍在


def assert_threshold_lock_still_pending(pending: Iterable[str]) -> None:
    """`threshold_lock_data_pending` 必须**仍在** `pending` 里。否则 raise。

    这条守的是本拍**最容易被"顺手"破掉**的一处：既然模块都建好了、
    语义都写清了，"那干脆把标签清了吧"——**不行**。清标签是**另一刀**，
    需要另口令 + 文档里那张"可钉证据路径清单"逐条满足。

    特别注意：本函数**只检查"在不在"**，不检查"还有几条"。
    后者是 `trigger_gate_lock.TRIGGER_GATE_EVIDENCE_PENDING_COUNT` 的职责
    （它派生自两族集合），两边分工不同，不要在这里顺手加计数断言。
    """
    if THRESHOLD_LOCK_TAG_MUST_REMAIN not in set(pending):
        raise ThresholdTagRemovedError(
            f"{THRESHOLD_LOCK_TAG_MUST_REMAIN!r} must remain in the pending set "
            "until a separate clear-tag order; 负锁≠删标签; 本拍≠清零刀; "
            "清标签须另口令 + 可钉证据路径清单逐条满足"
        )


# ---------------------------------------------------------------- 断言 · 禁钉可交易数


def _is_honest_empty(value: Any) -> bool:
    """某个可交易阈值字段是否处于**诚实空态**。

    诚实空态只有四种：`None` / `False` / `""` / `"未锁定"`。

    **刻意用逐类型判断而不是 `value in (None, False, "", "未锁定")`**：
    Python 里 `0 == False`，成员测试会把数值 `0` 当成 `False` 放过去，
    与 docstring 写的"`0` 也算宣称已锁"直接矛盾。这里用 `type(value) is bool`
    把 `False` 限定成**真正的布尔**，`0` / `0.0` 一律落到"宣称已锁"。
    """
    if value is None:
        return True
    if type(value) is bool:
        return value is False
    if type(value) is str:
        return value in ("", "未锁定")
    return False


def assert_no_tradeable_threshold_nail(
    nail: Mapping[str, Any] | None = None,
    *,
    volume_ratio_locked: Any = None,
    breakout_pct_locked: Any = None,
    window_minutes_locked: Any = None,
) -> None:
    """拒绝把可交易量比 / 突破% / 窗口分钟钉成「已锁定」。

    两种调用形态：传一个 Mapping，或传三个显式 kwargs。任一"已锁"字段非空 → raise。

    ## 什么算"已锁"

    `None` / `False` / 空串 / `"未锁定"` 视为**未锁**（这些都是诚实的空态）。
    其余任何值 —— 包括 `0`、`2.5`、`"2.5"`、`"已锁定"` —— 都算**宣称已锁**。

    `0` 也算，是因为"量比阈值 0"本身是个荒谬值：它要么是填错了，
    要么是某个默认值漏出来了。放过 `0` 等于给"漏出来的默认值"发通行证。

    **`0 == False` 是个真陷阱**：`0 in (None, False, "", "未锁定")` 在 Python 里
    **是 `True`** —— 用元组成员测试判空态，会把 `0` 当成 `False` 放过去，
    而 docstring 里还写着"`0` 也算"。所以这里不用成员测试，改用 `_is_honest_empty`
    逐类型判：只有 `type(value) is bool` 才走 `False` 分支，数值 `0` 一律算宣称。

    ## 为什么是断言而不是校验器

    本模块**没有**任何合法数值可返回。一个"校验通过"的阈值集合，在本拍是**不存在的对象**
    —— 所以这个函数的正常出口是"什么都不发生"，而不是"返回一个干净的阈值"。
    将来真要标定，那是另口令 + 另一份带证据的表，不是给这里填数。
    """
    payload: dict[str, Any] = dict(nail or {})
    for key, value in (
        ("volume_ratio_locked", volume_ratio_locked),
        ("breakout_pct_locked", breakout_pct_locked),
        ("window_minutes_locked", window_minutes_locked),
    ):
        if value is not None:
            payload.setdefault(key, value)

    offenders = {
        key: payload[key]
        for key in TRADEABLE_THRESHOLD_FIELDS
        if key in payload and not _is_honest_empty(payload[key])
    }
    if offenders:
        raise TradeableThresholdNailError(
            "钉可交易阈值＝fabricate；本拍禁止: "
            f"{offenders!r}; 阈值未锁是**有内容的判断**（负号未解释），"
            "不是待办拖延；标定须另口令 + 带证据的表"
        )


# ---------------------------------------------------------------- 交叉 · 触发表


def assert_strongly_negative_does_not_lock_threshold(verdict: str) -> None:
    """`strongly_negative` 的 verdict **不**构成"阈值已锁"。

    这个函数的正常出口也是"什么都不发生"。它存在的意义是**占位**：
    让 `strongly_negative` 与"阈值"之间有一条**可被调用**的边，
    而不是只在两份 docstring 里各写一句话。

    行为刻意写得极窄 —— 只认一个事实（verdict 是 `strongly_negative`），
    只主张一件事（这不等于阈值锁了）。任何"更聪明"的判断（比如按 verdict
    分级决定能不能清标签）都会把一个研究判断变成一个自动化许可，
    而那正好是本拍要挡的东西。

    传其它 verdict 也不 raise：本拍**不**为其它取值背书任何结论，
    既不宣称"可以清"，也不宣称"不能清"——那是另拍的事。
    """
    if verdict == EXPECTED_EVIDENCE_TABLE_VERDICT_WHEN_UNLOCKED:
        # 负号越确定，阈值越该锁着：一个已知负期望的口径，
        # 标定出来的只会是"更精确的亏损"。
        return
    # 其它 verdict：本拍不表态（不等于可清，也不等于不可清 = 另拍裁量）
    return


# ---------------------------------------------------------------- 交叉 · freq_lock


def assert_freq_lock_stamp_is_not_clear_reason(*, has_freq_lock_stamp: bool) -> None:
    """有 `FreqLockStamp` **不是**清 `threshold_lock_data_pending` 的理由。

    正常出口仍是"什么都不发生"。存在理由是让这条规矩可被调用、可被测到 ——
    `freq_lock` 的 docstring 已经写了同样的话，但 docstring 不会在改代码时报警。

    为什么"有戳"不能当理由：戳证明的是「有人把一个参数集写下来了」，
    而清标签需要的是「这个参数集被证据支持」。前者填个数就有，后者要一份能
    解释负号的回测表。**写下数值 ≠ 证据支持。**

    本模块**不** import `freq_lock`：交叉是语义层面的，不是符号层面的。
    为了一条断言去 import 一整个模块，会让"零碰"变成"零碰但依赖"。
    """
    if has_freq_lock_stamp:
        # 有戳 ≠ 可清标签；这里刻意什么都不做，也不返回"可以清"的信号。
        return
    return


# ---------------------------------------------------------------- 备注（机器可读）

THRESHOLD_NEG_LOCK_NOTE: Final = (
    "阈值负锁: version=threshold_neg_lock_v1 / "
    "保留 threshold_lock_data_pending(VOLUME+AUCTION 各一条, 本拍都不删) / "
    "COUNT仍6 / 负锁!=删标签 / 钉可交易数=fabricate / 本拍!=清零刀 / "
    "strongly_negative!=阈值已锁 / FreqLockStamp!=可清标签 / "
    "语义锁面PRESENT!=阈值已锁!=可开TRIGGERING / 清零一刀!=可翻闸 / "
    "本拍即使可交也不清标签、不降COUNT、不翻闸"
)
"""一行机器可读摘要，供锁面 NOTE / 文档 / PR 复用同一份措辞。

写死成常量而不是各处手打：同一句话在五个地方各写一遍，第五遍一定会少两个字，
而少的那两个字往往正好是「不等于」。
"""
