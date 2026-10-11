"""只读打板落盘。页面和热门事件不在这里出网。"""
from __future__ import annotations

import json
from pathlib import Path

from app.limit_board import store
from app.limit_board.parse import split_themes
from app.news.extract import code6_of


def reasons_for(data_dir: Path, trade_date: str) -> dict[str, dict]:
    """6 位代码 → 涨停原因。没有分区时返回空字典。"""
    frame = store.read_partition(data_dir, "ths", trade_date)
    found: dict[str, dict] = {}
    if frame.is_empty():
        return found
    for row in frame.iter_rows(named=True):
        code = str(row.get("code") or "")
        if not code:
            continue
        reason = str(row.get("reason") or "").strip()
        themes = split_themes(reason)
        if not themes:
            stored = str(row.get("themes") or "")
            themes = [part for part in stored.split("、") if part]
        seal = row.get("seal_rate")
        found[code] = {
            "limit_reason": reason or None,
            "limit_themes": themes,
            "seal_rate": None if seal is None else float(seal),
            "symbol": str(row.get("symbol") or ""),
        }
    return found


def sentiment_for(data_dir: Path, trade_date: str) -> dict | None:
    frame = store.read_partition(data_dir, "sentiment", trade_date)
    if frame.is_empty():
        return None
    row = frame.to_dicts()[-1]
    ladder = {}
    raw = row.get("ladder_json") or ""
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            ladder = {str(key): int(value) for key, value in parsed.items()}
    break_rate = row.get("break_rate")
    return {
        "trade_date": trade_date,
        "zt_count": _int(row.get("zt_count")),
        "zb_count": _int(row.get("zb_count")),
        "dt_count": _int(row.get("dt_count")),
        "yzt_count": _int(row.get("yzt_count")),
        "break_rate": None if break_rate is None else float(break_rate),
        "max_height": _int(row.get("max_height")),
        "ladder": ladder,
        "advance_count": _int(row.get("advance_count")),
    }


def theme_symbols(data_dir: Path, trade_date: str, concepts: list[str]) -> dict[str, list[str]]:
    """概念名 → 当天涨停原因对上的股票。用来核对热门事件的成分。"""
    wanted = [str(name).strip() for name in concepts if str(name).strip()]
    if not wanted:
        return {}
    reasons = reasons_for(data_dir, trade_date)
    found = {name: [] for name in wanted}
    for row in reasons.values():
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        themes = row.get("limit_themes") or []
        for name in wanted:
            if len(found[name]) >= 30:
                continue
            if any(_theme_matches(theme, name) for theme in themes) and symbol not in found[name]:
                found[name].append(symbol)
    return {name: symbols for name, symbols in found.items() if symbols}


def overlay_stock(stock: dict, reasons: dict[str, dict]) -> None:
    code = code6_of(str(stock.get("symbol") or ""))
    hit = reasons.get(code) or {}
    stock["limit_reason"] = hit.get("limit_reason")
    stock["limit_themes"] = list(hit.get("limit_themes") or [])
    stock["seal_rate"] = hit.get("seal_rate")


def health(data_dir: Path) -> dict:
    from app.eastmoney.client import get_limiter
    from app.market_feeds import config

    kinds = {}
    for kind in ("zt", "zb", "dt", "yzt", "sentiment", "ths"):
        kinds[kind] = store.latest_date(data_dir, kind)
    return {
        "enabled": config.limit_board_enabled(),
        "eastmoney": config.limit_board_eastmoney(),
        "ths": config.limit_board_ths(),
        "latest": kinds,
        "eastmoney_circuit": get_limiter().snapshot(),
    }


def _theme_matches(theme: str, concept: str) -> bool:
    left = _norm(theme)
    right = _norm(concept)
    if not left or not right:
        return False
    if left == right:
        return True
    return len(left) >= 2 and len(right) >= 2 and (left in right or right in left)


def _norm(text: str) -> str:
    value = str(text or "").strip()
    for suffix in ("概念", "板块", "题材"):
        if value.endswith(suffix) and len(value) > len(suffix):
            value = value[: -len(suffix)]
    return value.strip()


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
