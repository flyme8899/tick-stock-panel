# -*- coding: utf-8 -*-
"""ETF rotation engine (pure logic, no I/O).

Modes
-----
``blended_bucket`` (default):
    Score each ETF by the average rank of its 20/60/120-day returns (rank 1 is
    the highest return). A named bucket contributes only its best member.
    Hold the top buckets whose blended return is positive, size them by
    inverse 60-day volatility, and fill unused slots with the safe asset.
``equal_weight``:
    Hold the whole risk pool at equal weight and restore those weights on the
    monthly schedule.
``legacy``:
    The previous weekly single-lookback top-2 rule, including the switch
    buffer and the "do not re-equalise unchanged holdings" rule.

Every mode signals on the close and trades on the next close
(``EXECUTION_LAG_DAYS``). A bar never decides a trade that earns that same
bar's return. Portfolio drawdown risk-off is a hook and stays off unless
``drawdown_risk_off`` is set.

The strategy and the benchmark (equal-weight risk pool, daily rebalanced, no
costs) are both measured from the first execution day, so neither curve
carries a leading all-cash stretch the other does not have.

All inputs are wide close-price frames: index = trading date, columns = codes.
Prices are expected to be forward-adjusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
import logging
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

REBALANCE_CHOICES = ("weekly", "monthly")
MODE_CHOICES = ("blended_bucket", "equal_weight", "legacy")
WEIGHTING_CHOICES = ("inv_vol", "equal")
CASH = "CASH"
EXECUTION_LAG_DAYS = 1
TRADING_DAYS_PER_YEAR = 244
DAYS_PER_YEAR = 365.25
MAX_FORWARD_FILL_DAYS = 5
DEFAULT_SWEEP_LOOKBACKS = (20, 40, 60, 90, 120, 150, 200)
DEFAULT_LOOKBACKS = (20, 60, 120)
VOL_LOOKBACK_DAYS = 60
DRAWDOWN_LIMIT = 0.15
DEFAULT_BUCKET_SPEC = "A股:510300|510500|159915;512890;513100;518880"
RESULT_PREFIX = "TSP_ETF_RESULT "
MODE_LABELS = {
    "blended_bucket": "分桶混合动量",
    "equal_weight": "等权",
    "legacy": "经典双动量",
}
DISCLAIMER = (
    "信号在收盘计算，下一交易日收盘才执行，不使用信号日之后的价格。"
    "规则结果不代表未来收益，不构成投资建议。组合回撤风控默认关闭。"
)

NextSessionFn = Callable[[date], Optional[date]]

Weights = Dict[str, float]


@dataclass(frozen=True)
class Bucket:
    """One slot in the blended universe. ``members`` compete; only the best is held."""

    name: str
    members: Tuple[str, ...]


@dataclass(frozen=True)
class RotationParams:
    lookback_days: int = 60
    rebalance: str = "monthly"
    top_n: int = 3
    switch_buffer_pct: float = 2.0
    cost_bps: float = 10.0  # one-way cost per unit of turnover
    mode: str = "blended_bucket"
    lookbacks: Tuple[int, ...] = DEFAULT_LOOKBACKS
    weighting: str = "inv_vol"
    buckets: Tuple[Bucket, ...] = ()
    drawdown_risk_off: bool = False
    drawdown_limit: float = DRAWDOWN_LIMIT

    def __post_init__(self) -> None:
        if self.lookback_days < 1:
            raise ValueError("lookback_days must be >= 1")
        if self.rebalance not in REBALANCE_CHOICES:
            raise ValueError(f"rebalance must be one of {REBALANCE_CHOICES}")
        if self.mode not in MODE_CHOICES:
            raise ValueError(f"mode must be one of {MODE_CHOICES}")
        if self.weighting not in WEIGHTING_CHOICES:
            raise ValueError(f"weighting must be one of {WEIGHTING_CHOICES}")
        if not self.lookbacks or any(days < 1 for days in self.lookbacks):
            raise ValueError("lookbacks must be positive")
        if self.top_n < 1:
            raise ValueError("top_n must be >= 1")
        if self.switch_buffer_pct < 0 or self.cost_bps < 0:
            raise ValueError("switch_buffer_pct and cost_bps must be >= 0")
        if not 0 < self.drawdown_limit < 1:
            raise ValueError("drawdown_limit must be between 0 and 1")


def parse_mode(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower()
    if not normalized:
        return "blended_bucket"
    if normalized in MODE_CHOICES:
        return normalized
    logger.warning("ETF_ROTATION_MODE=%r is invalid; falling back to blended_bucket", value)
    return "blended_bucket"


def parse_lookbacks(value: Optional[str]) -> Tuple[int, ...]:
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_LOOKBACKS
    days: List[int] = []
    for part in raw.replace(" ", ",").split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            parsed = int(piece)
        except ValueError:
            parsed = 0
        if parsed < 1 or parsed > 500:
            logger.warning("ETF_ROTATION_LOOKBACKS=%r is invalid; falling back to 20,60,120", value)
            return DEFAULT_LOOKBACKS
        days.append(parsed)
    if not days:
        return DEFAULT_LOOKBACKS
    return tuple(days)


def parse_weighting(value: Optional[str], mode: str) -> str:
    """Legacy and equal-weight ignore an explicit inverse-vol request."""
    if mode in ("legacy", "equal_weight"):
        return "equal"
    normalized = (value or "").strip().lower()
    if not normalized:
        return "inv_vol"
    if normalized in WEIGHTING_CHOICES:
        return normalized
    logger.warning("ETF_ROTATION_WEIGHTING=%r is invalid; falling back to inv_vol", value)
    return "inv_vol"


def parse_buckets(value: Optional[str]) -> Tuple[Bucket, ...]:
    """Parse ``A股:510300|510500;512890`` into buckets. Invalid text uses the default."""
    raw = (value or "").strip() or DEFAULT_BUCKET_SPEC
    try:
        parsed = _parse_bucket_spec(raw)
    except ValueError:
        logger.warning("ETF_ROTATION_BUCKETS=%r is invalid; falling back to the default", value)
        parsed = _parse_bucket_spec(DEFAULT_BUCKET_SPEC)
    return parsed


def _parse_bucket_spec(raw: str) -> Tuple[Bucket, ...]:
    buckets: List[Bucket] = []
    seen: set[str] = set()
    for part in raw.split(";"):
        piece = part.strip()
        if not piece:
            continue
        if ":" in piece:
            name, members_raw = piece.split(":", 1)
            label = name.strip()
            members = tuple(code.strip() for code in members_raw.split("|") if code.strip())
        else:
            label = piece
            members = (piece,)
        if not label or not members:
            raise ValueError("empty bucket")
        fresh = tuple(code for code in members if code not in seen)
        for code in fresh:
            seen.add(code)
        if not fresh:
            continue
        buckets.append(Bucket(label, fresh))
    if not buckets:
        raise ValueError("no buckets")
    return tuple(buckets)


@dataclass(frozen=True)
class Trade:
    signal_date: pd.Timestamp
    exec_date: pd.Timestamp
    from_weights: Weights
    to_weights: Weights
    turnover: float


@dataclass
class BacktestResult:
    equity: pd.Series
    benchmark_equity: pd.Series
    trades: List[Trade] = field(default_factory=list)
    final_weights: Weights = field(default_factory=dict)
    final_holdings: Tuple[str, ...] = ()
    # Internal equity at the last bar, before the published curve rebases the
    # entry point to 1. The drawdown hook reads these so a disabled hook never
    # changes the published series.
    equity_level: float = 1.0
    peak_level: float = 1.0


def prepare_closes(closes: pd.DataFrame) -> pd.DataFrame:
    """Sort by date and bridge short suspensions; pre-listing gaps stay NaN."""
    return _valid_closes(closes).ffill(limit=MAX_FORWARD_FILL_DAYS)


def _valid_closes(closes: pd.DataFrame) -> pd.DataFrame:
    frame = closes.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")].apply(pd.to_numeric, errors="coerce")
    return frame.replace([math.inf, -math.inf], math.nan).where(frame > 0)


def momentum_scores(closes: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    return closes / closes.shift(lookback_days) - 1.0


def _period_code(rebalance: str) -> str:
    if rebalance not in REBALANCE_CHOICES:
        raise ValueError(f"rebalance must be one of {REBALANCE_CHOICES}")
    return "W" if rebalance == "weekly" else "M"


def rebalance_dates(index: pd.DatetimeIndex, rebalance: str) -> pd.DatetimeIndex:
    """Last available trading date of every week or month in ``index``."""
    period = _period_code(rebalance)
    if len(index) == 0:
        return index
    keys = index.to_period(period)
    is_last = keys != keys.to_series().shift(-1).values
    return index[is_last]


def is_rebalance_day(day: pd.Timestamp, next_session: pd.Timestamp, rebalance: str) -> bool:
    """True when ``day`` is the last trading day of its week/month."""
    period = _period_code(rebalance)
    return pd.Timestamp(day).to_period(period) != pd.Timestamp(next_session).to_period(period)


def select_holdings(
    scores: pd.Series,
    current: Sequence[str],
    params: RotationParams,
) -> Tuple[str, ...]:
    """Pick up to ``top_n`` risk assets; an empty tuple means fully defensive."""
    eligible = scores.dropna()
    eligible = eligible[eligible > 0].sort_values(ascending=False)
    if eligible.empty:
        return ()

    slots = min(params.top_n, len(eligible))
    cutoff = eligible.iloc[slots - 1] - params.switch_buffer_pct / 100.0
    kept = [code for code in current if code in eligible.index and eligible[code] >= cutoff]
    kept = sorted(kept, key=lambda code: eligible[code], reverse=True)[:slots]
    newcomers = [code for code in eligible.index if code not in kept][: slots - len(kept)]
    chosen = kept + newcomers
    return tuple(sorted(chosen, key=lambda code: eligible[code], reverse=True))


def holdings_to_weights(
    holdings: Sequence[str],
    top_n: int,
    safe_asset: Optional[str],
) -> Weights:
    """Each slot is 1/top_n; unfilled slots go to the safe asset (or cash)."""
    slot = 1.0 / top_n
    weights: Weights = {code: slot for code in holdings}
    spare = 1.0 - slot * len(holdings)
    if spare > 1e-12:
        defensive = safe_asset or CASH
        weights[defensive] = weights.get(defensive, 0.0) + spare
    return weights


def _turnover(old: Weights, new: Weights) -> float:
    """Traded notional as a fraction of equity; the cash leg is not a trade."""
    codes = (set(old) | set(new)) - {CASH}
    return sum(abs(new.get(c, 0.0) - old.get(c, 0.0)) for c in codes)


def _drift(weights: Weights, returns: Dict[str, float]) -> Tuple[Weights, float]:
    """Apply one day of returns; return drifted weights and portfolio return."""
    grown = {c: w * (1.0 + returns.get(c, 0.0)) for c, w in weights.items()}
    total = sum(grown.values())
    port_ret = total - 1.0 if weights else 0.0
    if total <= 0:
        return {}, port_ret
    return {c: v / total for c, v in grown.items()}, port_ret


def _strict_mean(parts: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Average aligned frames. A missing lookback makes that cell NaN."""
    count = parts[0].notna().astype(int)
    total = parts[0].fillna(0.0)
    for part in parts[1:]:
        count = count.add(part.notna().astype(int), fill_value=0)
        total = total.add(part.fillna(0.0), fill_value=0.0)
    return (total / len(parts)).where(count == len(parts))


