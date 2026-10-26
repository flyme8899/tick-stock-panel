"""TSP mechanical-backtest notes for DSA skill prompts.

The numbers live in ``quant_evidence.yaml`` next to this module. Rendering
never raises: a missing file, a bad type, or an unknown skill yields an empty
appendix so skill loading keeps working.

时效（P-qe）
------------
证据是**有保质期**的。这里加三道闸，任何一道不过就**不注入**：

1. ``_meta.as_of``：证据窗锚定日；缺了按 ``updated`` 兜底，两者都缺即视为
   无法判断时效 → 视为过期（fail-closed，宁可少注一条也不注入陈货）。
2. 过期：``as_of`` 距今超过 ``EXPIRY_DAYS``（默认 90 日历日），或显式
   ``expires_on`` 早于今天 → ``expired=True``，``render_skill_appendix``
   返回空串并计数。
3. 样本门槛：该 skill 的 ``sample_sizes`` **任一项** < ``MIN_SAMPLE_FOR_GUIDANCE``
   （默认 100）→ 该条 guidance 不注入，``summary`` 标 ``sample_below_threshold``。
   单条 59 笔的结论推不出「这类形态负期望」这种量级的判断。

环境标签（``_meta.env_label``）用来表达证据窗所处的行情环境。缺标签时
summary 会标 ``env_label_missing``，**不允许**把无标签的证据当「全环境适用」。

L5 表述歧义：yaml 文首「次日开盘建仓 / 信号日收盘清仓」与上游回测报告原文
是否一致**尚未定性**，``_meta.l5_entry_exit_status`` 记录该状态；未定性前
禁止当已核口径引用。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OFF = {"", "off", "0", "false", "none"}
CAVEAT = (
    "TSP 是「机械执行」（打分选前 N 只 → 次日开盘买入 → 等权持有），"
    "DSA 是「主观精选」。负收益说明该形态在无差别执行下是负期望，"
    "是要跑赢的基准线，不是禁用令。"
)

# ===== 时效门禁（P-qe）=====
MIN_SAMPLE_FOR_GUIDANCE: int = 100
EXPIRY_DAYS: int = 90
ENV_LABEL_MISSING: str = ""
L5_STATUS_PINNED: str = "pinned"
L5_STATUS_UNPINNED: str = "未定性·勿引用"


def _parse_iso_date(raw: object) -> date | None:
    """``YYYY-MM-DD`` 或可被 ``fromisoformat`` 吃下的日期串；否则 ``None``。"""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _period_end(period: object) -> date | None:
    """``"2026-07-08 ~ 2026-10-08"`` 的右端。"""
    text = str(period or "")
    if "~" not in text:
        return None
    return _parse_iso_date(text.rsplit("~", 1)[1].strip())


def resolve_as_of(meta: dict[str, Any]) -> date | None:
    """证据窗锚定日：``as_of`` → ``period`` 右端 → ``updated``。"""
    for key in ("as_of",):
        parsed = _parse_iso_date(meta.get(key))
        if parsed is not None:
            return parsed
    parsed = _period_end(meta.get("period"))
    if parsed is not None:
        return parsed
    return _parse_iso_date(meta.get("updated"))


def freshness(meta: dict[str, Any], *, today: date | None = None, expiry_days: int = EXPIRY_DAYS) -> dict[str, Any]:
    """时效判定。**判不出来就是过期**，不给「也许还行」。"""
    moment = today or date.today()
    as_of = resolve_as_of(meta)
    expires_on = _parse_iso_date(meta.get("expires_on"))
    if as_of is None:
        return {
            "as_of": "",
            "expires_on": "" if expires_on is None else expires_on.isoformat(),
            "age_days": None,
            "expiry_days": int(expiry_days),
            "expired": True,
            "reason": "as_of_missing",
        }
    age_days = (moment - as_of).days
    if expires_on is not None and moment > expires_on:
        return {
            "as_of": as_of.isoformat(),
            "expires_on": expires_on.isoformat(),
            "age_days": age_days,
            "expiry_days": int(expiry_days),
            "expired": True,
            "reason": "past_expires_on",
        }
    if age_days > int(expiry_days):
        return {
            "as_of": as_of.isoformat(),
            "expires_on": "" if expires_on is None else expires_on.isoformat(),
            "age_days": age_days,
            "expiry_days": int(expiry_days),
            "expired": True,
            "reason": "age_exceeded",
        }
    return {
        "as_of": as_of.isoformat(),
        "expires_on": "" if expires_on is None else expires_on.isoformat(),
        "age_days": age_days,
        "expiry_days": int(expiry_days),
        "expired": False,
        "reason": "",
    }


def sample_gate(sizes: list[int], *, threshold: int = MIN_SAMPLE_FOR_GUIDANCE) -> dict[str, Any]:
    """样本门槛：**任一项**不足即不过闸（不取平均、不取最大）。"""
    if not sizes:
        return {
            "threshold": int(threshold),
            "min_size": None,
            "passed": False,
            "reason": "sample_sizes_missing",
        }
    worst = min(sizes)
    passed = worst >= int(threshold)
    return {
        "threshold": int(threshold),
        "min_size": worst,
        "passed": passed,
        "reason": "" if passed else "sample_below_threshold",
    }


def shipped_evidence_path() -> Path:
    return Path(__file__).with_name("quant_evidence.yaml")


def evidence_path_from_env() -> Path | None:
    """Path selected by ``TSP_QUANT_EVIDENCE_FILE``.

    Unset means the caller should use the shipped file. ``off`` / empty
    disables injection and the decision-page summary.
    """
    if "TSP_QUANT_EVIDENCE_FILE" not in os.environ:
        return shipped_evidence_path()
    raw = os.environ.get("TSP_QUANT_EVIDENCE_FILE", "").strip()
    if raw.lower() in _OFF:
        return None
    return Path(raw)


def load_evidence(path: Path | None = None) -> dict[str, Any]:
    target = shipped_evidence_path() if path is None else path
    try:
        import yaml
    except Exception as exc:  # noqa: BLE001 - 缺解析器时只是不注入
        logger.debug("量化证据未读取：%s", exc)
        return {}
    try:
        with target.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - 坏文件不能打断技能加载或页面
        logger.debug("量化证据读取失败(%s): %s", target, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def render_skill_appendix(skill_name: str, path: Path | None = None) -> str:
    try:
        return _render_skill_appendix(skill_name, path)
    except Exception as exc:  # noqa: BLE001 - 渲染失败时保持原技能文本
        logger.debug("量化证据渲染失败(%s): %s", skill_name, exc)
        return ""

def evidence_gate(
    skill_name: str,
    body: dict[str, Any],
    meta: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """单条证据是否可用。三道闸任一不过即不可注入。"""
    fresh = freshness(meta, today=today)
    gate = sample_gate(_int_list(body.get("sample_sizes")))
    return {
        "injectable": (not fresh["expired"]) and gate["passed"],
        "freshness": fresh,
        "sample": gate,
    }


def evidence_summary(path: Path | None = None, *, today: date | None = None) -> dict[str, Any]:
    """JSON-ready summary for the decision page. Never raises."""
    selected = evidence_path_from_env() if path is None else path
    if selected is None:
        return {"available": False, "meta": _empty_meta(), "caveat": CAVEAT, "skills": []}
    data = load_evidence(selected)
    meta_raw = data.get("_meta")
    meta_dict = meta_raw if isinstance(meta_raw, dict) else {}
    meta = _meta(meta_dict)
    fresh = freshness(meta_dict, today=today)
    meta["freshness"] = fresh
    meta["env_label_missing"] = not meta["env_label"]
    meta["l5_entry_exit_status"] = meta["l5_entry_exit_status"] or L5_STATUS_UNPINNED
    skills: list[dict[str, Any]] = []
    for name, body in data.items():
        if not isinstance(name, str) or name.startswith("_") or not isinstance(body, dict):
            continue
        gate = evidence_gate(name, body, meta_dict, today=today)
        skills.append(
            {
                "name": name,
                "matched": _str_list(body.get("matched")),
                "returns": _float_list(body.get("returns")),
                "sample_sizes": _int_list(body.get("sample_sizes")),
                "verdict": str(body.get("verdict") or ""),
                "note": _collapse(body.get("note")),
                "guidance": _collapse(body.get("guidance")),
                "injectable": gate["injectable"],
                "sample_below_threshold": not gate["sample"]["passed"],
                "min_sample": gate["sample"]["min_size"],
                "sample_threshold": gate["sample"]["threshold"],
            }
        )
    return {
        "available": bool(skills),
        "meta": meta,
        "caveat": CAVEAT,
        "skills": skills,
        "expired": fresh["expired"],
        "sample_below_threshold": bool(skills) and not any(item["injectable"] for item in skills),
        "min_sample_for_guidance": MIN_SAMPLE_FOR_GUIDANCE,
    }


def _render_skill_appendix(skill_name: str, path: Path | None) -> str:
    data = load_evidence(shipped_evidence_path() if path is None else path)
    body = data.get(skill_name)
    if not isinstance(body, dict):
        return ""
    meta_raw = data.get("_meta")
    meta = meta_raw if isinstance(meta_raw, dict) else {}

    gate = evidence_gate(skill_name, body, meta)
    if not gate["injectable"]:
        reason = gate["freshness"]["reason"] or gate["sample"]["reason"] or "not_injectable"
        logger.info(
            "量化证据未注入 %s：%s（as_of=%s age=%s sample_min=%s）",
            skill_name,
            reason,
            gate["freshness"]["as_of"] or "-",
            gate["freshness"]["age_days"],
            gate["sample"]["min_size"],
        )
        return ""

    matched = _str_list(body.get("matched"))
    returns = body.get("returns") if isinstance(body.get("returns"), list) else []
    sizes = body.get("sample_sizes") if isinstance(body.get("sample_sizes"), list) else []

    lines = ["**量化回测参考**（来自 TSP 全市场机械回测，非本策略自身业绩）"]
    scope: list[str] = []
    period = meta.get("period")
    universe = meta.get("universe")
    benchmark = meta.get("benchmark_return")
    if period:
        scope.append(str(period))
    if isinstance(universe, int) and not isinstance(universe, bool):
        scope.append(f"全市场 {universe} 只")
    if benchmark:
        scope.append(f"同期基准 {benchmark}")
    if scope:
        lines.append(f"- 口径：{'；'.join(scope)}")
    if matched:
        pairs: list[str] = []
        for index, label in enumerate(matched):
            segment = f"「{label}」"
            if index < len(returns):
                rendered = _format_return(returns[index])
                if rendered:
                    segment += f" {rendered}"
            if index < len(sizes):
                rendered_size = _format_size(sizes[index])
                if rendered_size:
                    segment += f"（{rendered_size}）"
            pairs.append(segment)
        lines.append("- 对应 TSP 策略表现：" + "、".join(pairs))
    note = _collapse(body.get("note"))
    if note:
        lines.append(f"- 证据：{note}")
    guidance = body.get("guidance")
    if isinstance(guidance, str) and guidance.strip():
        lines.append("- 使用指引：")
        for raw in guidance.strip().splitlines():
            line = raw.strip()
            if line:
                lines.append(f"  {line}")
    lines.append(f"> {CAVEAT}")
    return "\n".join(lines)


def _format_return(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{float(value):+.2f}%"


def _format_size(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return f"{value} 笔"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value)} 笔"
    return ""


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None and not isinstance(item, (dict, list))]


def _float_list(value: object) -> list[float]:
    if not isinstance(value, list):
        return []
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        numbers.append(float(item))
    return numbers


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    numbers: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            numbers.append(item)
        elif isinstance(item, float) and item.is_integer():
            numbers.append(int(item))
    return numbers


def _collapse(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _empty_meta() -> dict[str, Any]:
    return {
        "source": "",
        "report": "",
        "period": "",
        "universe": None,
        "benchmark_return": "",
        "updated": "",
        "as_of": "",
        "env_label": "",
        "expires_on": "",
        "l5_entry_exit_status": L5_STATUS_UNPINNED,
        "freshness": {
            "as_of": "",
            "expires_on": "",
            "age_days": None,
            "expiry_days": EXPIRY_DAYS,
            "expired": True,
            "reason": "as_of_missing",
        },
        "env_label_missing": True,
    }


def _meta(raw: dict[str, Any]) -> dict[str, Any]:
    universe = raw.get("universe")
    universe_value = universe if isinstance(universe, int) and not isinstance(universe, bool) else None
    return {
        "source": str(raw.get("source") or ""),
        "report": str(raw.get("report") or ""),
        "period": str(raw.get("period") or ""),
        "universe": universe_value,
        "benchmark_return": str(raw.get("benchmark_return") or ""),
        "updated": str(raw.get("updated") or ""),
        "as_of": str(raw.get("as_of") or ""),
        "env_label": str(raw.get("env_label") or ENV_LABEL_MISSING),
        "expires_on": str(raw.get("expires_on") or ""),
        "l5_entry_exit_status": str(raw.get("l5_entry_exit_status") or ""),
    }
