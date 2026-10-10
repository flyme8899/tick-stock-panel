"""妙想个股主力净流入解析。

只认带金额单位的「主力净流入」列。收盘价和占比列不会被当成金额。
配额从响应里能读到的剩余次数字段取出；读不到则由调用方用本地上限。
"""
from __future__ import annotations

from typing import Any

from app.fund_flow.units import money_to_yuan

_AMOUNT_LABELS = {"主力净流入", "主力净流入资金", "主力净流入金额"}
_QUOTA_KEYS = (
    "quota_remaining",
    "quotaRemaining",
    "remainTimes",
    "remaining",
    "remain",
)


def parse_quota(payload: dict) -> int | None:
    found = _find_quota(payload, depth=0)
    if found is None:
        return None
    try:
        return int(found)
    except (TypeError, ValueError):
        return None


def _find_quota(node: object, *, depth: int) -> object | None:
    if depth > 4 or not isinstance(node, dict):
        return None
    for key in _QUOTA_KEYS:
        if key in node and node[key] is not None:
            return node[key]
    for value in node.values():
        if isinstance(value, dict):
            found = _find_quota(value, depth=depth + 1)
            if found is not None:
                return found
    return None


def _tables(payload: dict) -> list[dict]:
    data_node = payload.get("data")
    if not isinstance(data_node, dict):
        return []
    nested = data_node.get("data") if isinstance(data_node.get("data"), dict) else {}
    search = nested.get("searchDataResultDTO") if isinstance(nested, dict) else None
    candidate_a = search.get("dataTableDTOList") if isinstance(search, dict) else None
    candidate_b = data_node.get("dataTableDTOList")
    for candidate in (candidate_a, candidate_b):
        if isinstance(candidate, list) and candidate:
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _name_map(table: dict) -> dict[str, Any]:
    name_map = table.get("nameMap") or {}
    if isinstance(name_map, list):
        return {str(index): value for index, value in enumerate(name_map)}
    return name_map if isinstance(name_map, dict) else {}


def _labels(table: dict) -> list[str]:
    grid = table.get("table") or {}
    name_map = _name_map(table)
    labels = []
    for key in grid:
        if key == "headName":
            continue
        labels.append(str(name_map.get(key, name_map.get(str(key), key))))
    return labels


def _rows(table: dict) -> list[dict[str, str]]:
    grid = table.get("table") or {}
    name_map = _name_map(table)
    heads = grid.get("headName") or []
    rows: list[dict[str, str]] = []
    for key, values in grid.items():
        if key == "headName" or not isinstance(values, list):
            continue
        label = str(name_map.get(key, name_map.get(str(key), key)))
        for index, value in enumerate(values):
            while len(rows) <= index:
                rows.append({})
            rows[index][label] = "" if value is None else str(value)
    for index, head in enumerate(heads):
        while len(rows) <= index:
            rows.append({})
        rows[index]["_date"] = str(head)
    return rows


def parse_capital_flow(payload: dict) -> tuple[list[dict], int | None]:
    """返回 ``([{trade_date, main_net}], quota)``。没有金额表时行列表为空。"""
    quota = parse_quota(payload)
    if payload.get("status") not in (None, 0):
        return [], quota
    tables = [
        table for table in _tables(payload)
        if any(label in _AMOUNT_LABELS for label in _labels(table))
    ]
    if not tables:
        return [], quota
    parsed: list[dict] = []
    for row in _rows(tables[0]):
        label = next((key for key in row if key in _AMOUNT_LABELS), None)
        if label is None:
            continue
        amount = money_to_yuan(row.get(label), bare="yuan")
        trade_date = str(row.get("_date") or "")[:10]
        if not trade_date or amount is None:
            continue
        parsed.append({"trade_date": trade_date, "main_net": amount})
    return parsed, quota
