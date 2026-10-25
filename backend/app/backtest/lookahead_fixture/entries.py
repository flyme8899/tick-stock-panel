"""`t_rounds.offline` 入口适配器：把离线回测的记录压成可比对的平表。

**仅供研究，不构成投资建议；执行须人工确认。**

## 这个文件只做"翻译"，不做"判断"

夹具和业务代码之间只有一个接触面：`TRoundRecord` → `dict`。所有"这一轮该不该
算、这个状态对不对"的判断都留在 `t_rounds` 里，本模块只是把字段取出来、拼成
`registry.T_ROUNDS_OFFLINE_IDENTITY/COMPARE` 需要的那几个键。

这样做的好处是**零碰**：本拍不改 5c 的 `_settle` / 跑表 / 闸门一行代码，也不
给它们加"为了夹具"的参数。适配器只**读**已有字段。

## `path` 从哪来

记录的 `trigger_layer_ref` 是 `f"offline/{path}"`，`metadata["gate_path"]` 也是
`path`。两个都取自 `_make_record`，都指向同一个值。这里读 `metadata["gate_path"]`
（更结构化），回退到 `trigger_layer_ref` 的后缀。**不编造**：两个都取不到就抛错，
而不是填个空串 —— 空串会让 A 与 B 两格的记录撞键。

## 为什么不把 `t_round_id` 当键

它把 `confirm_ts` 拼进了串里，而 `confirm_ts` 恰恰是本夹具要审的东西之一。用
它当键，等于"入场时刻不同 → 认成两行 → 一边缺失一边多出"，报出来的错是
`missing_in_replay`，而真正的病灶（`leg1_price` 用了确认 bar 自己的价）被掩盖了。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.t_rounds.models import TRoundRecord

from .errors import KeySpecError

__all__ = [
    "record_to_row",
    "records_to_rows",
    "build_run_config",
    "replay_t_rounds_offline",
]

_PATH_FALLBACK_PREFIX = "offline/"


def _resolve_path(record: TRoundRecord) -> str:
    """取这一轮所属的路径格（`A` / `B`）。取不到就抛，不填默认值。"""
    meta = record.metadata or {}
    path = meta.get("gate_path")
    if isinstance(path, str) and path:
        return path
    ref = record.trigger_layer_ref or ""
    if ref.startswith(_PATH_FALLBACK_PREFIX) and len(ref) > len(_PATH_FALLBACK_PREFIX):
        return ref[len(_PATH_FALLBACK_PREFIX):]
    raise KeySpecError(
        f"记录 {record.t_round_id!r} 取不到 path（metadata.gate_path 与 "
        f"trigger_layer_ref 都不含）。填默认值会让 A/B 两格撞键，所以这里抛错。"
    )


def record_to_row(record: TRoundRecord) -> dict[str, Any]:
    """一条记录 → 可比对的一行。

    字段全部来自既有记录，**没有**任何计算或补值：

    | 输出键 | 来源 |
    |---|---|
    | `symbol` / `trade_date` / `mode` | 同名字段 |
    | `path` | `metadata.gate_path`（回退 `trigger_layer_ref` 后缀） |
    | `leg1_ts` / `leg2_ts` / `round_status` | 同名字段 |
    | `leg1_price` / `leg2_price` | 同名字段 |
    | `gross_spread` / `net_spread` | 同名字段（未结算即 `None`） |
    """
    if not isinstance(record, TRoundRecord):
        raise KeySpecError(
            f"期望 TRoundRecord，收到 {type(record).__name__}。"
            "适配器不做鸭子类型 —— 换类型意味着比对键可能已经对不上了。"
        )
    return {
        "symbol": record.symbol,
        "trade_date": record.trade_date,
        "mode": record.mode,
        "path": _resolve_path(record),
        "leg1_ts": record.leg1_ts,
        "leg2_ts": record.leg2_ts,
        "round_status": record.round_status,
        "leg1_price": record.leg1_price,
        "leg2_price": record.leg2_price,
        "gross_spread": record.gross_spread,
        "net_spread": record.net_spread,
    }


def records_to_rows(records: Iterable[TRoundRecord]) -> list[dict[str, Any]]:
    return [record_to_row(r) for r in records]


def build_run_config(
    frame: Any,
    *,
    require_prior_cross: bool = True,
) -> dict[str, Any]:
    """冻结一份离线回测的入参。

    `frame` 是合成特征表（**禁止**读生产采样）。返回的 dict 会被传给
    `run_mode_a_backtest(frame, **params)`，同时也是"这个入口的 run_config 长什么样"
    的唯一定义 —— 夹具不自己拼参数。
    """
    return {
        "feature_frame": frame,
        "params": {"require_prior_cross": require_prior_cross},
    }


def _coerce_run_config(run_config: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    """校验 run_config 形状；缺 `feature_frame` 就抛，不猜空表。"""
    if not isinstance(run_config, Mapping):
        raise KeySpecError(f"run_config 必须是 Mapping，收到 {type(run_config).__name__}")
    if "feature_frame" not in run_config:
        raise KeySpecError(
            "run_config 缺 'feature_frame'。用 build_run_config() 构造 —— "
            "缺输入就不能重跑，而'跑不了'绝不能被当成'没有差异'。"
        )
    params = dict(run_config.get("params") or {})
    return run_config["feature_frame"], params


def replay_t_rounds_offline(run_config: Mapping[str, Any], t: Any) -> list[dict[str, Any]]:
    """`t_rounds.offline` 的重跑实现。

    **截断在这里发生**：先把 `feature_frame` 裁到 `date <= t`，再跑
    `run_mode_a_backtest`。裁完立刻 `assert_truncated()` 复核 —— 不复核的话，
    "裁了但等于没裁"会静默通过。

    返回的是平表行，已按 `(path, mode, leg1_ts)` 排序：产出的**顺序**本身也是不变性
    的一部分（同一批数据跑两次顺序不同，说明有未显式化的依赖，比如 `set` 迭代）。
    """
    from app.t_rounds.offline.backtest import run_mode_a_backtest

    from .truncate import assert_truncated, truncate_frame

    frame, params = _coerce_run_config(run_config)
    truncated = truncate_frame(frame, t, t_kind="date")
    assert_truncated(truncated, frame, t, t_kind="date")

    summary = run_mode_a_backtest(truncated, **params)
    rows = records_to_rows(summary.records)
    return sorted(
        rows,
        key=lambda r: (str(r["path"]), str(r["mode"]), str(r["leg1_ts"]), str(r["symbol"])),
    )
