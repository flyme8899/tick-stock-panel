"""不变性断言：结构化逐字段相等，**禁止**只比条数或哈希。

**仅供研究，不构成投资建议；执行须人工确认。**

## 为什么"逐字段"是硬要求，而不是讲究

只比条数：一轮 T 的 `leg1_price` 从 10.05 变成 10.06，条数一个没少 —— 通过。
只比哈希：能查出"有变化"，但查不出"哪里变了"，而审计要的正是后者 —— 否则修复
前视时只能靠猜。而且哈希对字段顺序、浮点表示、`dict` 插入顺序敏感，换个序列化库
结论就变，等于把正确性绑在实现细节上。

所以这里做三件事：**规范化序列化** → **按键集合对齐** → **逐字段比对**。

## 差集白名单默认空

`allow_diff_fields` 缺省 `()`。默认空不是"暂时没想好放什么"，而是**本批次的
立场**：不变性被打破时，唯一正确的反应是去看代码，不是往白名单里加一列。

哪一类字段将来可能合法地进白名单？只有**非语义的**标识符 —— 比如 `run_id` 里
含随机后缀、`created_at` 取系统时钟。这类字段的特征是：它变化时不携带任何关于
"策略看到了什么数据"的信息。**任何价格、时点、状态字段都永远不能进白名单。**
这条限制不由代码强制执行（白名单是自由字符串），由 review 和执行它的任务书守。

## 为什么"缺键"必须抛错而不是跳过

`assert_invariant` 的调用方可能传一份手写的 baseline。如果某个记录的键字段
缺失，最省事的写法是 `if key not in rec: continue` —— 于是那份记录被整个跳过，
比对"全部通过"。这就是假绿。所以缺键直接抛 `KeySpecError`，让调用方补数据。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from .errors import KeySpecError, LookaheadInvariantError

__all__ = [
    "KeySpec",
    "normalize_value",
    "normalize_records",
    "assert_invariant",
]

# 浮点比较容差：相对 1e-9。
#
# 为什么不是精确 ==：截断重跑走的是同一条代码路径，同一份数据，理应逐位相同 ——
# 但 Polars 的分组顺序、`group_by` 之后的求和顺序都可能随行数变化而变，浮点加法
# 不满足结合律，于是 20.000000000000004 与 19.999999999999996 会合法地出现。
# 这不是"允许漂移"，而是承认 IEEE754 的加法顺序问题。
#
# 为什么不是 1e-6：那个量级足以把"一轮的价差从 20.00 变成 20.00001"放过 ——
# 一轮金额的 1e-6 看起来无害，但它是乘以 100 股之前的价差，且在样本量上千之后
# 能把期望值挪出可辨识的范围。1e-9 相对容差是"只吸收浮点加法顺序"的量级。
_REL_TOL = 1e-9


@dataclass(frozen=True, slots=True)
class KeySpec:
    """比对键规格。

    - `identity`: 定位同一行记录的字段（如 `(symbol, trade_date, mode, path)`）。
      这些字段**必须**存在，缺一个就抛 `KeySpecError` —— 没法定位的行无法比对。
    - `compare`: 语义字段（价格、盈亏、状态）。逐字段比对。
    - `t_field`: 该行的"产出时点"字段名。只有 `<= t` 的行进入不变性集合。
      这是"比对窗"的唯一依据，不靠调用方在外面先过滤 —— 外面过滤容易忘，
      而忘了过滤就等于把未来产出也算进不变性，测试会因为无关差异变红/变绿。
    """

    identity: tuple[str, ...]
    compare: tuple[str, ...]
    t_field: str = "leg1_ts"


def normalize_value(value: Any) -> Any:
    """把值规范成可比较、可打印的形式。

    - `float`：NaN → `"nan"` / `inf` → `"inf"` 字符串（`nan != nan`，直接比会永远不等）；
      其余保留原值，由 `_values_equal` 用容差比。
    - `datetime` / `date`：转 ISO 串。**带时区的 datetime 会带上偏移量** ——
      这正是我们要的：把无时区的 `09:30` 和带时区的 `09:30+08:00` 当成相等，
      就是抹掉了一个真实的差异。
    - `Mapping` / `Sequence`：递归规范化，`Mapping` 按键排序输出（`dict` 顺序不该
      影响判定）。
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): normalize_value(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [normalize_value(v) for v in value]
    return value


def normalize_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """逐条规范化。输出顺序与输入一致（顺序本身也是不变性的一部分，见下）。"""
    return [{str(k): normalize_value(v) for k, v in rec.items()} for rec in records]


def _values_equal(left: Any, right: Any) -> bool:
    """规范值比较。数值走相对容差，其余精确相等。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if left == right:
            return True
        scale = max(abs(float(left)), abs(float(right)), 1.0)
        return abs(float(left) - float(right)) <= _REL_TOL * scale
    return left == right


def _coerce_records(
    value: Any, *, where: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """接受 `ReplayResult` 或裸 `list[Mapping]`，返回 `(记录, meta)`。

    用鸭子类型而不是 import `ReplayResult`：`api.py` 会 import 本模块，反向
    import 就成环了。用 `hasattr` 判断也不够 —— 一个 dict 也可能有 `records`
    键。所以只认"有 `records` 且 `records` 是可迭代的非字符串"这一种形状。
    """
    if hasattr(value, "records"):
        records = getattr(value, "records")
        meta = dict(getattr(value, "meta", {}) or {})
        if callable(records):
            raise KeySpecError(f"{where}.records 是方法而不是属性，这个对象不是 ReplayResult")
        return list(records), meta
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value), {}
    raise KeySpecError(
        f"{where} 必须是 ReplayResult 或记录列表，收到 {type(value).__name__}"
    )


def _row_t(rec: Mapping[str, Any], t_field: str) -> Any:
    """取一行的产出时点（已规范化，可能是 ISO 串或 None）。"""
    return rec.get(t_field)


def _within_window(row_t: Any, t_norm: Any) -> bool:
    """该行的产出时点是否落在比对窗内（≤ t）。

    `row_t` 为 `None` 表示"这一行没有可判定的时点"。**保守处理：算作窗口内**。
    理由：算作窗口内 → 它参与比对 → 若它其实来自未来，比对会失败（我们要的）；
    算作窗口外 → 它被跳过 → 未来的产出被静默排除（假绿）。两种错法的代价不对称。
    """
    if row_t is None:
        return True
    if t_norm is None:
        return True
    return str(row_t) <= str(t_norm)


def assert_invariant(
    baseline: Any,
    replayed: Any,
    key_spec: KeySpec,
    *,
    t: Any = None,
    allow_diff_fields: tuple[str, ...] = (),
) -> None:
    """结构化逐字段相等；失败抛 `LookaheadInvariantError`。

    步骤（每一步都可能抛错，**没有**"某步不满足就跳过"的分支）：

    1. `key_spec` 自检：`identity` / `compare` 非空，否则 `KeySpecError`。
    2. 记录规范化。
    3. 按 `t` 过滤出比对窗（`t=None` 表示"调用方已经裁好"，但仍然要求
       `t_field` 字段存在 —— 缺字段说明这份数据不是被审入口产出的）。
    4. 键集合对齐：一方多出/少掉一行 → 失败（**不是**"取交集比"）。
    5. 逐字段比对；`compare` 之外的字段不参与（如 `t_round_id` 含 `ts` 后缀）。
    6. `allow_diff_fields` 里的字段跳过 —— 默认空。

    `allow_diff_fields` 只允许 `compare` 里的字段名，不允许 `identity` 字段：
    标识字段不同意味着"这是另一行"，把它加进白名单等于允许两行错配。
    """
    if not key_spec.identity:
        raise KeySpecError("key_spec.identity 不能为空：没有定位键就无法逐行比对")
    if not key_spec.compare:
        raise KeySpecError(
            "key_spec.compare 不能为空：只比条数就是假绿，至少要有一个语义字段参与比对"
        )
    bad_allowed = set(allow_diff_fields) - set(key_spec.compare)
    if bad_allowed:
        raise KeySpecError(
            f"allow_diff_fields 里有非 compare 字段: {sorted(bad_allowed)}。"
            "标识字段进白名单等于允许两行错配；不在 compare 里的字段本来就不比。"
        )

    base_records, _ = _coerce_records(baseline, where="baseline")
    play_records, _ = _coerce_records(replayed, where="replayed")

    if t is not None:
        t_norm = normalize_value(t)
        base_records = [r for r in base_records if _within_window(_row_t(r, key_spec.t_field), t_norm)]
        play_records = [r for r in play_records if _within_window(_row_t(r, key_spec.t_field), t_norm)]

    needed = set(key_spec.identity) | set(key_spec.compare) | {key_spec.t_field}

    def _index(records: list[dict[str, Any]], where: str) -> dict[tuple[Any, ...], dict[str, Any]]:
        out: dict[tuple[Any, ...], dict[str, Any]] = {}
        for pos, rec in enumerate(records):
            missing = needed - set(rec)
            if missing:
                raise KeySpecError(
                    f"{where} 第 {pos} 条记录缺少键 {sorted(missing)}。"
                    "缺键不能跳过 —— 跳过就等于这一行没被比对，而夹具会报通过。"
                )
            key = tuple(normalize_value(rec[f]) for f in key_spec.identity)
            if key in out:
                raise KeySpecError(
                    f"{where} 里出现重复定位键 {key}：同一轮出现两条记录，"
                    "比对时后者会覆盖前者，差异被吃掉。"
                )
            out[key] = rec
        return out

    base_idx = _index(base_records, "baseline")
    play_idx = _index(play_records, "replayed")

    diffs: list[dict[str, Any]] = []

    only_base = sorted(set(base_idx) - set(play_idx), key=lambda k: tuple(str(x) for x in k))
    only_play = sorted(set(play_idx) - set(base_idx), key=lambda k: tuple(str(x) for x in k))
    for key in only_base:
        diffs.append({"kind": "missing_in_replay", "key": key})
    for key in only_play:
        diffs.append({"kind": "extra_in_replay", "key": key})

    allowed = set(allow_diff_fields)
    for key in sorted(set(base_idx) & set(play_idx), key=lambda k: tuple(str(x) for x in k)):
        base_row = base_idx[key]
        play_row = play_idx[key]
        for field in key_spec.compare:
            if field in allowed:
                continue
            left = base_row[field]
            right = play_row[field]
            if not _values_equal(left, right):
                diffs.append(
                    {
                        "kind": "field_mismatch",
                        "key": key,
                        "field": field,
                        "baseline": left,
                        "replayed": right,
                    }
                )

    if diffs:
        raise LookaheadInvariantError(
            f"lookahead 不变性被打破：{len(diffs)} 处差异"
            f"（baseline {len(base_idx)} 行 / 重跑 {len(play_idx)} 行）",
            diffs=tuple(diffs),
        )

    if not base_idx and not play_idx:
        # 窗内两边都是空的 —— 这**不是**通过，是"没比到东西"。
        #
        # 空集合相等在数学上成立，在审计上毫无意义:它同样会出现在"截断把数据全裁掉了"
        # 和"入口返回空"这两种坏情况下。所以单独报错，交给调用方决定是不是真的没有轮次。
        raise LookaheadInvariantError(
            "比对窗内 baseline 与重跑都是 0 行：集合相等但没有可比内容。"
            "这不构成通过 —— 需要调用方显式确认'该窗口本就无产出'。"
        )
