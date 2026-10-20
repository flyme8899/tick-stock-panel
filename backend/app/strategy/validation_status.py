"""内置策略验证状态。来源：策略体检 r2，2026-10-20。

只供展示层和推送文案使用。信号计算、回测和信号导出不读这张表。
META 没有 validation_status 时，按 reference_only 处理。
"""
from __future__ import annotations

from collections.abc import Mapping

SOURCE = "策略体检 r2，2026-10-20"

STATUSES = ("reverse", "retired", "unverified", "reference_only")

REVERSE_NOTE = "历史上该信号后 20 日显著跑输行业"
RETIRED_NOTE = "已下线，不作为买点展示，也不推送"
UNVERIFIED_NOTE = "未经验证"
FACTOR_RANK_NOTE = "导出失败"
REFERENCE_NOTE = "历史未证实，仅供参考"
BULLISH_ALIGNMENT_NOTE = f"{REFERENCE_NOTE}。历史上信号后 5 日偏弱"

LABELS = {
    "reverse": "风险提示",
    "retired": "已下线",
    "unverified": "未经验证",
    "reference_only": "仅供参考",
}

# 买点事件在推送里不再写成买入。卖出和移出仍保留原动作，只附加说明。
_BUY_PUSH_EVENTS = frozenset({"buy_signal", "pool_entry", "new_entry"})

# id -> (status, note)。29 个内置策略，5 + 10 + 4 + 10。
CLASSIFICATION: dict[str, tuple[str, str]] = {
    # reverse：不再当买点，改成风险提示
    "consecutive_limit_ups": ("reverse", REVERSE_NOTE),
    "limit_up_momentum": ("reverse", REVERSE_NOTE),
    "broken_board_recovery": ("reverse", REVERSE_NOTE),
    "near_limit_up": ("reverse", REVERSE_NOTE),
    "strong_open": ("reverse", REVERSE_NOTE),
    # retired：不进买点，不推送；研究和回测仍可计算
    "breakout_new_high_60d": ("retired", RETIRED_NOTE),
    "boll_breakout": ("retired", RETIRED_NOTE),
    "active_limit_gene": ("retired", RETIRED_NOTE),
    "ma_golden_cross": ("retired", RETIRED_NOTE),
    "platform_consolidation_breakout": ("retired", RETIRED_NOTE),
    "trend_breakout": ("retired", RETIRED_NOTE),
    "macd_golden": ("retired", RETIRED_NOTE),
    "ma_convergence_breakout": ("retired", RETIRED_NOTE),
    "volume_price_surge": ("retired", RETIRED_NOTE),
    "high_turnover_surge": ("retired", RETIRED_NOTE),
    # unverified
    "fundamental_m1": ("unverified", UNVERIFIED_NOTE),
    "fundamental_m2": ("unverified", UNVERIFIED_NOTE),
    "fundamental_m3": ("unverified", UNVERIFIED_NOTE),
    "factor_rank_research": ("unverified", FACTOR_RANK_NOTE),
    # reference_only：其余内置策略，含 rsi_midline_pullback
    "bullish_alignment": ("reference_only", BULLISH_ALIGNMENT_NOTE),
    "long_lower_shadow_reversal": ("reference_only", REFERENCE_NOTE),
    "low_volatility_leader": ("reference_only", REFERENCE_NOTE),
    "macd_below_zero_revival": ("reference_only", REFERENCE_NOTE),
    "n_day_low_reversal": ("reference_only", REFERENCE_NOTE),
    "oversold_bounce": ("reference_only", REFERENCE_NOTE),
    "oversold_reversal": ("reference_only", REFERENCE_NOTE),
    "pullback_ma20_bounce": ("reference_only", REFERENCE_NOTE),
    "pullback_to_support": ("reference_only", REFERENCE_NOTE),
    "rsi_midline_pullback": ("reference_only", REFERENCE_NOTE),
}


def validation_fields(strategy_id: str) -> dict[str, str]:
    """写入策略 META 的两个字段。未列入分类表时默认 reference_only。"""
    status, note = CLASSIFICATION.get(strategy_id, ("reference_only", REFERENCE_NOTE))
    return {"validation_status": status, "validation_note": note}


def _default_note(status: str, strategy_id: str) -> str:
    if status == "reverse":
        return REVERSE_NOTE
    if status == "retired":
        return RETIRED_NOTE
    if status == "unverified":
        if strategy_id == "factor_rank_research":
            return FACTOR_RANK_NOTE
        return UNVERIFIED_NOTE
    return REFERENCE_NOTE


def resolve(meta: Mapping | None) -> tuple[str, str]:
    """返回 (validation_status, validation_note)。缺字段时默认 reference_only。"""
    data = meta or {}
    status = data.get("validation_status")
    strategy_id = str(data.get("id") or "")
    if status in STATUSES:
        note = data.get("validation_note")
        if isinstance(note, str) and note.strip():
            return str(status), note.strip()
        return str(status), _default_note(str(status), strategy_id)
    if strategy_id in CLASSIFICATION:
        return CLASSIFICATION[strategy_id]
    return "reference_only", REFERENCE_NOTE


def attach_validation(meta: dict) -> dict:
    """保证运行时 META 带有验证状态。已写明的字段保持不变。"""
    status, note = resolve(meta)
    meta["validation_status"] = status
    meta["validation_note"] = note
    return meta


def pushes(meta: Mapping | None) -> bool:
    """下线策略不推送。其余状态照常推送，并带上说明。"""
    return resolve(meta)[0] != "retired"


def annotate_strategy_push(meta: Mapping | None, event_type: str, message: str) -> str | None:
    """推送文案。返回 None 表示这条不推（下线策略）。"""
    status, note = resolve(meta)
    if status == "retired":
        return None
    text = message or ""
    if status == "reverse" and event_type in _BUY_PUSH_EVENTS:
        text = text.replace("买入信号", "风险提示").replace("进入选股结果", "风险提示")
        if note not in text:
            text = f"{text} · {note}" if text else note
        return text
    suffix = _push_suffix(status, note)
    if suffix and suffix not in text:
        text = f"{text} · {suffix}" if text else suffix
    return text


def _push_suffix(status: str, note: str) -> str:
    if status == "unverified":
        if note and note != UNVERIFIED_NOTE:
            return f"{UNVERIFIED_NOTE}：{note}"
        return UNVERIFIED_NOTE
    if status == "reference_only":
        return note or REFERENCE_NOTE
    if status == "reverse":
        return note or REVERSE_NOTE
    return ""


def present_trade_markers(meta: Mapping | None, markers: list[dict]) -> tuple[list[dict], str]:
    """买点列表的展示过滤。不改传入列表，也不影响信号计算。

    retired：去掉买点。
    reverse：买点改成风险提示，规则文案换成体检结论。
    unverified / reference_only：仍展示买点，并附上说明。
    """
    status, note = resolve(meta)
    shown: list[dict] = []
    for marker in markers:
        side = marker.get("side")
        if side == "buy" and status == "retired":
            continue
        if side == "buy" and status == "reverse":
            shown.append({**marker, "side": "risk", "style": "risk", "rule": note})
            continue
        if side == "buy" and note:
            rule = str(marker.get("rule") or "")
            if note not in rule:
                rule = f"{rule} · {note}" if rule else note
            shown.append({**marker, "rule": rule})
            continue
        shown.append(dict(marker))
    return shown, note
