#!/usr/bin/env python3
"""用分位分层看 Alpha158 头部因子扣费后还剩多少，只读，不写数据。

分组复用因子检验的 FactorBacktestService._add_groups（按截面秩分位，并列不拆开）。
换手的定义与 _calc_turnover 相同：相邻两期权重的 0.5 * L1，1 表示整组换掉。
收益不是 close(t) 到 close(t+1)，而是和 eval_alpha158.py --label-lag 1 一样，
买入价为 close(t+1)，持有 hold 个交易日，到 close(t+1+hold)。

单边成本按买卖金额收取。从空仓买进 100% 扣 1 倍 cost-bps；整组卖出再买进扣 2 倍。
默认 15 bps，大约覆盖 A 股佣金、卖出印花税和一点滑点。同时打印 0 成本对照。

不要在正在服务的 tsp 容器里跑。另开容器并挂上同一数据卷，例如:

    docker run --rm --volumes-from <tsp> ...

仓库根目录:

    PYTHONPATH=backend backend/.venv/bin/python scripts/eval_factor_layers.py
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

DEFAULT_FACTORS = (
    "a158_vsump_60",
    "a158_vsumn_60",
    "a158_vsumd_60",
    "a158_vstd_20",
    "a158_vma_20",
    "a158_std_20",
)
_CONTAINER_WARNING = (
    "不要在正在服务的 tsp 容器里跑分层回测，会把服务拖重启。"
    "另开容器并挂上同一数据卷，例如: docker run --rm --volumes-from <tsp> ..."
)


def parse_holds(text: str) -> list[int]:
    """'1,3,5' → [1, 3, 5]。去重并保持书写顺序。"""
    holds: list[int] = []
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        value = int(piece)
        if value < 1:
            raise ValueError("持有天数必须 ≥ 1")
        if value not in holds:
            holds.append(value)
    if not holds:
        raise ValueError("至少要一个持有天数")
    return holds


def rebalance_dates(dates: list[date], hold: int) -> list[date]:
    """每隔 hold 个交易日调仓一次，区间不重叠。"""
    if hold < 1:
        raise ValueError("持有天数必须 ≥ 1")
    return list(dates)[::hold]


def leg_turnover(previous: dict[str, float], current: dict[str, float]) -> float:
    """单边换手，与 FactorBacktestService._calc_turnover 相同。整组换掉为 1。"""
    symbols = previous.keys() | current.keys()
    return 0.5 * sum(abs(current.get(symbol, 0.0) - previous.get(symbol, 0.0)) for symbol in symbols)


def leg_cost(previous: dict[str, float], current: dict[str, float], cost_bps: float) -> float:
    """单边费率乘上买入金额与卖出金额。

    权重和为 1。从空仓建仓只付买入的 cost_bps；整组替换付两倍。
    cost_bps=15 时，建仓扣 0.0015，整组换仓扣 0.0030。
    """
    if cost_bps == 0 or (not previous and not current):
        return 0.0
    symbols = previous.keys() | current.keys()
    bought = sum(max(current.get(symbol, 0.0) - previous.get(symbol, 0.0), 0.0) for symbol in symbols)
    sold = sum(max(previous.get(symbol, 0.0) - current.get(symbol, 0.0), 0.0) for symbol in symbols)
    return (bought + sold) * (cost_bps / 10_000.0)


def monotonicity(group_means: list[float | None]) -> float | None:
    """分组序号与分组平均收益的 Spearman。严格递增为 1。有空组则没有单调性。"""
    if any(value is None or not np.isfinite(value) for value in group_means):
        return None
    if len(group_means) < 2:
        return None
    ranks = _average_rank(np.asarray(group_means, dtype=float))
    index = np.arange(1, len(group_means) + 1, dtype=float)
    if float(np.std(ranks)) < 1e-15:
        return 0.0
    return float(np.corrcoef(index, ranks)[0, 1])


def _average_rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    sorted_values = values[order]
    start = 0
    while start < len(values):
        stop = start
        while stop + 1 < len(values) and sorted_values[stop + 1] == sorted_values[start]:
            stop += 1
        average = (start + 1 + stop + 1) / 2.0
        for position in range(start, stop + 1):
            ranks[order[position]] = average
        start = stop + 1
    return ranks


def summarize_returns(returns: list[float], *, hold: int) -> dict[str, float | None]:
    """年化、夏普、最大回撤。夏普用总体标准差，年化系数 252/hold，与日频因子检验同一口径。"""
    empty = {"total": None, "annual": None, "sharpe": None, "max_drawdown": None, "n_periods": 0}
    if not returns or hold < 1:
        return empty
    series = np.asarray(returns, dtype=float)
    if not np.isfinite(series).all():
        return empty
    nav = np.cumprod(1.0 + series)
    path = np.concatenate(([1.0], nav))
    peak = np.maximum.accumulate(path)
    drawdown = float(np.min((path - peak) / np.maximum(peak, 1e-12)))
    last = float(nav[-1])
    years = (len(series) * hold) / 252.0
    annual = float(last ** (1.0 / years) - 1.0) if last > 0 and years > 0 else None
    deviation = float(np.std(series))
    sharpe = float(np.mean(series) / deviation * np.sqrt(252.0 / hold)) if deviation > 1e-12 else 0.0
    return {
        "total": float(last - 1.0),
        "annual": annual,
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "n_periods": len(series),
    }


def resolve_direction(ic: float | None, requested: str) -> tuple[int, str]:
    """+1 做多因子高分组，-1 做多因子低分组。requested 为 ic/high/low。"""
    if requested == "high":
        return 1, "高"
    if requested == "low":
        return -1, "低"
    if ic is None or not np.isfinite(ic) or ic >= 0:
        return 1, "高"
    return -1, "低"


def _rank_ic(frame, factor: str, ret: str) -> float | None:
    import polars as pl

    ic = (
        frame.filter(
            pl.col(factor).is_not_null()
            & pl.col(factor).is_finite()
            & pl.col(ret).is_not_null()
            & pl.col(ret).is_finite()
        )
        .group_by("date")
        .agg(
            pl.corr(
                pl.col(factor).rank(method="average"),
                pl.col(ret).rank(method="average"),
            ).alias("ic")
        )
    )
    values = [item for item in ic["ic"].to_list() if item is not None and item == item]
    if not values:
        return None
    return float(sum(values) / len(values))


def _equal_weights(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    weight = 1.0 / len(symbols)
    return {symbol: weight for symbol in symbols}


def evaluate_layers(
    frame,
    *,
    factor: str,
    n_groups: int,
    hold: int,
    direction: str,
    cost_bps: list[float],
) -> dict:
    """在一张已经带好 fwd / entry_up / entry_down 的面板上做分层。

    fwd 是 close(t+1+hold) / close(t+1) - 1。entry_up 为真表示买入日涨停，不能买。
    分位只在能交易的股票上做，复用 _add_groups。Q1 是得分最低的一组。
    """
    import polars as pl
    from app.backtest.factor import FactorBacktestService

    required = {"symbol", "date", factor, "fwd", "entry_up", "entry_down"}
    missing = required - set(frame.columns)
    if missing:
        return {"factor": factor, "hold": hold, "error": f"缺少列 {sorted(missing)}"}
    if n_groups < 2:
        return {"factor": factor, "hold": hold, "error": "分组数至少为 2"}

    tradable = frame.filter(
        pl.col(factor).is_not_null()
        & pl.col(factor).is_finite()
        & pl.col("fwd").is_not_null()
        & pl.col("fwd").is_finite()
        & ~pl.col("entry_up").fill_null(False)
        & ~pl.col("entry_down").fill_null(False)
    )
    ic = _rank_ic(tradable, factor, "fwd")
    sign, direction_label = resolve_direction(ic, direction)
    if tradable.is_empty():
        return {
            "factor": factor,
            "hold": hold,
            "ic": ic,
            "direction": direction_label,
            "error": "过滤后没有可交易的样本",
        }

    scored = tradable.with_columns((pl.col(factor) * sign).alias("_score"))
    grouped = FactorBacktestService._add_groups(scored, "_score", n_groups)
    dates = rebalance_dates(sorted(grouped.get_column("date").unique().to_list()), hold)
    grouped = grouped.filter(pl.col("date").is_in(dates))
    buckets: dict[date, dict[str, list[tuple[str, float]]]] = {}
    for row in grouped.select(["date", "symbol", "_group", "fwd"]).iter_rows(named=True):
        day = buckets.setdefault(row["date"], {})
        day.setdefault(str(row["_group"]), []).append((str(row["symbol"]), float(row["fwd"])))

    labels = [f"Q{index}" for index in range(1, n_groups + 1)]
    period_group: dict[str, list[float]] = {label: [] for label in labels}
    books: dict[float, dict[str, list[float]]] = {
        bps: {"long": [], "excess": [], "long_short": []} for bps in cost_bps
    }
    previous_long: dict[str, float] = {}
    previous_short: dict[str, float] = {}
    turnovers: list[float] = []
    for day in dates:
        members = buckets.get(day)
        if not members:
            continue
        for label in labels:
            held = members.get(label, [])
            if held:
                period_group[label].append(float(np.mean([item[1] for item in held])))
        long_rows = members.get(labels[-1], [])
        short_rows = members.get(labels[0], [])
        market_values = [ret for group in members.values() for _, ret in group]
        if not long_rows or not market_values:
            previous_long = {}
            previous_short = {}
            continue
        long_weights = _equal_weights([item[0] for item in long_rows])
        short_weights = _equal_weights([item[0] for item in short_rows])
        if previous_long:
            turnovers.append(leg_turnover(previous_long, long_weights))
        long_return = float(np.mean([item[1] for item in long_rows]))
        market_return = float(np.mean(market_values))
        short_return = float(np.mean([item[1] for item in short_rows])) if short_rows else None
        for bps in cost_bps:
            long_fee = leg_cost(previous_long, long_weights, bps)
            net_long = long_return - long_fee
            books[bps]["long"].append(net_long)
            books[bps]["excess"].append(net_long - market_return)
            if short_return is not None and short_rows:
                short_fee = leg_cost(previous_short, short_weights, bps)
                gross = 0.5 * long_return - 0.5 * short_return
                books[bps]["long_short"].append(gross - 0.5 * long_fee - 0.5 * short_fee)
        previous_long = long_weights
        previous_short = short_weights

    group_means = [
        float(np.mean(period_group[label])) if period_group[label] else None
        for label in labels
    ]
    costs = {}
    for bps, series in books.items():
        costs[bps] = {
            "long": summarize_returns(series["long"], hold=hold),
            "excess": summarize_returns(series["excess"], hold=hold),
            "long_short": summarize_returns(series["long_short"], hold=hold),
        }
    long_periods = costs[cost_bps[0]]["long"]["n_periods"] if cost_bps else 0
    return {
        "factor": factor,
        "hold": hold,
        "ic": ic,
        "direction": direction_label,
        "monotonicity": monotonicity(group_means),
        "group_means": group_means,
        "group_labels": labels,
        "turnover": float(np.mean(turnovers)) if turnovers else (0.0 if long_periods else None),
        "n_periods": long_periods,
        "costs": costs,
        "error": None if long_periods else "没有形成持仓",
    }


def _fmt(value: float | None, digits: int = 4) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    return f"{value:.{digits}f}"


def format_layer_table(rows: list[dict], cost_bps: list[float]) -> str:
    """紧凑中文表。收益是小数，0.01 表示 1%。"""
    lines = [
        "收益为小数（0.01 = 1%）。组均从 Q1 到 Q5，Q5 是做多的那一端。",
        "多头是 Q5 等权；超额 = 多头 − 等权可交易市场。多空是 Q5 对 Q1 的半仓。换手是相邻调仓的单边换手。",
    ]
    header = (
        f"{'因子':<16} {'持有':>4} {'IC':>8} {'方向':>4} {'单调':>6} {'换手':>6} {'期数':>4} "
        f"{'组均':<34}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for row in rows:
        if row.get("error") and not row.get("group_means"):
            lines.append(f"{row['factor']:<16} {row.get('hold', ''):>4}  {row['error']}")
            continue
        means = " ".join(_fmt(value, 3) for value in row.get("group_means") or [])
        lines.append(
            f"{row['factor']:<16} {row['hold']:>4} {_fmt(row.get('ic')):>8} {row.get('direction', '—'):>4} "
            f"{_fmt(row.get('monotonicity'), 2):>6} {_fmt(row.get('turnover'), 2):>6} "
            f"{row.get('n_periods') or 0:>4} {means:<34}"
        )
        for bps in cost_bps:
            stats = (row.get("costs") or {}).get(bps) or {}
            long = stats.get("long") or {}
            excess = stats.get("excess") or {}
            spread = stats.get("long_short") or {}
            lines.append(
                f"{'':<16} {bps:>4.0f}bp "
                f"多头 {_fmt(long.get('annual'))} 夏普 {_fmt(long.get('sharpe'), 2)} "
                f"回撤 {_fmt(long.get('max_drawdown'))} | "
                f"超额 {_fmt(excess.get('annual'))} | "
                f"多空 {_fmt(spread.get('annual'))} 夏普 {_fmt(spread.get('sharpe'), 2)} "
                f"回撤 {_fmt(spread.get('max_drawdown'))}"
            )
    return "\n".join(lines)


def csv_rows(results: list[dict], cost_bps: list[float]) -> list[dict]:
    output: list[dict] = []
    for row in results:
        means = row.get("group_means") or []
        labels = row.get("group_labels") or [f"Q{index}" for index in range(1, len(means) + 1)]
        base = {
            "factor": row.get("factor"),
            "hold": row.get("hold"),
            "ic": row.get("ic"),
            "direction": row.get("direction"),
            "monotonicity": row.get("monotonicity"),
            "turnover": row.get("turnover"),
            "n_periods": row.get("n_periods"),
            "error": row.get("error"),
        }
        for label, value in zip(labels, means, strict=False):
            base[label] = value
        for bps in cost_bps:
            stats = (row.get("costs") or {}).get(bps) or {}
            for name in ("long", "excess", "long_short"):
                perf = stats.get(name) or {}
                prefix = f"{name}_{bps:g}bps"
                base[f"{prefix}_annual"] = perf.get("annual")
                base[f"{prefix}_sharpe"] = perf.get("sharpe")
                base[f"{prefix}_max_drawdown"] = perf.get("max_drawdown")
                base[f"{prefix}_total"] = perf.get("total")
        output.append(base)
    return output


def _sibling():
    import importlib.util

    path = Path(__file__).with_name("eval_alpha158.py")
    spec = importlib.util.spec_from_file_location("eval_alpha158", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"找不到 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mark_limit_flags(frame):
    """给每一行标上相对前收的涨停、跌停。沿用 price_limits，不在这里删行。"""
    import polars as pl
    from app.price_limits import (
        polars_is_risk_warning_name,
        polars_limit_price,
        polars_price_limit_pct,
    )

    if "raw_close" not in frame.columns:
        raise RuntimeError("面板没有 raw_close，不能按涨跌停过滤。")
    ordered = frame.sort(["symbol", "date"])
    prev = pl.col("raw_close").shift(1).over("symbol")
    is_st = (
        polars_is_risk_warning_name(pl.col("name"))
        if "name" in ordered.columns
        else pl.lit(False)
    )
    limit_pct = polars_price_limit_pct(pl.col("symbol"), pl.col("date"), is_st)
    up = polars_limit_price(prev, limit_pct, up=True)
    down = polars_limit_price(prev, limit_pct, up=False)
    known = prev.is_not_null()
    return ordered.with_columns(
        (known & ((pl.col("raw_close") - up).abs() <= 0.011)).alias("_limit_up"),
        (known & ((pl.col("raw_close") - down).abs() <= 0.011)).alias("_limit_down"),
    )


def attach_entry_blocks(frame, *, lag: int):
    """把 t+lag 那天的涨跌停贴回信号日。买入日没有这只股票时视为不能交易。"""
    import polars as pl

    dates = sorted(item for item in frame.get_column("date").unique().to_list() if item is not None)
    index = {item: pos for pos, item in enumerate(dates)}
    rows = []
    for item in dates:
        entry_at = index[item] + lag
        if entry_at < len(dates):
            rows.append({"date": item, "_entry_date": dates[entry_at]})
    if not rows or "_limit_up" not in frame.columns:
        return frame.with_columns(
            pl.lit(False).alias("entry_up"),
            pl.lit(False).alias("entry_down"),
        )
    date_dtype = frame.schema["date"]
    mapping = pl.DataFrame(rows).with_columns(
        pl.col("date").cast(date_dtype),
        pl.col("_entry_date").cast(date_dtype),
    )
    flags = frame.select(
        "symbol",
        pl.col("date").alias("_entry_date"),
        pl.col("_limit_up").alias("_entry_limit_up"),
        pl.col("_limit_down").alias("_entry_limit_down"),
    )
    return (
        frame.join(mapping, on="date", how="left")
        .join(flags, on=["symbol", "_entry_date"], how="left")
        .with_columns(
            pl.col("_entry_limit_up").fill_null(True).alias("entry_up"),
            pl.col("_entry_limit_down").fill_null(True).alias("entry_down"),
        )
        .drop("_entry_date", "_entry_limit_up", "_entry_limit_down")
    )


def _cost_list(cost_bps: float) -> list[float]:
    if cost_bps == 0:
        return [0.0]
    return [0.0, float(cost_bps)]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alpha158 头部因子的分位分层多空，只读")
    parser.add_argument("--days", type=int, default=365, help="检验区间长度，自然日，默认 365")
    parser.add_argument("--end", default="", help="截止日 YYYY-MM-DD；默认是早于今天的最近分区")
    parser.add_argument(
        "--factors",
        default=",".join(DEFAULT_FACTORS),
        help="逗号分隔的因子 id，默认 6 个量能/波动头部",
    )
    parser.add_argument("--groups", type=int, default=5, help="分位组数，默认 5")
    parser.add_argument(
        "--hold",
        default="1,3,5",
        help="持有交易日，逗号分隔。默认 1,3,5，每隔这么多天调仓一次",
    )
    parser.add_argument(
        "--direction",
        choices=("ic", "high", "low"),
        default="ic",
        help="ic=按 Rank IC 符号决定做多哪一端；high=做多高分组；low=做多低分组",
    )
    parser.add_argument(
        "--cost-bps",
        type=float,
        default=15.0,
        help="单边成本，基点。默认 15，并同时打印 0 成本",
    )
    parser.add_argument("--min-symbols-per-date", type=int, default=200)
    parser.add_argument("--min-listed-days", type=int, default=60)
    parser.add_argument("--exclude-limit", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--exclude-st", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--symbols", default="", help="逗号分隔的股票代码；留空为全市场")
    parser.add_argument("--csv", default="", help="把结果写成 CSV 的路径")
    return parser


def _prepare_frame(panel, base, *, hold: int, start: date, end: date, exclude_limit: bool):
    import polars as pl

    labeled = panel.join(
        base.qlib_forward_return(panel, lag=1, horizon=hold, start=start, end=end).rename(
            {"_qlib_return": "fwd"},
        ),
        on=["symbol", "date"],
        how="left",
    )
    if exclude_limit:
        labeled = attach_entry_blocks(mark_limit_flags(labeled), lag=1)
    else:
        labeled = labeled.with_columns(
            pl.lit(False).alias("entry_up"),
            pl.lit(False).alias("entry_down"),
        )
    return labeled.filter((pl.col("date") >= start) & (pl.col("date") <= end))


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        holds = parse_holds(args.hold)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.groups < 2:
        print("--groups 至少为 2", file=sys.stderr)
        return 1
    if args.cost_bps < 0 or args.min_symbols_per_date < 0 or args.min_listed_days < 0 or args.days < 1:
        print("天数和成本不能为负", file=sys.stderr)
        return 1
    factor_ids = [item.strip() for item in args.factors.split(",") if item.strip()]
    if not factor_ids:
        print("至少要一个因子", file=sys.stderr)
        return 1

    base = _sibling()
    gate = base._polars_gate()
    if gate:
        print(gate)
        return 1

    import polars as pl
    from app.backtest.engine import BacktestEngine
    from app.backtest.factor import FactorBacktestService, FactorBatchConfig
    from app.enriched_generation import EnrichedGenerationUnavailableError
    from app.factors.registry import get_factor
    from app.tickflow.repository import DataStore, KlineRepository

    data_dir = base._data_dir()
    partitions = base._partition_dates(data_dir / "kline_daily_enriched")
    if not partitions:
        return base._print_missing(data_dir)
    today = base.beijing_today()
    if args.end:
        end = date.fromisoformat(args.end)
        if end >= today:
            print(f"警告: --end {end.isoformat()} 不是已收盘的历史日，当日分区可能仍在写入。")
    else:
        closed = base.latest_closed_trading_day(partitions, today)
        if closed is None:
            print(f"没有早于 {today.isoformat()} 的 enriched 分区，盘中不评估今天。")
            return 2
        end = closed
    start = end - timedelta(days=args.days)
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] or None
    print(_CONTAINER_WARNING)
    print(
        f"区间 {start.isoformat()} ~ {end.isoformat()}，因子 {len(factor_ids)} 个，"
        f"分位 {args.groups}，持有 {holds}，单边成本 {args.cost_bps:g} bp（另附 0 成本）。"
    )
    print("买入价是 close(t+1)，收益持有到 close(t+1+hold)。涨停或跌停的股票当天不进分组。")

    engine = BacktestEngine(KlineRepository(DataStore(data_dir)))
    service = FactorBacktestService(engine)
    config = FactorBatchConfig(
        factor_names=factor_ids,
        symbols=symbols,
        start=start,
        end=end,
        n_groups=args.groups,
        rebalance="daily",
        asset_type="stock",
    )
    try:
        panel = service._load_factor_panel(config, factor_ids)
    except EnrichedGenerationUnavailableError as exc:
        print(f"enriched 分区在读取过程中变了: {exc}")
        print("请改用已经收盘的 --end。")
        return 1
    if panel.is_empty():
        return base._print_missing(data_dir)
    prior = panel.filter(pl.col("date") < start).get_column("date").n_unique()
    print(f"起点前交易日 {prior} 个。窗口因子需要这段预热，预热日本身不进分层。")

    if args.exclude_limit and "raw_close" not in panel.columns:
        raw = engine.load_panel(
            symbols,
            panel.get_column("date").min(),
            end,
            columns=["symbol", "date", "raw_close"],
            asset_type="stock",
        )
        if raw.is_empty() or "raw_close" not in raw.columns:
            print("enriched 没有 raw_close，不能按涨跌停过滤。")
            return 1
        panel = panel.join(raw.select("symbol", "date", "raw_close"), on=["symbol", "date"], how="left")

    filtering = args.exclude_st or args.min_listed_days > 0
    need_names = args.exclude_limit or filtering
    instruments = engine.repo.get_instruments() if need_names and engine.repo is not None else None
    if (
        args.exclude_limit
        and instruments is not None
        and "name" in instruments.columns
        and "name" not in panel.columns
    ):
        panel = panel.join(
            instruments.select("symbol", "name").unique(subset=["symbol"], keep="last"),
            on="symbol",
            how="left",
        )
    panel, _dates, section = base.restrict_evaluation_dates(
        panel, start=start, end=end, min_symbols=args.min_symbols_per_date,
    )
    for line in base.format_cross_section(section, args.min_symbols_per_date):
        print(line)
    if not _dates:
        print("没有达到截面门槛的交易日。")
        return 1
    if filtering:
        try:
            panel = base.apply_row_filters(
                panel,
                exclude_limit=False,
                exclude_st=args.exclude_st,
                min_listed_days=args.min_listed_days,
                instruments=instruments,
            )
        except RuntimeError as exc:
            print(str(exc))
            return 1
        if args.exclude_st:
            print("ST 用的是 instruments 里的当前名称，不是历史上每一天的 ST 状态。")

    costs = _cost_list(args.cost_bps)
    results: list[dict] = []
    for hold in holds:
        signal = _prepare_frame(
            panel, base, hold=hold, start=start, end=end, exclude_limit=args.exclude_limit,
        )
        for factor_id in factor_ids:
            spec = get_factor(factor_id)
            if factor_id not in signal.columns:
                results.append({
                    "factor": factor_id,
                    "hold": hold,
                    "error": "因子列不存在",
                    "costs": {},
                })
                continue
            result = evaluate_layers(
                signal.select(
                    "symbol", "date", pl.col(factor_id).alias(factor_id),
                    "fwd", "entry_up", "entry_down",
                ),
                factor=factor_id,
                n_groups=args.groups,
                hold=hold,
                direction=args.direction,
                cost_bps=costs,
            )
            result["label"] = spec.label if spec is not None else factor_id
            results.append(result)
            print(f"  {factor_id} 持有 {hold} 日完成")

    print()
    print(format_layer_table(results, costs))
    if args.csv:
        target = Path(args.csv)
        _write_csv(target, csv_rows(results, costs))
        print(f"CSV 已写入 {target}")
    usable = [row for row in results if not row.get("error")]
    return 0 if usable else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0) from None

