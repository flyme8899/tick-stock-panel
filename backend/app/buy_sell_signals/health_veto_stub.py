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

### `if hit:` 到底意味着什么(批次 20261025gval 写死)

这一点必须说透, 否则"None 是假值"会变成一次静默的语义漂移:

```python
hit = health_veto_asof(sym, asof).health_veto_hit   # 当前恒 None

if hit:              # None 是假值 → 不进分支 → 行为是"不阻止"
    skip(sym)        # ↑ 这段永远不执行, 但它执行与不执行都**不是**"已健康通过"

if hit is False:     # 只有这一句才是"查过, 且没命中"
    allow(sym)       # 当前**永远不会**走到 —— 本模块永不返回 False
```

所以:

| 表达式 | 当前取值 | 语义 |
|---|---|---|
| `if hit:` | 不进分支 | **不阻止**(因为未知), **不是**"已健康通过" |
| `if not hit:` | 进分支 | 把"未知"当成"健康", **这是误用** |
| `if hit is False:` | 不成立 | 唯一合法的"健康通过"判据, 当前永不成立 |
| `if hit is None:` | 成立 | "未知"的唯一正确判据 |

结论: `None` 只能被解读为**未知**。任何把 `not hit` 当健康的调用方, 都是在没有
任何规则的情况下宣称一只票健康 —— 那正是本模块要阻止的事。

**健康门禁未测通。**

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

from .models import asof_date

HEALTH_RULE_VERSION_PENDING: Final = "design_pending"
"""健康规则版本。规则引擎落地之前, 这里只有这一个合法值。

本批次(20261025gval)**不得**把它升成任何"已定版"的标识 —— 定版的前提是
`HEALTH_RULE_DESIGN_REQUIRED` 三件事都齐了, 而三件都还没做。
"""

HEALTH_TESTED_STATUS: Final = "untested"

HEALTH_RULE_DESIGN_REQUIRED: Final = (
    "rule_catalog",       # 规则清单: 每条规则的来源/阈值/适用行业
    "historical_replay",  # 历史触发回放: 会拦掉多少信号
    "threshold_calibration",  # 跨行业阈值校准
)
"""规则引擎落地所需的三件事。缺任何一件, 健康门禁就仍是未验证状态。"""

HEALTH_VETO_REASON_CATALOG_DRAFT: Final = (
    "audit_opinion_bad",   # 非标审计意见等(若字段可得)
    "leverage_stress",     # 杠杆 / 偿债预警
    "cashflow_stress",     # 现金流恶化预警
    "regulatory_risk",     # 监管 / 立案等(若字段可得)
    "other_rule",          # 升版后扩展
)
"""**草稿**原因码目录(PHASE2 §3.3)。

这是草稿, 不是定稿:`health_veto_rule_version` 仍是 `design_pending`, 本批次
没有任何代码会产出这些码。它们的用途是先把"命中了会是什么情况"的词汇表钉下来,
免得将来实现时各人各写一套。

对应关系(示意, 阈值与数据来源都还没有):

| 原因码 | 含义 | 触发需要的数据 |
|---|---|---|
| `audit_opinion_bad` | 审计非标 / 无法表示意见 | 审计意见字段(本机未确认可得) |
| `leverage_stress` | 杠杆 / 偿债预警 | `debt_to_asset_ratio` 的跨行业阈值 |
| `cashflow_stress` | 现金流恶化 | 经营现金流序列(阈值未定) |
| `regulatory_risk` | 监管 / 立案 | 公告事件(本机未确认可得) |
| `other_rule` | 升版后扩展 | — |

**这五码不是"通过码"**, 是"拦下码": 它们的语义是"发现了这个问题", 不是"没发现
问题"。把它们理解反了, 门禁的方向就整个翻转了。
"""

HEALTH_VETO_FORBIDDEN_PASS_CODES: Final = frozenset(
    {
        "data_insufficient_treat_as_pass",
    }
)
"""**禁止**以任何形式收录的"通过码"。

`data_insufficient_treat_as_pass` 的意思是"数据不足, 当成通过"。这是把未知当成
健康 —— 与 `hit=None` 的全部设计意图正相反, 也与 `quality_proxy` 里"`None` 才是
不知道, 0 是真实数字"的纪律相冲突。

缺数据时的正确映射是: **未知**(`hit=None`)或 `data_abnormal`(数据异常), **绝不**
是 pass。本批次不实装映射, 只在常量里把这条禁令钉住, 由
`test_health_reason_catalog_excludes_pass_codes` 静态守着。

为什么单独列出来而不是只在注释里写一句: 注释没人扫, 常量会被测例扫。
"""

HEALTH_VETO_DATA_ABNORMAL_CODE: Final = "data_abnormal"
"""数据异常时应映射到的码(与"通过"分列)。本批次只钉名字, 不产出。"""

HEALTH_REASON_CATALOG_STATUS: Final = "draft"
"""原因码目录本身的状态: `draft`。定稿需随 `rule_catalog` 一起评审。"""


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
    reason_codes: tuple[str, ...] = ()
    """本次命中的原因码。当前**恒为空** —— 没有规则就没有命中。

    空元组表示"没有命中任何规则", 在当前实现里等价于"不知道有没有命中"。
    之所以不用 `None`: 调用方遍历它时不该需要判空, 而"命中集合为空"在
    `hit=None` 的语境下本来就是事实(没有规则可命中)。
    """
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def blocks_publish(self) -> bool | None:
        """是否应当阻止发布。

        返回 `None`(未知), **不是** False。调用方如果把它当布尔用, `None` 是假值,
        会走"不阻止"分支 —— 这与"未知"的语义相符, 而且不会给出任何"已通过"的错觉。
        真要放行时, 调用方应显式写 `if self.blocks_publish is False: 放行`;
        而本模块永不返回 False, 所以那句话当前永远不会成立。
        """
        return self.health_veto_hit

    @property
    def passed(self) -> bool:
        """是否**确认健康通过**。只有 `health_veto_hit is False` 才算。

        刻意写成 `is False` 而不是 `not self.health_veto_hit`: 后者会把 `None`
        读成通过, 那正是本模块唯一想防的误用。当前恒为 `False`。
        """
        return self.health_veto_hit is False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asof": self.asof.isoformat(),
            "health_veto_hit": self.health_veto_hit,
            "health_veto_rule_version": self.health_veto_rule_version,
            "tested_status": self.tested_status,
            "reason": self.reason,
            "missing_inputs": list(self.missing_inputs),
            "reason_codes": list(self.reason_codes),
            "passed": self.passed,
        }


def health_veto_asof(symbol: str, asof: date | datetime) -> HealthVetoStub:
    """查某只票在 `asof` 的健康 veto。当前恒返回未知。

    签名与 `sw_pit.industry_asof` / `valuation_stub.valuation_asof` 一致。

    本函数**不读盘、不联网、不看任何财务表**。它没有任何规则可执行, 所以结构上
    不可能返回 `True` 或 `False` —— 两个值都需要"执行过规则"这个前提。
    """
    return HealthVetoStub(symbol=symbol, asof=asof_date(asof))


__all__ = [
    "HEALTH_REASON_CATALOG_STATUS",
    "HEALTH_RULE_DESIGN_REQUIRED",
    "HEALTH_RULE_VERSION_PENDING",
    "HEALTH_TESTED_STATUS",
    "HEALTH_VETO_DATA_ABNORMAL_CODE",
    "HEALTH_VETO_FORBIDDEN_PASS_CODES",
    "HEALTH_VETO_REASON_CATALOG_DRAFT",
    "HealthVetoStub",
    "health_veto_asof",
]
