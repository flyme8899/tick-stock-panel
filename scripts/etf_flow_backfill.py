#!/usr/bin/env python3
"""回补公众号「ETF领航者」指定交易日的申赎稿。

网易号列表和搜狗微信归档都搜。可以早于 2026-07-08。
没有稿件的日期记成「源缺失」，下次默认不再请求。

示例::

    python scripts/etf_flow_backfill.py --data-dir ./data --dates 2026-07-07,2026-06-30
    python scripts/etf_flow_backfill.py --data-dir ./data --since 2026-06-01 --until 2026-07-07
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _backend_path() -> Path:
    return Path(__file__).resolve().parents[1] / "backend"


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_backend_path()))
    _load_dotenv(root / ".env")
    from app.news.etf_flow import main as run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
