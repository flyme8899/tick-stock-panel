"""一次性：把历史个股资金流分区的 symbol 收成带后缀（600000.SH）。

用法（容器内）：
    python -m scripts.normalize_fund_flow_symbols [--data-dir /app/data] [--dry-run]

只改 symbol 列，不增删行、不补数据。行数偏少的分区（比如 2026-10-08 只有 6 行 mx）
只报告，不动内容以外的任何东西。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

from app.fund_flow import store
from app.services.fs_utils import atomic_write_parquet


def run(data_dir: Path, *, dry_run: bool = False) -> list[dict]:
    report = []
    for kind in sorted(store.SUFFIX_SYMBOL_KINDS):
        for day in store.list_dates(data_dir, kind):
            path = store.partition_file(data_dir, kind, day)
            raw = pl.read_parquet(path)
            fixed = store.align_frame(kind, raw)
            changed = 0
            if "symbol" in raw.columns and raw.height == fixed.height:
                changed = int((raw["symbol"].fill_null("") != fixed["symbol"].fill_null("")).sum())
            item = {
                "kind": kind, "date": day, "rows": fixed.height, "changed": changed,
                "thin": kind == "stock" and fixed.height < store.FULL_MARKET_MIN,
            }
            report.append(item)
            if changed and not dry_run:
                atomic_write_parquet(fixed, path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        from app.config import settings

        data_dir = Path(settings.data_dir)
    for item in run(data_dir, dry_run=args.dry_run):
        flag = "  <- 行数不足全市场，只标记不补" if item["thin"] else ""
        print(f"{item['kind']} {item['date']} rows={item['rows']} changed={item['changed']}{flag}")


if __name__ == "__main__":
    main()
