"""给个股上下文、盘面确认和健康页读已经落盘的事件上下文。"""
from __future__ import annotations

from pathlib import Path

from app.event_context import config, store
from app.event_context.parse import code6, market_of

_HEALTH = (
    ("lhb_detail", "龙虎榜明细", "lhb", config.lhb_enabled),
    ("margin_detail", "两融明细", "margin", config.margin_enabled),
    ("qa", "互动问答", "qa", config.qa_enabled),
    ("changes", "盘口异动", "changes", config.changes_enabled),
)


def context_lines(data_dir: Path, symbol: str) -> list[str]:
    code = code6(symbol)
    if not code:
        return []
    lines: list[str] = []
    lines.extend(_lhb_lines(data_dir, code))
    lines.extend(_margin_lines(data_dir, code))
    lines.extend(_qa_lines(data_dir, code))
    lines.extend(_notice_lines(data_dir, code))
    lines.extend(_change_lines(data_dir, code))
    return lines[:12]


def changes_for(data_dir: Path, day: str) -> dict[str, list[dict]]:
    frame = store.read_partition(data_dir, "changes", day)
    grouped: dict[str, list[dict]] = {}
    for row in frame.to_dicts():
        code = str(row.get("code") or "")
        if not code:
            continue
        grouped.setdefault(code, []).append({"kind": row.get("kind") or "", "at": row.get("at") or ""})
    return grouped


def health_sources() -> list[dict]:
    from app.config import settings

    state = store.load_state(Path(settings.data_dir))
    rows = []
    for source_id, label, kind, flag in _HEALTH:
        block = state.get(kind) if isinstance(state.get(kind), dict) else {}
        error = str(block.get("last_error") or "")
        ok_at = block.get("last_ok_at")
        rows.append({
            "id": source_id,
            "label": label,
            "enabled": bool(flag()),
            "configured": True,
            "locked": True,
            "auth_state": "ok" if ok_at else "unknown",
            "last_ok_at": ok_at,
            "last_error": error,
            "items_ingested": int(block.get("items") or 0),
        })
    return rows


def _lhb_lines(data_dir: Path, code: str) -> list[str]:
    day = store.latest_date(data_dir, "lhb")
    if not day:
        return []
    rows = [row for row in store.read_partition(data_dir, "lhb", day).to_dicts() if row.get("code") == code]
    if not rows:
        return []
    net = rows[0].get("net_buy")
    brokers = [str(row.get("broker") or "") for row in rows if row.get("broker")][:3]
    text = f"龙虎榜 {day} 净买额 {_yuan(net)}"
    if brokers:
        text += " 营业部 " + "、".join(brokers)
    else:
        text += "（营业部明细为空）"
    return [text]


def _margin_lines(data_dir: Path, code: str) -> list[str]:
    dates = store.list_dates(data_dir, "margin")
    if not dates:
        return []
    day = dates[-1]
    row = _match(store.read_partition(data_dir, "margin", day), code)
    note = str((store.load_state(data_dir).get("margin") or {}).get("note") or "")
    if row is None and market_of(code) == "szse" and len(dates) >= 2:
        prev = dates[-2]
        row = _match(store.read_partition(data_dir, "margin", prev), code)
        day = prev
        note = note or f"深市明细不可用，沿用 {prev}"
    if row is None:
        return [note] if note and market_of(code) == "szse" else []
    text = (
        f"两融 {day} 融资余额 {_yuan(row.get('margin_balance'))}"
        f" 融券余额 {_yuan(row.get('short_balance'))}"
    )
    if note and market_of(code) == "szse":
        text += f"（{note}）"
    elif row.get("market") == "sse" and "szse" not in {
        item.get("market") for item in store.read_partition(data_dir, "margin", dates[-1]).to_dicts()
    }:
        text += "（仅沪市）"
    return [text]


def _qa_lines(data_dir: Path, code: str) -> list[str]:
    found = []
    for day in reversed(store.list_dates(data_dir, "qa")):
        for row in store.read_partition(data_dir, "qa", day).to_dicts():
            if row.get("code") != code:
                continue
            question = str(row.get("question") or "").strip()
            if question:
                found.append(f"互动问答 {row.get('published_at') or day} {question[:40]}")
            if len(found) >= 2:
                return found
    return found


def _notice_lines(data_dir: Path, code: str) -> list[str]:
    found = []
    for day in reversed(store.list_dates(data_dir, "notice")[-5:]):
        for row in store.read_partition(data_dir, "notice", day).to_dicts():
            if row.get("code") != code:
                continue
            title = str(row.get("title") or "").strip()
            if title:
                found.append(f"公告 {row.get('notice_date') or day} {title[:40]}")
            if len(found) >= 3:
                return found
    return found


def _change_lines(data_dir: Path, code: str) -> list[str]:
    day = store.latest_date(data_dir, "changes")
    if not day:
        return []
    kinds = []
    for row in store.read_partition(data_dir, "changes", day).to_dicts():
        if row.get("code") != code:
            continue
        label = f"{row.get('kind') or ''} {row.get('at') or ''}".strip()
        if label and label not in kinds:
            kinds.append(label)
    if not kinds:
        return []
    return [f"盘口 {day} " + "、".join(kinds[:4])]


def _match(frame, code: str) -> dict | None:
    for row in frame.to_dicts():
        if row.get("code") == code:
            return row
    return None


def _yuan(value) -> str:
    if value is None:
        return "缺失"
    number = float(value)
    if abs(number) >= 1e8:
        return f"{number / 1e8:.2f} 亿元"
    if abs(number) >= 1e4:
        return f"{number / 1e4:.2f} 万元"
    return f"{number:.0f} 元"
