#!/usr/bin/env python3
"""DSA 钉一致性与钩子挂载自检（只读，不改工作树）。

流程见 ``docs/dsa-upgrade.md``。``--check`` 是唯一模式：只读文件、只比对，
不写盘、不调 git、不起容器，重复执行结果一致，适合在开 PR 前跑一次。

检查项：四处钉同值且都是 40 位小写十六进制；钉不是 ``latest`` / ``main`` /
``HEAD`` 之类浮动引用；五个钩子文件齐全；compose 已把 ``fund_flow_bridge.py``
等钩子只读挂进 ``/opt/tsp/``。

退出码：``0`` 全部一致，``1`` 有不一致（逐条打印原因）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SHA_RE = re.compile(r"[0-9a-f]{40}")
FLOATING = {"latest", "main", "master", "head", "develop", "dev", "trunk"}

_COMMIT_LINE = re.compile(r"Upstream commit:\s*`([^`]+)`")

_PIN_SOURCES = (
    (
        "catalog.py",
        "backend/app/custom/dsa/catalog.py",
        re.compile(r'^UPSTREAM_COMMIT\s*=\s*"([^"]+)"', re.M),
    ),
    ("VENDOR.md", "vendor/daily_stock_analysis/VENDOR.md", _COMMIT_LINE),
    ("dsa-integration.md", "docs/dsa-integration.md", re.compile(r"提交\s*`([^`]+)`")),
    ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md", _COMMIT_LINE),
)

# 钩子文件名 -> 容器内挂载路径（只读）。
_HOOKS = {
    "quant_evidence.py": "/opt/tsp/quant_evidence.py",
    "news_bridge.py": "/opt/tsp/news_bridge.py",
    "etf_job.py": "/opt/tsp/etf_job.py",
    "fund_flow_bridge.py": "/opt/tsp/fund_flow_bridge.py",
    "analysis_bridge.py": "/opt/tsp/analysis_bridge.py",
}
_HOOK_DIR = "backend/app/custom/dsa"
_COMPOSE = "docker-compose.yml"


@dataclass
class Report:
    ok: bool = True
    pins: dict[str, str | None] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)


def _read(root: Path, relative: str) -> str | None:
    try:
        return (root / relative).read_text(encoding="utf-8")
    except OSError:
        return None


def check_pins(root: Path, report: Report) -> None:
    for label, relative, pattern in _PIN_SOURCES:
        text = _read(root, relative)
        if text is None:
            report.pins[label] = None
            report.fail(f"缺少 {relative}")
            continue
        found = pattern.search(text)
        value = found.group(1).strip() if found else None
        report.pins[label] = value
        if value is None:
            report.fail(f"{relative} 里没找到钉")
        elif value.lower() in FLOATING or not SHA_RE.fullmatch(value):
            report.fail(f"{relative} 的钉不是 40 位小写十六进制：{value}")

    values = {label: value for label, value in report.pins.items() if value}
    if len(values) == len(_PIN_SOURCES) and len(set(values.values())) > 1:
        detail = "、".join(f"{label}={value}" for label, value in values.items())
        report.fail(f"四处钉不一致：{detail}")


def check_hooks(root: Path, report: Report) -> None:
    for name in _HOOKS:
        if not (root / _HOOK_DIR / name).is_file():
            report.fail(f"缺少钩子文件 {_HOOK_DIR}/{name}")


def check_compose(root: Path, report: Report) -> None:
    text = _read(root, _COMPOSE)
    if text is None:
        report.fail(f"缺少 {_COMPOSE}")
        return
    for name, target in _HOOKS.items():
        expected = f"./{_HOOK_DIR}/{name}:{target}:ro"
        if expected not in text:
            report.fail(f"{_COMPOSE} 没把 {name} 只读挂到 {target}")


def run(root: Path = ROOT) -> Report:
    report = Report()
    check_pins(root, report)
    check_hooks(root, report)
    check_compose(root, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DSA 钉一致性与钩子挂载自检（只读）")
    parser.add_argument("--check", action="store_true", help="只检查不改动（默认行为）")
    parser.add_argument("--root", default=str(ROOT), help="仓库根目录，默认脚本上一级")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    report = run(Path(args.root).resolve())
    if args.json:
        payload = {"ok": report.ok, "pins": report.pins, "errors": report.errors}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif report.ok:
        pins = {value for value in report.pins.values() if value}
        print(f"OK 四处钉一致 {pins.pop()}；五个钩子齐全且已挂载")
    else:
        print("FAIL DSA 钉 / 钩子自检未通过：")
        for message in report.errors:
            print(f"  - {message}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
