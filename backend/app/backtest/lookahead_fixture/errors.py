"""lookahead 偏差审计夹具的专用错误。

**仅供研究，不构成投资建议；执行须人工确认。**

这个模块只有错误类型。分开的理由：夹具的其余部分（截断 / 不变性 / 注册）都要
抛错，而调用方往往只需要 `except LookaheadFixtureError` 一把兜住 —— 错误类型
在一个文件里读完，比在四个模块里各找一段 `class XxxError` 省事。

## 为什么是 `Exception` 而不是 `AssertionError`

`AssertionError` 会被 `pytest` 当成普通断言失败，也和 `python -O` 有关（优化模式
下 `assert` 被剥掉，但如果异常本身是 `AssertionError`，语义就乱了）。更实际的
理由：夹具的失败必须能**被调用方程序化识别**，而不是被当成"测试写错了"。所以
全部继承 `Exception`。

## fail-closed 的统一出口

`LookaheadFixtureError` 是所有夹具错误的基类。它存在的一个硬用途：
`assert_invariant` 的**所有**失败路径都必须抛它的子类 —— 包括"比对键缺失"这种
"看起来只是参数没给全"的情况。参数没给全就放过，正是假绿的入口。
"""

from __future__ import annotations

__all__ = [
    "LookaheadFixtureError",
    "UnregisteredEntryError",
    "TruncationNotAppliedError",
    "KeySpecError",
    "LookaheadInvariantError",
]


class LookaheadFixtureError(Exception):
    """夹具的基类错误。凡是"夹具没跑成"或"夹具判定不通过"，都是它的子类。"""


class UnregisteredEntryError(LookaheadFixtureError):
    """入口未注册。

    未注册意味着"夹具不知道这个入口的比对键和重跑方式"。此时**不能**退化成
    "没有入口 → 没有差异 → 通过" —— 那是假绿的定义。
    """

    def __init__(self, entry_id: str, known: tuple[str, ...] = ()) -> None:
        self.entry_id = entry_id
        self.known = known
        known_text = "、".join(known) if known else "（空注册表）"
        super().__init__(
            f"入口 {entry_id!r} 未注册，夹具无法审计。已注册: {known_text}。"
            "未注册不等于通过 —— 见 fail-closed 约定。"
        )


class TruncationNotAppliedError(LookaheadFixtureError):
    """截断没有生效：重跑输入里仍存在时点 > t 的行，或 t 无法判定。

    这条错误**专治**最常见的假绿：截断函数写了个 `if` 但条件永远不成立，
    于是"重跑"其实是"原样再跑一遍"，两次结果当然一样，夹具一路绿灯 —— 而它
    什么都没证明。所以截断之后必须**回头看**一眼输入。
    """


class KeySpecError(LookaheadFixtureError):
    """比对键规格本身不合格：缺最小键、键集合为空、字段不存在等。

    和 `TruncationNotAppliedError` 一样属于"夹具没跑成"，不是"被审对象有问题"。
    但两者都不许静默放过。
    """


class LookaheadInvariantError(LookaheadFixtureError):
    """不变性被打破：截断重跑的结果与 baseline 在 ≤ t 的记录上不一致。

    这是夹具真正想抓的东西。`diffs` 保留结构化差异清单，便于在 CI 日志里直接
    看出"哪一轮、哪个字段、baseline 是什么、重跑是什么"。
    """

    def __init__(self, message: str, diffs: tuple[dict[str, object], ...] = ()) -> None:
        self.diffs = diffs
        if diffs:
            lines = [message, "", "差异明细（最多 10 条）:"]
            for d in diffs[:10]:
                lines.append(f"  - {d}")
            if len(diffs) > 10:
                lines.append(f"  ... 另有 {len(diffs) - 10} 条")
            message = "\n".join(lines)
        super().__init__(message)
