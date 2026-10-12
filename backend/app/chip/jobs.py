"""筹码日表：盘后写当天，历史回补可中断后续跑。

全市场一天大约是最近 210 根前复权 K。单核实测量级是十几秒，不进 API 请求路径。
回补把进程 nice 调高，让出 2 核机器上的盘后管道和页面。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

import polars as pl

from app.chip.algorithm import LOOKBACK_BARS, ChipResult, compute_window
from app.chip.store import (
    CHIP_SCHEMA,
    enriched_root,
    list_dates,
    parquet_paths,
    parse_day,
    partition_file,
    progress_file,
)
from app.parquet import scan_enriched_parquet
from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger("tsp.chip")

_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "turnover_rate"]
_WINDOW_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "turnover_rate": pl.Float64,
}


def lower_priority() -> None:
    """回补让出 CPU。权限不够就继续跑，不把回补变成失败。"""
    try:
        os.nice(19)
    except OSError:
        logger.info("chip backfill keeps the current priority")


def refresh_latest(data_dir: Path | str) -> dict:
    """重写最近一个已落盘交易日。除权后前复权价会变，所以当天分区每次都覆盖。"""
    days = list_dates(enriched_root(data_dir))
    if not days:
        return {"date": None, "rows": 0}
    day = days[-1]
    rows = write_date(data_dir, day, force=True)
    return {"date": day.isoformat(), "rows": rows}


def write_date(data_dir: Path | str, day: date, *, force: bool = False) -> int:
    """写 ``chip_daily/date=<day>/part.parquet``。已有分区且 ``force`` 为假时直接返回。"""
    target = partition_file(data_dir, day)
    if target.exists() and not force:
        return 0
    frame = load_market_window(data_dir, day)
    table = summarize(frame, day)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(table, target)
    return table.height


def backfill(
    data_dir: Path | str,
    *,
    since: date | None = None,
    until: date | None = None,
    force: bool = False,
    priority: bool = True,
) -> dict:
    """按交易日补历史分区。已有 ``part.parquet`` 的日期跳过，杀掉后续跑接着写。"""
    if priority:
        lower_priority()
    days = list_dates(enriched_root(data_dir))
    if since is not None:
        days = [day for day in days if day >= since]
    if until is not None:
        days = [day for day in days if day <= until]
    written: list[str] = []
    skipped: list[str] = []
    for index, day in enumerate(days, start=1):
        path = partition_file(data_dir, day)
        if path.exists() and not force:
            skipped.append(day.isoformat())
            _write_progress(data_dir, day, index, len(days), skipped=True)
            continue
        rows = write_date(data_dir, day, force=True)
        written.append(day.isoformat())
        _write_progress(data_dir, day, index, len(days), rows=rows)
        logger.info("chip backfill %s rows=%s (%s/%s)", day, rows, index, len(days))
    return {"written": len(written), "skipped": len(skipped), "dates": written}


def load_market_window(data_dir: Path | str, day: date) -> pl.DataFrame:
    """目标日及之前最近 210 个交易日的前复权 OHLC 和换手率。没有目标日则空表。"""
    days = [item for item in list_dates(enriched_root(data_dir)) if item <= day]
    if not days or days[-1] != day:
        return pl.DataFrame(schema=_WINDOW_SCHEMA)
    window = days[-LOOKBACK_BARS:]
    paths = parquet_paths(enriched_root(data_dir), window)
    if not paths:
        return pl.DataFrame(schema=_WINDOW_SCHEMA)
    frame = scan_enriched_parquet(paths).select(_COLUMNS).collect()
    if frame.is_empty() or "date" not in frame.columns:
        return frame
    present = frame.filter(pl.col("date") == day)
    if present.is_empty():
        return present
    symbols = present.get_column("symbol").unique().to_list()
    return frame.filter(pl.col("symbol").is_in(symbols)).sort(["symbol", "date"])


def summarize(frame: pl.DataFrame, day: date) -> pl.DataFrame:
    """每个在目标日有 K 线的股票一行。换手全缺时指标为空，不写成 0。"""
    if frame.is_empty() or "symbol" not in frame.columns:
        return pl.DataFrame(schema=CHIP_SCHEMA)
    rows: list[dict] = []
    for key, group in frame.group_by("symbol", maintain_order=True):
        symbol = key[0] if isinstance(key, tuple) else key
        group = group.sort("date").tail(LOOKBACK_BARS)
        if group.filter(pl.col("date") == day).is_empty():
            continue
        result = compute_window(
            group["open"].to_numpy(),
            group["high"].to_numpy(),
            group["low"].to_numpy(),
            group["close"].to_numpy(),
            group["turnover_rate"].to_numpy(),
            with_bins=False,
        )
        rows.append(_row(str(symbol), day, result))
    if not rows:
        return pl.DataFrame(schema=CHIP_SCHEMA)
    return pl.DataFrame(rows, schema=CHIP_SCHEMA).sort("symbol")


def _row(symbol: str, day: date, result: ChipResult) -> dict:
    if not result.computable:
        values = {name: None for name in CHIP_SCHEMA if name not in {"symbol", "trade_date"}}
    else:
        values = {
            "benefit_ratio": result.benefit_ratio,
            "avg_cost": result.avg_cost,
            "cost_90_low": result.cost_90_low,
            "cost_90_high": result.cost_90_high,
            "concentration_90": result.concentration_90,
            "cost_70_low": result.cost_70_low,
            "cost_70_high": result.cost_70_high,
            "concentration_70": result.concentration_70,
            "avg_cost_bias": result.avg_cost_bias,
        }
    return {"symbol": symbol, "trade_date": day, **values}


def _write_progress(
    data_dir: Path | str,
    day: date,
    index: int,
    total: int,
    *,
    rows: int | None = None,
    skipped: bool = False,
) -> None:
    path = progress_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_date": day.isoformat(),
        "index": index,
        "total": total,
        "rows": rows,
        "skipped": skipped,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def parse_cli_day(text: str | None) -> date | None:
    if text is None or not str(text).strip():
        return None
    day = parse_day(text)
    if day is None:
        raise ValueError(f"无法解析日期: {text}")
    return day
