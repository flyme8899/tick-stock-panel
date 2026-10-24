"""健康预警(veto)接口桩: 只有挂点, 没有规则引擎。

## 这一层到底是什么

"健康 veto" 指的是: 某只票出现财务/合规/治理层面的**否决性**信号时(连续亏损、
审计非标、大额商誉减值等), 无论技术信号多好都不该发。

真正的规则引擎需要:

- 一份经过评审的规则清单(每条规则的数据来源、阈值、适用行业、时效)
- 每条规则的历史触发回放(否则不知道它会拦掉多少本来正确的信号)
- 阈值的跨行业校准(制造业的负债率与银行不是一回事)

这三件事**本批次都没有做**。所以这里只有挂点:

- `health_veto_rule_version = "design_pending"`
- `health_veto_hit = None`(不是 False —— False 表示"查过了, 没命中")
- `tested_status = "untested"`

在补齐上面三件事之前, 任何地方都不许对外说健康门禁已经过验证。

## 为什么 `hit=None` 而不是 `hit=False`

这是本模块唯一值得强调的设计点。`False` 是一个**结论**:"查过, 这只票健康"。
在没有任何规则的情况下返回 `False`, 等于向调用方保证了一件我们并不知道的事,
而调用方极可能把它当成"健康预警已通过"写进日志或界面。

`None` 在 Python 里天然表达"未知", 只要调用方不去 `if hit:` 就直接当成通过,
这个语义就是对的。为了让人不容易误用, `HealthVetoStub.blocks_publish` 也返回
`None` 而不是 `False`。

**健康门禁未测通。**

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

from .models import asof_date

HEALTH_RULE_VERSION_PENDING: Final = "design_pending"
"""健康规则版本。规则引擎落地之前, 这里只有这一个合法值。"""

HEALTH_TESTED_STATUS: Final = "untested"

HEALTH_RULE_DESIGN_REQUIRED: Final = (
    "rule_catalog",       # 规则清单: 每条规则的来源/阈值/适用行业
    "historical_replay",  # 历史触发回放: 会拦掉多少信号
    "threshold_calibration",  # 跨行业阈值校准
)
"""规则引擎落地所需的三件事。缺任何一件, 健康门禁就仍是未验证状态。"""


@dataclass(frozen=True, slots=True)
class HealthVetoStub:
    """一次健康 veto 查询的结果(当前恒为未知)。"""

    symbol: str
    asof: date
    health_veto_hit: bool | None = None
    """**恒为 None**。None = 未知; False 才是"查过且没命中"。"""
    health_veto_rule_version: str = HEALTH_RULE_VERSION_PENDING
    tested_status: str = HEALTH_TESTED_STATUS
    reason: str = "健康规则引擎尚未实现, 本批次只保留挂点"
    missing_inputs: tuple[str, ...] = HEALTH_RULE_DESIGN_REQUIRED
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def blocks_publish(self) -> bool | None:
        """是否应当阻止发布。

        返回 `None`(未知), **不是** False。调用方如果把它当布尔用, `None` 是假值,
        会走"不阻止"分支 —— 这与"未知"的语义相符, 而且不会给出任何"已通过"的错觉。
        真要拦住时, 调用方应显式写 `if self.blocks_publish is False: 放行`。
        """
        return self.health_veto_hit

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asof": self.asof.isoformat(),
            "health_veto_hit": self.health_veto_hit,
            "health_veto_rule_version": self.health_veto_rule_version,
            "tested_status": self.tested_status,
            "reason": self.reason,
            "missing_inputs": list(self.missing_inputs),
        }


def health_veto_asof(symbol: str, asof: date | datetime) -> HealthVetoStub:
    """查某只票在 `asof` 的健康 veto。当前恒返回未知。

    签名与 `sw_pit.industry_asof` / `valuation_stub.valuation_asof` 一致。
    """
    return HealthVetoStub(symbol=symbol, asof=asof_date(asof))


__all__ = [
    "HEALTH_RULE_DESIGN_REQUIRED",
    "HEALTH_RULE_VERSION_PENDING",
    "HEALTH_TESTED_STATUS",
    "HealthVetoStub",
    "health_veto_asof",
]
