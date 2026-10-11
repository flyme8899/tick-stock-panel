"""把东财四池和同花顺涨停揭秘写成当天分区。

东财四次请求走共用限流。同花顺先做一次轻量探活，探不到就跳过，不拖垮东财落盘。
情绪汇总只在涨停、炸板、跌停三池都成功时写入，避免用残缺样本算出炸板率。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.eastmoney.client import UA, em_get
from app.limit_board import store
from app.limit_board.parse import (
    compact_date,
    parse_dt_pool,
    parse_ths_pool,
    parse_yzt_pool,
    parse_zb_pool,
    parse_zt_pool,
    sentiment_row,
)

logger = logging.getLogger(__name__)

ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"
_POOLS = (
    ("zt", "getTopicZTPool", "fbt:asc", parse_zt_pool),
    ("zb", "getTopicZBPool", "fbt:asc", parse_zb_pool),
    ("dt", "getTopicDTPool", "fund:asc", parse_dt_pool),
    ("yzt", "getYesterdayZTPool", "zs:desc", parse_yzt_pool),
)
THS_URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
THS_FIELDS = (
    "199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004"
)
Get = Callable[..., Any]


def sync_day(
    data_dir: Path,
    trade_date: str,
    *,
    eastmoney: bool = True,
    ths: bool = True,
    get: Get | None = None,
    ths_get: Get | None = None,
) -> dict:
    result = {"date": trade_date, "wrote": {}, "errors": [], "ths": "skipped"}
    parsed: dict[str, list[dict] | None] = {}
    if eastmoney:
        fetch = get or _em
        for kind, endpoint, sort, parser in _POOLS:
            try:
                payload = _json(fetch(
                    f"https://push2ex.eastmoney.com/{endpoint}",
                    params={
                        "ut": ZTB_UT,
                        "dpt": "wz.ztzt",
                        "Pageindex": 0,
                        "pagesize": 10000,
                        "sort": sort,
                        "date": compact_date(trade_date),
                    },
                    headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"},
                    timeout=10,
                ))
                rows = parser(payload, trade_date)
            except Exception as exc:  # noqa: BLE001
                logger.warning("打板 %s %s 失败: %s", trade_date, kind, exc)
                result["errors"].append(f"{kind}: {exc}"[:180])
                parsed[kind] = None
                continue
            if rows is None:
                result["errors"].append(f"{kind}: 无有效数据")
                parsed[kind] = None
                continue
            store.write_rows(data_dir, kind, trade_date, rows)
            result["wrote"][kind] = len(rows)
            parsed[kind] = rows
        if parsed.get("zt") is not None and parsed.get("zb") is not None and parsed.get("dt") is not None:
            row = sentiment_row(
                trade_date,
                parsed["zt"] or [],
                parsed["zb"] or [],
                parsed["dt"] or [],
                parsed.get("yzt"),
            )
            store.write_rows(data_dir, "sentiment", trade_date, [row])
            result["wrote"]["sentiment"] = 1
    if ths:
        fetch_ths = ths_get or _ths_http
        allowed = _ths_healthy(fetch_ths, trade_date)
        if not allowed:
            result["ths"] = "unavailable"
        else:
            try:
                rows = _fetch_ths(fetch_ths, trade_date)
            except Exception as exc:  # noqa: BLE001
                logger.warning("同花顺涨停揭秘 %s 失败: %s", trade_date, exc)
                result["errors"].append(f"ths: {exc}"[:180])
                result["ths"] = "unavailable"
            else:
                if rows is None:
                    result["ths"] = "unavailable"
                else:
                    store.write_rows(data_dir, "ths", trade_date, rows)
                    result["wrote"]["ths"] = len(rows)
                    result["ths"] = "ok"
    return result


def _ths_healthy(fetch: Get, trade_date: str) -> bool:
    try:
        response = fetch(
            THS_URL,
            params={"page": 1, "limit": 1, "date": compact_date(trade_date)},
            headers={"User-Agent": UA},
            timeout=4,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("同花顺涨停揭秘不可达，跳过: %s", exc)
        return False
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        return False
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return False
    return isinstance(body, dict) and isinstance(body.get("data"), dict)


def _fetch_ths(fetch: Get, trade_date: str) -> list[dict] | None:
    collected: list[dict] = []
    for page in (1, 2):
        response = fetch(
            THS_URL,
            params={
                "page": page,
                "limit": 200,
                "field": THS_FIELDS,
                "filter": "HS,GEM2STAR",
                "order_field": "330324",
                "order_type": "0",
                "date": compact_date(trade_date),
            },
            headers={"User-Agent": UA},
            timeout=10,
        )
        rows = parse_ths_pool(_json(response), trade_date)
        if rows is None:
            return None if page == 1 else collected
        collected.extend(rows)
        if len(rows) < 200:
            break
    return collected


def _em(url: str, *, params: dict | None = None, headers: dict | None = None, timeout: float = 15):
    return em_get(url, params=params, headers=headers, timeout=timeout)


def _ths_http(url: str, *, params: dict | None = None, headers: dict | None = None, timeout: float = 10):
    import httpx

    return httpx.get(url, params=params, headers=headers, timeout=timeout)


def _json(response) -> Any:
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")
    return response.json()
