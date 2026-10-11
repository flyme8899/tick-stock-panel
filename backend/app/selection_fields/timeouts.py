"""补充字段出网的默认超时。

连接超时拦住对端不握手，读取超时拦住对端不回包。
子任务整体超时是后手：单次请求没有返回时，记失败并让出队列。
"""
from __future__ import annotations

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 30.0
REQUEST_TIMEOUT = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S)

# 北向大约 9 次请求。按 1 秒间隔加 30 秒读取估算后留余量。
DEFAULT_TASK_TIMEOUT_S = 360.0
# PE 要跑完整市场，整体预算按小时。单次请求仍用上面的连接/读取超时。
PE_TASK_TIMEOUT_S = 3 * 60 * 60.0


def task_timeout(name: str) -> float:
    if name == "pe":
        return PE_TASK_TIMEOUT_S
    return DEFAULT_TASK_TIMEOUT_S
