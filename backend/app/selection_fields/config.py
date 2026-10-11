"""选股补充字段的开关。

代码默认全部关闭：环境变量没出现时不调度、不出网。``.env.example`` 里写了打开值。
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
    return _flag("SELECTION_FIELDS_ENABLED")


def northbound_enabled() -> bool:
    return enabled() and _flag("SELECTION_NORTHBOUND")


def holders_enabled() -> bool:
    return enabled() and _flag("SELECTION_HOLDERS")


def index_members_enabled() -> bool:
    return enabled() and _flag("SELECTION_INDEX_MEMBERS")


def unlock_enabled() -> bool:
    return enabled() and _flag("SELECTION_UNLOCK")


def pe_ttm_enabled() -> bool:
    return enabled() and _flag("SELECTION_PE_TTM")


def pe_flush_every() -> int:
    raw = (os.environ.get("SELECTION_PE_FLUSH_EVERY") or "").strip()
    if not raw:
        return 25
    try:
        value = int(raw)
    except ValueError:
        return 25
    return min(100, max(1, value))
