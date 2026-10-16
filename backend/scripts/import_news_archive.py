"""一次性：把研究目录里补采的东财 7×24 和财联社加红导进历史归档库。

用法（容器内，research 目录需只读挂进来）：
    uv run --no-sync python -m scripts.import_news_archive \
        --research /research/event_driven_buy [--data-dir /app/data] [--dry-run]

可重复跑：按 (source, source_id) INSERT OR IGNORE，已有的不覆盖。
不写 news.sqlite，不触发推送、热点、提醒。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import datetime
from pathlib import Path

from app.market_time import CN_TZ
from app.news.archive import NewsArchive

_BATCH = 5000


def _cls_stocks(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return []
    out = []
    for s in value or []:
        if isinstance(s, dict) and s.get("StockID"):
            out.append(str(s["StockID"]))
    return out


def iter_cls(root: Path):
    for path in sorted(root.glob("raw_*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    ctime = int(r["ctime"])
                except (ValueError, KeyError, TypeError):
                    continue
                if not r.get("id"):
                    continue
                yield {
                    "source": "cls",
                    "source_id": str(r["id"]),
                    "published_at": datetime.fromtimestamp(ctime, CN_TZ),
                    "title": r.get("title") or "",
                    "content": r.get("content") or r.get("brief") or "",
                    "level": str(r.get("level") or ""),
                    "stock_codes": _cls_stocks(r.get("stock_list")),
                    "url": r.get("shareurl") or "",
                }


def iter_eastmoney(root: Path):
    import polars as pl

    for path in sorted(root.glob("month=*/*.parquet")):
        frame = pl.read_parquet(path, hive_partitioning=False)
        for r in frame.iter_rows(named=True):
            when = r.get("publish_time")
            if when is None or not r.get("id"):
                continue
            yield {
                "source": "eastmoney_724",
                "source_id": str(r["id"]),
                "published_at": when,
                "title": r.get("title") or "",
                "content": r.get("summary") or "",
                "level": "",
                "stock_codes": list(r.get("stock_codes") or []),
                "url": r.get("url") or "",
            }


def load(archive: NewsArchive | None, rows, *, label: str) -> dict:
    total = {"read": 0, "inserted": 0, "duplicate": 0}
    batch = []

    def flush():
        if archive is not None and batch:
            got = archive.insert_many(batch)
            total["inserted"] += got["inserted"]
            total["duplicate"] += got["duplicate"]
        batch.clear()

    for row in rows:
        total["read"] += 1
        batch.append(row)
        if len(batch) >= _BATCH:
            flush()
    flush()
    print(f"{label}: {total}", flush=True)
    return total


def run(research: Path, db: Path, *, dry_run: bool = False, only: str = "all") -> dict:
    archive = None if dry_run else NewsArchive(db)
    out = {}
    try:
        if only in ("all", "eastmoney"):
            src = research / "backfill_server" / "parquet"
            if src.exists():
                out["eastmoney_724"] = load(archive, iter_eastmoney(src), label="eastmoney_724")
            else:
                print(f"跳过东财：{src} 不存在", file=sys.stderr)
        if only in ("all", "cls"):
            src = research / "backfill_cls"
            if src.exists():
                out["cls"] = load(archive, iter_cls(src), label="cls")
            else:
                print(f"跳过财联社：{src} 不存在", file=sys.stderr)
        if archive is not None:
            for row in archive.stats():
                print(row)
    finally:
        if archive is not None:
            archive.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--research", type=Path, required=True, help="research/event_driven_buy 目录")
    ap.add_argument("--data-dir", type=Path, default=Path("/app/data"))
    ap.add_argument("--only", choices=("all", "eastmoney", "cls"), default="all")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.research, a.data_dir / "news_archive.sqlite", dry_run=a.dry_run, only=a.only)


if __name__ == "__main__":
    main()
