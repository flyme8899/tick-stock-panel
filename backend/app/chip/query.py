"""个股筹码查询和选股结果上的筹码列。

分布按 (symbol, date) 缓存。选股只读已经落盘的那一天，不在请求里重算全市场。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import date
from pathlib import Path

import polars as pl

from app.chip.algorithm import LOOKBACK_BARS, SOURCE_LABEL, compute_window, trapped_zones
from app.chip.store import (
    CHIP_COLUMNS,
    chip_root,
    enriched_root,
    list_dates,
    parquet_paths,
    parse_day,
    partition_file,
)
from app.news.extract import canonical_symbol
from app.parquet import scan_enriched_parquet

# 半年图再往前留一个 210 根窗口，悬停可见区间时不用逐日重读。
_VISIBLE_PAD = 220
_CACHE_MAX = 256
_cache_lock = threading.Lock()
_distribution_cache: OrderedDict[tuple, dict] = OrderedDict()
_bar_cache: dict[tuple, dict] = {}

_GAP = "筹码分布"
_GAP_KEYS = ("benefit_ratio", "avg_cost_bias", "concentration_90", "concentration_70")
_READ_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "turnover_rate"]


def clear_cache() -> None:
    with _cache_lock:
        _distribution_cache.clear()
        _bar_cache.clear()


def symbol_keys(symbol: str) -> list[str]:
    raw = str(symbol or "").strip()
    keys: list[str] = []
    canon = canonical_symbol(raw)
    for item in (canon, raw):
        if item and item not in keys:
            keys.append(item)
    return keys


def distribution(data_dir: Path | str, symbol: str, day: date | None = None) -> dict | None:
    """一只股票某一天的汇总和价格档。没有这根 K 线时返回 None。"""
    root = Path(data_dir)
    keys = symbol_keys(symbol)
    if not keys:
        return None
    enriched_days = [item for item in list_dates(enriched_root(root)) if day is None or item <= day]
    if not enriched_days:
        return None
    target = day or enriched_days[-1]
    if target not in enriched_days and day is not None:
        # 目标日没有分区。仍允许更早的缓存窗口里有这根 K，下面读不到就返回 None。
        pass
    token = _window_token(root, target)
    cache_key = (str(root), keys[0], target.isoformat(), token)
    with _cache_lock:
        hit = _distribution_cache.get(cache_key)
        if hit is not None:
            _distribution_cache.move_to_end(cache_key)
            return hit
    payload = _compute_distribution(root, keys, target)
    if payload is None:
        return None
    with _cache_lock:
        _distribution_cache[cache_key] = payload
        _distribution_cache.move_to_end(cache_key)
        while len(_distribution_cache) > _CACHE_MAX:
            _distribution_cache.popitem(last=False)
    return payload


def context_payload(data_dir: Path | str, symbol: str) -> dict | None:
    """DSA 用最近一个交易日。没有日 K 时返回 None，调用方写成缺失。"""
    days = list_dates(enriched_root(data_dir))
    if not days:
        return None
    return distribution(data_dir, symbol, days[-1])


def attach_result(payload: dict, data_dir: Path | str) -> dict:
    overlay, ready = _overlay(data_dir, parse_day(payload.get("as_of")))
    nxt = dict(payload)
    nxt["rows"] = [_merge_row(row, overlay) for row in (payload.get("rows") or [])]
    return _with_gap(nxt, ready)


def attach_cache(payload: dict, data_dir: Path | str) -> dict:
    overlay, ready = _overlay(data_dir, parse_day(payload.get("as_of")))
    nxt = dict(payload)
    results = {}
    for sid, item in (payload.get("results") or {}).items():
        if not isinstance(item, dict):
            results[sid] = item
            continue
        item_next = dict(item)
        item_next["rows"] = [_merge_row(row, overlay) for row in (item.get("rows") or [])]
        results[sid] = item_next
    nxt["results"] = results
    ever = payload.get("today_ever_rows")
    if isinstance(ever, dict):
        enriched = {}
        for sid, mapping in ever.items():
            if not isinstance(mapping, dict):
                continue
            enriched[sid] = {
                symbol: _merge_row(row, overlay) if isinstance(row, dict) else row
                for symbol, row in mapping.items()
            }
        nxt["today_ever_rows"] = enriched
    return _with_gap(nxt, ready)


def _compute_distribution(data_dir: Path, keys: list[str], day: date) -> dict | None:
    frame = _bars_through(data_dir, keys, day)
    if frame is None or frame.is_empty():
        return None
    group = frame.filter(pl.col("date") <= day).sort("date")
    if group.is_empty() or group["date"].max() != day:
        return None
    group = group.tail(LOOKBACK_BARS)
    result = compute_window(
        group["open"].to_numpy(),
        group["high"].to_numpy(),
        group["low"].to_numpy(),
        group["close"].to_numpy(),
        group["turnover_rate"].to_numpy(),
        with_bins=True,
    )
    zones = result.trapped_zones or trapped_zones(result.bins, result.close or 0, accuracy=result.accuracy)
    symbol = str(group["symbol"][-1]) if "symbol" in group.columns else keys[0]
    return {
        "symbol": symbol,
        "date": day.isoformat(),
        "source": SOURCE_LABEL,
        "estimate": True,
        "computable": result.computable,
        "close": result.close,
        "benefit_ratio": result.benefit_ratio,
        "avg_cost": result.avg_cost,
        "avg_cost_bias": result.avg_cost_bias,
        "cost_90_low": result.cost_90_low,
        "cost_90_high": result.cost_90_high,
        "concentration_90": result.concentration_90,
        "cost_70_low": result.cost_70_low,
        "cost_70_high": result.cost_70_high,
        "concentration_70": result.concentration_70,
        "accuracy": result.accuracy,
        "bins": result.bins,
        "trapped_zones": zones,
    }


def _bars_through(data_dir: Path, keys: list[str], day: date) -> pl.DataFrame | None:
    days = list_dates(enriched_root(data_dir))
    if not days:
        return None
    history_start = days[0]
    ending = [item for item in days if item <= day]
    if not ending:
        return None
    token = _window_token(data_dir, days[-1])
    cache_key = (str(data_dir), keys[0], token)
    with _cache_lock:
        cached = _bar_cache.get(cache_key)
    if cached is None or day < cached["start"] or day > cached["end"]:
        wide = [item for item in days if item <= days[-1]][-(LOOKBACK_BARS + _VISIBLE_PAD):]
        load_days = ending[-LOOKBACK_BARS:] if day < wide[0] or day > wide[-1] else wide
        frame = _read_symbol(data_dir, keys, load_days)
        if frame is None:
            return None
        cached = {
            "frame": frame,
            "start": load_days[0],
            "end": load_days[-1],
            "history_start": history_start,
        }
        with _cache_lock:
            _bar_cache[cache_key] = cached
            if len(_bar_cache) > 32:
                _bar_cache.pop(next(iter(_bar_cache)))
    frame = cached["frame"]
    if frame.is_empty():
        return frame
    sliced = frame.filter(pl.col("date") <= day)
    if sliced.is_empty() or sliced["date"].max() != day:
        # 宽窗口里没有这根 K（停牌或请求日早于缓存）。按目标日单独取 210 根。
        if cached["start"] <= day <= cached["end"]:
            return sliced
        return _read_symbol(data_dir, keys, ending[-LOOKBACK_BARS:])
    if (
        sliced.height < LOOKBACK_BARS
        and cached["start"] > cached["history_start"]
        and sliced["date"].min() <= cached["start"]
    ):
        return _read_symbol(data_dir, keys, ending[-LOOKBACK_BARS:])
    return sliced


def _read_symbol(data_dir: Path, keys: list[str], days: list[date]) -> pl.DataFrame | None:
    paths = parquet_paths(enriched_root(data_dir), days)
    if not paths:
        return None
    frame = (
        scan_enriched_parquet(paths)
        .filter(pl.col("symbol").is_in(keys))
        .select(_READ_COLUMNS)
        .collect()
    )
    if frame.is_empty():
        return frame
    if frame["symbol"].n_unique() > 1:
        preferred = keys[0]
        chosen = frame.filter(pl.col("symbol") == preferred)
        frame = chosen if not chosen.is_empty() else frame
    return frame.sort("date")


def _window_token(data_dir: Path, day: date) -> tuple:
    days = [item for item in list_dates(enriched_root(data_dir)) if item <= day]
    if not days:
        return ()
    window = days[-LOOKBACK_BARS:]
    mtimes: list[int] = []
    root = enriched_root(data_dir)
    for item in window:
        folder = root / f"date={item.isoformat()}"
        if not folder.is_dir():
            continue
        for path in folder.glob("*.parquet"):
            try:
                mtimes.append(path.stat().st_mtime_ns)
            except OSError:
                continue
    return (days[-1].isoformat(), len(window), sum(mtimes))


def _overlay(data_dir: Path | str, as_of: date | None) -> tuple[dict[str, dict], bool]:
    path = _partition_on_or_before(data_dir, as_of)
    if path is None:
        return {}, False
    frame = pl.read_parquet(path)
    overlay: dict[str, dict] = {}
    if frame.is_empty():
        return overlay, True
    columns = [name for name in ("symbol", *_GAP_KEYS) if name in frame.columns]
    for row in frame.select(columns).iter_rows(named=True):
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        overlay[symbol] = {key: row.get(key) for key in _GAP_KEYS if key in row}
    return overlay, True


def _partition_on_or_before(data_dir: Path | str, as_of: date | None) -> Path | None:
    days = list_dates(chip_root(data_dir))
    if as_of is not None:
        days = [day for day in days if day <= as_of]
    if not days:
        return None
    path = partition_file(data_dir, days[-1])
    return path if path.exists() else None


def _merge_row(row: object, overlay: dict[str, dict]) -> object:
    if not isinstance(row, dict):
        return row
    nxt = dict(row)
    extra = overlay.get(str(row.get("symbol") or "")) or {}
    for key, value in extra.items():
        if _blank(nxt.get(key)) and not _blank(value):
            nxt[key] = value
    return nxt


def _with_gap(payload: dict, ready: bool) -> dict:
    if ready:
        return payload
    gaps = list(payload.get("field_gaps") or [])
    if _GAP not in gaps:
        gaps.append(_GAP)
    keys = list(payload.get("gap_keys") or [])
    for key in _GAP_KEYS:
        if key not in keys:
            keys.append(key)
    payload["field_gaps"] = gaps
    payload["gap_keys"] = keys
    return payload


def _blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:
        return True
    return bool(isinstance(value, str) and not value.strip())


# 选股列用到的字段名，给测试和前端对齐。
SCREENER_FIELDS = CHIP_COLUMNS
