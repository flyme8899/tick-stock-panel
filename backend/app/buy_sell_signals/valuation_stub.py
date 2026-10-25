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

## 批次 20261025gval: 契约加固, 不是实装

本批次**没有**接逐日 PE/PB 表, 所以这里做的只是把契约钉得更死:

- `VALUATION_REQUIRED_INPUTS` 四项写死可见, 缺任何一项 → `data_pending`;
- `v_score` **恒为 None**, 不填 0 / 中位数 / 任何"看着像分"的数;
- `tested_status` 恒 `untested`;
- 新增 `VALUATION_SNAPSHOT_PE_FORBIDDEN`, 让"禁快照 pe"这条从注释变成可被测例
  扫描的**符号**。

### `None` 与 0 的区别, 在估值上尤其致命

PE 是一个**除法**, 亏损公司的 PE 是负的, 极端情况下还会被算成 0 或一个巨大的数。
所以"缺数据填 0"在这里不只是"少了一个数据点" —— 它会被读成"这家公司极度便宜",
正好是估值门禁最想拦住的那一类。宁可整条腿 pending, 也不要一个方向都搞错的分。

**估值门禁未测通。** 本模块不含任何阈值判定, 也不产出任何可用于排序的分数。

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

四个缺一不可, 各自对应一个具体的坑:

- `daily_close_series` —— 没有它, 算出来的是"公告那天的估值", 不是"这天的估值";
- `shares_outstanding_pit` —— 送转/增发后股本变了, 用期末股本回算历史 EPS 会失真;
- `eps_ttm_pit` —— 必须按 `announce_date` 判可知, 不能按 `period_end`(见 `quality_proxy`);
- `bps_pit` —— 同上; 且净资产被追溯调整过时, 只有带公告日的那一版才可信。
"""

VALUATION_SNAPSHOT_PE_FORBIDDEN: Final = True
"""**禁止**用 picker / `selection_fields` / `financials_metrics` 的当日 `pe` / `pe_ttm`
快照充当历史估值 PIT。

这个常量恒为 `True`, 没有任何开关作用 —— 它的存在只是为了让那条禁令有个可被
静态测例扫描的落点。把它改成 `False` 不是"打开一个选项", 是取消这条禁令本身。

理由见模块 docstring: 快照没有 price_date × announce_date 的配对, 拿它做 PIT 会
得到一条"能跑完、每个数字都对不上"的序列, 而且不报错。
"""

VALUATION_SNAPSHOT_SOURCE_PREFIXES: Final = ("pe", "pb", "ps")
"""快照口径的列名前缀。凡以这些开头的列都属于"当日快照", 不得进本模块。"""


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

    @property
    def score_emitted(self) -> bool:
        """是否产出了估值分。桩阶段恒 `False` —— 下游据此判断"这条能不能算"。

        刻意与 `QualityProxy.score_emitted` 同名同义: 两条腿在"没分就是没分"
        这件事上口径必须一致, 否则调用方要记两套规则。
        """
        return self.v_score is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asof": self.asof.isoformat(),
            "v_score": self.v_score,
            "score_emitted": self.score_emitted,
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

    这个函数**不读盘、不联网、不看任何 DataFrame**。它拿不到价、也拿不到 EPS,
    所以结构上不可能产出分数 —— 这不是"忘了实现", 是"没有输入就没有输出"。
    """
    return ValuationStub(symbol=symbol, asof=asof_date(asof))


__all__ = [
    "VALUATION_REQUIRED_INPUTS",
    "VALUATION_SNAPSHOT_PE_FORBIDDEN",
    "VALUATION_SNAPSHOT_SOURCE_PREFIXES",
    "VALUATION_STATUS",
    "ValuationStub",
    "valuation_asof",
]
