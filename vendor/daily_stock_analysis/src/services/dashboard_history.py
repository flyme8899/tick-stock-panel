"""决策仪表盘列表的纯函数：同日大盘复盘只留最新一条。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ISO_DAY = re.compile(r"(20\d{2})[-/](\d{2})[-/](\d{2})")
_CN_DAY = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")


def market_review_day(record: Any) -> str | None:
    """大盘复盘的日历日。非复盘记录返回 None。

    优先用摘要或正文里的日期（标题通常是「2026-10-10 大盘复盘」），
    没有再退回 created_at。无时区的 created_at 视为 sidecar 本地时钟，
    部署约定是 Asia/Shanghai，这里不再把它当成 UTC。
    """
    if not _is_market_review(record):
        return None
    for attr in ("analysis_summary", "news_content"):
        day = _day_in_text(getattr(record, attr, None))
        if day:
            return day
    created = getattr(record, "created_at", None)
    if isinstance(created, datetime):
        return _created_day(created)
    return "unknown"


def collapse_market_reviews(records: list[Any]) -> list[Any]:
    """同一天只保留最新一条大盘复盘，其余记录原样保留。

    最新按 created_at、再按 id。返回顺序仍是从新到旧。
    """
    ordered = sorted(records, key=_recency_key, reverse=True)
    seen_days: set[str] = set()
    kept: list[Any] = []
    for record in ordered:
        day = market_review_day(record)
        if day is None:
            kept.append(record)
            continue
        if day in seen_days:
            continue
        seen_days.add(day)
        kept.append(record)
    return kept


def _is_market_review(record: Any) -> bool:
    report_type = str(getattr(record, "report_type", "") or "").strip().lower()
    if report_type == "market_review":
        return True
    code = str(getattr(record, "code", "") or getattr(record, "stock_code", "") or "").strip().upper()
    return code == "MARKET"


def _day_in_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    iso = _ISO_DAY.search(value)
    if iso:
        return _format_day(iso.group(1), iso.group(2), iso.group(3))
    cn = _CN_DAY.search(value)
    if cn:
        return _format_day(cn.group(1), cn.group(2), cn.group(3))
    return None


def _format_day(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _created_day(value: datetime) -> str:
    if value.tzinfo is None:
        return value.date().isoformat()
    return value.astimezone(_SHANGHAI).date().isoformat()


def _recency_key(record: Any) -> tuple[datetime, int]:
    created = getattr(record, "created_at", None)
    if not isinstance(created, datetime):
        created = datetime.min
    elif created.tzinfo is not None:
        created = created.astimezone(_SHANGHAI).replace(tzinfo=None)
    raw_id = getattr(record, "id", None)
    try:
        record_id = int(raw_id)
    except (TypeError, ValueError):
        record_id = 0
    return created, record_id
