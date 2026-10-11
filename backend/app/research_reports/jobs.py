"""个股研报按自选和热门事件股票增量拉取，行业研报拉最近一段列表。

每只股票只翻到已经见过的 ``infoCode`` 就停。PDF 只拼地址，请求里不会出现 pdf.dfcfw.com。
新近几日的研报写入资讯库，来源类型是 ``research``，供热门事件聚类。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.eastmoney.client import UA, em_get
from app.market_time import CN_TZ, cn_today
from app.research_reports import store
from app.research_reports.parse import (
    REPORT_API,
    check_industry_code,
    page_count,
    parse_industry_reports,
    parse_stock_reports,
    publish_at,
    research_code,
)

logger = logging.getLogger(__name__)

Get = Callable[..., Any]
_NEWS_DAYS = 3
_STOCK_PAGES = 2
_INDUSTRY_PAGES = 2
_SEEN_CAP = 200


def sync_symbol(
    data_dir: Path,
    symbol: str,
    *,
    get: Get | None = None,
    today: date | None = None,
    ingest: bool = True,
) -> dict:
    code = research_code(symbol)
    state = store.load_state(data_dir)
    seen = _seen_map(state, "seen_stock").get(code, [])
    known = set(seen)
    fetch = get or _em
    fresh: list[dict] = []
    pages = 0
    for page in range(1, _STOCK_PAGES + 1):
        payload = _json(fetch(
            REPORT_API,
            params=_stock_params(code, page),
            headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
            timeout=30,
        ))
        rows = parse_stock_reports(payload, code)
        pages += 1
        if rows is None:
            raise RuntimeError("研报响应无法解析")
        if not rows:
            break
        novel = _novel(rows, known)
        fresh.extend(novel)
        if len(novel) < len(rows) or page >= page_count(payload):
            break
    _write_grouped(data_dir, "stock", fresh)
    seen_now = list(known) + [row["info_code"] for row in fresh]
    _seen_map(state, "seen_stock")[code] = seen_now[-_SEEN_CAP:]
    store.save_state(data_dir, state)
    inserted = _ingest(fresh, today=today or cn_today()) if ingest else 0
    return {"symbol": symbol, "fetched_pages": pages, "new": len(fresh), "ingested": inserted}


def sync_industry(
    data_dir: Path,
    *,
    industry_code: str = "*",
    begin: str = "",
    get: Get | None = None,
    today: date | None = None,
    ingest: bool = True,
) -> dict:
    code = check_industry_code(industry_code)
    day = today or cn_today()
    if not begin:
        begin = (day - timedelta(days=14)).isoformat()
    state = store.load_state(data_dir)
    known = set(_seen_list(state, "seen_industry"))
    fetch = get or _em
    fresh: list[dict] = []
    pages = 0
    for page in range(1, _INDUSTRY_PAGES + 1):
        payload = _json(fetch(
            REPORT_API,
            params=_industry_params(code, page, begin),
            headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"},
            timeout=30,
        ))
        rows = parse_industry_reports(payload)
        pages += 1
        if rows is None:
            raise RuntimeError("行业研报响应无法解析")
        if not rows:
            break
        novel = _novel(rows, known)
        fresh.extend(novel)
        if len(novel) < len(rows) or page >= page_count(payload):
            break
    _write_grouped(data_dir, "industry", fresh)
    _remember(state, "seen_industry", [row["info_code"] for row in fresh], cap=800)
    state["industry_begin"] = begin
    store.save_state(data_dir, state)
    inserted = _ingest(fresh, today=day) if ingest else 0
    return {"industry_code": code, "fetched_pages": pages, "new": len(fresh), "ingested": inserted}


def target_symbols(data_dir: Path, *, limit: int) -> list[str]:
    """热门事件里的股票在前，自选在后。都没有时返回空列表。"""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        try:
            code = research_code(text)
        except ValueError:
            return
        if code in seen:
            return
        seen.add(code)
        ordered.append(text if "." in text.upper() else code)

    for symbol in _hot_symbols():
        add(symbol)
        if len(ordered) >= limit:
            return ordered
    for symbol in _watchlist_symbols():
        add(symbol)
        if len(ordered) >= limit:
            return ordered
    return ordered


def _novel(rows: list[dict], known: set[str]) -> list[dict]:
    """本页没见过的研报。见过的代码留在 known 里，同一轮不重复计数。"""
    fresh = []
    for row in rows:
        code = str(row.get("info_code") or "")
        if not code or code in known:
            continue
        known.add(code)
        fresh.append(row)
    return fresh


def _write_grouped(data_dir: Path, kind: str, rows: list[dict]) -> None:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["publish_date"]), []).append(row)
    for day, chunk in grouped.items():
        store.write_rows(data_dir, kind, day, chunk)


def _ingest(rows: list[dict], *, today: date) -> int:
    cutoff = (today - timedelta(days=_NEWS_DAYS)).isoformat()
    recent = [row for row in rows if str(row.get("publish_date") or "") >= cutoff]
    if not recent:
        return 0
    try:
        from app.news.collectors import Item
        from app.news.config import source_enabled
        from app.news.extract import Lexicon, StructuredStock
        from app.news.service import ingest_items
    except Exception as exc:  # noqa: BLE001
        logger.warning("研报未能写入资讯库: %s", exc)
        return 0
    if not source_enabled("research"):
        return 0
    items = []
    for row in recent:
        published = publish_at(str(row.get("publish_date") or ""))
        if published.tzinfo is None:
            published = published.replace(tzinfo=CN_TZ)
        stocks = []
        if row.get("kind") == "stock" and row.get("symbol"):
            stocks.append(StructuredStock(code=str(row["symbol"])))
        sectors = [str(row.get("industry_name") or "").strip()]
        sectors = [name for name in sectors if name]
        broker = str(row.get("broker") or "")
        rating = str(row.get("rating") or "")
        title = str(row.get("title") or "")
        text = " ".join(part for part in (broker, rating, title) if part)
        items.append(Item(
            source="research",
            source_id=str(row["info_code"]),
            published_at=published,
            author=broker,
            title=title,
            text=text,
            url=str(row.get("pdf_url") or ""),
            stocks=stocks,
            sectors=sectors,
            raw={"kind": row.get("kind"), "rating": rating},
        ))
    result = ingest_items(items, Lexicon([]))
    try:
        from app.news.service import get_store

        get_store().mark_health("research", ok=True, auth_state="ok")
    except Exception as exc:  # noqa: BLE001
        logger.debug("研报健康状态未写入: %s", exc)
    return int(result.get("inserted") or 0)


def _hot_symbols() -> list[str]:
    try:
        from app.news.service import top_hot_events

        snapshot = top_hot_events()
    except Exception as exc:  # noqa: BLE001
        logger.info("热门事件股票未读到: %s", exc)
        return []
    found = []
    for event in snapshot.get("events") or []:
        if not isinstance(event, dict):
            continue
        for stock in event.get("stocks") or event.get("mentioned_stocks") or []:
            if isinstance(stock, dict):
                found.append(str(stock.get("key") or stock.get("symbol") or ""))
            elif isinstance(stock, str):
                found.append(stock)
    return found


def _watchlist_symbols() -> list[str]:
    try:
        from app.services import watchlist

        return [str(row.get("symbol") or "") for row in watchlist.list_symbols()]
    except Exception as exc:  # noqa: BLE001
        logger.info("自选未读到: %s", exc)
        return []


def _stock_params(code: str, page: int) -> dict[str, str]:
    return {
        "industryCode": "*",
        "pageSize": "50",
        "industry": "*",
        "rating": "*",
        "ratingChange": "*",
        "beginTime": "2000-01-01",
        "endTime": "2030-01-01",
        "pageNo": str(page),
        "fields": "",
        "qType": "0",
        "orgCode": "",
        "code": code,
        "rcode": "",
        "p": str(page),
        "pageNum": str(page),
        "pageNumber": str(page),
    }


def _industry_params(industry_code: str, page: int, begin: str) -> dict[str, str]:
    return {
        "industryCode": industry_code,
        "pageSize": "50",
        "industry": "*",
        "rating": "*",
        "ratingChange": "*",
        "beginTime": begin,
        "endTime": "2030-01-01",
        "pageNo": str(page),
        "fields": "",
        "qType": "1",
    }


def _seen_map(state: dict, key: str) -> dict[str, list]:
    raw = state.get(key)
    if not isinstance(raw, dict):
        raw = {}
        state[key] = raw
    return raw


def _seen_list(state: dict, key: str) -> list:
    raw = state.get(key)
    if not isinstance(raw, list):
        raw = []
        state[key] = raw
    return raw


def _remember(state: dict, key: str, codes: list[str], *, cap: int) -> None:
    current = _seen_list(state, key)
    for code in codes:
        if code and code not in current:
            current.append(code)
    del current[:-cap]


def _em(url: str, *, params: dict | None = None, headers: dict | None = None, timeout: float = 30):
    return em_get(url, params=params, headers=headers, timeout=timeout)


def _json(response) -> Any:
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")
    return response.json()
