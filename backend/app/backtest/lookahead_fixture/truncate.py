"""数据截断：把输入裁到「时点 ≤ t」，然后**回头验一遍**。

**仅供研究，不构成投资建议；执行须人工确认。**

## 为什么截断函数自己不能算完成

一个截断函数最典型的假绿长这样：

```python
def truncate(frame, t):
    return frame.filter(pl.col("datetime") <= t)   # 看着没问题
```

然后调用方拿到它、跑一遍、和原结果比 —— 如果 `t` 传错了类型（`date` 和
`datetime` 比大小在 Polars 里会静默给出你以为的结果）、或者时区没对齐、或者
`t` 恰好晚于数据末尾，这一行什么也没裁，而**没人会发现**：重跑结果与原结果
一模一样，夹具判定"通过"。

所以本模块的纪律是：`truncate_frame()` 裁完之后必须调 `assert_truncated()`，
后者检查"留下的行确实都 ≤ t"且"t 本身可判定"。这是两件不同的事 ——
前者是意图，后者是证据。

## t 的口径：入口内写死一种

本夹具对 `t_rounds.offline` 入口采用 **交易日（`date`）+ 当日 bar 时点截断** 的
双重读法，但这**不是**通用规则。每个入口在注册时用 `t_kind` 声明自己吃哪种：

- `"date"`：`t` 是 `datetime.date`，重跑只保留 `date 列 <= t` 的整天数据；
- `"timestamp"`：`t` 是带时区的 `datetime`，重跑只保留 `datetime 列 <= t` 的行。

`truncate_frame()` 按 `t_kind` 分派，**不猜**。传 `date` 给 `timestamp` 入口会抛
`TruncationNotAppliedError`，而不是"大概也能比"。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import polars as pl

from .errors import TruncationNotAppliedError

__all__ = [
    "T_KIND_DATE",
    "T_KIND_TIMESTAMP",
    "T_KINDS",
    "resolve_t_kind",
    "truncate_frame",
    "assert_truncated",
    "max_visible_ts",
]

T_KIND_DATE = "date"
T_KIND_TIMESTAMP = "timestamp"
T_KINDS = (T_KIND_DATE, T_KIND_TIMESTAMP)


def resolve_t_kind(t: Any, t_kind: str | None) -> str:
    """把 `t` 与声明的 `t_kind` 对齐；对不上就抛，不做类型转换。

    `t_kind=None` 时按 `t` 的类型推断 —— 这是给**测试**用的便利路径。生产调用
    应该显式传 `t_kind`，因为它写在入口注册里，由夹具统一决定，不由调用点决定。
    同一入口两次调用如果推断出不同的 `t_kind`，两次的截断口径就已经不同了。
    """
    if t_kind is not None and t_kind not in T_KINDS:
        raise TruncationNotAppliedError(
            f"未知的 t_kind={t_kind!r}，合法值: {T_KINDS}"
        )
    # datetime 是 date 的子类，必须先判 datetime
    if isinstance(t, datetime):
        inferred = T_KIND_TIMESTAMP
    elif isinstance(t, date):
        inferred = T_KIND_DATE
    else:
        raise TruncationNotAppliedError(
            f"t 必须是 date 或 datetime，收到 {type(t).__name__}。"
            "无法判定截断口径时不做截断 —— 静默放过就是假绿。"
        )
    if t_kind is None:
        return inferred
    if t_kind != inferred:
        raise TruncationNotAppliedError(
            f"t={t!r} 的类型（{inferred}）与入口声明的 t_kind={t_kind} 不一致。"
            "不做隐式转换：date 与 datetime 比大小的行为在 Polars 各版本间并不一致，"
            "会静默裁错或抛底层 SchemaError —— 两种都不该从这里漏出去。"
        )
    if inferred == T_KIND_TIMESTAMP and t.tzinfo is None:
        raise TruncationNotAppliedError(
            f"t={t!r} 是无时区 datetime。跨时区比较会静默偏 8 小时，"
            "本夹具拒绝在口径不明的情况下截断。"
        )
    return inferred


def _compare_expr(frame: pl.DataFrame, col: str, t: Any, *, op: str) -> pl.Expr:
    """构造「`col` 与 `t` 比较」的表达式，**统一时区表示**。

    ## 为什么要单独抽出来

    Polars 比两个 datetime 时要求时区**表示**一致。`Asia/Shanghai`（IANA 名）与
    `UTC+08:00`（固定偏移）是同一个时区，但 Polars 按名字比，认为不一致，直接抛
    `SchemaError`。

    这不是"静默裁错"，是底层异常漏出 —— 但它出现在两个地方（`truncate_frame`
    与 `assert_truncated`），所以必须抽成一处。分两处写，迟早只有一处被修好，
    而症状是"裁对了但校验报错"这种最难归因的组合。

    `op` 只接受 `"<="` / `">"`：这两个是截断与复核唯一需要的方向。开放任意
    运算符会让这个函数变成一个迷你查询语言，而那不该出现在夹具里。
    """
    if op not in ("<=", ">"):
        raise ValueError(f"_compare_expr 只支持 '<=' / '>'，收到 {op!r}")
    dtype = frame.schema[col]
    if not isinstance(dtype, pl.Datetime):
        # `Date` 列（日线截断）没有时区概念，也不该有 —— 直接比。
        return (pl.col(col) <= t) if op == "<=" else (pl.col(col) > t)
    series_tz = dtype.time_zone
    if series_tz is None:
        # naive 列：按墙上时间比
        return (pl.col(col) <= t) if op == "<=" else (pl.col(col) > t)
    if series_tz == str(t.tzinfo):
        return (pl.col(col) <= t) if op == "<=" else (pl.col(col) > t)
    # 时区表示不同 → 两边都转到 UTC 再比。
    #
    # 这里 `t.astimezone(utc)` 与直接用 `t` 是**等价**的：`pl.lit()` 会按 t 的
    # 实际时区建字面量（`+08:00` 会变成 `Datetime(tz='UTC')` 的同一瞬间），
    # 所以 Polars 比的时候比的本来就是时刻。变异测试证实了这一点：把这行换成
    # `t_utc = t`，42 个用例全绿 —— 它是等价变异，不是测试漏了。
    #
    # 留着 `astimezone` 是为了让"左边是 UTC、右边也是 UTC"在**代码上**一眼可见；
    # 真正必需的是左边的 `convert_time_zone("UTC")`（M33 证明：去掉它立刻红）。
    t_utc = t.astimezone(timezone.utc)
    lhs = pl.col(col).dt.convert_time_zone("UTC")
    rhs = pl.lit(t_utc)
    return (lhs <= rhs) if op == "<=" else (lhs > rhs)


def truncate_frame(
    frame: pl.DataFrame,
    t: Any,
    *,
    t_kind: str | None = None,
    ts_col: str = "datetime",
    date_col: str = "date",
) -> pl.DataFrame:
    """裁到「时点 ≤ t」。裁完**必须**调用 `assert_truncated()` 复核。

    返回的是新 DataFrame（Polars 本来就不原地改），调用方不要拿原 frame 继续用。

    ## `timestamp` 口径下的时区

    见 `_compare_expr`。用 `convert_time_zone` 而不是 `replace_time_zone`：
    前者保留时刻值只换表示，后者改写时刻值。搞反了会让每一行偏 8 小时，而且
    **结果看起来很正常**（只是截断位置错了）—— 这是最难自查的一类错。
    """
    kind = resolve_t_kind(t, t_kind)
    if kind == T_KIND_DATE:
        if date_col not in frame.columns:
            raise TruncationNotAppliedError(
                f"t_kind={T_KIND_DATE} 需要列 {date_col!r}，实际列: {frame.columns}"
            )
        return frame.filter(pl.col(date_col) <= t)

    if ts_col not in frame.columns:
        raise TruncationNotAppliedError(
            f"t_kind={T_KIND_TIMESTAMP} 需要列 {ts_col!r}，实际列: {frame.columns}"
        )
    return frame.filter(_compare_expr(frame, ts_col, t, op="<="))


def max_visible_ts(frame: pl.DataFrame, *, ts_col: str = "datetime") -> datetime | None:
    """输入里最晚的一个 bar 时点；空表或没有该列时返回 `None`。"""
    if frame is None or frame.is_empty() or ts_col not in frame.columns:
        return None
    series = frame[ts_col]
    if series.dtype == pl.Utf8:
        series = series.str.to_datetime()
    value = series.max()
    return value if isinstance(value, datetime) else None


def assert_truncated(
    truncated: pl.DataFrame,
    original: pl.DataFrame,
    t: Any,
    *,
    t_kind: str | None = None,
    ts_col: str = "datetime",
    date_col: str = "date",
) -> None:
    """截断的**证据**：留下的每一行都 ≤ t，且**恰好**是原表里 ≤ t 的那些行。

    三个检查，缺一不可：

    1. `t` 的口径可判定（`resolve_t_kind` 已抛）；
    2. 截断后**不存在**任何 > t 的 bar —— "没带进未来数据"的硬证据；
    3. 截断结果的行数**等于**原表里 ≤ t 的行数 —— "该留的都留了"的硬证据。

    第 2 条和第 3 条管的是两个方向：第 2 条防"多带了未来的"，第 3 条防"少了
    该有的"。少了的那几轮同样会污染结论（偏向"被裁掉的那类轮次"），而单看
    第 2 条是看不出来的。

    ## 为什么三条检查都跑完再抛，而不是撞到第一条就抛

    最初写的是"逐条 early-return"，变异测试立刻暴露了问题：第 2 条先命中时，
    第 3 条的代码**根本不会被执行**，于是删掉第 3 条测试照样全绿 —— 那个检查
    等于没写。

    更实际的理由是排查：一份输入同时犯两条错时，一次报全比"改一条、再跑、
    再报一条"快得多。所以先把问题收进 `problems`，最后合成一条错误抛出。
    """
    kind = resolve_t_kind(t, t_kind)
    col = date_col if kind == T_KIND_DATE else ts_col

    if col not in truncated.columns:
        raise TruncationNotAppliedError(
            f"截断结果里没有 {col!r} 列，无法复核。列: {truncated.columns}"
        )

    problems: list[str] = []

    # 检查 2：结果里不得有 > t 的行
    if not truncated.is_empty():
        latest = truncated[col].max()
        leaked = truncated.filter(_compare_expr(truncated, col, t, op=">")).height
        if leaked:
            problems.append(
                f"结果里仍有 {leaked} 行 {col} > t={t!r}，最晚一行是 {latest!r}"
            )

    # 检查 3：截断结果必须是原表里 ≤ t 的那些行，**不能多也不能少**。
    #
    # 这一条最初写的是"原表有超出时行数必须变少"，变异测试证明它被检查 2 蕴含：
    # 原表有 k 行超出、结果又没超出时，行数必然 ≤ 原表行数 − k < 原表行数，
    # 所以 `>=` 那半永远不成立，把 `>=` 改成 `>` 测不出来。
    #
    # 改成直接比**应该留下的行数**之后，这条检查自己就能成立，不再依赖检查 2：
    # 结果少了（丢数据）、多了（造数据）、一样多但内容不同，三种都能独立报出来。
    # 少数据的危险性不亚于多数据 —— 少掉的那几轮会让结论偏向"被裁掉的那类"，
    # 而这一情形检查 2 是看不见的。
    if original is not None and col in original.columns:
        expected = original.filter(_compare_expr(original, col, t, op="<=")).height
        if truncated.height != expected:
            problems.append(
                f"截断结果应有 {expected} 行 {col} ≤ t={t!r}，"
                f"实际 {truncated.height} 行（原表 {original.height} 行）"
            )

    if problems:
        raise TruncationNotAppliedError(
            "截断未生效：" + "；".join(problems) + "。重跑输入带未来数据 = 夹具假绿。"
        )
