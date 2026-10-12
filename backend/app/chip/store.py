"""筹码日表路径。``data/chip_daily/date=YYYY-MM-DD/part.parquet``，一天一分区。"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl

CHIP_DIR = "chip_daily"
ENRICHED_DIR = "kline_daily_enriched"

CHIP_COLUMNS = (
    "benefit_ratio",
    "avg_cost",
    "cost_90_low",
    "cost_90_high",
    "concentration_90",
    "cost_70_low",
    "cost_70_high",
    "concentration_70",
    "avg_cost_bias",
)

CHIP_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "trade_date": pl.Date,
    "benefit_ratio": pl.Float64,
    "avg_cost": pl.Float64,
    "cost_90_low": pl.Float64,
    "cost_90_high": pl.Float64,
    "concentration_90": pl.Float64,
    "cost_70_low": pl.Float64,
    "cost_70_high": pl.Float64,
    "concentration_70": pl.Float64,
    "avg_cost_bias": pl.Float64,
}


def enriched_root(data_dir: Path | str) -> Path:
    return Path(data_dir) / ENRICHED_DIR


def chip_root(data_dir: Path | str) -> Path:
    return Path(data_dir) / CHIP_DIR


def partition_file(data_dir: Path | str, day: date) -> Path:
    return chip_root(data_dir) / f"date={day.isoformat()}" / "part.parquet"


def progress_file(data_dir: Path | str) -> Path:
    return chip_root(data_dir) / "_backfill.json"


def list_dates(root: Path) -> list[date]:
    if not root.exists():
        return []
    found: list[date] = []
    for part in root.glob("date=*"):
        if not part.is_dir():
            continue
        try:
            found.append(date.fromisoformat(part.name[5:]))
        except ValueError:
            continue
    return sorted(found)


def parquet_paths(root: Path, days: list[date]) -> list[str]:
    paths: list[str] = []
    for day in days:
        folder = root / f"date={day.isoformat()}"
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.parquet")):
            if path.name.endswith(".tmp"):
                continue
            paths.append(str(path))
    return paths


def parse_day(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None
