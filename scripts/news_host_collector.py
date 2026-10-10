#!/usr/bin/env python3
"""宿主机定时采集钉钉 / 知识星球，写入 TSP 的 data/news/inbox。

只调用只读子命令。登录失效时可选发钉钉机器人提醒，不转发消息正文。
"""
from __future__ import annotations

import argparse
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


def ensure_supported_python(version: tuple[int, ...] | None = None) -> None:
    """采集脚本最低 3.10。3.11 仍是推荐版本，重建步骤在 docs/news-sources.md。"""
    info = sys.version_info if version is None else version
    if info >= (3, 10):
        return
    current = ".".join(str(part) for part in info[:3])
    print(
        f"宿主机采集器需要 Python 3.10 或更高，当前是 {current}。"
        "推荐用 Python 3.11 重建 ~/.venvs/tsp-collector，步骤见 docs/news-sources.md。",
        file=sys.stderr,
    )
    raise SystemExit(1)


def main() -> int:
    ensure_supported_python()
    parser = argparse.ArgumentParser(description="宿主机只读采集钉钉和知识星球资讯")
    parser.add_argument("--data-dir", default="", help="TSP data 目录，默认 <仓库>/data 或 DATA_DIR")
    parser.add_argument("--backfill-since", default="", help="知识星球回补起点，如 2025-08-23")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_backend_path()))
    _load_dotenv(root / ".env")
    data_dir = Path(args.data_dir or os.environ.get("DATA_DIR") or (root / "data"))
    if not data_dir.is_absolute():
        data_dir = (root / data_dir).resolve()

    import subprocess

    from app.news.host_collector import run_host

    def run(argv: list[str]):
        return subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)

    summary = run_host(data_dir, run, backfill_since=args.backfill_since)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