def _blended_matrices(
    closes: pd.DataFrame,
    codes: Sequence[str],
    lookbacks: Sequence[int],
    quoted: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    cols = [code for code in codes if code in closes.columns]
    frame = closes[cols]
    ranks = []
    returns = []
    for lookback in lookbacks:
        ret = frame / frame.shift(lookback) - 1.0
        returns.append(ret)
        ranks.append(ret.rank(axis=1, ascending=False, method="min"))
    visible = quoted.reindex(index=frame.index, columns=cols).notna()
    avg_rank = _strict_mean(ranks).where(visible)
    momentum = _strict_mean(returns).where(visible)
    return avg_rank, momentum


def _volatility(quoted: pd.DataFrame, codes: Sequence[str]) -> pd.DataFrame:
    cols = [code for code in codes if code in quoted.columns]
    returns = quoted[cols].pct_change(fill_method=None)
    return returns.rolling(VOL_LOOKBACK_DAYS, min_periods=VOL_LOOKBACK_DAYS).std(ddof=0)


def _bucket_members(params: RotationParams) -> Tuple[str, ...]:
    seen: List[str] = []
    for bucket in params.buckets:
        for code in bucket.members:
            if code not in seen:
                seen.append(code)
    return tuple(seen)


def _select_blended(
    rank_row: pd.Series,
    mom_row: pd.Series,
    params: RotationParams,
) -> List[Tuple[str, str, float]]:
    """Best member of each bucket, then the top positive-momentum buckets."""
    picked: List[Tuple[float, str, str]] = []
    for bucket in params.buckets:
        best: Optional[Tuple[float, str, float]] = None
        for code in bucket.members:
            if code not in rank_row.index:
                continue
            score = rank_row[code]
            momentum = mom_row[code]
            if pd.isna(score) or pd.isna(momentum):
                continue
            candidate = (float(score), code, float(momentum))
            if best is None or candidate < best:
                best = candidate
        if best is None or best[2] <= 0:
            continue
        picked.append((best[0], best[1], bucket.name))
    picked.sort(key=lambda item: (item[0], item[1]))
    return [(name, code, score) for score, code, name in picked[: params.top_n]]


def _risk_off_weights(
    weights: Weights,
    params: RotationParams,
    equity: float,
    peak: float,
    safe_asset: Optional[str],
    quoted_row: pd.Series,
) -> Tuple[Weights, bool]:
    """Disabled unless the hook is on. Uses only equity known at this close."""
    if not params.drawdown_risk_off or peak <= 0 or equity <= 0:
        return weights, False
    if equity / peak - 1.0 > -params.drawdown_limit:
        return weights, False
    defensive = safe_asset if safe_asset and pd.notna(quoted_row.get(safe_asset)) else None
    return {defensive or CASH: 1.0}, True


def _weights_with_spare(chosen: Sequence[str], raw: Sequence[float], top_n: int, spare_code: str) -> Weights:
    budget = len(chosen) / float(top_n)
    total = sum(raw)
    if total <= 0:
        slot = budget / len(chosen)
        weights = {code: slot for code in chosen}
    else:
        weights = {code: budget * (value / total) for code, value in zip(chosen, raw)}
    spare = 1.0 - sum(weights.values())
    if spare > 1e-8:
        weights[spare_code] = weights.get(spare_code, 0.0) + spare
    return weights


def signal_weights(
    closes: pd.DataFrame,
    quoted: pd.DataFrame,
    day: pd.Timestamp,
    params: RotationParams,
    risk_assets: Sequence[str],
    safe_asset: Optional[str],
    holdings: Sequence[str],
    equity: float,
    peak: float,
    avg_rank: Optional[pd.DataFrame] = None,
    momentum: Optional[pd.DataFrame] = None,
    vol: Optional[pd.DataFrame] = None,
) -> Tuple[Weights, Dict[str, float], Dict[str, str], str, bool]:
    """Target booked at ``day``'s close. Inputs on that row look backward only."""
    quoted_row = quoted.loc[day] if day in quoted.index else pd.Series(dtype=float)
    available_safe = safe_asset if safe_asset and pd.notna(quoted_row.get(safe_asset)) else None
    spare = available_safe or CASH
    score_map: Dict[str, float] = {}
    bucket_map: Dict[str, str] = {}
    score_kind = "none"

    if params.mode == "legacy":
        score_kind = "momentum"
        risk = [code for code in risk_assets if code in closes.columns]
        scores = momentum_scores(closes[risk], params.lookback_days)
        if not quoted.empty:
            scores = scores.where(quoted.reindex_like(scores).notna())
        row = scores.loc[day] if day in scores.index else pd.Series(dtype=float)
        chosen = select_holdings(row, holdings, params)
        weights = holdings_to_weights(chosen, params.top_n, available_safe)
        score_map = {code: float(row[code]) for code in row.index if pd.notna(row[code])}
    elif params.mode == "equal_weight":
        live = [code for code in risk_assets if code in quoted_row.index and pd.notna(quoted_row[code])]
        if not live:
            weights = {CASH: 1.0}
        else:
            slot = 1.0 / len(live)
            weights = {code: slot for code in live}
    else:
        score_kind = "avg_rank"
        members = _bucket_members(params)
        if avg_rank is None or momentum is None:
            avg_rank, momentum = _blended_matrices(closes, members, params.lookbacks, quoted)
        if vol is None and params.weighting == "inv_vol":
            vol = _volatility(quoted, members)
        rank_row = avg_rank.loc[day] if day in avg_rank.index else pd.Series(dtype=float)
        mom_row = momentum.loc[day] if day in momentum.index else pd.Series(dtype=float)
        selected = _select_blended(rank_row, mom_row, params)
        for _name, code, score in selected:
            score_map[code] = score
        for code in rank_row.index:
            if pd.notna(rank_row[code]) and code not in score_map:
                score_map[code] = float(rank_row[code])
        bucket_map = {code: bucket.name for bucket in params.buckets for code in bucket.members}
        bucket_map.update({code: name for name, code, _score in selected})
        chosen_codes = [code for _name, code, _score in selected]
        if not chosen_codes:
            weights = {spare: 1.0}
        elif params.weighting == "inv_vol":
            vol_row = vol.loc[day] if vol is not None and day in vol.index else pd.Series(dtype=float)
            raw: List[float] = []
            use_inv = True
            for code in chosen_codes:
                value = vol_row.get(code) if hasattr(vol_row, "get") else None
                if value is None or pd.isna(value) or float(value) <= 0:
                    use_inv = False
                    break
                raw.append(1.0 / float(value))
            if not use_inv:
                raw = [1.0] * len(chosen_codes)
            weights = _weights_with_spare(chosen_codes, raw, params.top_n, spare)
        else:
            weights = _weights_with_spare(chosen_codes, [1.0] * len(chosen_codes), params.top_n, spare)
        for code, weight in list(weights.items()):
            if code not in bucket_map and code not in (CASH, spare):
                bucket_map[code] = code
        if spare in weights and spare != CASH:
            bucket_map.setdefault(spare, "防守")

    weights, risk_off = _risk_off_weights(weights, params, equity, peak, available_safe, quoted_row)
    if risk_off and spare not in (None, ""):
        bucket_map = {spare: "防守"} if spare != CASH else {}
    return weights, score_map, bucket_map, score_kind, risk_off


def run_backtest(
    closes: pd.DataFrame,
    risk_assets: Sequence[str],
    safe_asset: Optional[str],
    params: RotationParams,
) -> BacktestResult:
    """Dispatch on ``params.mode``. Legacy stays on its original simulator."""
    if params.mode == "legacy":
        return _run_legacy_backtest(closes, risk_assets, safe_asset, params)
    return _run_mode_backtest(closes, risk_assets, safe_asset, params)


def _run_legacy_backtest(
    closes: pd.DataFrame,
    risk_assets: Sequence[str],
    safe_asset: Optional[str],
    params: RotationParams,
) -> BacktestResult:
    """Simulate the rotation; curves start on the first execution day.

    Signals start on the first day any risk score exists. Before the first
    order fills the portfolio is all cash (equity stays 1.0), so the returned
    curves begin at that fill with value 1.0; the entry cost shows up in the
    next bar's return. Without any fill the curves hold only the last bar.
    """
    risk = [c for c in risk_assets if c in closes.columns]
    if not risk:
        raise ValueError("no risk asset has price data")
    if safe_asset and safe_asset not in closes.columns:
        safe_asset = None

    frame = prepare_closes(closes)
    quoted = _valid_closes(closes)
    scores = momentum_scores(frame[risk], params.lookback_days).where(quoted[risk].notna())
    valid_rows = scores.notna().any(axis=1)
    if not valid_rows.any():
        raise ValueError(
            f"not enough history for lookback_days={params.lookback_days}"
        )
    start = valid_rows.idxmax()
    frame = frame.loc[start:]
    scores = scores.loc[start:]
    quoted = quoted.loc[start:]
    # Carry the last known mark for valuation, never for execution. On quote
    # recovery the full change from that mark belongs to existing holders.
    valuation = _valid_closes(closes).ffill().loc[start:]
    daily_returns = valuation.pct_change(fill_method=None).fillna(0.0)

    rebal = set(rebalance_dates(frame.index, params.rebalance))
    dates = list(frame.index)
    cost_rate = params.cost_bps / 10000.0

    weights: Weights = {CASH: 1.0}
    holdings: Tuple[str, ...] = ()
    pending: Optional[Tuple[int, pd.Timestamp, Tuple[str, ...]]] = None
    entry: Optional[int] = None
    trades: List[Trade] = []
    equity_values: List[float] = []
    bench_values: List[float] = []
    equity = 1.0
    bench = 1.0
    peak = 1.0

    for i, day in enumerate(dates):
        row = daily_returns.loc[day]
        if i > 0:
            weights, port_ret = _drift(weights, {c: row[c] for c in weights if c in row})
            equity *= 1.0 + port_ret
            peak = max(peak, equity)
            listed = [c for c in risk if pd.notna(valuation.loc[day, c]) and pd.notna(valuation[c].iloc[i - 1])]
            bench *= 1.0 + (float(row[listed].mean()) if listed else 0.0)

        if pending is not None and pending[0] == i:
            _, signal_day, new_holdings = pending
            pending = None
            available_safe = safe_asset if safe_asset and pd.notna(quoted.loc[day, safe_asset]) else None
            target = holdings_to_weights(new_holdings, params.top_n, available_safe)
            # Do not fabricate a buy or sell at a carried-forward price. Keep
            # the current portfolio until a later scheduled signal can trade.
            involved = (set(weights) | set(target)) - {CASH}
            can_trade = all(pd.notna(quoted.loc[day, code]) for code in involved)
            # Same holdings -> no trade; drifted weights are not re-equalised.
            if can_trade and (entry is None or set(target) != set(weights)):
                if entry is None:
                    entry = i
                turnover = _turnover(weights, target)
                if turnover > 1e-9:
                    equity *= 1.0 - turnover * cost_rate
                    trades.append(Trade(signal_day, day, dict(weights), target, turnover))
                weights, holdings = target, new_holdings

        if day in rebal and i + EXECUTION_LAG_DAYS < len(dates):
            chosen = select_holdings(scores.loc[day], holdings, params)
            if (
                params.drawdown_risk_off
                and peak > 0
                and equity > 0
                and equity / peak - 1.0 <= -params.drawdown_limit
            ):
                chosen = ()
            pending = (i + EXECUTION_LAG_DAYS, day, chosen)

        equity_values.append(equity)
        bench_values.append(bench)

    first = entry if entry is not None else len(dates) - 1
    index = frame.index[first:]
    # Equity is exactly 1.0 before the first fill (all cash), so the pre-cost
    # base is 1.0; the benchmark is rebased to the same day.
    strategy_values = [1.0] + equity_values[first + 1:]
    benchmark_values = [v / bench_values[first] for v in bench_values[first:]]
    return BacktestResult(
        equity=pd.Series(strategy_values, index=index, name="strategy"),
        benchmark_equity=pd.Series(benchmark_values, index=index, name="benchmark"),
        trades=trades,
        final_weights=weights,
        final_holdings=holdings,
        equity_level=equity,
        peak_level=peak,
    )


def _publish_curves(
    dates: Sequence[pd.Timestamp],
    entry: Optional[int],
    equity_values: Sequence[float],
    bench_values: Sequence[float],
    trades: List[Trade],
    weights: Weights,
    equity: float,
    peak: float,
) -> BacktestResult:
    first = entry if entry is not None else len(dates) - 1
    index = pd.DatetimeIndex(dates[first:])
    strategy_values = [1.0] + list(equity_values[first + 1:])
    base = bench_values[first] if bench_values[first] else 1.0
    benchmark_values = [value / base for value in bench_values[first:]]
    holdings = tuple(code for code in weights if code != CASH and weights[code] > 1e-8)
    return BacktestResult(
        equity=pd.Series(strategy_values, index=index, name="strategy"),
        benchmark_equity=pd.Series(benchmark_values, index=index, name="benchmark"),
        trades=trades,
        final_weights=weights,
        final_holdings=holdings,
        equity_level=equity,
        peak_level=peak,
    )


def _run_mode_backtest(
    closes: pd.DataFrame,
    risk_assets: Sequence[str],
    safe_asset: Optional[str],
    params: RotationParams,
) -> BacktestResult:
    """Bucket blend or equal weight. Same next-close fill as the legacy engine."""
    if params.mode == "blended_bucket" and not params.buckets:
        raise ValueError("blended_bucket requires ETF_ROTATION_BUCKETS")
    risk = list(_bucket_members(params) if params.mode == "blended_bucket" else risk_assets)
    risk = [code for code in risk if code in closes.columns]
    if not risk:
        raise ValueError("no risk asset has price data")
    if safe_asset and safe_asset not in closes.columns:
        safe_asset = None

    frame = prepare_closes(closes)
    quoted = _valid_closes(closes)
    members = risk
    avg_rank: Optional[pd.DataFrame] = None
    momentum: Optional[pd.DataFrame] = None
    vol_full = _volatility(quoted, members) if params.weighting == "inv_vol" else None
    if params.mode == "blended_bucket":
        avg_rank, momentum = _blended_matrices(frame, members, params.lookbacks, quoted)
        valid_rows = avg_rank.notna().any(axis=1)
        if not valid_rows.any():
            raise ValueError(f"not enough history for lookbacks={params.lookbacks}")
        start = valid_rows.idxmax()
    else:
        valid_rows = quoted[risk].notna().any(axis=1)
        if not valid_rows.any():
            raise ValueError("no risk asset has price data")
        start = valid_rows.idxmax()
    frame = frame.loc[start:]
    quoted = quoted.loc[start:]
    if avg_rank is not None and momentum is not None:
        avg_rank = avg_rank.loc[start:]
        momentum = momentum.loc[start:]
    vol = None if vol_full is None else vol_full.loc[start:]
    valuation = _valid_closes(closes).ffill().loc[start:]
    daily_returns = valuation.pct_change(fill_method=None).fillna(0.0)

    rebal = set(rebalance_dates(frame.index, params.rebalance))
    dates = list(frame.index)
    cost_rate = params.cost_bps / 10000.0
    weights: Weights = {CASH: 1.0}
    pending: Optional[Tuple[int, pd.Timestamp, Weights]] = None
    entry: Optional[int] = None
    trades: List[Trade] = []
    equity_values: List[float] = []
    bench_values: List[float] = []
    equity = 1.0
    bench = 1.0
    peak = 1.0

    for i, day in enumerate(dates):
        row = daily_returns.loc[day]
        if i > 0:
            weights, port_ret = _drift(weights, {code: row[code] for code in weights if code in row})
            equity *= 1.0 + port_ret
            listed = [code for code in risk if pd.notna(valuation.loc[day, code]) and pd.notna(valuation[code].iloc[i - 1])]
            bench *= 1.0 + (float(row[listed].mean()) if listed else 0.0)
            peak = max(peak, equity)

        if pending is not None and pending[0] == i:
            _, signal_day, target = pending
            pending = None
            involved = (set(weights) | set(target)) - {CASH}
            can_trade = all(code in quoted.columns and pd.notna(quoted.loc[day, code]) for code in involved)
            if can_trade and (entry is None or _turnover(weights, target) > 1e-6):
                if entry is None:
                    entry = i
                turnover = _turnover(weights, target)
                if turnover > 1e-9:
                    equity *= 1.0 - turnover * cost_rate
                    trades.append(Trade(signal_day, day, dict(weights), dict(target), turnover))
                weights = dict(target)

        if day in rebal and i + EXECUTION_LAG_DAYS < len(dates):
            target, _scores, _buckets, _kind, _risk_off = signal_weights(
                frame,
                quoted,
                day,
                params,
                risk,
                safe_asset,
                (),
                equity,
                peak,
                None if avg_rank is None else avg_rank,
                None if momentum is None else momentum,
                vol,
            )
            pending = (i + EXECUTION_LAG_DAYS, day, target)

        equity_values.append(equity)
        bench_values.append(bench)

    return _publish_curves(dates, entry, equity_values, bench_values, trades, weights, equity, peak)


def compute_metrics(equity: pd.Series) -> Dict[str, float]:
    """CAGR / vol / Sharpe (rf=0) / max drawdown / Calmar for an equity curve."""
    empty = {k: math.nan for k in ("total_return", "cagr", "ann_vol", "sharpe", "max_drawdown", "calmar")}
    series = equity.dropna()
    if len(series) < 2 or series.iloc[0] <= 0:
        return empty

    total_return = series.iloc[-1] / series.iloc[0] - 1.0
    years = (series.index[-1] - series.index[0]).days / DAYS_PER_YEAR
    cagr = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 and total_return > -1 else math.nan
    rets = series.pct_change(fill_method=None).dropna()
    ann_vol = float(rets.std(ddof=0) * math.sqrt(TRADING_DAYS_PER_YEAR))
    sharpe = float(rets.mean() * TRADING_DAYS_PER_YEAR / ann_vol) if ann_vol > 0 else math.nan
    max_drawdown = float((series / series.cummax() - 1.0).min())
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 and not math.isnan(cagr) else math.nan
    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "calmar": float(calmar),
    }


