"""第二批东财数据的开关。

代码默认关闭：环境变量没出现时不调度、不出网。``.env.example`` 里写了打开值。
子开关在总开关关闭时不生效。
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
    return _flag("EVENT_CONTEXT_ENABLED")


def lhb_enabled() -> bool:
    return enabled() and _flag("EVENT_CONTEXT_LHB")


def margin_enabled() -> bool:
    return enabled() and _flag("EVENT_CONTEXT_MARGIN")


def qa_enabled() -> bool:
    return enabled() and _flag("EVENT_CONTEXT_QA")


def notice_enabled() -> bool:
    return enabled() and _flag("EVENT_CONTEXT_NOTICE")


def changes_enabled() -> bool:
    return enabled() and _flag("EVENT_CONTEXT_CHANGES")
