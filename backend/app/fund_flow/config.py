"""资金进出开关。

代码默认全部关闭：环境变量没设置时不调度、不出网。``.env.example`` 里为本
HK 部署写了打开值，复制过去才会拉数。
"""
from __future__ import annotations

import os

_ON = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in _ON


def enabled() -> bool:
    return _flag("FUND_FLOW_ENABLED")


def stock_enabled() -> bool:
    return enabled() and _flag("FUND_FLOW_STOCK")


def sector_enabled() -> bool:
    return enabled() and _flag("FUND_FLOW_SECTOR")


def margin_enabled() -> bool:
    return enabled() and _flag("FUND_FLOW_MARGIN")


def etf_shares_enabled() -> bool:
    return enabled() and _flag("FUND_FLOW_ETF_SHARES")


def southbound_enabled() -> bool:
    return enabled() and _flag("FUND_FLOW_SOUTHBOUND")


def northbound_turnover_enabled() -> bool:
    return enabled() and _flag("FUND_FLOW_NORTHBOUND_TURNOVER")


def lhb_backup_enabled() -> bool:
    return enabled() and _flag("FUND_FLOW_LHB_BACKUP")


def mx_api_key() -> str:
    return (os.environ.get("MX_APIKEY") or "").strip()


def mx_max_calls() -> int:
    """单轮妙想调用上限。接口不回报剩余配额时，用这个上限避免打满。"""
    raw = (os.environ.get("FUND_FLOW_MX_MAX_CALLS") or "").strip()
    if not raw:
        return 30
    try:
        value = int(raw)
    except ValueError:
        return 30
    return value if value >= 0 else 30
