# -*- coding: utf-8 -*-
"""ETF rotation service: load ETF closes, run the rule-based rotation and
render a Markdown report (latest signal + backtest + parameter sweep).

The strategy itself lives in ``src.core.etf_rotation``; this module only
handles data loading, report rendering and optional notification.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
import json
import logging
import math
from typing import Callable, Dict, List, Optional, Sequence

import pandas as pd

from src.core.etf_rotation import (
    CASH,
    DAYS_PER_YEAR,
    RESULT_PREFIX,
    BacktestResult,
    RotationParams,
    MAX_FORWARD_FILL_DAYS,
    annual_returns,
    compose_snapshot,
    compute_metrics,
    parameter_sweep,
    parse_buckets,
    parse_lookbacks,
    parse_mode,
    parse_weighting,
)

logger = logging.getLogger(__name__)

RECENT_TRADES_LIMIT = 10
CASH_LABEL = "现金"
# History starting this long after the requested start is flagged as truncated.
TRUNCATED_HISTORY_TOLERANCE_DAYS = 30
# ETF daily limits are 10%/20%; larger moves are almost always split/adjustment artifacts.
SUSPICIOUS_DAILY_MOVE = 0.25
MAX_LISTED_JUMPS = 3
# Shorter backtests make annualised figures meaningless; only the signal is shown.
MIN_BACKTEST_YEARS = 1.0
CALENDAR_MARKET = "cn"

NextSessionFn = Callable[[date], Optional[date]]


@dataclass
class LoadedPrices:
    closes: pd.DataFrame
    requested_start: date
    failed: Dict[str, str] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)


@dataclass
class RotationReport:
    markdown: str
    as_of: Optional[pd.Timestamp] = None
    target_weights: Dict[str, float] = field(default_factory=dict)
    failed_codes: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    snapshot: Dict[str, object] = field(default_factory=dict)


def params_from_config(config) -> RotationParams:
    mode = parse_mode(getattr(config, "etf_rotation_mode", None))
    return RotationParams(
        lookback_days=config.etf_rotation_lookback_days,
        rebalance=config.etf_rotation_rebalance,
        top_n=config.etf_rotation_top_n,
        switch_buffer_pct=config.etf_rotation_switch_buffer_pct,
        cost_bps=config.etf_rotation_cost_bps,
        mode=mode,
        lookbacks=parse_lookbacks(
            ",".join(str(days) for days in getattr(config, "etf_rotation_lookbacks", ()) or ())
        ),
        weighting=parse_weighting(getattr(config, "etf_rotation_weighting", None), mode),
        buckets=parse_buckets(getattr(config, "etf_rotation_buckets", None)) if mode == "blended_bucket" else (),
        drawdown_risk_off=bool(getattr(config, "etf_rotation_drawdown_risk_off", False)),
        drawdown_limit=float(getattr(config, "etf_rotation_drawdown_limit", 0.15) or 0.15),
    )


def _clone_tickflow_forward(fetcher):
    """Request-local forward adjust. Does not change the shared fetcher."""
    from data_provider.tickflow_fetcher import TickFlowFetcher

    return TickFlowFetcher(
        api_key=getattr(fetcher, "api_key", ""),
        timeout=float(getattr(fetcher, "timeout", 30.0) or 30.0),
        kline_adjust="forward",
        batch_daily_enabled=bool(getattr(fetcher, "batch_daily_enabled", True)),
        batch_size=int(getattr(fetcher, "batch_size", 100) or 100),
        priority=getattr(fetcher, "priority", 2),
    )


def etf_daily_fetchers(fetchers: Sequence) -> list:
    """Forward-adjusted ETF sources, ordered by each fetcher's priority.

    TickFlow stays in the chain at ``TICKFLOW_PRIORITY`` even when the shared
    fetcher is not already set to forward adjustment. The clone is request-local
    and does not change that shared fetcher.
    """
    selected = []
    for fetcher in fetchers:
        name = getattr(fetcher, "name", "")
        if name in {"EfinanceFetcher", "AkshareFetcher", "BaostockFetcher"}:
            selected.append(fetcher)
            continue
        if name != "TickFlowFetcher":
            continue
        adjust = getattr(fetcher, "kline_adjust", "none")
        if adjust == "forward":
            selected.append(fetcher)
            continue
        selected.append(_clone_tickflow_forward(fetcher))
    selected.sort(key=lambda item: getattr(item, "priority", 99))
    return selected


def _close_series(code: str, df: Optional[pd.DataFrame], end_day: date) -> pd.Series:
    if df is None or df.empty:
        raise ValueError("empty daily data")
    missing = sorted({"date", "close"} - set(df.columns))
    if missing:
        raise ValueError(f"daily data missing columns: {missing}")
    closes = pd.to_numeric(df["close"], errors="coerce")
    closes = closes.replace([math.inf, -math.inf], math.nan).where(closes > 0)
    closes.index = pd.DatetimeIndex(pd.to_datetime(df["date"])).tz_localize(None).normalize()
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()
    kept = closes.loc[: pd.Timestamp(end_day)]
    if len(kept) < len(closes):
        logger.info("[ETF轮动] %s 丢弃 %s 之后的 %d 根未收盘 K 线", code, end_day, len(closes) - len(kept))
    if not kept.notna().any():
        raise ValueError("no valid close prices")
    return kept


def load_closes(
    fetcher_manager,
    codes: Sequence[str],
    years: int,
    end: Optional[date] = None,
) -> LoadedPrices:
    """Fetch daily closes per code; failures are returned, not swallowed.

    Bars after ``end`` (e.g. an unfinished intraday bar) are dropped. Reindex
    against actual A-share sessions through ``end``; missing quotes stay NaN.
    An unavailable historical calendar raises rather than inventing sessions.
    """
    end_day = end or date.today()
    start_day = end_day - timedelta(days=int(years * 365.25))
    series: Dict[str, pd.Series] = {}
    failed: Dict[str, str] = {}
    sources: Dict[str, str] = {}

    from data_provider.base import DataFetcherManager, _is_etf_code

    # Keep the existing priority/fallback manager, but give this consumer a
    # request-local list whose ETF paths explicitly request forward adjustment.
    # Never change the shared manager or quietly use raw-price fallback sources.
    adjusted_manager = None
    if isinstance(fetcher_manager, DataFetcherManager):
        eligible = etf_daily_fetchers(fetcher_manager._get_fetchers_snapshot())
        if eligible:
            logger.info(
                "[ETF轮动] 日线数据源按优先级: %s",
                ", ".join(f"{item.name}(P{getattr(item, 'priority', '?')})" for item in eligible),
            )
            adjusted_manager = DataFetcherManager(fetchers=eligible)

    for code in codes:
        try:
            if isinstance(fetcher_manager, DataFetcherManager):
                if not _is_etf_code(code):
                    raise ValueError("rotation requires an A-share ETF code")
                if adjusted_manager is None:
                    raise ValueError("no forward-adjusted ETF data source available")
            manager = adjusted_manager or fetcher_manager
            df, source = manager.get_daily_data(
                code,
                start_date=start_day.isoformat(),
                end_date=end_day.isoformat(),
            )
            close_series = _close_series(code, df, end_day)
            if adjusted_manager is None and df.attrs.get("price_adjustment") != "forward":
                raise ValueError("daily source does not confirm forward-adjusted prices")
            series[code] = close_series
        except Exception as exc:  # fetch errors and malformed frames alike
            failed[code] = str(exc) or type(exc).__name__
            logger.warning("[ETF轮动] %s 日线不可用: %s", code, failed[code])
            continue
        sources[code] = source
        logger.info("[ETF轮动] %s 获取 %d 条日线 (来源: %s)", code, len(series[code]), source)

    frame = pd.DataFrame(series).sort_index() if series else pd.DataFrame()
    if not frame.empty:
        from src.core.trading_calendar import get_trading_dates

        sessions = get_trading_dates(CALENDAR_MARKET, frame.index[0].date(), end_day)
        if sessions is None:
            raise ValueError("A-share trading calendar unavailable for ETF rotation history")
        # Keep sessions even when every source omits the date; otherwise missing
        # execution days become delayed trades and missing Fridays shift signals.
        frame = frame.reindex(sessions)
    return LoadedPrices(closes=frame, requested_start=start_day, failed=failed, sources=sources)


def data_quality_warnings(loaded: LoadedPrices) -> List[str]:
    """Surface data issues that silently distort a backtest."""
    warnings: List[str] = [
        f"{code} 数据获取失败，已从本次计算中剔除（{reason[:60]}）"
        for code, reason in loaded.failed.items()
    ]
    limit = pd.Timestamp(loaded.requested_start) + pd.Timedelta(days=TRUNCATED_HISTORY_TOLERANCE_DAYS)
    for code in loaded.closes.columns:
        series = loaded.closes[code].dropna()
        if series.empty:
            continue
        missing = loaded.closes[code].isna() & loaded.closes[code].notna().cummax()
        longest_gap = int(missing.groupby((~missing).cumsum()).sum().max())
        if longest_gap:
            warnings.append(
                f"{code} 上市后行情缺失，最长连续 {longest_gap} 根；缺报价日不成交，"
                "持仓按最后报价估值，恢复报价时计入完整损益"
            )
            if longest_gap > MAX_FORWARD_FILL_DAYS:
                warnings.append(f"{code} 行情缺口超过 {MAX_FORWARD_FILL_DAYS} 根，动量信号在缺口期间不可用")
        if series.index[0] > limit:
            warnings.append(
                f"{code} 历史起点为 {series.index[0]:%Y-%m-%d}（请求 {loaded.requested_start:%Y-%m-%d}），"
                "可能是上市较晚，或回退数据源只返回了部分历史，回测区间会相应缩短"
            )
        moves = series.pct_change(fill_method=None)
        jumps = moves[moves.abs() > SUSPICIOUS_DAILY_MOVE]
        if not jumps.empty:
            listed = "、".join(f"{d:%Y-%m-%d} {v * 100:+.0f}%" for d, v in jumps.head(MAX_LISTED_JUMPS).items())
            warnings.append(f"{code} 存在异常单日涨跌（{listed}），疑似拆分或复权缺失，回测结果可能失真")
    distinct = sorted(set(loaded.sources.values()))
    if len(distinct) > 1:
        detail = "、".join(f"{code}:{src}" for code, src in loaded.sources.items())
        warnings.append(f"数据来自多个数据源（{detail}），复权口径可能不一致")
    return warnings


def _resolve_names(fetcher_manager, codes: Sequence[str]) -> Dict[str, str]:
    names: Dict[str, str] = {CASH: CASH_LABEL}
    for code in codes:
        try:
            name = fetcher_manager.get_stock_name(code, allow_realtime=False)
        except Exception as exc:
            logger.debug("[ETF轮动] %s 名称获取失败: %s", code, exc)
            name = None
        names[code] = name or code
    return names


def _pct(value: float, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value * 100:.{digits}f}%"


def _num(value: float) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value:.2f}"


def _label(code: str, names: Dict[str, str]) -> str:
    name = names.get(code, code)
    return name if name == code else f"{name}({code})"


def _weights_text(weights: Dict[str, float], names: Dict[str, str]) -> str:
    if not weights:
        return CASH_LABEL
    ordered = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    return " + ".join(f"{_label(c, names)} {w * 100:.0f}%" for c, w in ordered)


def _render_signal(snapshot: Dict[str, object], names: Dict[str, str]) -> List[str]:
    basis = snapshot.get("position_basis")
    if basis == "target":
        conclusion = "今日为调仓日，下表是目标持仓，下一交易日收盘执行"
    else:
        conclusion = "今日不是调仓日，下表是当前规则持仓"
    score_kind = snapshot.get("score_kind")
    score_header = "平均排名" if score_kind == "avg_rank" else "动量" if score_kind == "momentum" else "得分"
    lines = [
        f"## 最新信号（基于 {snapshot.get('signal_date')} 收盘）",
        "",
        f"- 模式：{snapshot.get('mode_label')}",
        f"- 上次调仓：{snapshot.get('last_rebalance') or '—'}",
        f"- 下次调仓：{snapshot.get('next_rebalance') or '—'}",
        f"- 结论：{conclusion}",
        "",
        f"| 标的 | 分桶 | 权重 | {score_header} |",
        "| --- | --- | --- | --- |",
    ]
    for row in snapshot.get("holdings") or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "")
        score = row.get("score")
        if score_kind == "momentum" and isinstance(score, (int, float)):
            score_text = _pct(float(score))
        elif isinstance(score, (int, float)):
            score_text = f"{float(score):.2f}"
        else:
            score_text = "—"
        weight = float(row.get("weight") or 0.0)
        lines.append(
            f"| {_label(code, names)} | {row.get('bucket') or '—'} | {weight * 100:.1f}% | {score_text} |"
        )
    hint = snapshot.get("score_hint")
    if hint:
        lines += ["", str(hint)]
    lines += ["", f"> {snapshot.get('disclaimer')}"]
    return lines


def _render_metrics(result: BacktestResult) -> List[str]:
    strat = compute_metrics(result.equity)
    bench = compute_metrics(result.benchmark_equity)
    start, end = result.equity.index[0], result.equity.index[-1]
    rows = [
        ("累计收益", "total_return", _pct),
        ("年化收益", "cagr", _pct),
        ("年化波动", "ann_vol", _pct),
        ("夏普比率", "sharpe", _num),
        ("最大回撤", "max_drawdown", _pct),
        ("Calmar", "calmar", _num),
    ]
    lines = [
        f"## 回测表现（{start:%Y-%m-%d} ~ {end:%Y-%m-%d}，已扣交易成本）",
        "",
        "| 指标 | 轮动策略 | 风险池等权持有 |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {title} | {fmt(strat[key])} | {fmt(bench[key])} |" for title, key, fmt in rows]
    lines.append(f"| 调仓次数 | {len(result.trades)} | - |")
    return lines


def _render_annual(result: BacktestResult) -> List[str]:
    strat = annual_returns(result.equity)
    bench = annual_returns(result.benchmark_equity)
    lines = ["## 分年度收益", "", "| 年份 | 轮动策略 | 等权持有 | 差值 |", "| --- | --- | --- | --- |"]
    for year in strat.index:
        s, b = strat[year], bench.get(year, math.nan)
        lines.append(f"| {year} | {_pct(s)} | {_pct(b)} | {_pct(s - b)} |")
    return lines


def _render_sweep(rows, current_lookback: int) -> List[str]:
    if not rows:
        return []
    lines = [
        "## 参数平原检验（仅改变动量窗口，统一评估区间）",
        "",
        "| 动量窗口 | 年化收益 | 最大回撤 | 夏普 |",
        "| --- | --- | --- | --- |",
    ]
    for lookback, m in rows:
        mark = " ⬅ 当前" if lookback == current_lookback else ""
        lines.append(f"| {lookback}{mark} | {_pct(m['cagr'])} | {_pct(m['max_drawdown'])} | {_num(m['sharpe'])} |")
    lines += ["", "> 相邻窗口的表现应当接近；如果只有个别窗口表现好，说明结果大概率是过拟合。"]
    return lines


def _render_trades(result: BacktestResult, names: Dict[str, str]) -> List[str]:
    if not result.trades:
        return []
    lines = ["## 近期调仓记录", "", "| 信号日 | 执行日 | 调出 | 调入 |", "| --- | --- | --- | --- |"]
    for trade in result.trades[-RECENT_TRADES_LIMIT:][::-1]:
        lines.append(
            f"| {trade.signal_date:%Y-%m-%d} | {trade.exec_date:%Y-%m-%d} | "
            f"{_weights_text(trade.from_weights, names)} | {_weights_text(trade.to_weights, names)} |"
        )
    return lines


def _render_rules(params: RotationParams, safe_asset: Optional[str], names: Dict[str, str]) -> List[str]:
    period = "每周最后一个交易日" if params.rebalance == "weekly" else "每月最后一个交易日"
    defensive = _label(safe_asset, names) if safe_asset else CASH_LABEL
    if params.mode == "equal_weight":
        body = [
            f"- {period}收盘把风险池恢复为等权，下一交易日收盘执行",
            f"- 交易成本按单边 {params.cost_bps:g} bp 乘以换手率扣除",
        ]
    elif params.mode == "legacy":
        body = [
            f"- {period}收盘计算 {params.lookback_days} 日动量，下一交易日收盘执行",
            f"- 持有动量最强且为正的前 {params.top_n} 只，每只 {100 / params.top_n:.0f}%；空出的仓位切到 {defensive}",
            f"- 已持有标的在合格且落后幅度不超过 {params.switch_buffer_pct:g}% 时继续持有",
            f"- 交易成本按单边 {params.cost_bps:g} bp 乘以换手率扣除",
        ]
    else:
        days = "、".join(str(days) for days in params.lookbacks)
        weighting = "60 日波动率倒数" if params.weighting == "inv_vol" else "等权"
        body = [
            f"- {period}收盘按 {days} 日收益的平均排名给每个分桶打分，只保留桶内最强的一只",
            f"- 只持有混合动量为正的前 {params.top_n} 个分桶，权重按{weighting}；没选满的仓位切到 {defensive}",
            f"- 交易成本按单边 {params.cost_bps:g} bp 乘以换手率扣除",
            "- 组合回撤风控默认关闭",
        ]
    return [
        "## 规则说明",
        "",
        *body,
        "- 对比基准为风险池等权持有（日再平衡、不扣成本），与策略同从首次建仓日起算",
        "",
        "> 规则化的回测结果不代表未来收益，不构成投资建议。趋势策略在震荡市会反复止损，在急涨行情中会反应滞后。",
    ]


def build_report(
    closes: pd.DataFrame,
    risk_assets: Sequence[str],
    safe_asset: Optional[str],
    params: RotationParams,
    names: Dict[str, str],
    warnings: Sequence[str] = (),
    next_session: Optional[NextSessionFn] = None,
) -> RotationReport:
    """Render the report; ``next_session`` resolves the trading day after a date."""
    risk = [c for c in risk_assets if c in closes.columns]
    result, snapshot = compose_snapshot(closes, risk, safe_asset, params, names, next_session)
    as_of = pd.Timestamp(str(snapshot["signal_date"]))
    safe_close = closes.loc[as_of, safe_asset] if safe_asset and safe_asset in closes.columns else math.nan
    usable_safe = safe_asset if pd.notna(safe_close) and math.isfinite(safe_close) and safe_close > 0 else None
    shown = {
        str(row["code"]): float(row["weight"])
        for row in snapshot.get("holdings") or []
        if isinstance(row, dict) and row.get("code")
    }

    notes = list(warnings)
    if safe_asset and not usable_safe:
        notes.append(f"防守资产 {safe_asset} 不可用，防守仓位按现金（收益为 0）计算")
    start, end = result.equity.index[0], result.equity.index[-1]
    has_backtest = (end - start).days / DAYS_PER_YEAR >= MIN_BACKTEST_YEARS
    if has_backtest:
        sweep = parameter_sweep(closes, risk, safe_asset, params, min_years=MIN_BACKTEST_YEARS)
    else:
        sweep = []
        notes.append(
            f"可回测区间仅 {start:%Y-%m-%d} ~ {end:%Y-%m-%d}，不足 {MIN_BACKTEST_YEARS:g} 年，"
            "已省略回测表现、分年度收益、参数平原检验和调仓记录，只给出最新信号"
        )

    lines = [f"# ETF 轮动信号 {as_of:%Y-%m-%d}", ""]
    if notes:
        lines += ["## ⚠️ 数据告警", ""] + [f"- {note}" for note in notes] + [""]
    backtest_blocks = (
        (_render_metrics(result), _render_annual(result), _render_sweep(sweep, params.lookback_days),
         _render_trades(result, names))
        if has_backtest else ()
    )
    for block in (
        _render_signal(snapshot, names),
        *backtest_blocks,
        _render_rules(params, usable_safe, names),
    ):
        if block:
            lines += block + [""]

    return RotationReport(
        markdown="\n".join(lines).rstrip() + "\n",
        as_of=as_of,
        target_weights=shown,
        warnings=notes,
        snapshot=snapshot,
    )


def _deliver(report: RotationReport, send_notification: bool, notifier) -> None:
    """Save and push the report; failures are logged, never raised."""
    try:
        if notifier is None:
            from src.notification import NotificationService

            notifier = NotificationService()
    except Exception as exc:
        logger.warning("[ETF轮动] 通知服务初始化失败，报告未保存也未推送: %s\n%s", exc, report.markdown)
        return

    filename = f"etf_rotation_{report.as_of:%Y%m%d}.md"
    try:
        path = notifier.save_report_to_file(report.markdown, filename)
        logger.info("[ETF轮动] 报告已保存: %s", path)
    except Exception as exc:
        logger.warning("[ETF轮动] 报告保存失败: %s\n%s", exc, report.markdown)

    if not send_notification:
        return
    try:
        if not notifier.is_available():
            logger.info("[ETF轮动] 未配置通知渠道，跳过推送")
        elif not notifier.send(report.markdown, route_type="report"):
            logger.warning("[ETF轮动] 通知推送失败")
    except Exception as exc:
        logger.warning("[ETF轮动] 通知推送异常: %s", exc)


def run_etf_rotation(
    config,
    send_notification: bool = True,
    fetcher_manager=None,
    notifier=None,
    end: Optional[date] = None,
    next_session: Optional[NextSessionFn] = None,
) -> RotationReport:
    """CLI entry: fetch data, build report, save it, optionally push it.

    ``end`` defaults to the latest completed A-share session, so an intraday
    run never treats the unfinished bar as a close.
    """
    from src.core.trading_calendar import get_effective_trading_date, get_next_trading_date

    params = params_from_config(config)
    if params.mode == "blended_bucket":
        pool = [code for bucket in params.buckets for code in bucket.members]
    else:
        pool = [c for c in config.etf_rotation_pool if c]
    if not pool:
        raise ValueError("ETF_ROTATION_POOL 为空，请至少配置一只 ETF")
    safe_asset = config.etf_rotation_safe_asset or None

    if fetcher_manager is None:
        from data_provider import DataFetcherManager

        fetcher_manager = DataFetcherManager()

    codes = pool + ([safe_asset] if safe_asset and safe_asset not in pool else [])
    end_day = end or get_effective_trading_date(CALENDAR_MARKET)
    if next_session is None:
        def next_session(day: date) -> Optional[date]:
            return get_next_trading_date(CALENDAR_MARKET, day)

    loaded = load_closes(fetcher_manager, codes, config.etf_rotation_backtest_years, end=end_day)
    risk = [c for c in pool if c in loaded.closes.columns]
    if not risk:
        raise RuntimeError(f"ETF 轮动池全部数据获取失败: {loaded.failed}")

    names = _resolve_names(fetcher_manager, codes)
    warnings = data_quality_warnings(loaded)
    for warning in warnings:
        logger.warning("[ETF轮动] %s", warning)
    report = replace(
        build_report(loaded.closes, risk, safe_asset, params, names, warnings, next_session=next_session),
        failed_codes=dict(loaded.failed),
    )

    _deliver(report, send_notification, notifier)
    if report.snapshot:
        print(RESULT_PREFIX + json.dumps(report.snapshot, ensure_ascii=False), flush=True)
    return report
