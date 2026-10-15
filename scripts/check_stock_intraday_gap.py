#!/usr/bin/env python3
r"""只读诊断：盘中个股资金流快照的**实际间隔分布**。

盘中快照（``stock_intraday``）的调度间隔由 ``scheduler._intraday_due()`` 决定：
**成功落盘后满 6 分钟才允许下一轮**（``STOCK_INTRADAY_GAP_S``）。这个脚本把线上
已经落盘的数据读出来，反向核对真实间隔是否符合预期。

只读：**不创建、不修改、不删除任何数据文件**，也不 import 任何写盘路径。
发现异常只打印说明并以退出码区分。

读什么
------

1. 各交易日的快照分区 ``fund_flow/stock_intraday/date=YYYY-MM-DD/part.parquet``
   里的 ``captured_at``（形如 ``2026-10-15T10:30:00``）去重排序后的相邻间隔。
2. 调度状态 ``fund_flow/_state/status.json`` 里 ``stock_intraday`` 的
   ``last_snapshot`` / ``last_success_at`` / ``last_attempt_at``，以及据此算出的
   当前落后时长。

怎么判读
--------

- 间隔稳定在 **6–7 分钟**：符合设定（6 分钟间隔 + 30 秒轮询粒度）。
- 间隔 **远小于 6 分钟**（例如 1–2 分钟）：说明退化成「拉完就再拉」——判据看的是
  尝试时刻而不是成功落盘时刻。``_intraday_due()`` 读的正是 ``last_snapshot`` /
  ``last_success_at``，所以真出现这种分布，优先查这两个字段有没有被正确写入。
- 间隔 **远大于 6 分钟**（例如 > 20 分钟）：说明盘中拉取断档，查熔断、
  ``tickflow_sync_busy`` 让路，或旁路线程卡死（``INTRADAY_THREAD_TIMEOUT_S``）。
- 一轮拉取失败时**本来就允许更早重试**，所以偶尔一个短间隔不代表调度有问题；
  看的是**整体分布**，不是单个值。

用法（开发环境，在仓库根目录执行）
----------------------------------

    PYTHONPATH=backend backend/.venv/bin/python scripts/check_stock_intraday_gap.py

Windows（``dev.ps1`` 同一套环境）:

    $env:PYTHONPATH = "backend"
    backend\.venv\Scripts\python scripts\check_stock_intraday_gap.py

指定数据目录或分区根目录:

    ... --data-dir /path/to/data
    ... --path /path/to/data/fund_flow/stock_intraday

只看最近 3 个交易日、并打印每个间隔:

    ... --days 3 --verbose

Docker 镜像不含 ``scripts/``。compose 把宿主机 ``./data`` 挂到 ``/app/data``,
可在仓库根目录执行:

    docker compose run --rm --no-deps --entrypoint uv \
        -v "$PWD/scripts/check_stock_intraday_gap.py:/tmp/check.py:ro" \
        app run --no-sync python /tmp/check.py

退出码
------

    0  正常（有数据，且未发现明显异常）
    1  发现异常（间隔明显偏离设定值）
    2  没读到数据，或路径/参数有误
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

import polars as pl

CN_TZ = timezone(timedelta(hours=8))

# 与 scheduler.STOCK_INTRADAY_GAP_S 对齐。这里不 import 调度器，
# 避免为了看一眼数据就把整个调度栈（含写盘路径）拉起来。
DEFAULT_GAP_S = 6 * 60

# 判定阈值：明显偏短 / 明显偏长。
SHORT_SUSPECT_S = DEFAULT_GAP_S // 2      # 3 分钟以内 -> 疑似「拉完就再拉」
LONG_SUSPECT_S = 20 * 60                  # 20 分钟以上 -> 疑似断档

_DATE_PREFIX = "date="
_CAPTURED_KEYS = ("last_snapshot", "last_success_at", "last_attempt_at")


@dataclass
class DayFinding:
    trade_date: str
    captures: list[datetime] = field(default_factory=list)
    intervals_s: list[float] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def short(self) -> list[float]:
        return [s for s in self.intervals_s if s < DEFAULT_GAP_S]

    @property
    def suspect_short(self) -> list[float]:
        return [s for s in self.intervals_s if s < SHORT_SUSPECT_S]

    @property
    def suspect_long(self) -> list[float]:
        return [s for s in self.intervals_s if s > LONG_SUSPECT_S]


def _parse_captured(value: object) -> datetime | None:
    """``captured_at`` 形如 ``2026-10-15T10:30:00``（无时区，按上海解释）。"""
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed


def _partition_dates(root: Path) -> list[str]:
    found = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith(_DATE_PREFIX):
            continue
        found.append(child.name[len(_DATE_PREFIX):])
    return found


def _inspect_day(root: Path, trade_date: str) -> DayFinding:
    finding = DayFinding(trade_date=trade_date)
    path = root / f"{_DATE_PREFIX}{trade_date}" / "part.parquet"
    if not path.exists():
        finding.problems.append("缺少 part.parquet")
        return finding
    try:
        frame = pl.read_parquet(path, columns=["captured_at"])
    except Exception as exc:  # noqa: BLE001 - 坏文件要记入诊断，不能中断扫描
        finding.problems.append(f"无法读取: {type(exc).__name__}: {exc}")
        return finding
    if "captured_at" not in frame.columns:
        finding.problems.append("缺少 captured_at 列")
        return finding

    stamps = sorted(
        {parsed for value in frame["captured_at"].to_list() if (parsed := _parse_captured(value))}
    )
    finding.captures = stamps
    if not stamps:
        finding.problems.append("captured_at 全为空或无法解析")
        return finding

    finding.intervals_s = [
        (later - earlier).total_seconds()
        for earlier, later in pairwise(stamps)
    ]
    return finding


def _read_state(data_dir: Path) -> dict:
    path = data_dir / "fund_flow" / "_state" / "status.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    kinds = payload.get("kinds")
    if not isinstance(kinds, dict):
        return {}
    raw = kinds.get("stock_intraday")
    return raw if isinstance(raw, dict) else {}


def _default_root(data_dir: Path | None) -> Path | None:
    """``settings.data_dir`` / fund_flow / stock_intraday。"""
    if data_dir is not None:
        return data_dir / "fund_flow" / "stock_intraday"
    try:
        from app.config import settings
    except Exception:  # noqa: BLE001 - 只读工具，装不上就让人用 --path
        return None
    return Path(settings.data_dir) / "fund_flow" / "stock_intraday"


def _fmt_seconds(value: float) -> str:
    return f"{value:,.0f}s ({value / 60:.1f}分)"


def _print_day(finding: DayFinding, *, verbose: bool) -> None:
    print(f"  {finding.trade_date}  快照 {len(finding.captures)} 份")
    if finding.problems:
        for item in finding.problems:
            print(f"      问题: {item}")
    if not finding.intervals_s:
        return
    gaps = finding.intervals_s
    print(
        f"      间隔: 最小 {_fmt_seconds(min(gaps))}"
        f" · 中位 {_fmt_seconds(statistics.median(gaps))}"
        f" · 均值 {_fmt_seconds(statistics.fmean(gaps))}"
        f" · 最大 {_fmt_seconds(max(gaps))}"
    )
    span = finding.captures[-1] - finding.captures[0]
    print(
        f"      首份 {finding.captures[0].strftime('%H:%M:%S')}"
        f" · 末份 {finding.captures[-1].strftime('%H:%M:%S')}"
        f" · 跨度 {_fmt_seconds(span.total_seconds())}"
    )
    if finding.suspect_short:
        print(
            f"      ⚠ 低于 3 分钟的间隔 {len(finding.suspect_short)} 个"
            f"（最短 {_fmt_seconds(min(finding.suspect_short))}）"
            " — 疑似退化成「拉完就再拉」，查 last_snapshot / last_success_at 是否写入"
        )
    if finding.suspect_long:
        print(
            f"      ⚠ 超过 20 分钟的间隔 {len(finding.suspect_long)} 个"
            f"（最长 {_fmt_seconds(max(finding.suspect_long))}）"
            " — 疑似断档，查熔断/让路/旁路线程卡死"
        )
    if verbose:
        for earlier, later in pairwise(finding.captures):
            print(
                f"        {earlier.strftime('%H:%M:%S')} -> {later.strftime('%H:%M:%S')}"
                f"  {_fmt_seconds((later - earlier).total_seconds())}"
            )


def _print_state(state: dict) -> None:
    print()
    print("调度状态（fund_flow/_state/status.json）")
    if not state:
        print("  未读到 stock_intraday 状态（可能还没跑过，或状态文件不在预期位置）")
        return
    now = datetime.now(CN_TZ)
    for key in _CAPTURED_KEYS:
        raw = state.get(key)
        if not raw:
            continue
        stamp = _parse_captured(raw)
        if stamp is None:
            print(f"  {key:<18} {raw}  (无法解析)")
            continue
        age = (now - stamp).total_seconds()
        note = ""
        if key == "last_snapshot":
            note = "  <- _intraday_due 首要依据"
        elif key == "last_success_at":
            note = "  <- _intraday_due 回退依据"
        elif key == "last_attempt_at":
            note = "  （_intraday_due 不看这个字段）"
        print(f"  {key:<18} {stamp.strftime('%Y-%m-%d %H:%M:%S')}  距今 {_fmt_seconds(age)}{note}")
    day = state.get("last_success_date")
    if day:
        print(f"  {'last_success_date':<18} {day}")
    if not state.get("last_snapshot") and not state.get("last_success_at"):
        print("  ⚠ 没有 last_snapshot 也没有 last_success_at：")
        print("      _intraday_due 会一直视为「首轮」，间隔判定失效（每轮都该拉）。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读诊断：盘中个股资金流快照的实际间隔分布。",
    )
    parser.add_argument(
        "--path",
        type=Path,
        help="stock_intraday 分区根目录（里面有 date=YYYY-MM-DD/part.parquet）。默认取 TSP data_dir 下的 fund_flow/stock_intraday",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="TSP 的数据目录（会自动拼 fund_flow/stock_intraday）",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=5,
        help="只看最近 N 个交易日分区，默认 5",
    )
    parser.add_argument("--verbose", action="store_true", help="打印每一个间隔")
    parser.add_argument(
        "--gap",
        type=float,
        default=DEFAULT_GAP_S,
        help=f"设定的间隔秒数，默认 {DEFAULT_GAP_S}（6 分钟），仅用于提示文字",
    )
    args = parser.parse_args(argv)

    root = args.path if args.path is not None else _default_root(args.data_dir)
    print("=" * 68)
    print("盘中个股资金流间隔诊断（只读）")
    print("=" * 68)

    if root is None:
        print("无法确定数据目录：本项目依赖不可用（import app.config 失败）。")
        print("请显式指定：--data-dir <TSP数据目录>  或  --path <stock_intraday根目录>")
        return 2

    print(f"分区根目录: {root}")
    print(f"设定间隔  : {_fmt_seconds(args.gap)}")
    print()

    if not root.exists():
        print("目录不存在 —— 说明这个库里还没有落过盘中快照，或路径不对。")
        return 2

    dates = _partition_dates(root)
    if not dates:
        print("没有找到任何 date=YYYY-MM-DD 分区。")
        return 2

    recent = dates[-max(1, args.days):]
    print(f"共 {len(dates)} 个交易日分区，查看最近 {len(recent)} 个：")
    print()

    findings = [_inspect_day(root, day) for day in recent]
    for finding in findings:
        _print_day(finding, verbose=args.verbose)

    state = _read_state(args.data_dir) if args.data_dir else _read_state(
        root.parent.parent if root.parent.name == "fund_flow" else Path(".")
    )
    _print_state(state)

    # 汇总判定
    scanned = [f for f in findings if f.captures]
    problems = [p for f in findings for p in f.problems]
    short = sum(len(f.suspect_short) for f in scanned)
    long = sum(len(f.suspect_long) for f in scanned)

    print()
    print("=" * 68)
    if not scanned:
        print("结论: 没有可判读的快照数据" + ("（有分区但缺 captured_at）" if problems else ""))
        return 2
    print(f"结论: 判读 {len(scanned)} 个交易日")
    if short:
        print(f"  ⚠ {short} 个间隔低于 3 分钟 —— 疑似「拉完就再拉」")
    if long:
        print(f"  ⚠ {long} 个间隔超过 20 分钟 —— 疑似断档")
    if not short and not long:
        print("  ✅ 未发现明显异常：间隔集中在设定值附近")
    if problems:
        print(f"  另有 {len(problems)} 处数据问题（见上）")
    return 1 if (short or long) else 0


if __name__ == "__main__":
    sys.exit(main())
