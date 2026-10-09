#!/usr/bin/env python3
"""用现有因子检验评估 Alpha158 实验组，只读，不写数据。

没有 A 股日线 enriched 分区时直接说明并退出，不编造 IC。
默认截止日是最近一个已经收盘的交易日（分区日期早于北京时间的今天），
避免盘中碰到仍在写入的当日分区。

方向以 IC 符号为准，很多强因子是负的。下单前看分层多空和换手。
说明见 docs/alpha158.md。

开发环境（仓库根目录，与 ./dev.sh 同一套后端虚拟环境）:

    PYTHONPATH=backend backend/.venv/bin/python scripts/eval_alpha158.py

指定截止日、Qlib 式标签和样本过滤:

    PYTHONPATH=backend backend/.venv/bin/python scripts/eval_alpha158.py \\
        --end 2026-09-30 --label-lag 1 --exclude-limit --exclude-st --min-listed-days 60
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

_BEIJING = ZoneInfo("Asia/Shanghai")


def polars_matches_project(version: str) -> bool:
    """backend/pyproject.toml: polars>=1.44,<1.45。"""
    numbers: list[int] = []
    for part in version.split("."):
        digits = ""
        for char in part:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        numbers.append(int(digits))
    if len(numbers) < 2:
        return False
    major, minor = numbers[0], numbers[1]
    return (major, minor) >= (1, 44) and (major, minor) < (1, 45)


def latest_closed_trading_day(partition_dates: list[date], today: date) -> date | None:
    """最近一个早于 today 的分区日。今天的分区在盘中可能还在写。"""
    closed = [item for item in partition_dates if item < today]
    return max(closed) if closed else None


def beijing_today() -> date:
    return datetime.now(_BEIJING).date()


def _data_dir() -> Path:
    from app.config import settings

    return Path(settings.data_dir)


def _partition_dates(root: Path) -> list[date]:
    if not root.is_dir():
        return []
    found: list[date] = []
    for partition in root.glob("date=*"):
        try:
            found.append(date.fromisoformat(partition.name.removeprefix("date=")))
        except ValueError:
            continue
        if not (partition / "part.parquet").is_file():
            found.pop()
    return found


def _print_missing(data_dir: Path) -> int:
    enriched = data_dir / "kline_daily_enriched"
    raw = data_dir / "kline_daily"
    print("当前环境没有可评估的 A 股日线数据，下面不给出 IC。")
    print(f"数据目录: {data_dir}")
    print(f"kline_daily_enriched 分区数: {len(_partition_dates(enriched))}")
    print(f"kline_daily 分区数: {len(_partition_dates(raw))}")
    print("因子检验读的是 kline_daily_enriched。分区里有 part.parquet 之后，在仓库根目录执行:")
    print("  PYTHONPATH=backend backend/.venv/bin/python scripts/eval_alpha158.py")
    return 2


def _fmt(value: float | None, digits: int = 4) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    return f"{value:.{digits}f}"


def _print_table(title: str, rows: list[dict], *, with_lag: bool) -> None:
    print()
    print(title)
    lag = f" {'IC_lag':>8}" if with_lag else ""
    header = f"{'因子':<16} {'名称':<22} {'IC':>8} {'ICIR':>8} {'换手':>8} {'覆盖':>8} {'天数':>6}{lag}"
    print(header)
    print("-" * len(header))
    for row in rows:
        lag_text = f" {_fmt(row.get('ic_lag')):>8}" if with_lag else ""
        print(
            f"{row['factor_name']:<16} {str(row['label'])[:22]:<22} "
            f"{_fmt(row['ic_mean']):>8} {_fmt(row['ir']):>8} {_fmt(row['turnover']):>8} "
            f"{_fmt(row.get('coverage')):>8} {row.get('n_dates') or 0:>6}{lag_text}"
        )


def _cluster(sample: dict[str, np.ndarray], ranked_ids: list[str], threshold: float) -> list[list[str]]:
    """按 |IC| 从高到低贪心聚类。与簇代表的 |corr| ≥ threshold 则并入。"""
    clusters: list[tuple[str, list[str]]] = []
    for factor_id in ranked_ids:
        series = sample.get(factor_id)
        if series is None or series.size < 30:
            continue
        placed = False
        for rep, members in clusters:
            other = sample[rep]
            mask = np.isfinite(series) & np.isfinite(other)
            if int(mask.sum()) < 30:
                continue
            corr = float(np.corrcoef(series[mask], other[mask])[0, 1])
            if np.isfinite(corr) and abs(corr) >= threshold:
                members.append(factor_id)
                placed = True
                break
        if not placed:
            clusters.append((factor_id, [factor_id]))
    return [members for _, members in clusters if len(members) > 1]


def qlib_forward_return(panel, *, lag: int, horizon: int, start: date, end: date):
    """close(t+lag+horizon) / close(t+lag) - 1。lag=1、horizon=1 即 close(t+2)/close(t+1)-1。"""
    import polars as pl

    dates = sorted(item for item in panel.get_column("date").unique().to_list() if item is not None)
    index = {item: pos for pos, item in enumerate(dates)}
    rows = []
    for item in dates:
        if item < start or item > end:
            continue
        pos = index[item]
        base_at = pos + lag
        target_at = pos + lag + horizon
        if target_at >= len(dates):
            continue
        rows.append({
            "date": item,
            "_base_date": dates[base_at],
            "_target_date": dates[target_at],
        })
    empty = panel.select("symbol", "date").unique().with_columns(
        pl.lit(None).cast(pl.Float64).alias("_qlib_return"),
    )
    if not rows:
        return empty
    date_dtype = panel.schema["date"]
    mapping = pl.DataFrame(rows).with_columns(
        pl.col("date").cast(date_dtype),
        pl.col("_base_date").cast(date_dtype),
        pl.col("_target_date").cast(date_dtype),
    )
    prices = panel.select("symbol", "date", "close").unique(subset=["symbol", "date"], keep="last")
    base = prices.rename({"date": "_base_date", "close": "_base_close"})
    target = prices.rename({"date": "_target_date", "close": "_target_close"})
    return (
        panel.select("symbol", "date").unique()
        .join(mapping, on="date", how="left")
        .join(base, on=["symbol", "_base_date"], how="left")
        .join(target, on=["symbol", "_target_date"], how="left")
        .with_columns(
            pl.when(
                pl.col("_base_close").is_not_null()
                & (pl.col("_base_close") != 0)
                & pl.col("_target_close").is_not_null()
            )
            .then(pl.col("_target_close") / pl.col("_base_close") - 1.0)
            .otherwise(None)
            .alias("_qlib_return")
        )
        .select("symbol", "date", "_qlib_return")
    )


def _rank_ic_mean(panel, factor: str, ret: str) -> float | None:
    import polars as pl

    ic = (
        panel.filter(
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


def _drop_young_listings(frame, min_listed_days: int):
    """上市不足 min_listed_days 个交易日的行去掉。上市日早于本面板第一天的视为已满。"""
    import polars as pl

    if "listing_date" not in frame.columns:
        raise RuntimeError("instruments 没有 listing_date，不能按上市天数过滤。")
    if frame["listing_date"].dtype == pl.Utf8:
        frame = frame.with_columns(pl.col("listing_date").str.to_date(strict=False))
    dates = frame.get_column("date").unique().sort().to_list()
    if not dates:
        return frame
    first = dates[0]
    calendar = pl.DataFrame({"date": dates}).with_row_index("_di")
    right = calendar.rename({"date": "listing_date", "_di": "_li"}).sort("listing_date")
    known = (
        frame.select("symbol", "listing_date")
        .unique(subset=["symbol"])
        .filter(pl.col("listing_date").is_not_null())
        .sort("listing_date")
    )
    matched = known.join_asof(right, on="listing_date", strategy="forward")
    frame = (
        frame.join(calendar, on="date", how="left")
        .join(matched.select("symbol", "_li"), on="symbol", how="left")
    )
    age = (
        pl.when(pl.col("listing_date").is_null() | (pl.col("listing_date") <= first))
        .then(pl.lit(min_listed_days))
        .otherwise(pl.col("_di") - pl.col("_li") + 1)
    )
    return frame.filter(age >= min_listed_days).drop("_di", "_li")


def apply_row_filters(
    panel,
    *,
    exclude_limit: bool,
    exclude_st: bool,
    min_listed_days: int,
    instruments,
):
    """按当天涨跌停、当前名称里的 ST、上市交易日数删行。因子列已经算完，这里只改截面样本。"""
    import polars as pl

    if not exclude_limit and not exclude_st and min_listed_days <= 0:
        return panel
    if instruments is None or instruments.is_empty() or "symbol" not in instruments.columns:
        raise RuntimeError("过滤需要 instruments（名称、上市日），当前没有这份表。")
    keep = ["symbol"]
    for column in ("name", "listing_date"):
        if column in instruments.columns:
            keep.append(column)
    info = instruments.select(keep).unique(subset=["symbol"], keep="last")
    frame = panel.join(info, on="symbol", how="left")
    if exclude_st:
        if "name" not in frame.columns:
            raise RuntimeError("instruments 没有 name，不能排除 ST。")
        from app.price_limits import polars_is_risk_warning_name

        frame = frame.filter(~polars_is_risk_warning_name(pl.col("name")).fill_null(False))
    if min_listed_days > 0:
        frame = _drop_young_listings(frame, min_listed_days)
    if exclude_limit:
        if "raw_close" not in frame.columns:
            raise RuntimeError("面板没有 raw_close，不能按涨跌停过滤。")
        from app.price_limits import (
            polars_is_risk_warning_name,
            polars_limit_price,
            polars_price_limit_pct,
        )

        frame = frame.sort(["symbol", "date"])
        prev = pl.col("raw_close").shift(1).over("symbol")
        is_st = (
            polars_is_risk_warning_name(pl.col("name"))
            if "name" in frame.columns
            else pl.lit(False)
        )
        limit_pct = polars_price_limit_pct(pl.col("symbol"), pl.col("date"), is_st)
        up = polars_limit_price(prev, limit_pct, up=True)
        down = polars_limit_price(prev, limit_pct, up=False)
        at_limit = prev.is_not_null() & (
            ((pl.col("raw_close") - up).abs() <= 0.011)
            | ((pl.col("raw_close") - down).abs() <= 0.011)
        )
        frame = frame.filter(~at_limit.fill_null(False))
    extras = [column for column in ("name", "listing_date") if column in frame.columns and column not in panel.columns]
    if extras:
        frame = frame.drop(extras)
    return frame


def _sample_matrix(frame, factor_ids: list[str], *, max_symbols: int, tail_dates: int) -> dict[str, np.ndarray]:
    import polars as pl

    dates = frame.get_column("date").unique().sort().tail(tail_dates)
    symbols = frame.get_column("symbol").unique(maintain_order=True).head(max_symbols)
    sample = frame.filter(
        pl.col("date").is_in(dates.to_list()) & pl.col("symbol").is_in(symbols.to_list())
    ).sort(["date", "symbol"])
    out: dict[str, np.ndarray] = {}
    for factor_id in factor_ids:
        if factor_id not in sample.columns:
            continue
        out[factor_id] = sample.get_column(factor_id).to_numpy()
    return out


def drop_thin_dates(panel, min_symbols: int):
    """去掉横截面不足 min_symbols 只的交易日。

    返回 (过滤后的 panel, 保留日期的每日样本数表, 剔除的日期数)。
    分区还没补录全的日子只有几只股票, 这种日子的 Rank IC 是噪声, 会主导均值。

    调用方要把它放在算 market_dates 之前: 被剔除的日子不该参与 next_return,
    否则它们仍会通过"下一天收益"把标签带回来。
    """
    import polars as pl

    counts = panel.group_by("date").agg(pl.len().alias("_n"))
    thick = counts.filter(pl.col("_n") >= min_symbols)
    keep = set(thick.get_column("date").to_list())
    return panel.filter(pl.col("date").is_in(keep)), thick, counts.height - len(keep)


def _polars_gate() -> str | None:
    try:
        import polars as pl
    except ImportError:
        return (
            "没有安装 polars。请使用仓库里的后端虚拟环境:\n"
            "  PYTHONPATH=backend backend/.venv/bin/python scripts/eval_alpha158.py"
        )
    if polars_matches_project(pl.__version__):
        return None
    return (
        f"当前 polars {pl.__version__} 不符合 backend/pyproject.toml 的约束 polars>=1.44,<1.45。\n"
        "polars 2.x 去掉了 LazyFrame.collect(streaming=...)，因子面板会加载失败。\n"
        "请改用仓库的 backend/.venv:\n"
        "  PYTHONPATH=backend backend/.venv/bin/python scripts/eval_alpha158.py"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评估 Alpha158 实验组最近约一年的 IC")
    parser.add_argument("--days", type=int, default=365, help="检验区间长度，自然日，默认 365")
    parser.add_argument("--end", default="", help="截止日 YYYY-MM-DD；默认是早于今天的最近分区")
    parser.add_argument("--chunk", type=int, default=8, help="每批因子数，默认 8")
    parser.add_argument("--symbols", default="", help="逗号分隔的股票代码；留空为全市场")
    parser.add_argument("--corr-symbols", type=int, default=400, help="相关聚类最多使用的股票数")
    parser.add_argument("--corr-dates", type=int, default=20, help="相关聚类使用的最近交易日数")
    parser.add_argument("--threshold", type=float, default=0.8, help="冗余聚类的 |corr| 阈值")
    parser.add_argument(
        "--label-lag", type=int, default=0,
        help="额外打印从 close(t+lag) 到 close(t+lag+1) 的 Rank IC。1 为 Qlib 口径，0 不打印",
    )
    parser.add_argument("--exclude-limit", action="store_true", help="去掉当天涨停或跌停的股票")
    parser.add_argument("--exclude-st", action="store_true", help="去掉当前名称含 ST 的股票")
    parser.add_argument("--min-listed-days", type=int, default=0, help="去掉上市不足 N 个交易日的股票，例如 60")
    parser.add_argument(
        "--min-symbols-per-date", type=int, default=0,
        help="去掉横截面不足 N 只的交易日，默认 0 不过滤。分区还没补录全的日子只有几只股票，"
             "这种日子的 Rank IC 是噪声，会主导均值",
    )
    args = parser.parse_args(argv)

    gate = _polars_gate()
    if gate:
        print(gate)
        return 1
    if args.label_lag < 0:
        print("--label-lag 不能为负", file=sys.stderr)
        return 1
    if args.chunk < 1:
        print("--chunk 必须 ≥ 1", file=sys.stderr)
        return 1
    if args.min_listed_days < 0:
        print("--min-listed-days 不能为负", file=sys.stderr)
        return 1
    if args.min_symbols_per_date < 0:
        print("--min-symbols-per-date 不能为负", file=sys.stderr)
        return 1

    data_dir = _data_dir()
    enriched = data_dir / "kline_daily_enriched"
    partitions = _partition_dates(enriched)
    if not partitions:
        return _print_missing(data_dir)

    today = beijing_today()
    if args.end:
        end = date.fromisoformat(args.end)
        if end >= today:
            print(f"警告: --end {end.isoformat()} 不是已收盘的历史日，当日分区可能仍在写入。")
    else:
        closed = latest_closed_trading_day(partitions, today)
        if closed is None:
            print(f"没有早于 {today.isoformat()} 的 enriched 分区，盘中不评估今天。")
            return 2
        end = closed
    start = end - timedelta(days=args.days)

    import polars as pl
    from app.backtest.engine import BacktestEngine
    from app.backtest.factor import FactorBacktestService, FactorBatchConfig, FactorConfig
    from app.enriched_generation import EnrichedGenerationUnavailableError
    from app.factors.alpha158 import ALPHA158_IDS, compute_alpha158
    from app.factors.registry import get_factor
    from app.tickflow.repository import DataStore, KlineRepository

    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] or None
    engine = BacktestEngine(KlineRepository(DataStore(data_dir)))
    service = FactorBacktestService(engine)
    factor_ids = list(ALPHA158_IDS)
    filtering = args.exclude_limit or args.exclude_st or args.min_listed_days > 0
    instruments = engine.repo.get_instruments() if filtering and engine.repo is not None else None
    print(
        f"区间 {start.isoformat()} ~ {end.isoformat()}（北京时间今天 {today.isoformat()}），"
        f"因子 {len(factor_ids)} 个，每批 {args.chunk} 个。"
    )
    if filtering:
        print(
            "过滤: "
            + " ".join(part for part in (
                "去掉涨跌停" if args.exclude_limit else "",
                "去掉 ST" if args.exclude_st else "",
                f"上市满 {args.min_listed_days} 个交易日" if args.min_listed_days else "",
            ) if part)
        )
        print("ST 用的是 instruments 里的当前名称，不是历史上每一天的 ST 状态。")

    rows: list[dict] = []
    warned_warmup = False
    warned_thin = False
    for offset in range(0, len(factor_ids), args.chunk):
        batch_ids = factor_ids[offset:offset + args.chunk]
        batch_config = FactorBatchConfig(
            factor_names=batch_ids,
            symbols=symbols,
            start=start,
            end=end,
            n_groups=5,
            rebalance="daily",
            asset_type="stock",
        )
        try:
            panel = service._load_factor_panel(batch_config, batch_ids)
        except EnrichedGenerationUnavailableError as exc:
            print(f"enriched 分区在读取过程中变了: {exc}")
            print("请改用已经收盘的 --end，不要评今天仍在写入的分区。")
            return 1
        if panel.is_empty():
            print(f"本批没有数据 ({batch_ids[0]} …)")
            return _print_missing(data_dir)
        if not warned_warmup:
            prior = panel.filter(pl.col("date") < start).get_column("date").n_unique()
            print(f"起点前交易日 {prior} 个。60 日窗口至少需要 61 个；不足时这一档在区间开头为空。")
            if prior < 61:
                print("预热交易日不够 61，60 日因子的覆盖率会偏低。这和 IC 接近 0 不是一回事，先看覆盖率。")
            warned_warmup = True
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
        # 横截面门槛要放在算 market_dates 之前: 门槛内的日期不该参与 next_return,
        # 否则被剔除的日子仍会通过"下一天收益"把标签带进来。
        if args.min_symbols_per_date > 0:
            panel, thick, dropped = drop_thin_dates(panel, args.min_symbols_per_date)
            if not warned_thin:
                sizes = thick.get_column("_n")
                print(
                    f"横截面门槛: 去掉不足 {args.min_symbols_per_date} 只的交易日 {dropped} 个，"
                    f"剩 {thick.height} 个；每天样本数 中位 {int(sizes.median())}、最少 {int(sizes.min())}。"
                )
                warned_thin = True
        market_dates = sorted(
            item for item in panel.get_column("date").unique().to_list()
            if start <= item <= end
        )
        panel = service._attach_shared_next_return(
            panel, batch_config, trading_dates=market_dates,
        )
        if args.label_lag:
            labels = qlib_forward_return(
                panel, lag=args.label_lag, horizon=1, start=start, end=end,
            )
            panel = panel.join(labels, on=["symbol", "date"], how="left")
        try:
            panel = apply_row_filters(
                panel,
                exclude_limit=args.exclude_limit,
                exclude_st=args.exclude_st,
                min_listed_days=args.min_listed_days,
                instruments=instruments,
            )
        except RuntimeError as exc:
            print(str(exc))
            return 1
        for index, factor_name in enumerate(batch_ids, start=1):
            spec = get_factor(factor_name)
            label = spec.label if spec is not None else factor_name
            factor_config = FactorConfig(
                factor_name=factor_name,
                symbols=symbols,
                start=start,
                end=end,
                n_groups=5,
                rebalance="daily",
                asset_type="stock",
            )
            try:
                result = service._evaluate_panel(
                    panel,
                    factor_config,
                    f"a158-{offset + index}",
                    0.0,
                    market_trading_dates=market_dates,
                )
            except Exception as exc:  # noqa: BLE001 — 单因子失败不能中止整组
                rows.append({
                    "factor_name": factor_name,
                    "label": label,
                    "ic_mean": None,
                    "ir": None,
                    "turnover": None,
                    "coverage": None,
                    "n_dates": 0,
                    "ic_lag": None,
                    "abs_ic": None,
                    "abs_ir": None,
                    "ic_decay": [],
                    "error": str(exc),
                })
                continue
            ic = result.ic_mean
            ir = result.ir
            ic_lag = None
            if args.label_lag and not result.error and factor_name in panel.columns and "_qlib_return" in panel.columns:
                scoped = panel.filter((pl.col("date") >= start) & (pl.col("date") <= end))
                ic_lag = _rank_ic_mean(scoped, factor_name, "_qlib_return")
            rows.append({
                "factor_name": factor_name,
                "label": label,
                "ic_mean": ic,
                "ir": ir,
                "turnover": result.turnover,
                "coverage": result.coverage,
                "n_dates": result.n_dates,
                "ic_lag": ic_lag,
                "abs_ic": abs(ic) if ic is not None else None,
                "abs_ir": abs(ir) if ir is not None else None,
                "ic_decay": result.ic_decay,
                "error": result.error,
            })
        print(f"  完成 {min(offset + args.chunk, len(factor_ids))}/{len(factor_ids)}")

    usable = [row for row in rows if row["error"] is None and row["abs_ic"] is not None]
    failed = [row for row in rows if row["error"]]
    if not usable:
        print("没有算出有效 IC。请确认 enriched 分区覆盖这段日期。")
        for row in failed[:8]:
            print(f"  {row['factor_name']}: {row['error']}")
        return 1

    by_ic = sorted(usable, key=lambda row: row["abs_ic"], reverse=True)
    by_ir = sorted(
        [row for row in usable if row["abs_ir"] is not None],
        key=lambda row: row["abs_ir"],
        reverse=True,
    )
    with_lag = args.label_lag > 0
    _print_table("按 |IC| 最高的 15 个", by_ic[:15], with_lag=with_lag)
    _print_table("按 |IC| 最低的 15 个", list(reversed(by_ic[-15:])), with_lag=with_lag)
    _print_table("按 |ICIR| 最高的 15 个", by_ir[:15], with_lag=with_lag)
    _print_table("按 |ICIR| 最低的 15 个", list(reversed(by_ir[-15:])), with_lag=with_lag)

    print()
    print("IC 衰减（|IC| 最高的 5 个，horizon 为从 close(t) 出发的向前收益日）")
    for row in by_ic[:5]:
        parts = [
            f"{point.get('horizon')}日={_fmt(point.get('ic_mean'))}"
            for point in row["ic_decay"]
        ]
        print(f"  {row['factor_name']}: {', '.join(parts) if parts else '—'}")
    if with_lag:
        print(f"IC_lag 是 close(t+{args.label_lag}+1) / close(t+{args.label_lag}) - 1 的 Rank IC，和上表 IC 并列，不替换它。")

    if failed:
        print()
        print(f"计算失败 {len(failed)} 个:")
        for row in failed:
            print(f"  {row['factor_name']}: {row['error']}")

    probe_config = FactorBatchConfig(
        factor_names=["a158_kmid"],
        symbols=symbols,
        start=end - timedelta(days=max(args.corr_dates * 3, 40)),
        end=end,
        asset_type="stock",
    )
    try:
        probe = service._load_factor_panel(probe_config, ["a158_kmid"])
    except EnrichedGenerationUnavailableError as exc:
        print()
        print(f"相关聚类跳过，分区在读取时变了: {exc}")
        return 0
    if probe.is_empty():
        print()
        print("样本面板为空，跳过相关聚类。")
        return 0
    symbols_for_corr = (
        probe.get_column("symbol").unique(maintain_order=True).head(args.corr_symbols).to_list()
    )
    probe = probe.filter(pl.col("symbol").is_in(symbols_for_corr))
    if args.exclude_limit and "raw_close" not in probe.columns:
        raw = engine.load_panel(
            symbols_for_corr,
            probe.get_column("date").min(),
            end,
            columns=["symbol", "date", "raw_close"],
            asset_type="stock",
        )
        if not raw.is_empty() and "raw_close" in raw.columns:
            probe = probe.join(raw.select("symbol", "date", "raw_close"), on=["symbol", "date"], how="left")
    try:
        probe = apply_row_filters(
            probe,
            exclude_limit=args.exclude_limit,
            exclude_st=args.exclude_st,
            min_listed_days=args.min_listed_days,
            instruments=instruments,
        )
    except RuntimeError as exc:
        print()
        print(f"相关聚类跳过: {exc}")
        return 0
    valued = compute_alpha158(probe, chunk_symbols=200)
    sample = _sample_matrix(
        valued,
        [row["factor_name"] for row in by_ic],
        max_symbols=args.corr_symbols,
        tail_dates=args.corr_dates,
    )
    clusters = _cluster(sample, [row["factor_name"] for row in by_ic], args.threshold)
    print()
    print(f"相关聚类 |corr| ≥ {args.threshold:.2f}，样本 {args.corr_dates} 个交易日 × 最多 {args.corr_symbols} 只")
    if not clusters:
        print("  没有达到阈值的冗余簇。")
    else:
        for index, members in enumerate(clusters, start=1):
            print(f"  簇 {index} ({len(members)}): {', '.join(members)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0) from None
