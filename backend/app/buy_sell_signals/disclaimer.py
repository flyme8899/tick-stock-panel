"""合规脚注文案。

**仅供研究，不构成投资建议；执行须人工确认。**

这条脚注必须出现在三处, 缺一处都算没做:

1. 本模块(即你要读的这里)与下游模块的 docstring
2. `SignalRecord` 序列化后的 `disclaimer` 字段
3. 对外可读的说明文档(`docs/buy-sell-signals-skeleton.md`)

写成一个常量而不是让人手抄, 是因为手抄的文案会漂 —— 有人把"不构成投资建议"
改成"仅供参考", 有人把"人工确认"漏掉, 而这类文案的每一次弱化都不会被任何测试
抓到, 除非有人专门看。这里至少保证程序输出里那份永远一字不差。
"""
from __future__ import annotations

from typing import Final

DISCLAIMER_TEXT: Final = "仅供研究，不构成投资建议；执行须人工确认。"

DISCLAIMER_LONG: Final = (
    "本模块产出的信号仅为研究用途，不构成任何投资建议或交易指令。"
    "任何执行动作都必须由人工复核确认后自行决策。"
)

# 这两个字段不是"可选的合规装饰", 它们恒为 True 且不可配置:
# - not_investment_advice: 一旦允许改成 False, 就等于允许某条信号对外声称
#   "这是投资建议", 而它背后的验证强度并不足以支撑这句话。
# - mcp_trade_forbidden: 本包的信号不接任何自动下单通道。留成可配置开关,
#   等于给"顺手接一下 MCP 交易"留了个后门。
NOT_INVESTMENT_ADVICE: Final = True
MCP_TRADE_FORBIDDEN: Final = True

__all__ = [
    "DISCLAIMER_LONG",
    "DISCLAIMER_TEXT",
    "MCP_TRADE_FORBIDDEN",
    "NOT_INVESTMENT_ADVICE",
]
