"""按新浪分红/份额折算重建 ``adj_factor_etf/all.parquet``。

用法（在 backend 目录）：

    uv run python -m scripts.rebuild_etf_adj_factor [--data-dir PATH] [--dry-run]
        [--symbols 510500.SH,512100.SH]

先把现有 ``all.parquet`` 复制到 ``backup/adj_factor_etf/``（与因子目录并列，
避免 ``adj_factor_etf/**/*.parquet`` 把旧因子再并进净流入和 DuckDB 视图），
再按标的整表替换。内容没变化时不备份、不写。拉取失败的标的保留旧行。
重复执行是幂等的。
"""
from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

from app.services.etf_adj_factor import (
    collect_etf_events,
    discover_etf_symbols,
    plan_replace,
    write_planned,
)


def backup_store(path: Path) -> Path | None:
    """复制当前因子表。文件不存在时不备份。

    备份放在数据目录的 ``backup/adj_factor_etf/``，不放进 ``adj_factor_etf/``。
    净流入、分钟复权和 DuckDB 视图会扫描 ``adj_factor_etf/**/*.parquet``，
    备份若留在里面，旧的错误因子会和修正后的表叠在一起。
    """
    if not path.is_file():
        return None
    dest_dir = path.parent.parent / "backup" / path.parent.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = dest_dir / f"all-{stamp}.parquet"
    serial = 0
    while dest.exists():
        serial += 1
        dest = dest_dir / f"all-{stamp}-{serial}.parquet"
    shutil.copy2(path, dest)
    return dest


def rebuild(
    data_dir: Path,
    symbols: list[str] | None = None,
    *,
    dry_run: bool = False,
    fetch_text=None,
    fetch_prices=None,
    sleep_s: float = 0.05,
) -> dict:
    """重建已存储的 ETF 除权因子。返回可打印的摘要。"""
    chosen = list(symbols) if symbols else discover_etf_symbols(data_dir)
    path = Path(data_dir) / "adj_factor_etf" / "all.parquet"
    summary = {
        "symbols": len(chosen),
        "replaced": 0,
        "failed": [],
        "affected": [],
        "backup": None,
        "written": False,
        "dry_run": dry_run,
        "rows": 0,
    }
    if not chosen:
        return summary
    incoming, replaced, failed = collect_etf_events(
        chosen, data_dir,
        fetch_text=fetch_text, fetch_prices=fetch_prices, sleep_s=sleep_s,
    )
    summary["replaced"] = len(replaced)
    summary["failed"] = failed
    merged, affected = plan_replace(path, incoming, replaced)
    summary["affected"] = affected
    if merged is not None:
        summary["rows"] = merged.height
    if dry_run or merged is None:
        return summary
    backup = backup_store(path)
    if path.is_file() and backup is None:
        raise RuntimeError(f"备份失败, 未改写 {path}")
    summary["backup"] = str(backup) if backup else None
    write_planned(path, merged)
    summary["written"] = True
    return summary


def _parse_symbols(text: str | None) -> list[str] | None:
    if not text:
        return None
    return [item.strip() for item in text.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="重建 ETF 除权因子 adj_factor_etf")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--symbols", default=None, help="逗号分隔，默认维表或已存储代码")
    args = parser.parse_args()
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        from app.config import settings

        data_dir = Path(settings.data_dir)
    summary = rebuild(data_dir, _parse_symbols(args.symbols), dry_run=args.dry_run)
    print(
        f"symbols={summary['symbols']} replaced={summary['replaced']} "
        f"affected={len(summary['affected'])} failed={len(summary['failed'])} "
        f"written={summary['written']} dry_run={summary['dry_run']}"
    )
    if summary["backup"]:
        print(f"backup={summary['backup']}")
    if summary["failed"]:
        print("failed=" + ",".join(summary["failed"]))


if __name__ == "__main__":
    main()