def annual_returns(equity: pd.Series) -> pd.Series:
    """Calendar-year returns; the first year is measured from the curve start."""
    series = equity.dropna()
    if series.empty:
        return pd.Series(dtype=float)
    year_end = series.groupby(series.index.year).last()
    prev = year_end.shift(1)
    prev.iloc[0] = series.iloc[0]
    return year_end / prev - 1.0


def parameter_sweep(
    closes: pd.DataFrame,
    risk_assets: Sequence[str],
    safe_asset: Optional[str],
    params: RotationParams,
    lookbacks: Sequence[int] = DEFAULT_SWEEP_LOOKBACKS,
    min_years: float = 0.0,
) -> List[Tuple[int, Dict[str, float]]]:
    """Re-run the backtest over several lookbacks to expose overfitting.

    The configured ``params.lookback_days`` is always included. Every run is
    evaluated from the latest start among the kept runs (normally the longest
    lookback), so the rows cover the same calendar window. Lookbacks whose own
    curve is shorter than ``min_years`` are dropped so they cannot shrink that
    common window below ``min_years``.
    """
    if params.mode != "legacy":
        return []
    frame = prepare_closes(closes)
    risk = [c for c in risk_assets if c in frame.columns]
    if not risk:
        return []

    runs: List[Tuple[int, BacktestResult]] = []
    for lookback in sorted(set(lookbacks) | {params.lookback_days}):
        swept = replace(params, lookback_days=lookback)
        try:
            runs.append((lookback, run_backtest(closes, risk, safe_asset, swept)))
        except ValueError:  # not enough history for this lookback
            continue
    last_day = frame.index[-1]
    runs = [
        (lookback, result) for lookback, result in runs
        if (last_day - result.equity.index[0]).days / DAYS_PER_YEAR >= min_years
    ]
    if not runs:
        return []
    common_start = max(result.equity.index[0] for _, result in runs)
    return [(lookback, compute_metrics(result.equity.loc[common_start:])) for lookback, result in runs]


