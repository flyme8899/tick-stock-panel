"""metrics_v2 统计函数 (P3) — Newey-West HAC t 值 / BH-FDR q 值 / DSR。

运行时零新增第三方依赖 (后端无 scipy/statsmodels), 全部 numpy 手写;
数值测试用固定黄金参考向量锁定 (tests/test_stats_v2.py)。

口径 (设计文档 factor-system-design.md §6):
- IC 序列因 h 日前瞻收益存在 h-1 阶移动平均自相关, 主口径 t 值取 NW HAC, 滞后 L=h。
- 多因子批量检验按 Benjamini-Hochberg 步进法控制 FDR。
- DSR (Deflated Sharpe Ratio, Bailey & Lopez de Prado 2014) 用于多重试验校正后的
  夏普显著性; 期望最大夏普 EM = sqrt(V[SR]) * ((1-gamma)Φ^-1(1-1/N) + gammaΦ^-1(1-1/(Ne)))
  其中 gamma 为欧拉-马歇罗尼常数。
"""
from __future__ import annotations

import math

import numpy as np

EULER_GAMMA = 0.5772156649015329


def _clean_values(values) -> np.ndarray:
    array = np.asarray([value for value in values if value is not None and np.isfinite(value)], dtype=float)
    return array


def newey_west_t(values, lag: int) -> tuple[float, float, float] | None:
    """Newey-West HAC 稳健 t 统计量 (Bartlett 核)。

    返回 (t值, 均值, NW标准误); 样本不足 (n <= lag+2) 或方差为零返回 None。
    """
    array = _clean_values(values)
    n = array.size
    if n <= lag + 2 or n < 3:
        return None
    mean = float(array.mean())
    centered = array - mean
    # 长方差 S = gamma0 + 2 Σ_l w_l gamma_l, w_l = 1 - l/(lag+1) (Bartlett)
    gamma = [float(np.dot(centered[: n - lag_i], centered[lag_i:]) / n) for lag_i in range(lag + 1)]
    long_variance = gamma[0]
    for lag_i in range(1, lag + 1):
        weight = 1.0 - lag_i / (lag + 1)
        long_variance += 2.0 * weight * gamma[lag_i]
    long_variance = max(long_variance, 0.0)
    nw_se = math.sqrt(long_variance / n)
    if nw_se == 0:
        return None
    return (mean - 0.0) / nw_se, mean, nw_se


def naive_t(values) -> float | None:
    array = _clean_values(values)
    n = array.size
    if n < 3:
        return None
    std = float(array.std(ddof=1))
    if std == 0:
        return None
    return float(array.mean()) / (std / math.sqrt(n))


def normal_two_sided_p(t_stat: float) -> float:
    """标准正态双侧 p 值: erfc(|t|/sqrt(2))。"""
    return math.erfc(abs(t_stat) / math.sqrt(2.0))


def bh_fdr_qvalues(pvalues: list[float | None]) -> list[float | None]:
    """Benjamini-Hochberg 步进法 q 值 (与输入等长, None 透传)。

    m 取可检验假设数 (None 不计入); q_i = min over j>=rank_i { p_j * m / rank_j },
    从大到小单调回填保证递增约束。
    """
    indexed = [
        (index, p) for index, p in enumerate(pvalues)
        if p is not None and np.isfinite(p)
    ]
    qvalues: list[float | None] = [None] * len(pvalues)
    if not indexed:
        return qvalues
    m = len(indexed)
    indexed.sort(key=lambda pair: pair[1])
    running_min = float("inf")
    for reverse_rank in range(len(indexed) - 1, -1, -1):
        index, p = indexed[reverse_rank]
        rank = reverse_rank + 1
        candidate = p * m / rank
        running_min = min(running_min, candidate)
        qvalues[index] = min(1.0, running_min)
    return qvalues


