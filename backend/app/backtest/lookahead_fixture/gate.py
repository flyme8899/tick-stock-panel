"""`lookahead_gate`：对外前置契约的**查询桩**（fail-closed）。

**仅供研究，不构成投资建议；执行须人工确认。**

## 这个模块故意什么都不"放行"

它回答一个问题：**"这个入口/策略，能不能对外宣称过了 lookahead 审计、能不能对
第三方开放 `run:backtest` 的结果？"**

答案只有两种：`False`（有明确理由）或抛错（问不出来）。**没有 `True`。**

理由不是懒，而是本拍**没有任何运行时证据来源**：

- 仓内 CI 跑夹具 → 结果是 GitHub Actions 的绿勾，不是仓里的持久记录；
- 运行时进程读不到 CI 的结论；
- 若此时按"CI 绿过就放行"去实现，需要一份"通过记录"落盘，而这份记录的写入端
  也在本拍范围外。

所以任何 `True` 都只能是**写死的**，而写死的 `True` 就是假绿 —— 它会在夹具明明
红了的时候继续放行。宁可抛 `LookaheadGateUnavailable`（fail-closed 的最强形式：
连"不确定"都不说，直接说"查不了"），也不给一个看起来能用的 `True`。

## 那这个桩有什么用

1. **把契约写进代码**：调用点必须显式处理"查不了"这件事，而不是默认放行。
2. **给下一个批次一个明确的落点**：接入真实通过记录时，只需要实现
   `_load_pass_record()`，其它调用点不用改。
3. **测试能守**：`test_gate_is_fail_closed` 断言它永远不放行 —— 将来谁把桩改成
   `return True`，那条测试会红。

## 明确不做

- **不在** MCP `run_backtest` 热路径里调用本模块去重跑夹具。主门禁在仓内 CI；
  在热路径重跑一遍既是性能事故，也会让"门禁"变成"每次调用都可能不同的随机结论"。
"""
from __future__ import annotations

from typing import Final

__all__ = [
    "LookaheadGateUnavailable",
    "GATE_STATUS_PENDING",
    "gate_status",
    "is_cleared_for_external_claim",
]

GATE_STATUS_PENDING: Final = "design_pending"
"""本拍的对接口径：设计已钉、运行时检测点未接。"""


class LookaheadGateUnavailable(RuntimeError):
    """查不到通过记录。**不是**"没通过"，而是"无法判定" —— 两者都禁止放行。"""


def gate_status(entry_id: str) -> dict[str, object]:
    """返回该入口的 gate 状态（**恒**为未放行）。

    字段说明：

    - `entry_id`：被查的入口；
    - `status`：`design_pending` —— 运行时检测点尚未接入；
    - `cleared`：**恒 False**；
    - `evidence_source`：说明证据该从哪来（本拍：仓内 CI 的 pytest 节点）；
    - `reason`：为什么现在是 False。
    """
    return {
        "entry_id": entry_id,
        "status": GATE_STATUS_PENDING,
        "cleared": False,
        "evidence_source": (
            "仓内 CI：backend/tests/backtest/test_lookahead_fixture.py 的 pytest 节点。"
            "CI 绿是本拍唯一的自动化证据，但它不落到仓内，运行时读不到。"
        ),
        "reason": (
            "运行时无通过记录来源，按 fail-closed 一律不放行。"
            "接入真实通过记录属另一拍（见 docs/lookahead-fixture.md §挂钩点）。"
        ),
    }


def is_cleared_for_external_claim(entry_id: str) -> bool:
    """能否对外宣称"已过 lookahead 审计"。

    **恒 `False`。** 这个函数存在的意义是让调用点被迫显式处理"不放行"分支，
    而不是让它返回 True。
    """
    del entry_id  # 参数保留：将来接入通过记录后要按 entry_id 查
    return False
