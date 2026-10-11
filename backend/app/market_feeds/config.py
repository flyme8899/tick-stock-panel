"""打板、研报和东财限流的开关。

未设置环境变量时默认打开。``.env.example`` 里同样写成 true。
设为 false 才关闭对应拉取。
"""
from __future__ import annotations

import os

_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}


def _flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    return default


def limit_board_enabled() -> bool:
    return _flag("LIMIT_BOARD_ENABLED", default=True)


def limit_board_eastmoney() -> bool:
    return limit_board_enabled() and _flag("LIMIT_BOARD_EASTMONEY", default=True)


def limit_board_ths() -> bool:
    return limit_board_enabled() and _flag("LIMIT_BOARD_THS", default=True)


def research_enabled() -> bool:
    return _flag("RESEARCH_ENABLED", default=True)


def research_stock() -> bool:
    return research_enabled() and _flag("RESEARCH_STOCK", default=True)


def research_industry() -> bool:
    return research_enabled() and _flag("RESEARCH_INDUSTRY", default=True)


def research_batch_size() -> int:
    raw = (os.environ.get("RESEARCH_BATCH_SIZE") or "").strip()
    if not raw:
        return 8
    try:
        value = int(raw)
    except ValueError:
        return 8
    return min(20, max(1, value))


def research_max_symbols() -> int:
    raw = (os.environ.get("RESEARCH_MAX_SYMBOLS") or "").strip()
    if not raw:
        return 40
    try:
        value = int(raw)
    except ValueError:
        return 40
    return min(80, max(1, value))