def _normal_ppf(probability: float) -> float:
    """标准正态分位数 Acklam 逆逼近 (相对误差 < 1.15e-9), 零依赖替代 scipy.stats.norm.ppf。"""
    if not (0.0 < probability < 1.0):
        raise ValueError("probability 必须在 (0,1) 开区间")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    p_low, p_high = 0.02425, 1 - 0.02425
    if probability < p_low:
        q_value = math.sqrt(-2 * math.log(probability))
        return (((((c[0] * q_value + c[1]) * q_value + c[2]) * q_value + c[3]) * q_value + c[4]) * q_value + c[5]) / \
               ((((d[0] * q_value + d[1]) * q_value + d[2]) * q_value + d[3]) * q_value + 1)
    if probability <= p_high:
        q_value = probability - 0.5
        r = q_value * q_value
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q_value / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q_value = math.sqrt(-2 * math.log(1 - probability))
    return -(((((c[0] * q_value + c[1]) * q_value + c[2]) * q_value + c[3]) * q_value + c[4]) * q_value + c[5]) / \
        ((((d[0] * q_value + d[1]) * q_value + d[2]) * q_value + d[3]) * q_value + 1)


def expected_max_sharpe(n_trials: int, variance_sharpes: float) -> float:
    """N 次独立试验的期望最大夏普 EM (方差>0 时); 单次试验不校正。"""
    if n_trials <= 1 or variance_sharpes <= 0:
        return 0.0
    z1 = _normal_ppf(1.0 - 1.0 / n_trials)
    z2 = _normal_ppf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(variance_sharpes) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def ulcer_index(equity: np.ndarray) -> float | None:
    """Ulcer Index: 回撤比例 (负数或 0) 的均方根。

    与最大回撤同一比例口径 (0.05 = 5%), 不是百分数点数。权益不足 2 点时返回 None。
    """
    values = np.asarray(equity, dtype=float)
    values = values[np.isfinite(values) & (values > 0)]
    if values.size < 2:
        return None
    peaks = np.maximum.accumulate(values)
    drawdown = values / peaks - 1.0
    return float(np.sqrt(np.mean(np.square(drawdown))))


def sqn(trade_returns: np.ndarray) -> float | None:
    """系统质量数 SQN = sqrt(N) * mean(R) / std(R), R 为每笔收益率 (不是 R 倍数)。

    没有把 N 截断到 100, 样本长度差很多时数值不可比。
    样本不足 2 笔或标准差为 0 时无定义。部分成交必须先合并成一笔逻辑交易再传入。
    """
    returns = np.asarray(trade_returns, dtype=float)
    returns = returns[np.isfinite(returns)]
    n = returns.size
    if n < 2:
        return None
    std = float(returns.std(ddof=1))
    if std == 0:
        return None
    return float(math.sqrt(n) * returns.mean() / std)


def kelly_fraction(trade_returns: np.ndarray) -> float | None:
    """凯利比例 f* = W - (1-W) / R, R = 平均盈利 / 平均亏损绝对值。

    收益恰好为 0 的交易不计入胜率分母, 也不算亏损 (否则会稀释平均亏损、压低胜率)。
    没有亏损样本或没有盈利样本时赔率无定义, 返回 None (不把空样本伪装成 0 或 1)。
    """
    returns = np.asarray(trade_returns, dtype=float)
    returns = returns[np.isfinite(returns)]
    returns = returns[returns != 0]
    if returns.size == 0:
        return None
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    if wins.size == 0 or losses.size == 0:
        return None
    avg_loss = abs(float(losses.mean()))
    if avg_loss == 0:
        return None
    win_rate = wins.size / returns.size
    payoff = float(wins.mean()) / avg_loss
    if payoff == 0:
        return None
    return float(win_rate - (1.0 - win_rate) / payoff)


# 5% 分位在样本很小时只落到一两个点, 20 笔以下不报告。
CVAR_MIN_SAMPLES = 20


def cvar_95(returns: np.ndarray) -> float | None:
    """历史 CVaR 95%: 不超过 5% 分位的收益率均值 (带符号, 亏损为负)。

    分位用线性插值。样本不足 20 个返回 None。调用方必须保证传入的是同一口径
    (日收益或逐笔收益), 不要把两种序列混进同一个字段。
    """
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    if values.size < CVAR_MIN_SAMPLES:
        return None
    threshold = float(np.quantile(values, 0.05, method="linear"))
    tail = values[values <= threshold + 1e-15]
    if tail.size == 0:
        return None
    return float(tail.mean())


