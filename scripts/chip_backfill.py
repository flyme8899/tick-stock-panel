#!/usr/bin/env python3
"""回补筹码分布日表。可中断，已有分区会跳过，并以低优先级运行。

在 backend 目录执行，这样用得到已安装的 polars::

    uv run python ../scripts/chip_backfill.py --data-dir ../data
    uv run python ../scripts/chip_backfill.py --since 2021-06-01 --until 2026-10-09
    uv run python ../scripts/chip_backfill.py --force
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    # 限制计算线程，避免回补占满 2 核。必须在 import polars / numpy 之前。
    os.environ.setdefault("POLARS_MAX_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("NUMBA_NUM_THREADS", "1")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "backend"))
    parser = argparse.ArgumentParser(description="回补本地筹码分布（东财算法）")
    parser.add_argument("--data-dir", default=str(root / "data"))
    parser.add_argument("--since", default="", help="起始交易日 YYYY-MM-DD")
    parser.add_argument("--until", default="", help="结束交易日 YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="覆盖已有分区")
    args = parser.parse_args()

    from app.chip.jobs import backfill, parse_cli_day

    summary = backfill(
        args.data_dir,
        since=parse_cli_day(args.since),
        until=parse_cli_day(args.until),
        force=args.force,
        priority=True,
    )
    print(
        f"筹码回补完成：新写 {summary['written']} 天，跳过 {summary['skipped']} 天"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