def latest_ranking(
    closes: pd.DataFrame,
    risk_assets: Sequence[str],
    params: RotationParams,
) -> Tuple[pd.Timestamp, pd.Series]:
    """Momentum scores on the latest bar, sorted high to low (NaN last)."""
    frame = prepare_closes(closes)
    risk = [c for c in risk_assets if c in frame.columns]
    scores = momentum_scores(frame[risk], params.lookback_days)
    scores = scores.where(_valid_closes(closes)[risk].notna())
    as_of = frame.index[-1]
    return as_of, scores.loc[as_of].sort_values(ascending=False, na_position="last")


def _round_weight(value: float) -> float:
    return round(float(value), 6)


def _score_hint(params: RotationParams, score_kind: str) -> str:
    if score_kind == "avg_rank":
        days = "、".join(str(days) for days in params.lookbacks)
        return f"得分是 {days} 日收益的平均排名，越小越强。"
    if score_kind == "momentum":
        return f"得分是 {params.lookback_days} 日动量。"
    return ""


def next_rebalance_on(
    as_of: pd.Timestamp,
    rebalance: str,
    next_session: Optional[NextSessionFn],
) -> Optional[str]:
    """Execution date of the next scheduled close-to-close rebalance."""
    if next_session is None:
        return None
    day = pd.Timestamp(as_of).normalize()
    for _ in range(80):
        nxt = next_session(day.date())
        if nxt is None:
            return None
        nxt_ts = pd.Timestamp(nxt).normalize()
        if nxt_ts <= day:
            return None
        if is_rebalance_day(day, nxt_ts, rebalance):
            return f"{nxt_ts:%Y-%m-%d}"
        day = nxt_ts
    return None


