"""给 API 读的查询。只读本地 parquet，不触发出网。"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from app.fund_flow import factors, normalize, store
from app.fund_flow.scheduler import current_flags, get_worker
from app.fund_flow.units import percent_points_to_decimal

_FLOW_COLUMNS = ("net_flow", "flow_net", "net_inflow", "净流入")
_CODE_COLUMNS = ("code", "symbol", "基金代码")
_DATE_COLUMNS = ("trade_date", "date", "日期")


def _dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    from app.config import settings
    return Path(settings.data_dir)


def health(data_dir: Path | None = None) -> dict:
    root = _dir(data_dir)
    state = store.load_state(root)
    worker = get_worker()
    kinds = {}
    for kind in (
        "stock", "industry", "concept", "margin", "etf_shares",
        "southbound", "northbound_turnover", "lhb",
    ):
        latest = store.latest_date(root, kind)
        rows = 0
        if latest:
            rows = store.read_partition(root, kind, latest).height
        saved = dict((state.get("kinds") or {}).get(kind) or {})
        kinds[kind] = {
            "latest_date": latest,
            "rows": rows,
            "last_error": saved.get("last_error"),
            "last_success_date": saved.get("last_success_date"),
            "last_attempt_at": saved.get("last_attempt_at"),
        }
    scheduler_state = (state.get("kinds") or {}).get("_scheduler") or {}
    return {
        "enabled": current_flags()["enabled"],
        "flags": current_flags(),
        "deferred_reason": worker.deferred_reason or scheduler_state.get("deferred"),
        "kinds": kinds,
        "sources": state.get("sources") or {},
    }


def stock_series(symbol: str, *, limit: int = 120, data_dir: Path | None = None) -> dict:
    root = _dir(data_dir)
    code = normalize.code6(symbol)
    try:
        frame = store.read_range(root, "stock")
    except (KeyError, pl.exceptions.ColumnNotFoundError):
        frame = store.empty_frame("stock")
    if code and not frame.is_empty() and "code" in frame.columns:
        frame = frame.filter(pl.col("code") == code).sort("trade_date")
    else:
        frame = store.empty_frame("stock")
    rows = frame.tail(limit).to_dicts()
    main = factors.main_net_frame(root)
    five = None
    if code and not main.is_empty():
        matched = main.filter(pl.col("code") == code).sort("trade_date")
        if not matched.is_empty():
            five = matched.tail(1).to_dicts()[0].get("ff_main_net_5d")
    return {
        "symbol": symbol,
        "code": code,
        "main_net_5d": five,
        "items": rows,
    }


def sectors(kind: str, *, trade_date: str | None = None, data_dir: Path | None = None) -> dict:
    if kind not in {"industry", "concept"}:
        raise ValueError("kind 只能是 industry 或 concept")
    root = _dir(data_dir)
    day = trade_date or store.latest_date(root, kind)
    if day is None:
        return {"kind": kind, "trade_date": None, "snapshot": None, "items": []}
    ranked = factors.latest_sector_ranks(root, kind, day)
    snapshot = "close"
    part = _partition(root, kind, day)
    if (
        not part.is_empty()
        and "snapshot" in part.columns
        and part.filter(pl.col("snapshot") == "close").is_empty()
    ):
        snapshot = "intraday"
    return {
        "kind": kind,
        "trade_date": day,
        "snapshot": snapshot,
        "items": ranked.sort("rank").to_dicts(),
    }


def margin(*, trade_date: str | None = None, detail_limit: int = 100, data_dir: Path | None = None) -> dict:
    root = _dir(data_dir)
    day = trade_date or store.latest_date(root, "margin")
    if day is None:
        return {"trade_date": None, "summary": [], "details": [], "detail_count": 0}
    frame = _partition(root, "margin", day)
    if frame.is_empty() or "row_kind" not in frame.columns:
        return {"trade_date": day, "summary": [], "details": [], "detail_count": 0}
    summary = frame.filter(pl.col("row_kind") == "summary").to_dicts()
    details = frame.filter(pl.col("row_kind") == "detail")
    return {
        "trade_date": day,
        "summary": summary,
        "details": details.head(detail_limit).to_dicts(),
        "detail_count": details.height,
    }


def _partition(root: Path, kind: str, trade_date: str) -> pl.DataFrame:
    """读一个分区。缺列或坏文件变成空表，不把 KeyError 变成 500。"""
    try:
        return store.read_partition(root, kind, trade_date)
    except (KeyError, pl.exceptions.ColumnNotFoundError):
        return store.empty_frame(kind)


def etf_shares(*, trade_date: str | None = None, data_dir: Path | None = None) -> dict:
    root = _dir(data_dir)
    day = trade_date or store.latest_date(root, "etf_shares")
    if day is None:
        return {"trade_date": None, "etf_flow_linked": False, "items": []}
    frame = _partition(root, "etf_shares", day)
    linked = _etf_flow_map(root, day)
    items = []
    for row in frame.to_dicts():
        code = row.get("code")
        if not code:
            continue
        flow = linked.get(code)
        items.append({**row, "flow_net": flow, "etf_flow_linked": flow is not None})
    return {
        "trade_date": day,
        "etf_flow_linked": any(item["etf_flow_linked"] for item in items),
        "items": items,
    }


def _etf_flow_map(data_dir: Path, trade_date: str) -> dict[str, float]:
    """ETF领航者落在 data/etf_flow 时，按代码和交易日把净流入接上。对不上就空。"""
    base = Path(data_dir) / "etf_flow"
    if not base.exists():
        return {}
    frames = []
    for path in base.glob("**/*.parquet"):
        try:
            frames.append(pl.read_parquet(path))
        except Exception:  # noqa: BLE001
            continue
    if not frames:
        return {}
    frame = pl.concat(frames, how="diagonal_relaxed")
    code_col = next((name for name in _CODE_COLUMNS if name in frame.columns), None)
    flow_col = next((name for name in _FLOW_COLUMNS if name in frame.columns), None)
    if code_col is None or flow_col is None:
        return {}
    date_col = next((name for name in _DATE_COLUMNS if name in frame.columns), None)
    if date_col is not None:
        frame = frame.filter(pl.col(date_col).cast(pl.Utf8).str.slice(0, 10) == trade_date)
    found: dict[str, float] = {}
    for row in frame.select(code_col, flow_col).iter_rows():
        code = normalize.code6(str(row[0]))
        try:
            value = float(row[1])
        except (TypeError, ValueError):
            continue
        if code:
            found[code] = value
    return found


def lhb_backup_payload(data_dir: Path, trade_date: str) -> dict | None:
    """收成龙虎榜页面已有的形状。涨跌幅从百分点换成小数，和 fuyao 的 change 一致。"""
    frame = store.read_partition(data_dir, "lhb", trade_date)
    if frame.is_empty():
        return None
    items = []
    for row in frame.to_dicts():
        items.append({
            "thscode": row["code"],
            "ticker": row["code"],
            "name": row["name"],
            "change": percent_points_to_decimal(row.get("change_pct_points")),
            "net_value": row.get("net_buy"),
            "range_days": 1,
        })
    board = {
        "trade_date": trade_date,
        "stock_count": len(items),
        "count": len(items),
        "stock_items": items,
        "hot_money_items": [],
    }
    empty = {**board, "stock_count": 0, "count": 0, "stock_items": []}
    return {
        "state": "ok",
        "source": "akshare_lhb",
        "requested_date": trade_date,
        "trade_date": trade_date,
        "all": board,
        "org": empty,
        "hot_money": empty,
    }


def dsa_context(data_dir: Path | None = None) -> dict:
    """给 DSA 的一段文字。没有落盘数据时 items 为空，不编造数字。"""
    root = _dir(data_dir)
    lines = []
    published = None
    stock_day = store.latest_date(root, "stock")
    if stock_day:
        frame = _partition(root, "stock", stock_day)
        ranked = _stock_sides(root, frame, "main_net")
        if ranked is not None:
            inflow, _outflow = ranked
            top = inflow[:5]
            if top:
                published = stock_day
                lines.append("主力净流入居前: " + "；".join(
                    f"{row.get('name') or row.get('code')} {_yi(row.get('main_net'))}" for row in top
                ))
    for kind, label in (("industry", "行业"), ("concept", "概念")):
        day = store.latest_date(root, kind)
        if not day:
            continue
        ranked = factors.latest_sector_ranks(root, kind, day).sort("rank").head(5)
        rows = ranked.to_dicts()
        if not rows:
            continue
        published = published or day
        lines.append(f"{label}净流入排名: " + "；".join(
            f"{row['name']} 第{row['rank']} {_yi(row['net_inflow'])}" for row in rows
        ))
    south_day = store.latest_date(root, "southbound")
    if south_day:
        south = _partition(root, "southbound", south_day)
        if not south.is_empty() and "net_flow" in south.columns and south["net_flow"][0] is not None:
            published = published or south_day
            lines.append(f"南向净流入 {_yi(south['net_flow'][0])}")
    if not lines:
        return {"items": []}
    return {
        "items": [{
            "title": f"资金进出 {published}",
            "summary": "。".join(lines),
            "published_at": f"{published}T16:30:00+08:00",
        }],
    }


_PARTIAL_NOTE = "个股样本不完整，不作为全市场排名。"
_PARTIAL_5D_NOTE = "5日样本不完整，不作为排名。"
_BOARD_SIDE_LIMIT = 10
_MARGIN_COVERAGE = {
    ("sse", "szse"): ("both", "沪深合计"),
    ("sse",): ("sse", "仅沪市"),
    ("szse",): ("szse", "仅深市"),
}


def _listed_count(root: Path) -> int | None:
    path = Path(root) / "instruments" / "instruments.parquet"
    if not path.exists():
        return None
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    if frame.is_empty():
        return None
    return frame.height


def _instrument_names(root: Path) -> dict[str, str]:
    path = Path(root) / "instruments" / "instruments.parquet"
    if not path.exists():
        return {}
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return {}
    if frame.is_empty() or "name" not in frame.columns:
        return {}
    found: dict[str, str] = {}
    symbols = frame["symbol"].to_list() if "symbol" in frame.columns else [""] * frame.height
    codes = frame["code"].to_list() if "code" in frame.columns else [""] * frame.height
    for symbol, code, name in zip(symbols, codes, frame["name"].to_list(), strict=False):
        text = str(name or "").strip()
        if not text or text.lower() in {"none", "nan"}:
            continue
        for key in (normalize.code6(str(code or "")), normalize.code6(str(symbol or "")), str(symbol or "")):
            if key:
                found[key] = text
    return found


def _with_names(rows: list[dict], names: dict[str, str]) -> list[dict]:
    named = []
    for row in rows:
        code = normalize.code6(str(row.get("code") or row.get("symbol") or ""))
        name = str(row.get("name") or "").strip()
        if not name or name.lower() in {"none", "nan"}:
            name = names.get(code) or names.get(str(row.get("symbol") or "")) or ""
        named.append({**row, "code": code or row.get("code"), "name": name or None})
    return named


def _stock_sides(
    root: Path,
    frame: pl.DataFrame,
    column: str,
) -> tuple[list[dict], list[dict]] | None:
    if frame.is_empty() or column not in frame.columns or "code" not in frame.columns:
        return None
    usable = frame.filter(pl.col(column).is_not_null()).unique(subset=["code"], keep="last")
    if store.is_partial_ranking(usable.height, _listed_count(root)):
        return None
    keep = [name for name in ("symbol", "code", "name", column, "large_net", "super_net") if name in usable.columns]
    names = _instrument_names(root)
    inflow = _with_names(
        usable.filter(pl.col(column) > 0).sort(column, descending=True).head(_BOARD_SIDE_LIMIT).select(keep).to_dicts(),
        names,
    )
    outflow = _with_names(
        usable.filter(pl.col(column) < 0).sort(column, descending=False).head(_BOARD_SIDE_LIMIT).select(keep).to_dicts(),
        names,
    )
    return inflow, outflow


def _empty_stock(day: str | None, *, partial: bool = False, note: str | None = None) -> dict:
    return {
        "trade_date": day,
        "partial": partial,
        "note": note,
        "items": [],
        "inflow": [],
        "outflow": [],
    }


_BROAD_HS300 = frozenset({"510300", "510310", "510330", "159919"})
_BROAD_KC50 = frozenset({"588000", "588080", "588050"})
_BOARD_SECTOR_LIMIT = 12
_BOARD_HISTORY = 40
_BOARD_ETF_OTHERS = 12


def _broad_label(code: str | None, name: str | None) -> str | None:
    """沪深300 / 科创50 宽基，页面上当作国家队代理。对不上就不是宽基。"""
    text = name or ""
    code6 = normalize.code6(code or "") or ""
    if "科创50" in text or code6 in _BROAD_KC50:
        return "科创50"
    if "沪深300" in text or code6 in _BROAD_HS300:
        return "沪深300"
    return None


def board(data_dir: Path | None = None) -> dict:
    """资金页一次读完。只读已落盘分区，没有的块留空，不把缺失写成 0。"""
    root = _dir(data_dir)
    return {
        "industry": _sector_board(root, "industry"),
        "concept": _sector_board(root, "concept"),
        "stocks_today": _stocks_today(root),
        "stocks_5d": _stocks_5d(root),
        "margin": _margin_trend(root),
        "southbound": _southbound_trend(root),
        "etf_shares": _etf_changes(root),
    }


def _sector_board(root: Path, kind: str) -> dict:
    payload = sectors(kind, data_dir=root)
    payload["items"] = payload["items"][:_BOARD_SECTOR_LIMIT]
    return payload


def _stocks_today(root: Path) -> dict:
    day = store.latest_date(root, "stock")
    if day is None:
        return _empty_stock(None)
    frame = _partition(root, "stock", day)
    ranked = _stock_sides(root, frame, "main_net")
    if ranked is None:
        partial = store.code_count(frame) > 0
        return _empty_stock(day, partial=partial, note=_PARTIAL_NOTE if partial else None)
    inflow, outflow = ranked
    return {
        "trade_date": day,
        "partial": False,
        "note": None,
        "items": inflow,
        "inflow": inflow,
        "outflow": outflow,
    }


def _five_day_frame(root: Path, day: str) -> pl.DataFrame:
    """优先用已落盘的 5 日合计，没有时用排名接口写入的 ``main_net_5d``。"""
    part = _partition(root, "stock", day)
    if part.is_empty() or "code" not in part.columns:
        return part
    factor = factors.main_net_frame(root)
    if not factor.is_empty():
        latest = factor.filter(pl.col("trade_date") == day).select("code", "ff_main_net_5d")
        if not latest.is_empty():
            part = part.join(latest, on="code", how="left")
    if "ff_main_net_5d" not in part.columns:
        part = part.with_columns(pl.lit(None).cast(pl.Float64).alias("ff_main_net_5d"))
    if "main_net_5d" not in part.columns:
        part = part.with_columns(pl.lit(None).cast(pl.Float64).alias("main_net_5d"))
    return part.with_columns(
        pl.coalesce(pl.col("ff_main_net_5d"), pl.col("main_net_5d")).alias("ff_main_net_5d"),
    )


def _stocks_5d(root: Path) -> dict:
    """全市场 5 日主力。样本不够时不把少数几只排成榜。"""
    day = store.latest_date(root, "stock")
    if day is None:
        return _empty_stock(None)
    frame = _five_day_frame(root, day)
    ranked = _stock_sides(root, frame, "ff_main_net_5d")
    if ranked is None:
        usable = 0
        if not frame.is_empty() and "ff_main_net_5d" in frame.columns:
            usable = frame.filter(pl.col("ff_main_net_5d").is_not_null()).height
        partial = usable > 0
        return _empty_stock(day, partial=partial, note=_PARTIAL_5D_NOTE if partial else None)
    inflow, outflow = ranked
    return {
        "trade_date": day,
        "partial": False,
        "note": None,
        "items": inflow,
        "inflow": inflow,
        "outflow": outflow,
    }


def _margin_day(day: str, rows: list[dict]) -> dict:
    by_market = {}
    for row in rows:
        market = str(row.get("market") or "")
        if market in {"sse", "szse"} and row.get("margin_balance") is not None:
            by_market[market] = row
    present = tuple(market for market in ("sse", "szse") if market in by_market)
    coverage, label = _MARGIN_COVERAGE.get(present, ("none", ""))
    total = None
    if present:
        total = sum(by_market[market]["margin_balance"] for market in present)
    shorts = [
        by_market[market]["short_balance"]
        for market in present
        if by_market[market].get("short_balance") is not None
    ]
    return {
        "trade_date": day,
        "margin_balance": total,
        "short_balance": sum(shorts) if shorts else None,
        "coverage": coverage,
        "label": label,
        "sse_margin_balance": (by_market.get("sse") or {}).get("margin_balance"),
        "szse_margin_balance": (by_market.get("szse") or {}).get("margin_balance"),
    }


def _margin_trend(root: Path) -> dict:
    dates = store.list_dates(root, "margin")[-_BOARD_HISTORY:]
    items = []
    days = []
    for day in dates:
        frame = _partition(root, "margin", day)
        if frame.is_empty() or "row_kind" not in frame.columns:
            continue
        summary = frame.filter(pl.col("row_kind") == "summary")
        rows = summary.select("trade_date", "market", "margin_balance", "short_balance").to_dicts()
        if not rows:
            continue
        items.extend(rows)
        days.append(_margin_day(day, rows))
    return {"items": items, "days": days}


def _southbound_trend(root: Path) -> dict:
    try:
        frame = store.read_range(root, "southbound")
    except (KeyError, pl.exceptions.ColumnNotFoundError):
        return {"items": []}
    if frame.is_empty() or "trade_date" not in frame.columns or "net_flow" not in frame.columns:
        return {"items": []}
    ranked = frame.sort("trade_date").tail(_BOARD_HISTORY)
    return {"items": ranked.select("trade_date", "net_flow").to_dicts()}


def _etf_changes(root: Path) -> dict:
    dates = store.list_dates(root, "etf_shares")
    if not dates:
        return {"trade_date": None, "prev_trade_date": None, "items": []}
    day = dates[-1]
    prev_day = dates[-2] if len(dates) > 1 else None
    current = _partition(root, "etf_shares", day)
    previous: dict[str, float] = {}
    if prev_day is not None:
        prev = _partition(root, "etf_shares", prev_day)
        for row in prev.to_dicts():
            code = row.get("code")
            if code and row.get("shares") is not None:
                previous[code] = row["shares"]
    linked = _etf_flow_map(root, day)
    broad = []
    others = []
    for row in current.to_dicts():
        code = row.get("code")
        if not code:
            continue
        label = _broad_label(code, row.get("name"))
        prev_shares = previous.get(code)
        shares = row.get("shares")
        change = None if prev_shares is None or shares is None else shares - prev_shares
        item = {
            "code": code,
            "name": row.get("name"),
            "shares": shares,
            "prev_shares": prev_shares,
            "share_change": change,
            "broad": label,
            "flow_net": linked.get(code),
        }
        if label:
            broad.append(item)
        elif change is not None:
            others.append(item)
    broad.sort(key=lambda item: (item["broad"] or "", item["name"] or ""))
    others.sort(key=lambda item: abs(item["share_change"] or 0), reverse=True)
    return {
        "trade_date": day,
        "prev_trade_date": prev_day,
        "items": broad + others[:_BOARD_ETF_OTHERS],
    }


def _yi(value: float | None) -> str:
    if value is None:
        return "无"
    return f"{value / 1e8:.2f}亿元"
