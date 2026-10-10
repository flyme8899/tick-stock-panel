"""个股日K买卖点 — 策略信号、DSA 价位、模拟盘成交、做T提醒。

信号只使用目标日及之前已经能看到的 K 线（上升沿对比前一根）。
后 5 / 后 20 日涨跌用信号日之后的前复权收盘价，只用于展示，不回写信号。
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

WARMUP_FLOOR_DAYS = 180
FORWARD_DAYS = 45
CACHE_TTL_S = 120
CACHE_MAX = 128
VWAP_BAND = 0.015
T_COOLDOWN_MIN = 60

_BREAKOUT_TOKENS = ("breakout", "n_day_high", "new_high", "突破")
_BUY_T = frozenset({"below_vwap", "near_low"})
_SELL_T = frozenset({"above_vwap", "near_high"})
_LEVEL_LABEL = {"support": "支撑", "resistance": "压力", "stop": "止损"}
_KEY_KIND = {
    "support_level": "support",
    "support": "support",
    "支撑": "support",
    "支撑位": "support",
    "第一支撑": "support",
    "resistance_level": "resistance",
    "resistance": "resistance",
    "压力": "resistance",
    "压力位": "resistance",
    "第一压力": "resistance",
    "stop_loss": "stop",
    "止损": "stop",
    "止损价": "stop",
    "止损位": "stop",
}
_TEXT_PATTERNS = (
    ("support", re.compile(r"支撑(?:位|价)?\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)")),
    ("resistance", re.compile(r"压力(?:位|价)?\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)")),
    ("stop", re.compile(r"止损(?:位|价)?\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)")),
)
_PRICE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")
_CROSS_SECTION_NOTE = (
    "基本面预设依赖全市场截面（行业名次、入选数量）。"
    "当前没有覆盖该区间的全市场日K缓存，不能按同一口径计算，因此不标买卖点。"
)

_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


def clear_trade_mark_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def needs_cross_section(defn) -> bool:
    tags = defn.meta.get("tags") or []
    sid = str(defn.meta.get("id") or "")
    return sid.startswith("fundamental_") or "基本面" in tags


def is_breakout_entry(defn, signal_ids: tuple[str, ...] | list[str]) -> bool:
    tags = [str(tag) for tag in (defn.meta.get("tags") or [])]
    name = str(defn.meta.get("name") or "")
    blob = " ".join([*tags, name, *[str(item) for item in signal_ids]])
    return any(token in blob for token in _BREAKOUT_TOKENS)


def catalog(engine) -> list[dict]:
    rows = []
    for meta in engine.list_strategies():
        timeframes = meta.get("timeframes") or ["1d"]
        if "1d" not in timeframes or meta.get("research_only"):
            continue
        tags = list(meta.get("tags") or [])
        sid = str(meta.get("id") or "")
        rows.append({
            "id": sid,
            "name": meta.get("name") or sid,
            "source": meta.get("source") or "",
            "tags": tags,
            "fundamental": sid.startswith("fundamental_") or "基本面" in tags,
        })
    return rows


def parse_price(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 0 and math.isfinite(number):
            return number
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or text in {"-", "—", "无", "null", "None"}:
            return None
        match = _PRICE_RE.search(text)
        if match is None:
            return None
        number = float(match.group(1))
        return number if number > 0 else None
    return None


def parse_dsa_levels(report: Any) -> list[dict]:
    """从最新 DSA 报告里取支撑 / 压力 / 止损。没有则空列表。"""
    found: dict[str, float] = {}
    if not isinstance(report, dict):
        return []
    strategy = report.get("strategy")
    if isinstance(strategy, dict):
        price = parse_price(strategy.get("stop_loss"))
        if price is not None:
            found["stop"] = price
    _walk_levels(report, found, depth=0)
    if len(found) < 3:
        _scan_level_text(report, found, depth=0)
    ordered = []
    for kind in ("support", "resistance", "stop"):
        price = found.get(kind)
        if price is None:
            continue
        ordered.append({
            "kind": kind,
            "label": _LEVEL_LABEL[kind],
            "price": round(price, 4),
        })
    return ordered


def replay_t_marks(
    bars: list[dict],
    *,
    symbol: str,
    prev_close: float | None,
    cooldown_min: int = T_COOLDOWN_MIN,
) -> list[dict]:
    """用做T推送规则在分钟序列上找边沿。第一根不触发，同一原因默认冷却 60 根。"""
    from app.news.push import RANGE_RULES, T_LABELS, session_signal_sets

    if not bars:
        return []
    sets = session_signal_sets(
        bars,
        mode="t",
        symbol=symbol,
        prev_close=prev_close,
        vwap_band=VWAP_BAND,
    )
    previous: set[str] | None = None
    last_fire: dict[str, int] = {}
    marks: list[dict] = []
    cooldown = max(1, int(cooldown_min))
    for index, current in enumerate(sets):
        if previous is None:
            previous = set(current)
            continue
        clock = _clock_of(bars[index].get("datetime"))
        fresh = []
        for reason in sorted(current - previous):
            last = last_fire.get(reason)
            if last is not None and index - last < cooldown:
                continue
            if clock is not None and not _in_t_span(clock):
                continue
            if reason in RANGE_RULES and clock is not None and clock < dt_time(10, 0):
                continue
            fresh.append(reason)
        previous = set(current)
        price = _finite(bars[index].get("close"))
        stamp = _hhmm(bars[index].get("datetime"))
        for reason in fresh:
            last_fire[reason] = index
            if price is None or not stamp:
                continue
            side = "buy" if reason in _BUY_T else "sell" if reason in _SELL_T else "neutral"
            marks.append({
                "time": stamp,
                "rule": T_LABELS.get(reason, reason),
                "side": side,
                "price": round(price, 4),
            })
    return marks


def paper_fills(data_dir: Path, symbol: str, start: str, end: str) -> list[dict]:
    from app.strategy import paper

    try:
        account_ids = [row["id"] for row in paper.list_accounts(data_dir)]
    except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
        logger.warning("paper accounts unreadable", exc_info=True)
        account_ids = []
    if not account_ids:
        account_ids = [paper.DEFAULT_ACCOUNT_ID]
    found = []
    for account_id in account_ids:
        try:
            rows = paper.load_fills(data_dir, account_id)
        except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
            logger.warning("paper fills unreadable for %s", account_id, exc_info=True)
            continue
        for row in rows:
            if row.get("kind", "fill") != "fill":
                continue
            if str(row.get("symbol") or "") != symbol:
                continue
            day = str(row.get("date") or "")[:10]
            side = row.get("side")
            price = _finite(row.get("price"))
            if side not in {"buy", "sell"} or not day or day < start or day > end or price is None:
                continue
            try:
                qty = int(row.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0
            found.append({
                "date": day,
                "side": side,
                "price": round(price, 4),
                "qty": qty,
                "account": account_id,
            })
    found.sort(key=lambda item: (item["date"], item["side"], item["account"]))
    return found


def forward_return(closes: list[float | None], index: int, horizon: int) -> float | None:
    if index < 0 or index + horizon >= len(closes):
        return None
    start = closes[index]
    future = closes[index + horizon]
    if start is None or future is None or start <= 0 or future <= 0:
        return None
    if not math.isfinite(start) or not math.isfinite(future):
        return None
    return (future / start) - 1.0


def summarize_entries(markers: list[dict]) -> dict:
    buys = [row for row in markers if row.get("side") == "buy"]
    sample = [row["fwd20"] for row in buys if isinstance(row.get("fwd20"), (int, float))]
    wins = sum(1 for value in sample if value > 0)
    return {
        "signal_count": len(buys),
        "win_rate": (wins / len(sample)) if sample else None,
        "avg_fwd20": (sum(sample) / len(sample)) if sample else None,
        "win_sample": len(sample),
    }


def strategy_signal_marks(
    engine,
    strategy_id: str,
    panel: pl.DataFrame,
    *,
    symbol: str,
    start: str,
    end: str,
    params: dict | None = None,
) -> tuple[list[dict], str | None]:
    """返回 [start, end] 内的买卖标记。panel 需包含 start 之前的预热行。"""
    defn = engine.get(strategy_id)
    resolved = engine.resolve_params(defn, params)
    if defn.execution_backend == "minute_filter":
        return [], "该策略按分钟触发，日K买卖点不计算"
    if needs_cross_section(defn) and not _has_other_symbols(panel, symbol):
        return [], _CROSS_SECTION_NOTE
    if defn.execution_backend == "composite":
        masks = _composite_masks(engine, defn, panel, symbol, resolved)
        if isinstance(masks, str):
            return [], masks
        dates, entry, exit_, entry_ids, exit_ids = masks
    else:
        dates, entry, exit_, entry_ids, exit_ids = _strategy_masks(
            engine, defn, panel, symbol, resolved,
        )
    markers = _markers_from_edges(
        defn,
        dates,
        entry,
        exit_,
        entry_ids,
        exit_ids,
        start=start,
        end=end,
    )
    return markers, None


def apply_forward_returns(markers: list[dict], dated_closes: list[tuple[str, float | None]]) -> list[dict]:
    dates = [day for day, _close in dated_closes]
    closes = [close for _day, close in dated_closes]
    index = {day: pos for pos, day in enumerate(dates)}
    priced = []
    for marker in markers:
        pos = index.get(marker["date"])
        close = closes[pos] if pos is not None else None
        if pos is None or close is None or close <= 0:
            continue
        fwd5 = forward_return(closes, pos, 5)
        fwd20 = forward_return(closes, pos, 20)
        priced.append({
            **marker,
            "price": round(close, 4),
            "fwd5": None if fwd5 is None else round(fwd5, 6),
            "fwd20": None if fwd20 is None else round(fwd20, 6),
        })
    return priced


def build_payload(
    *,
    repo,
    engine,
    data_dir: Path,
    symbol: str,
    strategy_id: str | None,
    start: date,
    end: date,
    intraday: date | None,
    dsa_loader=None,
) -> dict:
    start_s = start.isoformat()
    end_s = end.isoformat()
    intraday_s = intraday.isoformat() if intraday is not None else ""
    # 命中缓存时不再读日K。120 秒 TTL 覆盖新K线；成交文件 mtime 覆盖模拟盘。
    cache_key = (symbol, strategy_id or "", start_s, end_s, intraday_s, _fills_mtime(data_dir))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    strategies = catalog(engine) if engine is not None else []
    signal_panel, symbol_panel = _load_panels(
        repo, symbol, start, end, strategy_id, engine,
    )

    markers: list[dict] = []
    note = None
    if strategy_id:
        if symbol_panel is None and signal_panel is None:
            note = "没有这段日K，无法计算策略信号"
        else:
            try:
                raw, note = strategy_signal_marks(
                    engine,
                    strategy_id,
                    signal_panel if signal_panel is not None else symbol_panel,
                    symbol=symbol,
                    start=start_s,
                    end=end_s,
                )
                markers = apply_forward_returns(raw, _close_series(symbol_panel, symbol))
            except ValueError as exc:
                if str(exc).startswith("unknown strategy"):
                    raise
                logger.warning("trade marks %s %s failed: %s", symbol, strategy_id, exc)
                note = "策略信号计算失败"
                markers = []
            except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
                logger.warning("trade marks %s %s failed", symbol, strategy_id, exc_info=True)
                note = "策略信号计算失败"
                markers = []
    stats = summarize_entries(markers)
    if note:
        stats["note"] = note
    elif strategy_id and _strategy_is_fundamental(strategies, strategy_id):
        stats["note"] = "基本面预设使用全市场截面；买卖点只保留本股入选与退出的日期。"
    payload = {
        "symbol": symbol,
        "strategy": strategy_id or None,
        "strategies": strategies,
        "markers": markers,
        "stats": stats,
        "levels": (dsa_loader or load_dsa_levels)(symbol),
        "fills": paper_fills(data_dir, symbol, start_s, end_s),
        "t_trade": _t_trade_block(repo, symbol_panel, symbol, intraday),
    }
    _cache_put(cache_key, payload)
    return payload


def load_dsa_levels(symbol: str) -> list[dict]:
    try:
        from app.custom.dsa.proxy import InvalidUpstreamPathError, UpstreamError, enabled, forward
    except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
        logger.warning("dsa proxy import failed", exc_info=True)
        return []
    if not enabled():
        return []
    try:
        for code in _dsa_codes(symbol):
            status, body, _media, _extra = forward(
                "GET",
                "history",
                params=[("stock_code", code), ("limit", "5"), ("page", "1")],
                timeout=8,
            )
            if status >= 400:
                continue
            items = json.loads(body).get("items") or []
            if not items:
                continue
            items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            record_id = str(items[0].get("id") or items[0].get("query_id") or "")
            if not record_id:
                continue
            status, body, _media, _extra = forward("GET", f"history/{record_id}", timeout=8)
            if status >= 400:
                continue
            levels = parse_dsa_levels(json.loads(body))
            if levels:
                return levels
    except (UpstreamError, InvalidUpstreamPathError, ValueError, OSError, TypeError):
        logger.info("dsa levels unavailable for %s", symbol, exc_info=True)
        return []
    return []


def _strategy_is_fundamental(strategies: list[dict], strategy_id: str) -> bool:
    for row in strategies:
        if row["id"] == strategy_id:
            return bool(row.get("fundamental"))
    return strategy_id.startswith("fundamental_")


def _load_panels(repo, symbol, start: date, end: date, strategy_id: str | None, engine):
    """本股日K含预热和后 20 日；基本面截面另取全市场，且不触发缓存重建。"""
    warmup = WARMUP_FLOOR_DAYS
    if engine is not None and strategy_id:
        try:
            bars = engine.required_history_bars([strategy_id])
            warmup = max(warmup, int(bars) * 2 + 30)
        except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
            logger.info("warmup bars fallback for %s", strategy_id, exc_info=True)
    load_start = start - timedelta(days=warmup)
    load_end = end + timedelta(days=FORWARD_DAYS)
    symbol_panel = _read_daily(repo, symbol, load_start, load_end)
    signal_panel = symbol_panel
    if strategy_id and engine is not None:
        try:
            defn = engine.get(strategy_id)
        except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
            defn = None
        if defn is not None and needs_cross_section(defn):
            universe = _read_universe(repo, load_start, end)
            if universe is not None:
                signal_panel = universe
    return signal_panel, symbol_panel


def _read_daily(repo, symbol: str, start: date, end: date) -> pl.DataFrame | None:
    getter = getattr(repo, "get_daily", None)
    if getter is None:
        return None
    try:
        frame = getter(symbol, start, end)
    except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
        logger.warning("get_daily failed for %s", symbol, exc_info=True)
        return None
    if frame is None or frame.is_empty():
        return None
    return _with_name(repo, frame)


def _read_universe(repo, start: date, end: date) -> pl.DataFrame | None:
    cache = getattr(repo, "_enriched_history_cache", None)
    if cache is None or getattr(cache, "is_empty", lambda: True)():
        return None
    getter = getattr(repo, "get_enriched_range", None)
    if getter is None:
        return None
    try:
        frame = getter(start, end)
    except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
        logger.warning("enriched range failed", exc_info=True)
        return None
    if frame is None or frame.is_empty():
        return None
    return _with_name(repo, frame)


def _with_name(repo, frame: pl.DataFrame) -> pl.DataFrame:
    if "name" in frame.columns or not hasattr(repo, "get_instruments_asset"):
        return frame
    try:
        instruments = repo.get_instruments_asset("stock")
    except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
        return frame
    if instruments is None or instruments.is_empty() or "name" not in instruments.columns:
        return frame
    cols = [col for col in ("symbol", "name", "total_shares", "float_shares") if col in instruments.columns]
    return frame.join(instruments.select(cols), on="symbol", how="left")


def _fills_mtime(data_dir: Path) -> int:
    root = data_dir / "paper"
    if not root.exists():
        return 0
    latest = 0
    try:
        for path in root.glob("**/fills.jsonl"):
            latest = max(latest, path.stat().st_mtime_ns)
    except OSError:
        return 0
    return latest


def _t_trade_block(repo, panel, symbol: str, intraday: date | None) -> dict:
    block = {
        "date": intraday.isoformat() if intraday else None,
        "band": VWAP_BAND,
        "cooldown_min": T_COOLDOWN_MIN,
        "marks": [],
    }
    if intraday is None or not hasattr(repo, "get_minute"):
        return block
    asset_type = "stock"
    if hasattr(repo, "resolve_asset_type"):
        try:
            asset_type = repo.resolve_asset_type(symbol) or "stock"
        except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
            asset_type = "stock"
    try:
        minute = repo.get_minute(symbol, intraday, asset_type)
    except Exception:  # noqa: BLE001 — 图表叠加失败时留空，不把错误价位当成信号
        logger.warning("minute read failed %s %s", symbol, intraday, exc_info=True)
        return block
    if minute is None or minute.is_empty():
        return block
    bars = minute.sort("datetime").to_dicts() if "datetime" in minute.columns else minute.to_dicts()
    prev = _previous_raw_close(panel, symbol, intraday.isoformat()) if panel is not None else None
    block["marks"] = replay_t_marks(bars, symbol=symbol, prev_close=prev, cooldown_min=T_COOLDOWN_MIN)
    return block


def _previous_raw_close(panel: pl.DataFrame, symbol: str, day: str) -> float | None:
    if panel is None or panel.is_empty() or "date" not in panel.columns:
        return None
    one = panel.filter(pl.col("symbol") == symbol) if "symbol" in panel.columns else panel
    previous = None
    for row in one.sort("date").iter_rows(named=True):
        current = str(row.get("date"))[:10]
        if current >= day:
            break
        raw = _finite(row.get("raw_close"))
        close = _finite(row.get("close"))
        previous = raw if raw is not None else close
    return previous


def _close_series(panel: pl.DataFrame | None, symbol: str) -> list[tuple[str, float | None]]:
    if panel is None or panel.is_empty():
        return []
    one = panel.filter(pl.col("symbol") == symbol) if "symbol" in panel.columns else panel
    if one.is_empty() or "date" not in one.columns or "close" not in one.columns:
        return []
    series = []
    for row in one.sort("date").iter_rows(named=True):
        series.append((str(row["date"])[:10], _finite(row.get("close"))))
    return series


def _strategy_masks(engine, defn, panel: pl.DataFrame, symbol: str, params: dict):
    if defn.execution_backend == "matrix_native":
        return _matrix_masks(defn, _symbol_frame(panel, symbol), params)
    if defn.execution_backend == "composite":
        return _composite_masks(engine, defn, panel, symbol, params)
    return _python_masks(defn, panel, symbol, params)


def _matrix_masks(defn, frame: pl.DataFrame, params: dict):
    from app.backtest.matrix import (
        MatrixPipelineConfig,
        MatrixStrategyPipeline,
        build_market_data_matrix,
    )
    from app.strategy.scoring import effective_scoring, effective_scoring_directions

    if frame.is_empty():
        return [], [], [], (), ()
    fields = set(defn.matrix_strategy.required_fields())
    for column in ("amount", "total_shares", "float_shares", "turnover_rate"):
        if column in frame.columns:
            fields.add(column)
    market = build_market_data_matrix(frame, field_columns=fields)
    basic = dict(defn.basic_filter or {})
    signals = MatrixStrategyPipeline().run(
        defn.matrix_strategy,
        market,
        params,
        MatrixPipelineConfig(
            basic_filter=basic,
            scoring=effective_scoring(defn.meta.get("scoring"), None),
            scoring_directions=effective_scoring_directions(None),
            order_by=defn.meta.get("order_by"),
            descending=bool(defn.meta.get("descending", True)),
        ),
    )
    dates = [label[:10] for label in market.timestamp_labels]
    return (
        dates,
        [bool(value) for value in signals.entry[:, 0]],
        [bool(value) for value in signals.exit[:, 0]],
        tuple(signals.entry_signal_ids),
        tuple(signals.exit_signal_ids),
    )


def _python_masks(defn, panel: pl.DataFrame, symbol: str, params: dict):
    cross = needs_cross_section(defn)
    source = panel if cross else _symbol_frame(panel, symbol)
    frame = _symbol_frame(source if cross else panel, symbol)
    if frame.is_empty():
        return [], [], [], (), ()
    dates = _frame_dates(frame)
    if defn.filter_history_fn is not None:
        hits = defn.filter_history_fn(source, params)
        keys = _hit_keys(hits, symbol if cross else None)
        entry = [day in keys for day in dates]
    elif defn.filter_fn is not None:
        expr = defn.filter_fn(frame, params)
        flags = frame.select(expr.alias("_hit"))["_hit"].fill_null(False).to_list()
        entry = [bool(flag) for flag in flags]
    else:
        entry = [True] * len(dates)
    entry_ids = tuple(defn.entry_signals or [])
    exit_ids = tuple(defn.exit_signals or [])
    if entry_ids:
        signal = _column_mask(frame, entry_ids)
        if defn.filter_history_fn is None and defn.filter_fn is None:
            entry = signal
        else:
            entry = [bool(left and right) for left, right in zip(entry, signal, strict=True)]
    exit_mask = _column_mask(frame, exit_ids) if exit_ids else [False] * len(dates)
    return dates, entry, exit_mask, entry_ids, exit_ids


def _composite_masks(engine, defn, panel, symbol, params):
    if defn.composite is None:
        return "叠加策略没有子策略"
    mode = str(params.get("merge_mode") or "union")
    entries: list[list[bool]] = []
    exits: list[list[bool]] = []
    dates: list[str] | None = None
    entry_ids: list[str] = []
    exit_ids: list[str] = []
    for child in defn.composite.children:
        child_def = engine.get(child.strategy_id)
        if child_def.execution_backend in {"minute_filter", "composite"}:
            continue
        if needs_cross_section(child_def) and not _has_other_symbols(panel, symbol):
            return _CROSS_SECTION_NOTE
        child_params = engine.resolve_params(child_def)
        child_dates, entry, exit_, child_entry_ids, child_exit_ids = _strategy_masks(
            engine, child_def, panel, symbol, child_params,
        )
        if dates is None:
            dates = child_dates
        elif child_dates != dates:
            return "子策略日期无法对齐"
        entries.append(entry)
        exits.append(exit_)
        entry_ids.extend(child_entry_ids)
        exit_ids.extend(child_exit_ids)
    if not entries or dates is None:
        return "叠加策略没有可在日K上计算的子策略"
    if mode == "intersect":
        merged_entry = [all(col[index] for col in entries) for index in range(len(dates))]
    else:
        merged_entry = [any(col[index] for col in entries) for index in range(len(dates))]
    merged_exit = [any(col[index] for col in exits) for index in range(len(dates))]
    return dates, merged_entry, merged_exit, tuple(dict.fromkeys(entry_ids)), tuple(dict.fromkeys(exit_ids))


def _markers_from_edges(defn, dates, entry, exit_, entry_ids, exit_ids, *, start: str, end: str):
    buy_at = _rising(entry)
    sell_at = _rising(exit_) if any(exit_) else _falling(entry)
    breakout = is_breakout_entry(defn, entry_ids)
    markers = []
    for index in buy_at:
        day = dates[index]
        if day < start or day > end:
            continue
        markers.append(_marker(defn, day, "buy", "breakout" if breakout else "triangle", entry_ids))
    for index in sell_at:
        day = dates[index]
        if day < start or day > end:
            continue
        markers.append(_marker(defn, day, "sell", "triangle", exit_ids))
    markers.sort(key=lambda row: (row["date"], row["side"]))
    return markers


def _marker(defn, day: str, side: str, style: str, signal_ids) -> dict:
    return {
        "id": f"{side}:{day}:{style}",
        "date": day,
        "side": side,
        "style": style,
        "rule": _rule_text(defn, side, signal_ids),
        "price": None,
        "fwd5": None,
        "fwd20": None,
    }


def _rule_text(defn, side: str, signal_ids) -> str:
    from app.indicators.pipeline import ENRICHED_COLUMNS

    name = str(defn.meta.get("name") or defn.meta.get("id") or "策略")
    labels = []
    for signal in signal_ids or []:
        label = ENRICHED_COLUMNS.get(str(signal))
        if label:
            labels.append(label.split("（")[0].split("(")[0].strip())
    if labels:
        return f"{name} · {'、'.join(labels)}"
    description = str(defn.meta.get("description") or "").strip()
    verb = "入场" if side == "buy" else "出场"
    if description:
        return f"{name}{verb} · {description[:96]}"
    return f"{name}{verb}"


def _rising(flags: list[bool]) -> list[int]:
    found = []
    previous = None
    for index, flag in enumerate(flags):
        if previous is None:
            previous = bool(flag)
            continue
        if flag and not previous:
            found.append(index)
        previous = bool(flag)
    return found


def _falling(flags: list[bool]) -> list[int]:
    found = []
    previous = None
    for index, flag in enumerate(flags):
        if previous is None:
            previous = bool(flag)
            continue
        if previous and not flag:
            found.append(index)
        previous = bool(flag)
    return found


def _symbol_frame(panel: pl.DataFrame, symbol: str) -> pl.DataFrame:
    if panel is None or panel.is_empty():
        return pl.DataFrame()
    frame = panel.filter(pl.col("symbol") == symbol) if "symbol" in panel.columns else panel
    if frame.is_empty() or "date" not in frame.columns:
        return frame
    return frame.unique(subset=["date"], keep="last").sort("date")


def _frame_dates(frame: pl.DataFrame) -> list[str]:
    if frame.is_empty() or "date" not in frame.columns:
        return []
    return [str(value)[:10] for value in frame["date"].to_list()]


def _hit_keys(hits: pl.DataFrame | None, symbol: str | None) -> set[str]:
    if hits is None or hits.is_empty() or "date" not in hits.columns:
        return set()
    frame = hits
    if symbol is not None and "symbol" in frame.columns:
        frame = frame.filter(pl.col("symbol") == symbol)
    return {str(value)[:10] for value in frame["date"].to_list()}


def _column_mask(frame: pl.DataFrame, signals: list[str] | tuple[str, ...]) -> list[bool]:
    if frame.is_empty():
        return []
    columns = []
    for signal in signals:
        name = str(signal)
        if not (name.startswith("signal_") or name.startswith("csg_")):
            name = f"signal_{name}"
        if name in frame.columns:
            columns.append(name)
    if not columns:
        return [False] * frame.height
    combined = frame.select(pl.any_horizontal(pl.col(name).fill_null(False) for name in columns)).to_series()
    return [bool(value) for value in combined.to_list()]


def _has_other_symbols(panel: pl.DataFrame, symbol: str) -> bool:
    if panel is None or panel.is_empty() or "symbol" not in panel.columns:
        return False
    others = panel.filter(pl.col("symbol") != symbol)
    return not others.is_empty()


def _walk_levels(node: Any, found: dict[str, float], depth: int) -> None:
    if depth > 8 or len(found) >= 3:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            kind = _KEY_KIND.get(str(key))
            if kind and kind not in found:
                price = parse_price(value)
                if price is not None:
                    found[kind] = price
                    continue
            _walk_levels(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node[:40]:
            _walk_levels(item, found, depth + 1)


def _scan_level_text(node: Any, found: dict[str, float], depth: int) -> None:
    if depth > 8 or len(found) >= 3:
        return
    if isinstance(node, str):
        for kind, pattern in _TEXT_PATTERNS:
            if kind in found:
                continue
            match = pattern.search(node)
            if match:
                found[kind] = float(match.group(1))
        return
    if isinstance(node, dict):
        for value in node.values():
            _scan_level_text(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node[:40]:
            _scan_level_text(item, found, depth + 1)


def _dsa_codes(symbol: str) -> list[str]:
    raw = symbol.strip()
    bare = raw.split(".")[0]
    codes = []
    for code in (raw, bare, bare.upper()):
        if code and code not in codes:
            codes.append(code)
    return codes


def _clock_of(value) -> dt_time | None:
    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None)
    match = re.search(r"(\d{2}):(\d{2})", str(value or ""))
    if match is None:
        return None
    return dt_time(int(match.group(1)), int(match.group(2)))


def _hhmm(value) -> str:
    match = re.search(r"(\d{2}):(\d{2})", str(value or ""))
    if match is None:
        return ""
    return f"{match.group(1)}:{match.group(2)}"


def _in_t_span(clock: dt_time) -> bool:
    return (dt_time(9, 35) <= clock <= dt_time(11, 25)) or (dt_time(13, 5) <= clock <= dt_time(14, 55))


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _cache_get(key: tuple):
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        if now - hit[0] > CACHE_TTL_S:
            _CACHE.pop(key, None)
            return None
        return hit[1]


def _cache_put(key: tuple, value: dict) -> None:
    now = time.monotonic()
    with _CACHE_LOCK:
        if len(_CACHE) >= CACHE_MAX:
            oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
            _CACHE.pop(oldest, None)
        _CACHE[key] = (now, value)
