"""东财 7×24 快讯：只写历史归档库。

不进 news.sqlite、资讯页、热点、推送和提醒。每分钟拉最新一页；
启动或停机后按 sortEnd 往回翻，直到碰到已归档的 id，最多回补 24 小时。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import httpx

from app.market_time import CN_TZ, cn_now
from app.news.archive import NewsArchive, get_archive

logger = logging.getLogger(__name__)

SOURCE = "eastmoney_724"
POLL_SECONDS = 60
CATCHUP_HOURS = 24
MAX_PAGES = 40
PAGE_SIZE = 200
_URL = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://kuaixun.eastmoney.com/"}


def parse_row(row: dict) -> dict | None:
    code = str(row.get("code") or "").strip()
    shown = str(row.get("showTime") or "").strip()
    if not code or not shown:
        return None
    try:
        when = datetime.strptime(shown, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
    except ValueError:
        return None
    return {
        "source": SOURCE,
        "source_id": code,
        "published_at": when,
        "title": row.get("title") or "",
        "content": row.get("summary") or "",
        "level": "",
        "stock_codes": [str(s) for s in (row.get("stockList") or []) if s],
        "url": f"https://finance.eastmoney.com/a/{code}.html",
    }


def fetch_page(client: httpx.Client, sort_end: str) -> tuple[list[dict], str]:
    resp = client.get(_URL, params={
        "client": "web", "biz": "web_724", "fastColumn": "102",
        "sortEnd": sort_end, "pageSize": PAGE_SIZE, "req_trace": int(time.time() * 1000),
    }, headers=_HEADERS)
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    items = data.get("fastNewsList") or []
    next_end = str(items[-1].get("realSort") or "") if items else ""
    return items, next_end


def collect(
    client: httpx.Client | None = None,
    *,
    archive: NewsArchive | None = None,
    now: datetime | None = None,
) -> dict:
    """返回 {"inserted", "pages", "stop"}。网络错误向上抛，由调度器记日志。"""
    now = now or cn_now()
    floor = now - timedelta(hours=CATCHUP_HOURS)
    archive = archive or get_archive()
    own = client is None
    client = client or httpx.Client(timeout=20)
    inserted, pages, stop, sort_end = 0, 0, "max_pages", ""
    try:
        while pages < MAX_PAGES:
            items, next_end = fetch_page(client, sort_end)
            pages += 1
            rows = [r for r in (parse_row(i) for i in items) if r]
            rows = [r for r in rows if r["published_at"] >= floor]
            got = archive.insert_many(rows) if rows else {"inserted": 0, "duplicate": 0}
            inserted += got["inserted"]
            if got["duplicate"]:
                stop = "overlap"
                break
            if len(rows) < len(items) or not items:
                stop = "floor" if items else "empty"
                break
            if not next_end or next_end == sort_end:
                stop = "end"
                break
            sort_end = next_end
    finally:
        if own:
            client.close()
    return {"inserted": inserted, "pages": pages, "stop": stop}
