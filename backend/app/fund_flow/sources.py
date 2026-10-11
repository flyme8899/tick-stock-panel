"""数据源入口。

个股历史账单直接请求东财 push2his，secid 由代码推导（沪市 ``1.``、深市和北交所
``0.``），不再走 efinance 的搜索缓存。efinance 仍可能在导入时写包内缓存，镜像里
把那个目录放开，运行时再把搜索缓存指到 ``data/efinance``。

个股历史失败后，妙想只补已经请求过、但没有拿到的代码。全市场今日 / 5 日排名用
同花顺 ``stock_fund_flow_individual``，不行再用东财 ``stock_individual_fund_flow_rank``。
逐只的 ``stock_individual_fund_flow`` 和 push2 板块接口仍然不调用。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.fund_flow.mx import parse_capital_flow
from app.fund_flow.normalize import code6

logger = logging.getLogger(__name__)

# 允许实际调用的 akshare 函数。测试锁这份名单，防止把被拦接口加回来。
AK_ALLOWLIST = frozenset({
    "stock_fund_flow_industry",
    "stock_fund_flow_concept",
    "stock_fund_flow_individual",
    "stock_individual_fund_flow_rank",
    "stock_margin_sse",
    "stock_margin_detail_sse",
    "stock_margin_szse",
    "stock_margin_detail_szse",
    "fund_etf_scale_sse",
    # 深交所 ETF 规模日频。fund_etf_scale_szse 只有当前快照，没有交易日，不能回补。
    "fund_scale_daily_szse",
    "stock_lhb_detail_em",
    "stock_hsgt_hist_em",
})

BLOCKED_AK_NAMES = frozenset({
    "stock_individual_fund_flow",
    "stock_main_fund_flow",
    "stock_sector_fund_flow_rank",
})

EASTMONEY_BILL_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://data.eastmoney.com/zjlx/detail.html",
}
_BILL_FIELDS = ("日期", "主力净流入", "小单净流入", "中单净流入", "大单净流入", "超大单净流入")
# 沪市 A/B 股和基金。深市、北交所在东财 push2his 上都是市场号 0。
_SH_PREFIXES = ("5", "6", "9")

SOUTHBOUND_SYMBOL = "南向资金"
NORTHBOUND_SYMBOL = "北向资金"


def eastmoney_secid(symbol: str) -> str:
    """``600519.SH`` → ``1.600519``，``000001.SZ`` → ``0.000001``。推不出来就返回空串。"""
    code = code6(symbol)
    if len(code) != 6:
        return ""
    text = str(symbol).upper()
    if text.endswith(".SH"):
        market = "1"
    elif text.endswith((".SZ", ".BJ")):
        market = "0"
    elif code.startswith(_SH_PREFIXES):
        market = "1"
    else:
        market = "0"
    return f"{market}.{code}"


def parse_bill_klines(klines: list) -> list[dict]:
    rows = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < len(_BILL_FIELDS):
            continue
        rows.append(dict(zip(_BILL_FIELDS, parts[: len(_BILL_FIELDS)], strict=True)))
    return rows


def fetch_history_bill(symbol: str) -> list[dict]:
    """用显式 secid 拉日频资金流。空 secid 不出网。``data`` 为空视为这次失败，交给重试。"""
    secid = eastmoney_secid(symbol)
    if not secid:
        raise RuntimeError(f"无法生成 secid: {symbol}")
    from app.eastmoney.client import em_get

    response = em_get(
        EASTMONEY_BILL_URL,
        params={
            "lmt": "0",
            "klt": "101",
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
        },
        headers=EASTMONEY_HEADERS,
        timeout=15.0,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("eastmoney 响应不是 JSON 对象")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"eastmoney 无行情 secid={secid}")
    klines = data.get("klines") or []
    if not isinstance(klines, list):
        raise RuntimeError(f"eastmoney klines 异常 secid={secid}")
    return parse_bill_klines(klines)


def prepare_efinance_cache(data_dir: Path) -> None:
    """导入 efinance 后把搜索缓存改到 ``data/efinance``。

    包内 ``data/`` 在 import 时就会创建。镜像构建把该目录设成 1777，非 root 才能
    完成这次导入。搜索缓存改指向数据卷，避免再写进 site-packages。
    """
    try:
        import efinance.config as cfg
        import efinance.utils as utils
    except Exception as exc:  # noqa: BLE001
        logger.warning("efinance 未就绪，搜索缓存保持原路径: %s", exc)
        return
    target = Path(data_dir) / "efinance"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("无法创建 efinance 缓存目录: %s", exc)
        return
    path = str(target / "search-cache-v2.json")
    cfg.SEARCH_RESULT_CACHE_PATH = path
    utils.SEARCH_RESULT_CACHE_PATH = path


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


def live_clients(data_dir: Path | None = None) -> SourceClients:
    """真正出网的客户端。导入放在函数里，测试不加载 akshare。"""
    if data_dir is not None:
        prepare_efinance_cache(data_dir)

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

    return SourceClients(history_bill=fetch_history_bill, mx_query=mx_query, ak_call=ak_call)
