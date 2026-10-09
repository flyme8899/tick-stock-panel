#!/usr/bin/env python3
"""用现有因子检验评估 Alpha158 实验组，打印排序表和相关聚类。

本环境若没有 A 股日线 enriched 分区，脚本会直接说明并退出，不编造 IC。
检验口径与「因子发现」相同：Rank IC、ICIR、换手、IC 衰减、分层多空。
因子按批计算，避免一次把 158 列都摊在全市场面板上。

开发环境（仓库根目录，与 ./dev.sh 同一套后端虚拟环境）:

    PYTHONPATH=backend backend/.venv/bin/python scripts/eval_alpha158.py

指定最近一段交易日和每批因子数:

    PYTHONPATH=backend backend/.venv/bin/python scripts/eval_alpha158.py --days 365 --chunk 8

相关聚类默认在最近 20 个交易日、最多 400 只股票的因子值上算皮尔逊相关，
|corr| ≥ 0.8 的归为同一簇。IC 仍按全市场（或 --symbols）计算。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np


def _data_dir() -> Path:
    from app.config import settings

    return Path(settings.data_dir)


def _parquet_count(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.rglob("*.parquet"))


def _print_missing(data_dir: Path) -> int:
    enriched = data_dir / "kline_daily_enriched"
    raw = data_dir / "kline_daily"
    print("当前环境没有可评估的 A 股日线数据，下面不给出 IC。")
    print(f"数据目录: {data_dir}")
    print(f"kline_daily_enriched parquet 文件数: {_parquet_count(enriched)}")
    print(f"kline_daily parquet 文件数: {_parquet_count(raw)}")
    print("因子检验读的是 kline_daily_enriched。分区里有 part.parquet 之后，在仓库根目录执行:")
    print("  PYTHONPATH=backend backend/.venv/bin/python scripts/eval_alpha158.py")
    return 2


def _fmt(value: float | None, digits: int = 4) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    return f"{value:.{digits}f}"


def _print_table(title: str, rows: list[dict]) -> None:
    print()
    print(title)
    header = f"{'因子':<16} {'名称':<22} {'IC':>8} {'ICIR':>8} {'换手':>8} {'|IC|':>8} {'|ICIR|':>8}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['factor_name']:<16} {row['label'][:22]:<22} "
            f"{_fmt(row['ic_mean']):>8} {_fmt(row['ir']):>8} {_fmt(row['turnover']):>8} "
            f"{_fmt(row['abs_ic']):>8} {_fmt(row['abs_ir']):>8}"
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


def _sample_matrix(
    frame,
    factor_ids: list[str],
    *,
    max_symbols: int,
    tail_dates: int,
) -> dict[str, np.ndarray]:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评估 Alpha158 实验组最近约一年的 IC")
    parser.add_argument("--days", type=int, default=365, help="检验区间长度，自然日，默认 365")
    parser.add_argument("--chunk", type=int, default=8, help="每批因子数，默认 8")
    parser.add_argument("--symbols", default="", help="逗号分隔的股票代码；留空为全市场")
    parser.add_argument("--corr-symbols", type=int, default=400, help="相关聚类最多使用的股票数")
    parser.add_argument("--corr-dates", type=int, default=20, help="相关聚类使用的最近交易日数")
    parser.add_argument("--threshold", type=float, default=0.8, help="冗余聚类的 |corr| 阈值")
    args = parser.parse_args(argv)

    data_dir = _data_dir()
    enriched = data_dir / "kline_daily_enriched"
    if _parquet_count(enriched) == 0:
        return _print_missing(data_dir)

    import polars as pl

    from app.backtest.engine import BacktestEngine
    from app.backtest.factor import FactorBacktestService, FactorBatchConfig
    from app.factors.alpha158 import ALPHA158_IDS, compute_alpha158
    from app.tickflow.repository import DataStore, KlineRepository

    end = date.today()
    start = end - timedelta(days=args.days)
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()] or None
    engine = BacktestEngine(KlineRepository(DataStore(data_dir)))
    service = FactorBacktestService(engine)
    factor_ids = list(ALPHA158_IDS)
    if args.chunk < 1:
        print("--chunk 必须 ≥ 1", file=sys.stderr)
        return 1

    rows: list[dict] = []
    print(f"区间 {start.isoformat()} ~ {end.isoformat()}，因子 {len(factor_ids)} 个，每批 {args.chunk} 个。")
    for offset in range(0, len(factor_ids), args.chunk):
        batch_ids = factor_ids[offset:offset + args.chunk]
        result = service.run_batch(FactorBatchConfig(
            factor_names=batch_ids,
            symbols=symbols,
            start=start,
            end=end,
            n_groups=5,
            rebalance="daily",
            asset_type="stock",
        ))
        if result.error:
            print(f"本批失败 ({batch_ids[0]} …): {result.error}")
            if "无数据" in result.error:
                return _print_missing(data_dir)
            return 1
        for item in result.results:
            ic = item.ic_mean
            ir = item.ir
            rows.append({
                "factor_name": item.factor_name,
                "label": item.label,
                "group": item.group,
                "ic_mean": ic,
                "ir": ir,
                "turnover": item.turnover,
                "abs_ic": abs(ic) if ic is not None else None,
                "abs_ir": abs(ir) if ir is not None else None,
                "ic_decay": item.ic_decay,
                "error": item.error,
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
    _print_table("按 |IC| 最高的 15 个", by_ic[:15])
    _print_table("按 |IC| 最低的 15 个", list(reversed(by_ic[-15:])))
    _print_table("按 |ICIR| 最高的 15 个", by_ir[:15])
    _print_table("按 |ICIR| 最低的 15 个", list(reversed(by_ir[-15:])))

    print()
    print("IC 衰减（|IC| 最高的 5 个，horizon 为向前收益日）")
    for row in by_ic[:5]:
        parts = [
            f"{point.get('horizon')}日={_fmt(point.get('ic_mean'))}"
            for point in row["ic_decay"]
        ]
        print(f"  {row['factor_name']}: {', '.join(parts) if parts else '—'}")

    if failed:
        print()
        print(f"计算失败 {len(failed)} 个:")
        for row in failed:
            print(f"  {row['factor_name']}: {row['error']}")

    # 相关聚类只取样本，不把 158 列留在全市场面板上。
    probe = service._load_factor_panel(
        FactorBatchConfig(
            factor_names=["a158_kmid"],
            symbols=symbols,
            start=end - timedelta(days=max(args.corr_dates * 3, 40)),
            end=end,
            asset_type="stock",
        ),
        ["a158_kmid"],
    )
    if probe.is_empty():
        print()
        print("样本面板为空，跳过相关聚类。")
        return 0
    symbols_for_corr = (
        probe.get_column("symbol").unique(maintain_order=True).head(args.corr_symbols).to_list()
    )
    probe = probe.filter(pl.col("symbol").is_in(symbols_for_corr))
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
        raise SystemExit(0)