def benchmark_relative_metrics(
    strategy_returns: np.ndarray,
    benchmark_returns: np.ndarray,
    *,
    periods_per_year: int = 252,
) -> dict[str, float | None]:
    """跟踪误差、信息比率、beta。输入必须已经按同一交易日对齐。

    跟踪误差 = std(策略-基准) * sqrt(252), 样本标准差 (ddof=1)。
    信息比率 = 年化超额均值 / 跟踪误差。基准方差为 0 时 beta 为 None。
    对齐样本不足 3 个交易日时三项都为 None。
    """
    strategy = np.asarray(strategy_returns, dtype=float)
    benchmark = np.asarray(benchmark_returns, dtype=float)
    empty = {"tracking_error": None, "information_ratio": None, "beta": None}
    if strategy.shape != benchmark.shape or strategy.ndim != 1:
        return empty
    valid = np.isfinite(strategy) & np.isfinite(benchmark)
    strategy = strategy[valid]
    benchmark = benchmark[valid]
    if strategy.size < 3:
        return empty
    excess = strategy - benchmark
    tracking = float(excess.std(ddof=1))
    annual_tracking = tracking * math.sqrt(periods_per_year)
    variance = float(benchmark.var(ddof=1))
    beta = None
    if variance > 0:
        covariance = float(np.cov(strategy, benchmark, ddof=1)[0, 1])
        beta = covariance / variance
    information = None
    if annual_tracking > 0:
        information = float(excess.mean() * math.sqrt(periods_per_year) / tracking) if tracking > 0 else None
    return {
        "tracking_error": annual_tracking,
        "information_ratio": information,
        "beta": beta,
    }


def align_benchmark_returns(
    equity_rows: list[dict],
    benchmark_rows: list[dict],
) -> tuple[np.ndarray, np.ndarray]:
    """按权益曲线日期对齐简单收益率。

    策略收益用相邻权益点; 基准收益用同一对日期的收盘价。基准缺任一端则丢掉该对,
    不用前值填充 (缺数据不假装连续)。
    """
    equity = []
    for row in equity_rows:
        date = str(row.get("date", ""))[:10]
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            continue
        if date and math.isfinite(value) and value > 0:
            equity.append((date, value))
    benchmark: dict[str, float] = {}
    for row in benchmark_rows:
        date = str(row.get("date", ""))[:10]
        raw = row.get("close", row.get("value"))
        try:
            close = float(raw)
        except (TypeError, ValueError):
            continue
        if date and math.isfinite(close) and close > 0:
            benchmark[date] = close
    strategy_returns: list[float] = []
    benchmark_returns: list[float] = []
    for index in range(1, len(equity)):
        prev_date, prev_equity = equity[index - 1]
        date, value = equity[index]
        prev_bench = benchmark.get(prev_date)
        bench = benchmark.get(date)
        if prev_bench is None or bench is None or prev_equity <= 0 or prev_bench <= 0:
            continue
        strategy_returns.append(value / prev_equity - 1.0)
        benchmark_returns.append(bench / prev_bench - 1.0)
    return np.asarray(strategy_returns, dtype=float), np.asarray(benchmark_returns, dtype=float)


def deflated_sharpe_psr(
    sharpe: float,
    n_obs: int,
    skewness: float | None = None,
    kurtosis: float | None = None,
    expected_max_sharpe: float = 0.0,
) -> float | None:
    """Deflated Sharpe (PSR 对 EM 校正) 概率; 参数不足或退化返回 None。

    PSR = Φ( (SR - SR*) * sqrt(n-1) / sqrt(1 - gamma3 SR + (gamma4-1)/4 SR^2) )
    """
    if n_obs < 5 or not np.isfinite(sharpe):
        return None
    skewness = 0.0 if skewness is None else skewness
    kurtosis = 3.0 if kurtosis is None else kurtosis
    denominator = 1.0 - skewness * sharpe + (kurtosis - 1.0) / 4.0 * sharpe * sharpe
    if denominator <= 0:
        return None
    statistic = (sharpe - expected_max_sharpe) * math.sqrt(n_obs - 1) / math.sqrt(denominator)
    return 0.5 * (1.0 + math.erf(statistic / math.sqrt(2.0)))
