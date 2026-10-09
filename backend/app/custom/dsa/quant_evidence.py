"""TSP mechanical-backtest notes for DSA skill prompts.

The numbers live in ``quant_evidence.yaml`` next to this module. Rendering
never raises: a missing file, a bad type, or an unknown skill yields an empty
appendix so skill loading keeps working.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OFF = {"", "off", "0", "false", "none"}
CAVEAT = (
    "TSP 是「机械执行」（打分选前 N 只 → 次日开盘买入 → 等权持有），"
    "DSA 是「主观精选」。负收益说明该形态在无差别执行下是负期望，"
    "是要跑赢的基准线，不是禁用令。"
)


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


def evidence_summary(path: Path | None = None) -> dict[str, Any]:
    """JSON-ready summary for the decision page. Never raises."""
    selected = evidence_path_from_env() if path is None else path
    if selected is None:
        return {"available": False, "meta": _empty_meta(), "caveat": CAVEAT, "skills": []}
    data = load_evidence(selected)
    meta_raw = data.get("_meta")
    meta = _meta(meta_raw if isinstance(meta_raw, dict) else {})
    skills: list[dict[str, Any]] = []
    for name, body in data.items():
        if not isinstance(name, str) or name.startswith("_") or not isinstance(body, dict):
            continue
        skills.append(
            {
                "name": name,
                "matched": _str_list(body.get("matched")),
                "returns": _float_list(body.get("returns")),
                "sample_sizes": _int_list(body.get("sample_sizes")),
                "verdict": str(body.get("verdict") or ""),
                "note": _collapse(body.get("note")),
                "guidance": _collapse(body.get("guidance")),
            }
        )
    return {
        "available": bool(skills),
        "meta": meta,
        "caveat": CAVEAT,
        "skills": skills,
    }


def _render_skill_appendix(skill_name: str, path: Path | None) -> str:
    data = load_evidence(shipped_evidence_path() if path is None else path)
    body = data.get(skill_name)
    if not isinstance(body, dict):
        return ""
    meta_raw = data.get("_meta")
    meta = meta_raw if isinstance(meta_raw, dict) else {}
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
    }
