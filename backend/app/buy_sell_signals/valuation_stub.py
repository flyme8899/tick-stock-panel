"""估值接口桩: 明确返回 `data_pending`, 不假装有 PE / PB 表。

## 为什么写成桩而不是"顺手用 financials_metrics 里的 pe"

`financials_metrics` 里确实出现过 `pe` / `pe_ttm` 这类列(见 `services/picker.py`),
但那是**选股器在某个时点算出来的快照**, 不是逐日估值序列:

- 它没有随日线滚动的价格, 只有算那一刻的价格
- 它没有 `announce_date` 与 `price_date` 的配对关系, 无法回答"2022 年 6 月 1 日
  收盘时市场给的 PE 是多少"

拿它当估值 PIT, 得到的是一条"看起来有数、其实只在几个孤立时点成立"的序列。这类
数据的危险之处在于它**不会报错** —— 回测能跑完, 只是每个数字都对不上。

所以这里返回 `data_pending=True`, `v_score=None`, 并在 `reason` 里写清缺什么。
等真有逐日估值表时, 替换本模块的实现, 接口形状不用变。

**估值门禁未测通。** 本模块不含任何阈值判定。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

from .models import asof_date

VALUATION_STATUS: Final = "data_pending"
"""估值数据状态。**不要**在没接真实逐日估值表之前把它改成 ready。"""

VALUATION_REQUIRED_INPUTS: Final = (
    "daily_close_series",       # 与交易日对齐的收盘价
    "shares_outstanding_pit",   # 时点股本(送转/增发会影响)
    "eps_ttm_pit",              # 按 announce_date 已知的 TTM EPS
    "bps_pit",                  # 按 announce_date 已知的每股净资产
)
"""估值 PIT 需要的最小输入集。缺任何一项都算 `data_pending`。

写在这里是为了让"什么时候可以去掉这个桩"有个客观标准, 而不是靠感觉。
"""


@dataclass(frozen=True, slots=True)
class ValuationStub:
    """一次估值查询的结果(当前恒为 pending)。"""

    symbol: str
    asof: date
    v_score: float | None = None
    """估值分。**恒为 None** —— 没有数据就没有分, 不用 0 或中位数顶替。"""
    data_pending: bool = True
    status: str = VALUATION_STATUS
    reason: str = "逐日估值序列(PE/PB)尚未接入"
    missing_inputs: tuple[str, ...] = VALUATION_REQUIRED_INPUTS
    tested_status: str = "untested"
    """**未测通**。这个字段存在是为了让任何读代码的人一眼看到它, 而不是去猜。"""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asof": self.asof.isoformat(),
            "v_score": self.v_score,
            "data_pending": self.data_pending,
            "status": self.status,
            "reason": self.reason,
            "missing_inputs": list(self.missing_inputs),
            "tested_status": self.tested_status,
        }


def valuation_asof(symbol: str, asof: date | datetime) -> ValuationStub:
    """查某只票在 `asof` 的估值。当前恒返回 `data_pending`。

    签名与 `sw_pit.industry_asof` / `quality_proxy.evaluate_quality` 保持形状一致,
    将来换成真实实现时调用方不用改。
    """
    return ValuationStub(symbol=symbol, asof=asof_date(asof))


__all__ = [
    "VALUATION_REQUIRED_INPUTS",
    "VALUATION_STATUS",
    "ValuationStub",
    "valuation_asof",
]
