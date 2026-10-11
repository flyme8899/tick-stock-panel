"""东财研报列表解析。

字段和 PDF 地址模板移植自 simonlin1212/a-stock-data SKILL.md §2.1
（Apache-2.0，Copyright 2026 Simon Lin）。见 ``app/eastmoney/NOTICE``。

不实现 ``download_pdf``。北交所老号段（43/83/87）直接拒绝，避免静默当成没有研报。
"""
from __future__ import annotations

import re
from datetime import datetime

from app.market_time import CN_TZ
from app.news.extract import canonical_symbol, code6_of

REPORT_API = "https://reportapi.eastmoney.com/report/list"
PDF_TPL = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
_OLD_BJ = {"43", "83", "87"}
_INDUSTRY_CODE = re.compile(r"^(\*|\d{1,8})$")


def research_code(symbol: str) -> str:
    """个股研报只接受 6 位代码。显式指数和北交所老号段抛 ValueError。"""
    raw = str(symbol or "").strip()
    upper = raw.upper()
    if upper.startswith("SH") and upper[2:8].isdigit() and len(upper) == 8:
        digits = upper[2:]
        market = "SH"
    elif "." in upper:
        digits = code6_of(upper)
        market = upper.rsplit(".", 1)[-1]
    else:
        digits = code6_of(upper)
        market = ""
    if len(digits) != 6:
        raise ValueError(f"无法把 {symbol!r} 解析为 6 位股票代码")
    if market == "SH" and digits.startswith("000"):
        raise ValueError(f"{symbol!r} 指向沪市指数而非个股")
    if digits[:2] in _OLD_BJ:
        raise ValueError(
            f"{digits} 属北交所老号段（43/83/87），东财研报不再按老码索引。"
            "请改用 920 开头的现行代码。"
        )
    return digits


def check_industry_code(industry_code: str) -> str:
    text = str(industry_code or "").strip() or "*"
    if _INDUSTRY_CODE.match(text) is None:
        raise ValueError(f"行业码不合法: {industry_code!r}")
    return text


def pdf_url(info_code: str) -> str:
    code = str(info_code or "").strip()
    if not code:
        return ""
    return PDF_TPL.format(info_code=code)


def report_rows(payload) -> tuple[list, int] | None:
    if not isinstance(payload, dict):
        return None
    rows = payload.get("data")
    if rows is None:
        return [], int(payload.get("TotalPage") or 0)
    if not isinstance(rows, list):
        return None
    total = payload.get("TotalPage") or payload.get("totalPage") or 1
    try:
        pages = int(total)
    except (TypeError, ValueError):
        pages = 1
    return rows, max(pages, 1)


def parse_stock_reports(payload, symbol: str) -> list[dict] | None:
    parsed = report_rows(payload)
    if parsed is None:
        return None
    rows, _pages = parsed
    code = research_code(symbol)
    return [item for item in (_stock_row(raw, code) for raw in rows) if item]


def parse_industry_reports(payload) -> list[dict] | None:
    parsed = report_rows(payload)
    if parsed is None:
        return None
    rows, _pages = parsed
    return [item for item in (_industry_row(raw) for raw in rows) if item]


def page_count(payload) -> int:
    parsed = report_rows(payload)
    if parsed is None:
        return 1
    return parsed[1]


def _stock_row(raw, code: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    info = str(raw.get("infoCode") or "").strip()
    title = str(raw.get("title") or "").strip()
    published = _publish_date(raw.get("publishDate"))
    if not info or not title or not published:
        return None
    return {
        "info_code": info,
        "code": code,
        "symbol": canonical_symbol(code),
        "title": title[:180],
        "broker": str(raw.get("orgSName") or "").strip()[:40],
        "rating": str(raw.get("emRatingName") or "").strip()[:16],
        "eps_this": _eps(raw.get("predictThisYearEps")),
        "eps_next": _eps(raw.get("predictNextYearEps")),
        "eps_two": _eps(raw.get("predictNextTwoYearEps")),
        "publish_date": published,
        "pdf_url": pdf_url(info),
        "industry_name": str(raw.get("indvInduName") or raw.get("industryName") or "").strip()[:40],
        "kind": "stock",
        "source": "eastmoney",
    }


def _industry_row(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    info = str(raw.get("infoCode") or "").strip()
    title = str(raw.get("title") or "").strip()
    published = _publish_date(raw.get("publishDate"))
    if not info or not title or not published:
        return None
    return {
        "info_code": info,
        "industry_code": str(raw.get("industryCode") or "").strip()[:16],
        "industry_name": str(raw.get("industryName") or "").strip()[:40],
        "title": title[:180],
        "broker": str(raw.get("orgSName") or "").strip()[:40],
        "rating": str(raw.get("emRatingName") or "").strip()[:16],
        "publish_date": published,
        "pdf_url": pdf_url(info),
        "kind": "industry",
        "source": "eastmoney",
    }


def _publish_date(value) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def publish_at(value: str) -> datetime:
    text = str(value or "").strip()
    day = _publish_date(text)
    clock = "12:00:00"
    if len(text) >= 19 and text[10] in {" ", "T"}:
        clock = text[11:19]
    parsed = datetime.fromisoformat(f"{day}T{clock}")
    return parsed.replace(tzinfo=CN_TZ)


def _eps(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "null"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number:
        return None
    return number
