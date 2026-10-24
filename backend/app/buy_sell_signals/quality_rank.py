"""申万一级行业内的质量相对分位 + 白名单试跑判定(批次 20261025g2 ②)。

## 这一层回答什么

`quality_proxy` 给的是**绝对**的 `q_score`(满足几项财务条件, 0–5)。绝对分的毛病
是它没有基准: 一个 q_score=3 的银行和一个 q_score=3 的计算机, 前者在银行业可能
是中上, 后者在计算机里可能是垫底。

这一层补的是**同日、同申万一级行业内的相对位置** —— 只在同一天、同一 L1 内部比较,
不做跨行业、不做跨日。跨行业比较只有在行业间可比性被验证之后才成立, 而那件事本
批次没做。

## 约定(写死, 不留想象空间)

- `q_rank_within_ind` ∈ [0, 1], 语义为**百分位, 越高越好**: 0 是本行业最低, 1 是
  最高。同分并列时取**平均秩**(competition ranking 的 average 变体)。
- 只有「有 q_score 且 q_missing_flag 为 False 且行业已知」的票才**有 rank 资格**。
  其余票 `q_rank_within_ind=None` + `q_missing=True` + 原因码, **绝不填 0** ——
  0 是一个真实的百分位(行业垫底), 填 0 等于把它们当成最差的那批。
- 单票行业(本行业只有这一只有资格)时 rank 无法定义 → 同样按 missing 处理,
  原因 `singleton_industry`。只有一只票的"行业分位"恒为 1, 那个 1 是算术产物, 不是
  相对位置。

## 白名单试跑: 未锁定

`WHITELIST_PERCENTILE_TRIAL = "p40"` 表示**试跑**阈值, 判定式为
`q_rank_within_ind >= 0.40`(≥, 闭区间)。之所以反复强调是"试跑":

- 没有做过 p30/p50 敏感性, 所以不能说 0.40 比 0.30 或 0.50 好;
- `WHITELIST_PERCENTILE_LOCKED` 恒为 `None`, 且**没有任何代码路径**能把它写成非空;
- 判定结果里带 `whitelist_threshold_locked=False`, 下游即便只读一个字段也能看出来。

如果将来真要做敏感性, 那条线也不是"改一个常量": 它要求先有成本实测与样本外验证。

## 与其它包的关系

只吃 `sw_pit.IndustryPit` 与 `quality_proxy.QualityProxy` 的形状, 不读盘、不联网、
不 import `t_rounds` / `trade_marks` / `news.push`。行业来源**必须**由调用方通过
`industry_asof` 拿到的 `IndustryPit` 传进来 —— 本模块不提供任何"拿当前快照顶上"的
后门。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Iterable, Mapping

from .models import asof_date
from .quality_proxy import QUALITY_WEIGHT_STATUS, QualityProxy

WHITELIST_PERCENTILE_TRIAL: Final = "p40"
"""白名单**试跑**阈值。`p40` 即百分位 40。

未锁定。见模块 docstring 与 `WHITELIST_PERCENTILE_LOCKED`。
"""

WHITELIST_PERCENTILE_LOCKED: Final = None
"""锁定的白名单阈值。**恒为 None**。

本批次没有跑过 p30/p40/p50 敏感性, 也没有样本外验证, 因此没有任何阈值可以被称作
"锁定"。把这里改成 `"p40"` 不是一次配置调整, 是一次没有依据的结论声明。
"""

WHITELIST_TRIAL_QUANTILE: Final = 0.40
"""由 `WHITELIST_PERCENTILE_TRIAL` 解析出的数值, 判定用 `>=`。