def compose_snapshot(
    closes: pd.DataFrame,
    risk_assets: Sequence[str],
    safe_asset: Optional[str],
    params: RotationParams,
    names: Optional[Dict[str, str]] = None,
    next_session: Optional[NextSessionFn] = None,
) -> Tuple[BacktestResult, Dict[str, object]]:
    """Backtest plus the card payload for the latest close."""
    result = run_backtest(closes, risk_assets, safe_asset, params)
    frame = prepare_closes(closes)
    quoted = _valid_closes(closes)
    as_of = frame.index[-1]
    labels = dict(names or {})
    labels.setdefault(CASH, "现金")
    following = next_session(as_of.date()) if next_session else None
    rebalance_day = (
        is_rebalance_day(as_of, pd.Timestamp(following), params.rebalance) if following else False
    )
    weights, score_map, bucket_map, score_kind, risk_off = signal_weights(
        frame,
        quoted,
        as_of,
        params,
        list(risk_assets),
        safe_asset,
        result.final_holdings,
        result.equity_level,
        result.peak_level,
    )
    shown = weights if rebalance_day or not result.final_weights else result.final_weights
    holdings = []
    for code, weight in sorted(shown.items(), key=lambda item: item[1], reverse=True):
        if weight <= 1e-8:
            continue
        score = score_map.get(code)
        holdings.append({
            "code": code,
            "name": labels.get(code, code),
            "bucket": bucket_map.get(code, "防守" if code == safe_asset else ""),
            "weight": _round_weight(weight),
            "score": None if score is None or (isinstance(score, float) and math.isnan(score)) else round(float(score), 4),
        })
    last = result.trades[-1].exec_date if result.trades else None
    snapshot: Dict[str, object] = {
        "signal_date": f"{as_of:%Y-%m-%d}",
        "mode": params.mode,
        "mode_label": MODE_LABELS.get(params.mode, params.mode),
        "holdings": holdings,
        "last_rebalance": None if last is None else f"{pd.Timestamp(last):%Y-%m-%d}",
        "next_rebalance": next_rebalance_on(as_of, params.rebalance, next_session),
        "disclaimer": DISCLAIMER,
        "score_kind": score_kind,
        "score_hint": _score_hint(params, score_kind),
        "position_basis": "target" if rebalance_day else "current",
        "risk_off": risk_off and rebalance_day,
    }
    return result, snapshot
