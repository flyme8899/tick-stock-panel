"""OpenBB V5 sidecar 的窄白名单 HTTP 客户端。

主进程不安装、不导入 OpenBB；这里只访问固定 REST 路径。研究数据不进入
A 股行情同步、财务 enrichment、回测或因子落盘。
"""
from __future__ import annotations

import math
import os
from datetime import date
from typing import Any

import httpx

from app.data_providers.schemas import DAILY_COLUMNS, MACRO_COLUMNS

OPENBB_PIN = "5.0.0"
OPENBB_LICENSE = "Apache-2.0"
UPSTREAM_REPOSITORY = "https://github.com/openbq-org/OpenBB"
HEALTH_PATH = "/api/v1/tsp/health"
EQUITY_PATH = "/api/v1/tsp_yahoo/equity/historical"
MACRO_PATH = "/api/v1/fred/economy/fred_series"
ALLOWED_SYMBOLS = {
    "hk_daily": frozenset({"0700.HK", "9988.HK"}),
    "us_daily": frozenset({"AAPL", "MSFT"}),
    "macro": frozenset({"DGS10", "CPIAUCSL"}),
}
DATASETS = tuple(ALLOWED_SYMBOLS)


class OpenBBError(Exception):
    """可安全返回给调用方的 sidecar 错误。"""

    def __init__(self, detail: str, status_code: int = 503) -> None:
        super().__init__(detail)
        self.status_code = status_code


def base_url() -> str:
    return os.getenv("OPENBB_BASE_URL", "").strip().rstrip("/")


def enabled() -> bool:
    # enabled 仅表示配置存在，不能冒充 reachable。
    return bool(base_url())


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iso_date(value: Any) -> str | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _rows(payload: Any) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise OpenBBError("OpenBB sidecar 响应缺少 results", 502)
    return [row for row in payload["results"] if isinstance(row, dict)]


def map_daily_rows(dataset: str, symbol: str, payload: Any) -> list[dict]:
    """映射日K必需列；上游未给的金额等字段保持 null，绝不补 0。"""
    market = dataset.removesuffix("_daily")
    rows = []
    for raw in _rows(payload):
        volume = _number(raw.get("volume"))
        rows.append({
            "symbol": symbol,
            "asset_type": "stock",
            "source": "openbb",
            "date": _iso_date(raw.get("date")),
            "open": _number(raw.get("open")),
            "high": _number(raw.get("high")),
            "low": _number(raw.get("low")),
            "close": _number(raw.get("close")),
            # 内部日K成交量单位为手；无成交量时保持 null。
            "volume": math.floor(volume / 100) if volume is not None else None,
            "amount": None,
            "pre_close": None,
            "change_pct": None,
            "market": market,
        })
    return rows


def map_macro_rows(series_id: str, payload: Any) -> list[dict]:
    """宏观使用独立 schema，不伪装成日K。"""
    return [
        {
            "date": _iso_date(raw.get("date")),
            "series_id": series_id,
            "value": _number(raw.get("value")),
            "provider": "fred",
            "source": "openbb",
        }
        for raw in _rows(payload)
    ]


class OpenBBClient:
    def __init__(self, *, timeout: float = 5.0, transport: httpx.BaseTransport | None = None) -> None:
        self.timeout = timeout
        self.transport = transport

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not enabled():
            raise OpenBBError("未配置 OPENBB_BASE_URL")
        try:
            with httpx.Client(
                timeout=self.timeout, follow_redirects=False, transport=self.transport
            ) as client:
                response = client.get(f"{base_url()}{path}", params=params)
        except httpx.TimeoutException as exc:
            raise OpenBBError("OpenBB sidecar 响应超时") from exc
        except httpx.HTTPError as exc:
            raise OpenBBError("OpenBB sidecar 不可达") from exc
        if response.status_code >= 400:
            raise OpenBBError(f"OpenBB sidecar 返回 HTTP {response.status_code}", 502)
        try:
            return response.json()
        except ValueError as exc:
            raise OpenBBError("OpenBB sidecar 响应不是 JSON", 502) from exc

    def health(self) -> tuple[bool, str]:
        try:
            payload = self._get(HEALTH_PATH)
        except OpenBBError as exc:
            return False, str(exc)
        if not isinstance(payload, dict):
            return False, "OpenBB sidecar health 响应无效"
        if payload.get("openbb_version") != OPENBB_PIN:
            return False, f"OpenBB 版本不符合 pin {OPENBB_PIN}"
        if payload.get("license") != OPENBB_LICENSE:
            return False, "OpenBB 许可证路径不是 Apache-2.0"
        providers = payload.get("providers")
        if (
            not isinstance(providers, list)
            or not all(isinstance(provider, str) for provider in providers)
            or set(providers) != {"fred", "tsp_yahoo"}
        ):
            return False, "OpenBB provider 集合不符合最小白名单"
        return True, "已连接"

    def query(
        self,
        dataset: str,
        symbol: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        normalized = symbol.strip().upper()
        if dataset not in ALLOWED_SYMBOLS or normalized not in ALLOWED_SYMBOLS[dataset]:
            raise OpenBBError("数据集或符号不在 OpenBB 只读白名单", 403)
        # 先 health 再取数，连接断开时不把上游异常扩散到主进程。
        reachable, detail = self.health()
        if not reachable:
            raise OpenBBError(detail)
        params: dict[str, Any] = {"symbol": normalized, "provider": "tsp_yahoo" if dataset != "macro" else "fred"}
        if start_date:
            params["start_date"] = start_date.isoformat()
        if end_date:
            params["end_date"] = end_date.isoformat()
        if dataset == "macro":
            params["interval"] = "1d"
            payload = self._get(MACRO_PATH, params)
            rows = map_macro_rows(normalized, payload)
            columns = list(MACRO_COLUMNS)
            provider = "fred"
        else:
            params["interval"] = "1d"
            payload = self._get(EQUITY_PATH, params)
            rows = map_daily_rows(dataset, normalized, payload)
            columns = [*DAILY_COLUMNS, "market"]
            provider = "tsp_yahoo"
        return {
            "dataset": dataset,
            "symbol": normalized,
            "provider": provider,
            "source": "openbb",
            "research_only": True,
            "columns": columns,
            "rows": rows,
        }


def status() -> dict:
    reachable, detail = OpenBBClient().health()
    return {
        "enabled": enabled(),
        "base_url": base_url(),
        "reachable": reachable,
        "detail": detail,
        "pin": OPENBB_PIN,
        "license": OPENBB_LICENSE,
        "upstream": UPSTREAM_REPOSITORY,
        "research_only": True,
        "allowlist": {key: sorted(value) for key, value in ALLOWED_SYMBOLS.items()},
    }