单独写出来是为了让不等式方向只有一处定义。与 `WHITELIST_PERCENTILE_TRIAL` 的一致
性由 `test_trial_quantile_matches_trial_label` 守着。
"""

WHITELIST_THRESHOLD_LOCKED: Final = WHITELIST_PERCENTILE_LOCKED is not None
"""派生常量: 阈值是否已锁定。当前恒 False —— 即从未锁定过。"""

RANK_MISSING_REASONS: Final = frozenset(
    {
        "no_industry",     # 没有申万一级归属
        "no_q_score",      # 没算出质量分
        "q_missing",       # 质量代理自身标了缺失
        "singleton_industry",  # 本行业只有这一只有资格的票, 相对位置无从谈起
    }
)
"""`q_rank_within_ind` 为 None 的全部原因码。"""


class QualityRankError(ValueError):
    """输入形状不对(重复 symbol / 行业码为空串之类)。"""


@dataclass(frozen=True, slots=True)
class IndustryRankEntry:
    """一只票在 `asof` 的行业相对分位结果。"""

    symbol: str
    asof: date | None
    industry_code_sw_l1: str | None = None
    q_score: int | None = None
    q_rank_within_ind: float | None = None
    """行业内百分位 ∈ [0, 1], 越高越好。None 表示没有 rank 资格。"""
    q_rank_missing: bool = True
    q_rank_missing_reason: str | None = None
    industry_n_ranked: int = 0
    """本行业内有 rank 资格的票数(含自己)。缺失时为 0。"""
    whitelist_pass: bool = False
    """是否通过**试跑**阈值。只有有 rank 的票才可能为 True。"""
    whitelist_percentile_trial: str = WHITELIST_PERCENTILE_TRIAL
    whitelist_threshold_locked: bool = WHITELIST_THRESHOLD_LOCKED
    w_quality: float | None = None
    weighting_status: str = QUALITY_WEIGHT_STATUS

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asof": self.asof.isoformat() if hasattr(self.asof, "isoformat") else str(self.asof),
            "industry_code_sw_l1": self.industry_code_sw_l1,
            "q_score": self.q_score,
            "q_rank_within_ind": self.q_rank_within_ind,
            "q_rank_missing": self.q_rank_missing,
            "q_rank_missing_reason": self.q_rank_missing_reason,
            "industry_n_ranked": self.industry_n_ranked,
            "whitelist_pass": self.whitelist_pass,
            "whitelist_percentile_trial": self.whitelist_percentile_trial,
            "whitelist_percentile_locked": WHITELIST_PERCENTILE_LOCKED,
            "whitelist_threshold_locked": self.whitelist_threshold_locked,
            "w_quality": self.w_quality,
            "weighting_status": self.weighting_status,
        }


@dataclass(frozen=True, slots=True)
class IndustryRankResult:
    """一次批量分位计算的结果。"""

    asof: date | None
    entries: Mapping[str, IndustryRankEntry] = field(default_factory=dict)

    @property
    def whitelist_symbols(self) -> tuple[str, ...]:
        """通过试跑阈值的票, 按 symbol 排序 —— 便于测试与日志比对。"""
        return tuple(sorted(sym for sym, e in self.entries.items() if e.whitelist_pass))

    @property
    def missing_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(sym for sym, e in self.entries.items() if e.q_rank_missing))

    def to_dict(self) -> dict[str, Any]:
        return {
            "asof": self.asof.isoformat() if hasattr(self.asof, "isoformat") else str(self.asof),
            "whitelist_percentile_trial": WHITELIST_PERCENTILE_TRIAL,
            "whitelist_percentile_locked": WHITELIST_PERCENTILE_LOCKED,
            "whitelist_threshold_locked": WHITELIST_THRESHOLD_LOCKED,
            "n_ranked": sum(1 for e in self.entries.values() if not e.q_rank_missing),
            "n_missing": sum(1 for e in self.entries.values() if e.q_rank_missing),
            "n_whitelist_trial": len(self.whitelist_symbols),
            "entries": {sym: e.to_dict() for sym, e in self.entries.items()},
        }


# ---------------------------------------------------------------------------
# 百分位
# ---------------------------------------------------------------------------


def _average_ranks(values: list[float]) -> list[float]:
    """返回与 `values` 等长的秩(1 起), 同分取平均秩。

    例: [10, 20, 20, 30] → [1, 2.5, 2.5, 4]。
    写这一步而不是直接 `enumerate(sorted(...))`, 是因为并列在企业财务里很常见
    (大量公司 gross_margin 恰好落在同一档), 不平均的话同一个分数会拿到不同的分位,
    取决于它们在输入里的先后顺序。
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        # 秩从 1 起, 区间 [i, j] 的平均秩
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def percentile_rank(values: list[float]) -> list[float]:
    """把一组值映射到 [0, 1] 的百分位, 越高越好, 同分平均秩。

    公式 `(avg_rank - 1) / (n - 1)`:

    - 最小值 → 0, 最大值 → 1(两端都取到, 落在闭区间内)
    - 全部同分 → 全为 0.5(而不是 0 或 1 —— 谁也没排在谁前面)
    - `n == 1` → 调用方应判为 singleton, 这里返回 [0.5] 只是不让它炸

    之所以不用 `rank / n`: 那个公式下最小值拿到 `1/n` 而不是 0, 单票时恒为 1,
    下游按 `>= 0.40` 判定会得到"小行业里的票天然更容易过"的偏差。
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    ranks = _average_ranks(values)
    return [(r - 1.0) / (n - 1.0) for r in ranks]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _q_eligible(proxy: QualityProxy) -> tuple[bool, str | None]:
    """判断一只票有没有 rank 资格。返回 (是否有资格, 原因码)。"""
    if proxy.q_score is None:
        return False, "no_q_score"
    if proxy.q_missing_flag:
        return False, "q_missing"
    if not math.isfinite(float(proxy.q_score)):
        return False, "no_q_score"
    return True, None


def rank_within_industry(
    proxies: Iterable[QualityProxy],
    industries: Mapping[str, Any],
    *,
    asof: date | None = None,
) -> IndustryRankResult:
    """按同日同申万一级行业算质量相对分位。

    `industries` 的值可以是 `sw_pit.IndustryPit`, 也可以是裸的行业代码字符串 ——
    只取 `industry_code_sw_l1` 这一个信息, 不关心它怎么来的, 但**拒绝空串**:
    空串会被当成一个合法行业码, 把一堆没有归属的票聚成一组互相比较。

    缺行业 / 缺分 / 质量缺失 → 该票 missing, 不参与别人的分母, 自己也不填 0。

    本函数**不修改** `w_quality` / `w_value`, 也不会因为进了白名单就改
    `weighting_status`。白名单试跑只是一张名单, 不是权重。
    """
    proxy_list = list(proxies)
    seen: dict[str, QualityProxy] = {}
    for proxy in proxy_list:
        if proxy.symbol in seen:
            raise QualityRankError(f"symbol 重复: {proxy.symbol!r}")
        seen[proxy.symbol] = proxy

    resolved_asof = asof_date(asof) if asof is not None else None
    if resolved_asof is None and proxy_list:
        resolved_asof = proxy_list[0].asof

    # 第一步: 定行业 + 判资格。分组只在"有资格"的票之间做。
    buckets: dict[str, list[tuple[str, QualityProxy]]] = {}
    prelim: dict[str, tuple[str | None, bool, str | None]] = {}
    for proxy in proxy_list:
        if resolved_asof is not None and proxy.asof != resolved_asof:
            # 跨日混算会让"同一天内的相对位置"这个语义失效, 而且很难在结果里发现。
            raise QualityRankError(
                f"{proxy.symbol!r} 的 asof={proxy.asof} 与本次计算的 asof={resolved_asof} 不一致"
            )
        industry = _industry_code(industries.get(proxy.symbol))
        eligible, reason = _q_eligible(proxy)
        if industry is None:
            eligible, reason = False, "no_industry"
        prelim[proxy.symbol] = (industry, eligible, reason)
        if eligible and industry is not None:
            buckets.setdefault(industry, []).append((proxy.symbol, proxy))

    # 第二步: 行业内排序 → 百分位。单票行业按 singleton 处理。
    ranks: dict[str, float] = {}
    sizes: dict[str, int] = {}
    for industry, members in buckets.items():
        sizes[industry] = len(members)
        if len(members) < 2:
            continue
        values = [float(p.q_score) for _, p in members]  # type: ignore[arg-type]
        for (symbol, _), pct in zip(members, percentile_rank(values)):
            ranks[symbol] = pct

    entries: dict[str, IndustryRankEntry] = {}
    for proxy in proxy_list:
        industry, eligible, reason = prelim[proxy.symbol]
        if eligible and industry is not None and industry not in ranks:
            # 唯一成员
            eligible, reason = False, "singleton_industry"
        pct = ranks.get(proxy.symbol)
        passed = pct is not None and pct >= WHITELIST_TRIAL_QUANTILE
        entries[proxy.symbol] = IndustryRankEntry(
            symbol=proxy.symbol,
            asof=resolved_asof,
            industry_code_sw_l1=industry,
            q_score=proxy.q_score,
            q_rank_within_ind=pct,
            q_rank_missing=pct is None,
            q_rank_missing_reason=None if pct is not None else reason,
            industry_n_ranked=sizes.get(industry, 0) if industry is not None else 0,
            whitelist_pass=passed,
        )

    return IndustryRankResult(asof=resolved_asof, entries=entries)


def _industry_code(value: Any) -> str | None:
    """从 `IndustryPit` 或裸字符串里取出申万一级代码。取不到返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        code = value.strip()
        return code or None
    code = getattr(value, "industry_code_sw_l1", None)
    if isinstance(code, str) and code.strip():
        return code.strip()
    return None


__all__ = [
    "IndustryRankEntry",
    "IndustryRankResult",
    "QualityRankError",
    "RANK_MISSING_REASONS",
    "WHITELIST_PERCENTILE_LOCKED",
    "WHITELIST_PERCENTILE_TRIAL",
    "WHITELIST_THRESHOLD_LOCKED",
    "WHITELIST_TRIAL_QUANTILE",
    "percentile_rank",
    "rank_within_industry",
]
