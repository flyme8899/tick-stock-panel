"""只读研报落盘。"""
from __future__ import annotations

from pathlib import Path

from app.news.extract import code6_of
from app.research_reports import store
from app.research_reports.parse import research_code


def for_symbol(data_dir: Path, symbol: str, *, limit: int = 20) -> list[dict]:
    code = research_code(symbol)
    rows: list[dict] = []
    for day in reversed(store.list_dates(data_dir, "stock")):
        frame = store.read_partition(data_dir, "stock", day)
        if frame.is_empty():
            continue
        matched = frame.filter(frame["code"] == code)
        rows.extend(matched.to_dicts())
        if len(rows) >= limit:
            break
    rows.sort(key=lambda item: str(item.get("publish_date") or ""), reverse=True)
    return [_public_stock(row) for row in rows[:limit]]


def industry(data_dir: Path, *, limit: int = 30) -> list[dict]:
    rows: list[dict] = []
    for day in reversed(store.list_dates(data_dir, "industry")):
        frame = store.read_partition(data_dir, "industry", day)
        rows.extend(frame.to_dicts())
        if len(rows) >= limit:
            break
    rows.sort(key=lambda item: str(item.get("publish_date") or ""), reverse=True)
    return [_public_industry(row) for row in rows[:limit]]


def health(data_dir: Path) -> dict:
    from app.eastmoney.client import get_limiter
    from app.market_feeds import config

    return {
        "enabled": config.research_enabled(),
        "stock": config.research_stock(),
        "industry": config.research_industry(),
        "latest": {
            "stock": store.latest_date(data_dir, "stock"),
            "industry": store.latest_date(data_dir, "industry"),
        },
        "eastmoney_circuit": get_limiter().snapshot(),
    }


def _public_stock(row: dict) -> dict:
    return {
        "info_code": row.get("info_code"),
        "symbol": row.get("symbol") or "",
        "code": row.get("code") or code6_of(str(row.get("symbol") or "")),
        "title": row.get("title") or "",
        "broker": row.get("broker") or "",
        "rating": row.get("rating") or "",
        "eps_this": row.get("eps_this"),
        "eps_next": row.get("eps_next"),
        "eps_two": row.get("eps_two"),
        "publish_date": row.get("publish_date") or "",
        "pdf_url": row.get("pdf_url") or "",
        "industry_name": row.get("industry_name") or "",
    }


def _public_industry(row: dict) -> dict:
    return {
        "info_code": row.get("info_code"),
        "industry_code": row.get("industry_code") or "",
        "industry_name": row.get("industry_name") or "",
        "title": row.get("title") or "",
        "broker": row.get("broker") or "",
        "rating": row.get("rating") or "",
        "publish_date": row.get("publish_date") or "",
        "pdf_url": row.get("pdf_url") or "",
    }
