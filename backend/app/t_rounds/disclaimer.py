"""合规脚注文案。

**仅供研究，不构成投资建议；执行须人工确认。**

这条脚注必须出现在三处: 本模块、`TRoundRecord` 序列化后的 `disclaimer` 字段、
以及 `docs/t-rounds-skeleton.md`。写成一个常量而不是手抄, 理由与
`buy_sell_signals.disclaimer` 相同: 手抄的文案会漂, 而每一次弱化都不会被任何
测试抓到, 除非有人专门看。

本模块与 `buy_sell_signals.disclaimer` **不共享代码**: 两个包的隔离是硬要求
(见 `constants` 开头的说明), 宁可重复这十几行, 也不要为了 DRY 建一个公共模块
把两个包重新绑在一起。
"""
from __future__ import annotations

from typing import Final

DISCLAIMER_TEXT: Final = "仅供研究，不构成投资建议；执行须人工确认。"

DISCLAIMER_LONG: Final = (
    "本轮次记录仅为研究用途，不构成任何投资建议或交易指令。"
    "任何执行动作都必须由人工复核确认后自行决策。"
)

# 这两个恒 True 且不可配置:
# - not_investment_advice: 允许改成 False 就等于允许某条轮次记录对外声称
#   "这是投资建议", 而它背后的验证强度并不支撑这句话。
# - mcp_trade_forbidden: 做 T 是最容易"顺手接个自动下单"的场景 —— 一轮 T 只
#   持有几分钟, 人来不及确认。留成开关等于给这条通道留了后门。
NOT_INVESTMENT_ADVICE: Final = True
MCP_TRADE_FORBIDDEN: Final = True

# 三条"不要宣称已验证"的护栏。它们不是配置, 是文案纪律的锚点:
# 本批次没有接线真实分钟评估, 没有跑特征回测, 没有 ε 门禁实测, 也没有可卖
# 老股实盘字段。把这三项写成"已验证"一律是错的。
NOT_CLAIMED_VERIFIED: Final = (
    "特征回测",
    "ε 门禁",
    "可卖老股实盘",
)

__all__ = [
    "DISCLAIMER_LONG",
    "DISCLAIMER_TEXT",
    "MCP_TRADE_FORBIDDEN",
    "NOT_CLAIMED_VERIFIED",
    "NOT_INVESTMENT_ADVICE",
]
