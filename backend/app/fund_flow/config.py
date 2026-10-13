"""资金进出开关。

代码默认全部关闭：环境变量没设置时不调度、不出网。``.env.example`` 里为本
HK 部署写了打开值，复制过去才会拉数。例外是盘中个股即时资金流：总开关打开
且 ``FUND_FLOW_STOCK_INTRADAY`` 未设置时默认拉取，写成 false 才停。
盘中优先源默认东财，``FUND_FLOW_INTRADAY_PRIMARY=ths`` 才改走同花顺。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

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


def intraday_primary() -> str:
    """盘中个股主力净流入先打哪一家。缺省 ``eastmoney``，只有写成 ``ths`` 才改同花顺。

    无法识别的值按东财处理，并记一条日志。大小写不敏感。
    """
    raw = (os.environ.get("FUND_FLOW_INTRADAY_PRIMARY") or "").strip().lower()
    if raw in {"", "eastmoney"}:
        return "eastmoney"
    if raw == "ths":
        return "ths"
    logger.warning("FUND_FLOW_INTRADAY_PRIMARY=%s 无法识别，改用 eastmoney", raw)
    return "eastmoney"


def stock_intraday_enabled() -> bool:
    """盘中个股即时资金流。总开关打开时默认开，显式写成 false 才关。

    未设置和空字符串都算开。写了别的值（false / 0 / no / off）才关，避免拼错时继续出网。
    """
    if not enabled():
        return False
    raw = os.environ.get("FUND_FLOW_STOCK_INTRADAY")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in _ON


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
