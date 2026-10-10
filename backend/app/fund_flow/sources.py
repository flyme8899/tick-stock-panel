"""数据源入口。

个股顺序是 efinance，然后妙想。akshare 不参与个股主力（东财个股资金流和
push2 板块接口在 HK 被拦，调用表里没有这些名字）。板块、两融、ETF 份额、
南向和龙虎榜备份走 akshare 允许列表。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.fund_flow.mx import parse_capital_flow

# 允许实际调用的 akshare 函数。测试锁这份名单，防止把被拦接口加回来。
AK_ALLOWLIST = frozenset({
    "stock_fund_flow_industry",
    "stock_fund_flow_concept",
    "stock_margin_sse",
    "stock_margin_detail_sse",
    "stock_margin_szse",
    "stock_margin_detail_szse",
    "fund_etf_scale_sse",
    "stock_lhb_detail_em",
    "stock_hsgt_hist_em",
})

BLOCKED_AK_NAMES = frozenset({
    "stock_individual_fund_flow",
    "stock_individual_fund_flow_rank",
    "stock_main_fund_flow",
    "stock_sector_fund_flow_rank",
})

SOUTHBOUND_SYMBOL = "南向资金"
NORTHBOUND_SYMBOL = "北向资金"


def as_records(obj: Any) -> list[dict]:
    if obj is None:
        return []
    if isinstance(obj, list):
        return [row for row in obj if isinstance(row, dict)]
    to_dicts = getattr(obj, "to_dicts", None)
    if callable(to_dicts):
        return list(to_dicts())
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            rows = to_dict(orient="records")
        except TypeError:
            rows = to_dict()
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    raise TypeError(f"无法把 {type(obj).__name__} 收成行")


class SourceClients:
    """可替换的三个源。``ak_call`` 只接受允许列表里的名字。"""

    def __init__(
        self,
        *,
        history_bill: Callable[[str], Any] | None = None,
        mx_query: Callable[[str], Any] | None = None,
        ak_call: Callable[..., Any] | None = None,
    ) -> None:
        self._history_bill = history_bill
        self._mx_query = mx_query
        self._ak_call = ak_call

    def history_bill(self, symbol: str) -> list[dict]:
        if self._history_bill is None:
            raise RuntimeError("efinance 未配置")
        return as_records(self._history_bill(symbol))

    def mx_capital_flow(self, symbol: str) -> tuple[list[dict], int | None]:
        if self._mx_query is None:
            raise RuntimeError("妙想未配置")
        payload = self._mx_query(symbol)
        if isinstance(payload, tuple):
            rows, quota = payload
            return as_records(rows), quota
        if isinstance(payload, list):
            return as_records(payload), None
        if isinstance(payload, dict):
            return parse_capital_flow(payload)
        raise TypeError("妙想返回值不是表格或 JSON")

    def ak(self, name: str, **kwargs: Any) -> list[dict]:
        if name not in AK_ALLOWLIST:
            raise RuntimeError(f"akshare 函数不在允许列表: {name}")
        if self._ak_call is None:
            raise RuntimeError("akshare 未配置")
        return as_records(self._ak_call(name, **kwargs))


def live_clients() -> SourceClients:
    """真正出网的客户端。导入放在函数里，测试不加载这两个包。"""

    def history_bill(symbol: str):
        import efinance as ef

        return ef.stock.get_history_bill(symbol)

    def mx_query(symbol: str) -> dict:
        import httpx

        from app.fund_flow.config import mx_api_key

        key = mx_api_key()
        if not key:
            raise RuntimeError("未配置 MX_APIKEY")
        response = httpx.post(
            "https://mkapi2.dfcfs.com/finskillshub/api/claw/query",
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"toolQuery": f"{symbol} 近10日每日主力净流入资金"},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("妙想响应不是 JSON 对象")
        return payload

    def ak_call(name: str, **kwargs: Any):
        import akshare as ak

        fn = getattr(ak, name)
        return fn(**kwargs)

    return SourceClients(history_bill=history_bill, mx_query=mx_query, ak_call=ak_call)
