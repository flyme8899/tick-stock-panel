"""第二批东财数据的一次同步。请求都走注入的 get，默认是 em_get。"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from app.event_context import store
from app.event_context.parse import (
    as_data,
    code6,
    day_text,
    parse_changes,
    parse_lhb_seats,
    parse_lhb_summary,
    parse_margin_detail,
    parse_notices,
    parse_qa_payload,
)
from app.event_context.timeouts import REQUEST_TIMEOUT
from app.market_time import cn_now

logger = logging.getLogger(__name__)

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
QA_URL = "https://search-api-web.eastmoney.com/search/jsonp"
NOTICE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
CHANGES_URL = "https://push2ex.eastmoney.com/getAllStockChanges"
_EMPTY = 9201
_PAGE = 500
_QA_BATCH = 4
_SEEN_CAP = 4000
_CHANGE_TYPES = (
    (8193, "大笔买入"),
    (8194, "大笔卖出"),
    (4, "封涨停板"),
    (8, "封跌停板"),
    (8201, "火箭发射"),
)

Getter = Callable[..., object]


class EventStoppedError(Exception):
    """当前子任务被要求停下来。已经写入的行保留。"""


def sync_lhb(data_dir: Path, day: str, *, get: Getter | None = None, stop=None) -> dict:
    try:
        return _sync_lhb(data_dir, day, _http(get), stop)
    except EventStoppedError:
        return {"ok": False, "stopped": True}


def sync_margin(data_dir: Path, day: str, *, get: Getter | None = None, stop=None) -> dict:
    try:
        return _sync_margin(data_dir, day, _http(get), stop)
    except EventStoppedError:
        return {"ok": False, "stopped": True}


def sync_qa(
    data_dir: Path,
    *,
    get: Getter | None = None,
    stop=None,
    symbols: list[str] | None = None,
    batch: int = _QA_BATCH,
) -> dict:
    try:
        return _sync_qa(data_dir, _http(get), stop, symbols, batch)
    except EventStoppedError:
        return {"ok": False, "stopped": True}


def sync_notice(data_dir: Path, *, get: Getter | None = None, stop=None) -> dict:
    try:
        return _sync_notice(data_dir, _http(get), stop)
    except EventStoppedError:
        return {"ok": False, "stopped": True}


def sync_changes(data_dir: Path, day: str, *, get: Getter | None = None, stop=None) -> dict:
    try:
        return _sync_changes(data_dir, day, _http(get), stop)
    except EventStoppedError:
        return {"ok": False, "stopped": True}


def _sync_lhb(data_dir: Path, day: str, get: Getter, stop) -> dict:
    summary = parse_lhb_summary(_dc_pages(
        get, stop,
        report="RPT_DAILYBILLBOARD_DETAILSNEW",
        columns="SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,BILLBOARD_NET_AMT,CHANGE_RATE,EXPLANATION",
        filt=f"(TRADE_DATE='{day}')",
        referer="https://data.eastmoney.com/stock/lhb.html",
        what="龙虎榜",
        max_pages=8,
    ))
    summary = [row for row in summary if row["trade_date"] == day]
    if not summary:
        note = "龙虎榜明细为空，保留上次结果"
        _touch(data_dir, "lhb", ok=False, error=note, note=note)
        return {"ok": False, "kept": True, "note": note}
    note = ""
    try:
        buys = parse_lhb_seats(_dc_pages(
            get, stop,
            report="RPT_BILLBOARD_DAILYDETAILSBUY",
            columns="SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,OPERATEDEPT_NAME,BUY,SELL,NET,EXPLANATION",
            filt=f"(TRADE_DATE='{day}')",
            referer="https://data.eastmoney.com/stock/lhb.html",
            what="龙虎榜买入营业部",
        ), side="buy")
        sells = parse_lhb_seats(_dc_pages(
            get, stop,
            report="RPT_BILLBOARD_DAILYDETAILSSELL",
            columns="SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,OPERATEDEPT_NAME,BUY,SELL,NET,EXPLANATION",
            filt=f"(TRADE_DATE='{day}')",
            referer="https://data.eastmoney.com/stock/lhb.html",
            what="龙虎榜卖出营业部",
        ), side="sell")
        seats = [row for row in (*buys, *sells) if row["trade_date"] == day]
    except EventStoppedError:
        raise
    except Exception as exc:  # noqa: BLE001
        seats = []
        note = f"营业部明细失败，保留净买额: {exc}"[:200]
    rows = _merge_lhb(summary, seats)
    if not seats and not note:
        note = "营业部明细为空，仅保留净买额"
    store.write_rows(data_dir, "lhb", day, rows)
    _touch(data_dir, "lhb", ok=True, note=note, items=len(rows))
    return {"ok": True, "rows": len(rows), "note": note}


def _merge_lhb(summary: list[dict], seats: list[dict]) -> list[dict]:
    by_code = {row["code"]: row for row in summary}
    if not seats:
        return summary
    covered: set[str] = set()
    rows = []
    for seat in seats:
        base = by_code.get(seat["code"], {})
        seat["net_buy"] = base.get("net_buy")
        seat["change_pct_points"] = base.get("change_pct_points")
        seat["reason"] = seat.get("reason") or base.get("reason") or ""
        seat["name"] = seat.get("name") or base.get("name") or ""
        rows.append(seat)
        covered.add(seat["code"])
    for code, base in by_code.items():
        if code not in covered:
            rows.append(base)
    return rows


def _sync_margin(data_dir: Path, day: str, get: Getter, stop) -> dict:
    raw = _dc_pages(
        get, stop,
        report="RPTA_WEB_RZRQ_GGMX",
        columns="SCODE,SECNAME,RZYE,RQYE,RZMRE,TRADE_MARKET,DATE,SECUCODE,TRADE_DATE",
        filt=f"(TRADE_DATE='{day}')",
        referer="https://data.eastmoney.com/rzrq/detail/all.html",
        what="两融明细",
        max_pages=20,
    )
    rows = [row for row in parse_margin_detail(raw) if row["trade_date"] == day]
    if not rows:
        note = "两融明细为空，保留上次结果"
        _touch(data_dir, "margin", ok=False, error=note, note=note)
        return {"ok": False, "kept": True, "note": note}
    markets = {row["market"] for row in rows}
    note = ""
    if "szse" not in markets:
        prev = _previous_day(data_dir, "margin", day)
        note = f"深市明细不可用，沿用 {prev}" if prev else "深市明细不可用"
    store.write_rows(data_dir, "margin", day, rows)
    _touch(data_dir, "margin", ok=True, note=note, items=len(rows))
    return {"ok": True, "rows": len(rows), "note": note, "markets": sorted(markets)}


def _sync_qa(data_dir: Path, get: Getter, stop, symbols: list[str] | None, batch: int) -> dict:
    if symbols is None:
        from app.research_reports.jobs import target_symbols

        symbols = target_symbols(Path(data_dir), limit=80)
    symbols = [item for item in symbols if code6(item)]
    state = store.load_state(data_dir)
    block = dict(state.get("qa") or {})
    seen = {str(item) for item in (block.get("seen") or [])}
    cursor = int(block.get("cursor") or 0)
    if not symbols:
        note = "没有自选或热门个股"
        _touch(data_dir, "qa", ok=True, note=note, items=0)
        return {"ok": True, "rows": 0, "complete": True, "note": note}
    width = max(1, batch)
    picked = [symbols[(cursor + offset) % len(symbols)] for offset in range(min(width, len(symbols)))]
    fresh: list[dict] = []
    for symbol in picked:
        _raise_if_stopped(stop)
        code = code6(symbol)
        payload = as_data(get(
            QA_URL,
            params=_qa_params(code),
            headers={"Referer": "https://so.eastmoney.com/"},
            timeout=REQUEST_TIMEOUT,
        ))
        for item in parse_qa_payload(payload, code=code):
            if item["question_id"] in seen:
                break
            fresh.append(item)
            seen.add(item["question_id"])
    written = _write_grouped(data_dir, "qa", fresh, "published_at")
    new_cursor = (cursor + len(picked)) % len(symbols)
    complete = cursor + len(picked) >= len(symbols)
    _touch(
        data_dir, "qa", ok=True, items=written,
        seen=_cap(sorted(seen), _SEEN_CAP), cursor=new_cursor,
    )
    return {"ok": True, "rows": written, "complete": complete, "symbols": [code6(item) for item in picked]}


def _sync_notice(data_dir: Path, get: Getter, stop) -> dict:
    state = store.load_state(data_dir)
    block = dict(state.get("notice") or {})
    seen = {str(item) for item in (block.get("seen") or [])}
    resume = max(1, int(block.get("page") or 1))
    fresh: list[dict] = []
    page = 1
    next_page = 1
    while page <= 30:
        _raise_if_stopped(stop)
        payload = as_data(get(
            NOTICE_URL,
            params={
                "sr": "-1",
                "page_size": "100",
                "page_index": str(page),
                "ann_type": "A",
                "client_source": "web",
                "f_node": "0",
                "s_node": "0",
            },
            headers={"Referer": "https://data.eastmoney.com/notices/"},
            timeout=REQUEST_TIMEOUT,
        ))
        rows = parse_notices(payload)
        if not rows:
            next_page = 1
            break
        novel = [row for row in rows if row["art_code"] not in seen]
        fresh.extend(novel)
        seen.update(row["art_code"] for row in novel)
        if not novel and page >= resume:
            next_page = 1
            break
        page += 1
        next_page = page
        if page > 30:
            break
    written = _write_grouped(data_dir, "notice", fresh, "notice_date")
    ingested = _ingest_notices(fresh) if fresh else 0
    _touch(
        data_dir, "notice", ok=True, items=written,
        seen=_cap(list(seen), _SEEN_CAP), page=next_page,
    )
    _mark_notice(ok=True, items=ingested)
    return {"ok": True, "rows": written, "ingested": ingested}


def _sync_changes(data_dir: Path, day: str, get: Getter, stop) -> dict:
    rows: list[dict] = []
    for type_code, kind in _CHANGE_TYPES:
        _raise_if_stopped(stop)
        payload = as_data(get(
            CHANGES_URL,
            params={
                "type": str(type_code),
                "pageindex": "0",
                "pagesize": "5000",
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "dpt": "wzchanges",
            },
            headers={"Referer": "https://quote.eastmoney.com/changes/"},
            timeout=REQUEST_TIMEOUT,
        ))
        rows.extend(parse_changes(payload, kind=kind, trade_date=day))
    if not rows:
        note = "盘口异动为空，保留上次结果"
        _touch(data_dir, "changes", ok=False, error=note, note=note)
        return {"ok": False, "kept": True, "note": note}
    count = store.write_rows(data_dir, "changes", day, rows)
    _touch(data_dir, "changes", ok=True, items=count)
    return {"ok": True, "rows": count}


def _dc_pages(
    get: Getter,
    stop,
    *,
    report: str,
    columns: str,
    filt: str,
    referer: str,
    what: str,
    max_pages: int = 12,
) -> list[dict]:
    found: list[dict] = []
    page = 1
    while page <= max_pages:
        _raise_if_stopped(stop)
        payload = as_data(get(
            DC_URL,
            params={
                "reportName": report,
                "columns": columns,
                "pageNumber": str(page),
                "pageSize": str(_PAGE),
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
                "filter": filt,
            },
            headers={"Referer": referer},
            timeout=REQUEST_TIMEOUT,
        ))
        if not isinstance(payload, dict):
            raise RuntimeError(f"{what}返回异常")
        if not payload.get("success"):
            if int(payload.get("code") or 0) == _EMPTY:
                return found
            raise RuntimeError(str(payload.get("message") or f"{what}失败")[:300])
        result = payload.get("result") or {}
        rows = result.get("data") or []
        if not isinstance(rows, list) or not rows:
            break
        found.extend(item for item in rows if isinstance(item, dict))
        pages = int(result.get("pages") or 0)
        if pages and page >= pages:
            break
        page += 1
    return found


def _qa_params(code: str) -> dict:
    body = {
        "uid": "",
        "keyword": code,
        "type": ["wenDongMiWeb"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {"wenDongMiWeb": {"pageSize": 20, "pageIndex": 1, "preTag": "", "postTag": ""}},
    }
    return {"cb": "jQuery", "param": json.dumps(body, ensure_ascii=False)}


def _write_grouped(data_dir: Path, kind: str, rows: list[dict], date_key: str) -> int:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        day = day_text(row.get(date_key))
        if not day:
            continue
        grouped.setdefault(day, []).append(row)
    total = 0
    for day, chunk in grouped.items():
        total += store.write_rows(data_dir, kind, day, chunk)
    return total


def _previous_day(data_dir: Path, kind: str, day: str) -> str:
    dates = [item for item in store.list_dates(data_dir, kind) if item < day]
    return dates[-1] if dates else ""


def _touch(data_dir: Path, kind: str, *, ok: bool, error: str = "", note: str = "", items: int = 0, **extra) -> None:
    state = store.load_state(data_dir)
    block = dict(state.get(kind) or {})
    block["items"] = items
    if ok:
        block["last_ok_at"] = cn_now().isoformat(timespec="seconds")
        block["last_error"] = note[:300]
    else:
        block["last_error"] = (error or note)[:300]
    if note:
        block["note"] = note[:300]
    elif ok:
        block.pop("note", None)
    block.update(extra)
    state[kind] = block
    store.save_state(data_dir, state)


def _ingest_notices(rows: list[dict]) -> int:
    if not rows:
        return 0
    from app.news.collectors import Item
    from app.news.extract import StructuredStock
    from app.news.service import ingest_items

    items = []
    for row in rows:
        published = _published(row.get("notice_date"))
        code = str(row.get("code") or "")
        items.append(Item(
            source="notice",
            source_id=str(row["art_code"]),
            published_at=published,
            title=str(row.get("title") or ""),
            text=str(row.get("title") or ""),
            url=str(row.get("url") or ""),
            stocks=[StructuredStock(name=str(row.get("name") or ""), code=code)] if code else [],
        ))
    result = ingest_items(items)
    return int(result.get("inserted") or 0)


def _mark_notice(*, ok: bool, items: int) -> None:
    try:
        from app.news.service import get_store

        get_store().mark_health(
            "notice", ok=ok, error="" if ok else "公告入库失败", auth_state="ok" if ok else "unknown",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("公告健康状态没写上: %s", exc)
    if items:
        logger.info("公告新入库 %s 条", items)


def _published(day: object) -> datetime:
    text = day_text(day) or cn_now().date().isoformat()
    year, month, date = (int(part) for part in text.split("-"))
    return cn_now().replace(year=year, month=month, day=date, hour=15, minute=0, second=0, microsecond=0)


def _http(get: Getter | None) -> Getter:
    if get is not None:
        return get

    def default(url: str, *, params=None, headers=None, timeout=None):
        from app.eastmoney.client import em_get

        return em_get(url, params=params, headers=headers, timeout=timeout or REQUEST_TIMEOUT)

    return default


def _raise_if_stopped(stop) -> None:
    if stop is not None and stop():
        raise EventStoppedError


def _cap(items: list, limit: int) -> list:
    if len(items) <= limit:
        return items
    return items[-limit:]
