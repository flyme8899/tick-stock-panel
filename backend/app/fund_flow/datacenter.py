"""东财数据中心个股日频资金流。

报表 ``RPT_DMSK_TS_FUNDFLOW``。``NET_INFLOW`` 是主力净流入，``BIGDEAL_NET`` 是大单，
``SUPERDEAL_NET`` 是超大单。这三个金额字段的单位是万元，这里换成元后再进
``stock`` 分区。``SECUCODE`` 已带交易所后缀。

全市场历史回补和深市主板 / 北交所当日补缺都走这里，避免两套分页。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.fund_flow.normalize import code6
from app.fund_flow.units import BARE_WAN, money_to_yuan
from app.selection_fields.timeouts import REQUEST_TIMEOUT

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_REPORT = "RPT_DMSK_TS_FUNDFLOW"
# 当日板块补缺沿用原来的列，请求体不改。
BOARD_COLUMNS = "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,NET_INFLOW,BIGDEAL_NET,SUPERDEAL_NET"
# 回补多要 SECUCODE，符号直接用接口上的后缀，不再猜交易所。
MARKET_COLUMNS = (
    "SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,TRADE_DATE,NET_INFLOW,BIGDEAL_NET,SUPERDEAL_NET"
)
PAGE_SIZE = 500
# 深市主板按交易日拉全市场再在本地筛，原来最多 20 页。
BOARD_PAGE_LIMIT = 20
# 全市场一天大约 12 页。40 页够用；再多就停，免得把残缺页写成全市场。
MARKET_PAGE_LIMIT = 40
EMPTY_CODE = 9201
SOURCE = "eastmoney_datacenter"
_REFERER = "https://data.eastmoney.com/zjlx/detail.html"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Get = Callable[..., Any]


def _trade_day(value: object) -> str:
    text = str(value or "").strip().replace("/", "-")
    if "T" in text:
        text = text.split("T", 1)[0]
    text = text.split(" ", 1)[0]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def _default_get() -> Get:
    from app.eastmoney.client import em_get

    return em_get


def fetch_report_rows(
    trade_date: str,
    *,
    get: Get,
    extra_filter: str = "",
    columns: str = BOARD_COLUMNS,
    page_size: int = PAGE_SIZE,
    page_limit: int = BOARD_PAGE_LIMIT,
    timeout: tuple[float, float] | float = REQUEST_TIMEOUT,
    on_truncated: str = "stop",
) -> list[dict]:
    """按交易日分页拉原始行。``9201`` 当成这一天没有数据。

    ``on_truncated="raise"`` 时，页数打满仍是满页就抛错，调用方不要落盘。
    ``stop`` 保持板块补缺的旧行为：到页数上限就返回已经拿到的行。
    """
    if not _DATE_RE.match(trade_date):
        raise ValueError(f"交易日格式应为 YYYY-MM-DD: {trade_date}")
    if page_size < 1 or page_limit < 1:
        raise ValueError("分页参数无效")
    filt = f"(TRADE_DATE='{trade_date}'){extra_filter}"
    collected: list[dict] = []
    truncated = False
    page = 1
    while page <= page_limit:
        response = get(
            DC_URL,
            params={
                "reportName": DC_REPORT,
                "columns": columns,
                "pageNumber": str(page),
                "pageSize": str(page_size),
                "sortColumns": "SECURITY_CODE",
                "sortTypes": "1",
                "source": "WEB",
                "client": "WEB",
                "filter": filt,
            },
            headers={"Referer": _REFERER},
            timeout=timeout,
        )
        payload = response.json() if hasattr(response, "json") else response
        if not isinstance(payload, dict):
            raise RuntimeError(f"数据中心资金流返回异常 {trade_date}")
        if not payload.get("success"):
            if int(payload.get("code") or 0) == EMPTY_CODE:
                break
            raise RuntimeError(str(payload.get("message") or "数据中心资金流失败")[:300])
        result = payload.get("result") or {}
        rows = result.get("data") or []
        if not isinstance(rows, list) or not rows:
            break
        collected.extend(row for row in rows if isinstance(row, dict))
        pages = int(result.get("pages") or 0)
        full = len(rows) >= page_size
        if (pages and page >= pages) or not full:
            break
        if page >= page_limit:
            truncated = True
            break
        page += 1
    if truncated and on_truncated == "raise":
        raise RuntimeError(
            f"数据中心资金流 {trade_date} 超过 {page_limit} 页，已停止以免写残缺分区"
        )
    return collected


def map_datacenter_row(row: dict, *, trade_date: str, source: str = SOURCE) -> dict | None:
    """一行报表收成 ``stock`` 分区字段。金额万元换成元。对不上这一天的行丢掉。"""
    secucode = str(row.get("SECUCODE") or "").strip().upper()
    code = code6(str(row.get("SECURITY_CODE") or secucode))
    if not code:
        return None
    raw_day = row.get("TRADE_DATE")
    if raw_day is None or str(raw_day).strip() == "":
        day = trade_date
    else:
        day = _trade_day(raw_day)
        if day != trade_date:
            return None
    main = money_to_yuan(row.get("NET_INFLOW"), bare=BARE_WAN)
    large = money_to_yuan(row.get("BIGDEAL_NET"), bare=BARE_WAN)
    super_net = money_to_yuan(row.get("SUPERDEAL_NET"), bare=BARE_WAN)
    if main is None and large is None and super_net is None:
        return None
    name = row.get("SECURITY_NAME_ABBR")
    if name is not None:
        name = str(name).strip() or None
    symbol = secucode if "." in secucode else code
    return {
        "symbol": symbol,
        "code": code,
        "name": name,
        "trade_date": day,
        "main_net": main,
        "large_net": large,
        "super_net": super_net,
        "source": source,
    }


def map_datacenter_rows(rows: list[dict], *, trade_date: str, source: str = SOURCE) -> list[dict]:
    """同一代码重复出现时留先见到的那行。"""
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = map_datacenter_row(row, trade_date=trade_date, source=source)
        if item is None or item["code"] in seen:
            continue
        seen.add(item["code"])
        out.append(item)
    return out


def fetch_market_day(
    trade_date: str,
    *,
    get: Get | None = None,
    page_size: int = PAGE_SIZE,
    page_limit: int = MARKET_PAGE_LIMIT,
) -> list[dict]:
    """一个交易日的全市场资金流，已经换成 ``stock`` 分区的字段和元。"""
    getter = get or _default_get()
    raw = fetch_report_rows(
        trade_date,
        get=getter,
        columns=MARKET_COLUMNS,
        page_size=page_size,
        page_limit=page_limit,
        on_truncated="raise",
    )
    return map_datacenter_rows(raw, trade_date=trade_date)
