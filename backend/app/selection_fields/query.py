"""把已落盘的补充字段左连到选股结果上。

缺某一项时保留原有行，只在 field_gaps 里写缺了什么。有值的格子始终显示数值。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.parquet import scan_enriched_parquet
from app.selection_fields.bars import derive_period_snapshot
from app.selection_fields.parse import (
    GAP_HOLDERS,
    GAP_INDEX,
    GAP_KEYS,
    GAP_NORTHBOUND,
    GAP_PE,
    GAP_PERIOD,
    GAP_UNLOCK,
    INDEXES,
    combine_index_members,
    northbound_note,
)
from app.selection_fields.store import (
    load_checkpoint,
    read_frame,
    read_index_meta,
    read_index_part,
    read_meta,
)

_LOOKBACK_DAYS = 420
_DATASET_GAPS = (
    ("northbound", GAP_NORTHBOUND),
    ("holders", GAP_HOLDERS),
    ("unlock", GAP_UNLOCK),
)


def merge_rows(rows: list[dict], overlay: dict[str, dict]) -> list[dict]:
    """按原顺序补空字段。已有非空值不覆盖，行数不变。"""
    merged: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            merged.append(row)
            continue
        nxt = dict(row)
        extra = overlay.get(str(row.get("symbol") or "")) or {}
        for key, value in extra.items():
            if _blank(nxt.get(key)) and not _blank(value):
                nxt[key] = value
        merged.append(nxt)
    return merged


def selection_overlay(
    data_dir: Path | str,
    symbols: list[str],
    as_of: object = None,
) -> tuple[dict[str, dict], list[str], list[str], bool, list[str]]:
    """返回 overlay、缺项名称、说明、PE 是否还在跑、缺数据列键。"""
    overlay: dict[str, dict] = {}
    gaps: list[str] = []
    notes: list[str] = []
    for kind, label in _DATASET_GAPS:
        frame = read_frame(data_dir, kind)
        meta = read_meta(data_dir, kind)
        if _note_of(meta):
            notes.append(_note_of(meta))
        if frame.height == 0 and meta.get("status") != "ok":
            gaps.append(label)
            continue
        _merge_overlay(overlay, frame)
        if kind == "northbound":
            _append_northbound_note(notes, frame)
        if meta.get("status") == "error" and frame.height > 0 and meta.get("error"):
            notes.append(str(meta["error"]))
    _attach_indexes(data_dir, overlay, gaps, notes)

    pe_frame = read_frame(data_dir, "pe_ttm")
    pe_meta = read_meta(data_dir, "pe_ttm")
    if _note_of(pe_meta) and _note_of(pe_meta) not in notes:
        notes.append(_note_of(pe_meta))
    usable = _pe_usable(pe_frame)
    if usable or pe_frame.height:
        _merge_overlay(overlay, pe_frame)
    pe_incomplete, progress = _pe_progress(data_dir)
    if pe_incomplete and progress:
        notes.append(progress)
    if not usable:
        gaps.append(GAP_PE)

    period_failed = False
    if symbols:
        try:
            period = _period_frame(data_dir, symbols, as_of)
        except Exception:  # noqa: BLE001 — 日 K 读失败时只标记这一项
            period_failed = True
        else:
            _merge_overlay(overlay, period)
    if period_failed:
        gaps.append(GAP_PERIOD)

    gap_keys = _gap_keys(gaps, pe_incomplete=pe_incomplete, period_failed=period_failed)
    return overlay, gaps, notes, pe_incomplete, gap_keys


def annotate_result(result: dict, data_dir: Path | str) -> dict:
    rows = [row for row in (result.get("rows") or []) if isinstance(row, dict)]
    symbols = [str(row.get("symbol")) for row in rows if row.get("symbol")]
    overlay, gaps, notes, pe_incomplete, gap_keys = selection_overlay(
        data_dir, symbols, result.get("as_of")
    )
    payload = dict(result)
    payload["rows"] = merge_rows(result.get("rows") or [], overlay)
    payload["field_gaps"] = gaps
    payload["field_notes"] = notes
    payload["pe_incomplete"] = pe_incomplete
    payload["gap_keys"] = gap_keys
    return payload


def annotate_cache(cached: dict, data_dir: Path | str) -> dict:
    symbols: list[str] = []
    for item in (cached.get("results") or {}).values():
        if not isinstance(item, dict):
            continue
        for row in item.get("rows") or []:
            if isinstance(row, dict) and row.get("symbol"):
                symbols.append(str(row["symbol"]))
    ever = cached.get("today_ever_rows")
    if isinstance(ever, dict):
        for mapping in ever.values():
            if isinstance(mapping, dict):
                symbols.extend(str(symbol) for symbol in mapping if symbol)
    as_of = cached.get("as_of")
    overlay, gaps, notes, pe_incomplete, gap_keys = selection_overlay(data_dir, symbols, as_of)
    payload = dict(cached)
    results = {}
    for sid, item in (cached.get("results") or {}).items():
        if not isinstance(item, dict):
            results[sid] = item
            continue
        nxt = dict(item)
        nxt["rows"] = merge_rows(item.get("rows") or [], overlay)
        results[sid] = nxt
    payload["results"] = results
    if isinstance(ever, dict):
        enriched: dict = {}
        for sid, mapping in ever.items():
            if not isinstance(mapping, dict):
                continue
            enriched[sid] = {
                symbol: merge_rows([row], overlay)[0] if isinstance(row, dict) else row
                for symbol, row in mapping.items()
            }
        payload["today_ever_rows"] = enriched
    payload["field_gaps"] = gaps
    payload["field_notes"] = notes
    payload["pe_incomplete"] = pe_incomplete
    payload["gap_keys"] = gap_keys
    return payload


def _merge_overlay(overlay: dict[str, dict], frame: pl.DataFrame) -> None:
    if frame.is_empty():
        return
    for row in frame.to_dicts():
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        bucket = overlay.setdefault(symbol, {})
        for key, value in row.items():
            if key == "symbol" or _blank(value):
                continue
            bucket[key] = value


def _blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:
        return True
    return bool(isinstance(value, str) and not value.strip())


def _note_of(meta: dict) -> str:
    text = str(meta.get("note") or "").strip()
    return text


def _append_northbound_note(notes: list[str], frame: pl.DataFrame) -> None:
    if any("季度口径" in item for item in notes):
        return
    day = ""
    if not frame.is_empty() and "trade_date" in frame.columns:
        values = [str(item) for item in frame["trade_date"].to_list() if item]
        day = max(values) if values else ""
    if day or not frame.is_empty():
        notes.append(northbound_note(day))


def _attach_indexes(
    data_dir: Path | str,
    overlay: dict[str, dict],
    gaps: list[str],
    notes: list[str],
) -> None:
    """有数据的指数照常拼上。失败的指数单独写进说明，不把整列标成缺数据。"""
    parts: list[tuple[str, dict[str, str]]] = []
    failed: list[str] = []
    any_rows = False
    for code, label, _source in INDEXES:
        frame = read_index_part(data_dir, code)
        meta = read_index_meta(data_dir, code)
        if frame.height:
            any_rows = True
            mapping = {
                str(row.get("symbol") or ""): label
                for row in frame.to_dicts()
                if row.get("symbol")
            }
            parts.append((label, mapping))
        if frame.height == 0 or meta.get("status") == "error":
            failed.append(label)
    if not any_rows:
        legacy = read_frame(data_dir, "index_members")
        if legacy.height:
            _merge_overlay(overlay, legacy)
            return
        gaps.append(GAP_INDEX)
        return
    combined = combine_index_members(parts)
    if combined:
        _merge_overlay(overlay, pl.DataFrame(combined))
    if failed:
        notes.append(f"指数成分未完成：{'、'.join(failed)}")


def _pe_usable(frame: pl.DataFrame) -> bool:
    if frame.is_empty() or "pe_ttm" not in frame.columns:
        return False
    return frame.filter(pl.col("pe_ttm").is_not_null()).height > 0


def _pe_progress(data_dir: Path | str) -> tuple[bool, str]:
    checkpoint = load_checkpoint(data_dir)
    total = int(checkpoint.get("total") or 0)
    done = len(checkpoint.get("done") or [])
    skipped = len(checkpoint.get("skipped") or [])
    if total <= 0 or done + skipped >= total:
        return False, ""
    return True, f"PE(TTM) 已完成 {done}/{total}"


def _gap_keys(gaps: list[str], *, pe_incomplete: bool, period_failed: bool) -> list[str]:
    keys: list[str] = []
    for label in gaps:
        if label == GAP_PERIOD and not period_failed:
            continue
        if label == GAP_PE and pe_incomplete:
            continue
        for key in GAP_KEYS.get(label, ()):
            if key not in keys:
                keys.append(key)
    if pe_incomplete and "pe_ttm" not in keys:
        keys.append("pe_ttm")
    if period_failed:
        for key in GAP_KEYS[GAP_PERIOD]:
            if key not in keys:
                keys.append(key)
    return keys


def _period_frame(data_dir: Path | str, symbols: list[str], as_of: object) -> pl.DataFrame:
    root = Path(data_dir) / "kline_daily_enriched"
    paths = sorted(root.rglob("*.parquet")) if root.exists() else []
    if not paths:
        raise FileNotFoundError("没有日 K")
    lazy = scan_enriched_parquet([str(path) for path in paths]).filter(
        pl.col("symbol").is_in(symbols)
    )
    day = _as_date(as_of)
    if day is not None:
        start = day - timedelta(days=_LOOKBACK_DAYS)
        lazy = lazy.filter((pl.col("date") >= start) & (pl.col("date") <= day))
    frame = lazy.select(["symbol", "date", "open", "high", "low", "close"]).collect()
    return derive_period_snapshot(frame)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
