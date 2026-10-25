"""夹具的两个入口函数：`run_truncated_replay` 与 `assert_invariant`。

**仅供研究，不构成投资建议；执行须人工确认。**

## 截断重放到底在问什么

一句话：**"一个只看到 ≤ t 数据的跑，能不能复现出一个看到全部数据的跑里 ≤ t 的那部分？"**

如果不能，说明原跑在 ≤ t 的时点上用了 t 之后才知道的东西 —— 那就是前视（lookahead）。
这是一个**必要条件检验**，不是充分条件：

- 通过了，只能说明"没有检测到跨 t 的信息泄漏"；
- 没通过，一定有问题（前提是同一个入口、同一套比对键）。

所以夹具给出的结论句式是"**未检出**跨截断点的前视"，而不是"已无前视"。后者是
本任务书明令禁止的空喊，也是这类夹具最常见的过度承诺：它只覆盖被注册的入口、
被选中的 t、被列入 compare 的字段。任何一处没覆盖，都在它的视野之外。

## 为什么 baseline 也必须由同一个 `build_baseline` 产出

最省事的写法是 `baseline = run_truncated_replay(entry, cfg, t_最后一天)` ——
"用全部数据跑一遍当基准"。听起来对，实际是个坑：

`run_truncated_replay` 内部会先截断再跑。拿 `t=最后一天` 调用它，截断把那一天
之后的（不存在的）行裁掉，结果"看起来"就是全量跑。**但如果截断逻辑本身坏了**
（比如 `<=` 写成了 `<`，把最后一天整个裁掉），baseline 和 replayed 会一起被裁坏，
两边一致 → 夹具报通过。**截断坏了反而更绿**，这是最坏的一种假绿。

所以 `build_baseline()` 单独存在：它**不截断**，直接跑全量。两条路径分开，截断
出问题时两边才对不上。

## 与前视无关的"另一类红"

夹具还有一种失败方式，和对 `t` 的处理无关：**某一轮自己的因果顺序错了**。
比如 `leg2_ts`（出场）早于 `leg1_ts`（入场）—— 一轮 T 在买入之前就卖出了。
这种记录在"截断 vs 全量"比对里可能完全一致（两边都错得一样），所以不变性
测不出来。`assert_invariant` 之外还有 `assert_causal_order()` 专门守这一类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .entries import replay_t_rounds_offline
from .errors import KeySpecError, LookaheadInvariantError
from .invariant import KeySpec, assert_invariant as _assert_invariant
from .registry import (
    ENTRY_T_ROUNDS_OFFLINE,
    T_ROUNDS_OFFLINE_COMPARE,
    T_ROUNDS_OFFLINE_IDENTITY,
    EntrySpec,
    get_entry,
    list_entries,
    register_entry,
)
from .truncate import T_KIND_DATE

__all__ = [
    "ReplayResult",
    "run_truncated_replay",
    "build_baseline",
    "assert_invariant",
    "assert_causal_order",
    "ensure_default_entries_registered",
]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """一次重跑的结果。

    `records` 是可比对的行；`meta` 是"关于这次跑"的信息（截断后行数、t、入口 id）。

    **`meta` 不参与不变性比对**，这是刻意的：如果 meta 也能参与判定，就可以把
    真正的差异塞进 meta 然后声称"比对通过"。它只用来在失败时帮人读日志。
    """

    entry_id: str
    t: Any
    records: tuple[dict[str, Any], ...]
    meta: Mapping[str, Any] = field(default_factory=dict)


def ensure_default_entries_registered() -> None:
    """把本拍注册的入口装上（幂等）。

    `t_rounds.offline` 是**本拍唯一**注册的入口。其它入口（策略回测
    `/api/backtest/strategy/run` 等）需要各自的比对键，本拍不钉 —— 见
    `docs/lookahead-fixture.md` 的 `design_pending` 说明。未注册的入口调用
    `run_truncated_replay` 会 fail-closed。
    """
    try:
        get_entry(ENTRY_T_ROUNDS_OFFLINE)
    except Exception:  # 未注册 → 装上；已注册 → 保持原样（幂等）
        register_entry(
            EntrySpec(
                entry_id=ENTRY_T_ROUNDS_OFFLINE,
                t_kind=T_KIND_DATE,
                key_spec=KeySpec(
                    identity=T_ROUNDS_OFFLINE_IDENTITY,
                    compare=T_ROUNDS_OFFLINE_COMPARE,
                    t_field="leg1_ts",
                ),
                replay=replay_t_rounds_offline,
                note="做 T 离线纸面回测（run_mode_a_backtest），t 为交易日",
            ),
            replace=True,
        )


def run_truncated_replay(
    entry_id: str,
    run_config: Mapping[str, Any],
    t: Any,
) -> ReplayResult:
    """用**仅含 ≤ t 的数据**重跑已注册入口。

    失败语义（全部 fail-closed，没有"跑了但没比"的中间态）：

    - 入口未注册 → `UnregisteredEntryError`
    - `run_config` 形状不对 / 缺 `feature_frame` → `KeySpecError`
    - `t` 的类型与入口声明的 `t_kind` 不一致 → `TruncationNotAppliedError`
    - 截断没生效 → `TruncationNotAppliedError`

    ## 返回的记录是"该入口的产出"，不做窗口过滤

    过滤 `<= t` 是 `assert_invariant` 的事（它拿得到 baseline 和 t，只有它能做
    对称的过滤）。在这里先裁一刀，会出现"我裁了你没裁"的不对称 —— 而那种不对称
    恰好会让差异消失。
    """
    ensure_default_entries_registered()
    spec = get_entry(entry_id)
    rows = spec.replay(run_config, t)
    if rows is None:
        raise KeySpecError(
            f"入口 {entry_id!r} 的 replay 返回了 None。"
            "返回 None 既不是空列表也不是错误 —— 无法判定，按 fail-closed 处理。"
        )
    records = tuple(dict(r) for r in rows)
    return ReplayResult(
        entry_id=entry_id,
        t=t,
        records=records,
        meta={
            "entry_id": entry_id,
            "t": str(t),
            "n_records": len(records),
            "note": "meta 不参与不变性比对",
        },
    )


def build_baseline(
    entry_id: str,
    run_config: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """用**全量数据**跑一遍，作为 baseline。

    刻意**不**走 `run_truncated_replay(..., t=最晚一天)`：那条路会连带把截断逻辑
    也用在 baseline 上，于是截断坏掉时两边一起坏、比对一致、夹具假绿。这里直接
    调 `spec.replay` 的原始形态需要截断参数，所以用一个**哨兵 t**（晚于所有数据）
    走同一条 replay 路径，但**不**经过 `truncate_frame` 的"严格变少"检查 —— 因为
    `t = date.max` 时本来就不该少行。

    具体做法：把 `t` 设成 `date.max`，`truncate_frame` 会保留全部行，
    `assert_truncated` 的第 3 条（原表有超出则必须变少）不会触发，第 2 条
    （不得有 > t 的行）恒成立。于是得到的就是全量结果，而路径与重跑**同构** ——
    这一点很重要：baseline 与 replayed 必须走同一段代码，否则两者的差异可能来自
    调用路径而不是数据可见性。
    """
    import datetime as _dt

    ensure_default_entries_registered()
    spec = get_entry(entry_id)
    if spec.t_kind != T_KIND_DATE:
        raise KeySpecError(
            f"build_baseline 目前只支持 t_kind=date 的入口（收到 {spec.t_kind}）。"
            "timestamp 入口需要显式给一个晚于数据的 t，本拍未注册此类入口。"
        )
    rows = spec.replay(run_config, _dt.date.max)
    if rows is None:
        raise KeySpecError(f"入口 {entry_id!r} 的 replay 在 baseline 上返回了 None")
    return tuple(dict(r) for r in rows)


def assert_invariant(
    baseline: Any,
    replayed: Any,
    key_spec: KeySpec | None = None,
    *,
    t: Any = None,
    allow_diff_fields: tuple[str, ...] = (),
) -> None:
    """`invariant.assert_invariant` 的门面：`key_spec` 省略时按 `replayed.entry_id` 取。

    为什么允许省略：调用方多半是"跑一遍截断重放，然后和 baseline 比"，它手上
    只有 `ReplayResult`。让它自己 `get_entry().key_spec` 是重复劳动，而且抄错一个
    字段名就变成两套键。省略不等于"用通用键" —— 仍然按注册表取，未注册照抛。
    """
    if key_spec is None:
        if not hasattr(replayed, "entry_id"):
            raise KeySpecError(
                "key_spec 省略时必须传入带 entry_id 的 ReplayResult；"
                "裸记录列表无法反查比对键，而'猜一套键'正是假绿的来源。"
            )
        ensure_default_entries_registered()
        key_spec = get_entry(getattr(replayed, "entry_id")).key_spec
    _assert_invariant(
        baseline,
        replayed,
        key_spec,
        t=t,
        allow_diff_fields=allow_diff_fields,
    )


def assert_causal_order(
    records: Any,
    *,
    entry_id: str = ENTRY_T_ROUNDS_OFFLINE,
) -> None:
    """检查每一轮自己的时间顺序：**出场不得早于入场**。

    ## 这一类红和"截断重放"是两回事

    截断重放比的是"同一条记录在两个数据可见性下是否一致"。如果某轮记录本身就
    反了（`leg2_ts < leg1_ts`），那它在截断跑和全量跑里会**一样地反**，不变性
    检查完全看不出问题。

    而这恰恰是路径 A 那一类前视 bug 的直接后果：确认时刻被拖到下午，出场却从
    边沿那根之后开始搜，于是记下一笔早于入场的出场。所以这里要单独一条检查。

    失败抛 `LookaheadInvariantError`（带 diffs 明细），与不变性失败同一个异常族 ——
    两者都是"这一轮不成立"，调用方通常一把 `except LookaheadFixtureError` 兜住。
    """
    spec = get_entry(entry_id) if entry_id in list_entries() else None
    if spec is None:
        ensure_default_entries_registered()
        spec = get_entry(entry_id)
    rows = list(getattr(records, "records", records))

    bad: list[dict[str, Any]] = []
    for rec in rows:
        leg1_ts = rec.get("leg1_ts")
        leg2_ts = rec.get("leg2_ts")
        if leg1_ts is None:
            # 没有入场时点 —— 这一轮连第一腿都没成交，不该出现；记 FAIL 而不是跳过
            bad.append({"kind": "missing_leg1_ts", "row": dict(rec)})
            continue
        if leg2_ts is None:
            # 未结算 / 未回摆：只看第一腿，跳过
            continue
        if str(leg2_ts) < str(leg1_ts):
            bad.append(
                {
                    "kind": "leg2_before_leg1",
                    "symbol": rec.get("symbol"),
                    "mode": rec.get("mode"),
                    "path": rec.get("path"),
                    "leg1_ts": leg1_ts,
                    "leg2_ts": leg2_ts,
                }
            )

    if bad:
        raise LookaheadInvariantError(
            f"因果顺序被打破：{len(bad)} 行记录的时间顺序不成立"
            "（出场早于入场或入场时点缺失）。这类问题不变性比对测不出来 —— "
            "两边会一样地错。",
            diffs=tuple(bad),
        )
