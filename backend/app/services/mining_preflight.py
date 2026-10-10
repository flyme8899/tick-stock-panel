"""Lightweight mining date availability checks shared by API and workers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.backtest.mining import (
    nested_fold_count,
    required_outer_folds,
    required_trading_bars,
    validation_config_for_profile,
)
from app.tickflow.repository import enriched_dirname


@dataclass(frozen=True)
class MiningAvailability:
    asset_type: str
    budget_profile: str
    trading_bars: int
    required_bars: int
    outer_folds: int
    required_outer_folds: int
    eligible: bool
    available_start: date | None
    available_end: date | None
    effective_start: date | None
    effective_end: date | None
    suggested_start: date | None
    # 本地历史不够时, 告诉用户还差多少交易日、以及去哪补:
    # 缺口按窗口内有效交易日数计算, 而非自然日。
    deficit_bars: int
    suggested_backfill_value: int | None
    suggested_backfill_unit: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value.isoformat() if isinstance(value, date) else value
            for key, value in asdict(self).items()
        }


def enriched_partition_dates(
    data_dir: Path,
    asset_type: str,
    start: date | None = None,
    end: date | None = None,
) -> list[date]:
    root = data_dir / enriched_dirname(asset_type)
    values: set[date] = set()
    for partition in root.glob("date=*"):
        try:
            value = date.fromisoformat(partition.name.removeprefix("date="))
        except ValueError:
            continue
        if start is not None and value < start:
            continue
        if end is not None and value > end:
            continue
        if (partition / "part.parquet").is_file():
            values.add(value)
    return sorted(values)


def mining_availability(
    data_dir: Path,
    *,
    asset_type: str,
    budget_profile: str,
    start: date | None = None,
    end: date | None = None,
) -> MiningAvailability:
    if asset_type not in {"stock", "etf"}:
        raise ValueError(f"unsupported mining asset type: {asset_type}")
    if start is not None and end is not None and start > end:
        raise ValueError("mining start must not be after end")

    config = validation_config_for_profile(budget_profile)
    required_folds = required_outer_folds(budget_profile)
    required_bars = required_trading_bars(config, required_folds)
    all_dates = enriched_partition_dates(data_dir, asset_type)
    scoped = [
        value
        for value in all_dates
        if (start is None or value >= start) and (end is None or value <= end)
    ]
    dates_through_end = [
        value for value in all_dates if end is None or value <= end
    ]
    suggested_start = (
        dates_through_end[-required_bars]
        if len(dates_through_end) >= required_bars
        else None
    )
    trading_bars = len(scoped)
    # 本地物理深度不足时的补救指引: 数据页「向前扩展历史」按 x 天/月/年补,
    # 这里把交易日缺口换算成建议补的跨度 (向上取整到月, 1 月按 21 交易日估)。
    # 仅当窗口已覆盖全部本地数据 (即真缺历史, 而非把区间选窄) 时给出建议。
    full_history_scoped = start is None and end is None
    deficit_bars = max(0, required_bars - trading_bars)
    suggested_backfill_value: int | None = None
    suggested_backfill_unit: str | None = None
    if deficit_bars > 0 and full_history_scoped and all_dates:
        months = -(-deficit_bars // 21)  # ceil
        if months <= 36:
            suggested_backfill_value, suggested_backfill_unit = months, "month"
        else:
            years = -(-deficit_bars // 243)  # ceil, 一年约 243 交易日
            suggested_backfill_value, suggested_backfill_unit = years, "year"
    return MiningAvailability(
        asset_type=asset_type,
        budget_profile=budget_profile,
        trading_bars=trading_bars,
        required_bars=required_bars,
        outer_folds=nested_fold_count(trading_bars, config),
        required_outer_folds=required_folds,
        eligible=trading_bars >= required_bars,
        available_start=all_dates[0] if all_dates else None,
        available_end=all_dates[-1] if all_dates else None,
        effective_start=scoped[0] if scoped else None,
        effective_end=scoped[-1] if scoped else None,
        suggested_start=suggested_start,
        deficit_bars=deficit_bars,
        suggested_backfill_value=suggested_backfill_value,
        suggested_backfill_unit=suggested_backfill_unit,
    )


def require_mining_availability(
    data_dir: Path,
    *,
    asset_type: str,
    budget_profile: str,
    start: date | None = None,
    end: date | None = None,
) -> MiningAvailability:
    availability = mining_availability(
        data_dir,
        asset_type=asset_type,
        budget_profile=budget_profile,
        start=start,
        end=end,
    )
    if availability.eligible:
        return availability

    if availability.effective_start is None:
        effective_range = "contains no enriched data"
    else:
        effective_range = (
            f"{availability.effective_start.isoformat()} to "
            f"{availability.effective_end.isoformat()}"
        )
    fold_label = "outer fold" if availability.required_outer_folds == 1 else "outer folds"
    hint = ""
    if availability.suggested_backfill_value and availability.suggested_backfill_unit:
        hint = (
            f"; 可在数据页用「向前扩展历史」补约 "
            f"{availability.suggested_backfill_value} "
            f"{'个月' if availability.suggested_backfill_unit == 'month' else '年'}"
        )
    raise ValueError(
        f"{budget_profile} mining requires at least {availability.required_bars} "
        f"enriched trading bars for {availability.required_outer_folds} {fold_label}; "
        f"effective range {effective_range} has {availability.trading_bars} "
        f"(缺 {availability.deficit_bars} 个交易日){hint}"
    )
