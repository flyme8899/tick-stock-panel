"""第二批东财数据的超时。口径与选股补充字段相同。

连接超时拦住对端不握手，读取超时拦住对端不回包。
子任务整体超时是后手：到点记失败，并让该线程在当前请求结束后停下来。
"""
from __future__ import annotations

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 30.0
REQUEST_TIMEOUT = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S)

DEFAULT_TASK_TIMEOUT_S = 180.0
MARGIN_TASK_TIMEOUT_S = 360.0


def task_timeout(name: str) -> float:
    if name == "margin":
        return MARGIN_TASK_TIMEOUT_S
    return DEFAULT_TASK_TIMEOUT_S
