"""盘中个股资金流快照的**间隔观测**（只读）。

盘中个股资金流（``stock_intraday``）由 ``fund_flow/scheduler.py`` 调度，间隔判据是
``_intraday_due()``：**成功落盘后满 6 分钟**（``STOCK_INTRADAY_GAP_S``）才允许下一轮。
本模块把**已经落盘的数据**读出来，反向核对真实间隔是否符合预期，并顺带回答一个
运维问题：这台实例到底**有没有在写**盘中快照。

**只读保证**：只调 ``pl.read_parquet`` / ``json.loads`` / ``Path.iterdir``；
不写入、不删除、不改名任何文件；不 import 调度或写盘路径（``scheduler`` / ``jobs`` /
``store`` 都不 import），所以不会因为看一眼数据就把调度栈拉起来。

容器内直接可用（``app/`` 在镜像里；``scripts/`` 被 ``.dockerignore`` 排除）::

    # compose 里已设 DATA_DIR=/app/data，无参即可
    docker compose exec app python -m app.fund_flow.intraday_gap

    # 或显式指定
    docker compose exec app python -m app.fund_flow.intraday_gap --data-dir /app/data --days 3 --verbose

    # 只看分区目录
    docker compose exec app python -m app.fund_flow.intraday_gap --path /app/data/fund_flow/stock_intraday

开发环境::

    cd backend && uv run python -m app.fund_flow.intraday_gap --days 3

读什么
------

1. ``fund_flow/stock_intraday/date=YYYY-MM-DD/part.parquet`` 里的 ``captured_at``
   （形如 ``2026-10-15T10:30:00``），去重排序后的相邻间隔。
2. ``fund_flow/_state/status.json`` 里 ``stock_intraday`` 的 ``last_snapshot`` /
   ``last_success_at`` / ``last_attempt_at`` / ``last_error`` / ``last_success_date``。

怎么判读
--------

- **间隔稳定在 6–7 分钟**：符合设定（6 分钟间隔 + 30 秒轮询粒度）。
- **普遍 < 3 分钟**：见下方「为什么短间隔不该出现」。先确认 ``last_snapshot`` /
  ``last_success_at`` 有没有被正确写入（本模块会把这两个字段单独列出来）。
- **普遍 > 20 分钟**：盘中断档。查 ``last_error``、熔断、``tickflow_sync_busy`` 让路，
  或旁路线程卡死（``INTRADAY_THREAD_TIMEOUT_S``）。
- 一轮**失败**时本来就允许更早重试，所以偶尔出现一个短间隔不代表调度有问题 ——
  看的是**整体分布**，不是单个值。

为什么短间隔不该出现
--------------------

``stock_intraday`` 只有两条写入路径（``scheduler.py`` 的旁路线程与同线程分支），
两条都由 ``due_tasks`` 门控；``catchup_tasks`` **不包含** ``stock_intraday``，
没有旁路补采。而 ``_intraday_due()`` 的要求是「距上次成功落盘满 6 分钟」，
且 ``captured_at`` 与 ``last_success_at`` 取自**同一个** ``now``。

因此在**当前代码**下，同一交易日内相邻快照的间隔不应该小于 6 分钟（扣掉秒级取整）。
若实测普遍 < 3 分钟，说明前提被打破了，优先查这三件事：

1. ``last_snapshot`` / ``last_success_at`` 实际有没有写进去（写不进去 ⇒ 每轮都算首轮）；
2. 是否有**别的进程/实例**也在往同一个 ``data/`` 写（两个容器共用一个卷）；
3. 是不是手工触发过 ``sync_stock_intraday``（补数、调试），那会绕过调度间隔。

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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

import polars as pl

CN_TZ = timezone(timedelta(hours=8))

#: 与 ``scheduler.STOCK_INTRADAY_GAP_S`` 对齐。这里刻意不 import 调度器，
#: 避免为了读一份数据就把整个调度栈（含写盘路径）拉起来。
DEFAULT_GAP_S = 6 * 60
GAP_ENV_HINT = "scheduler.STOCK_INTRADAY_GAP_S"

#: 判定阈值：明显偏短 / 明显偏长。
SHORT_SUSPECT_S = DEFAULT_GAP_S // 2      # 3 分钟以内 -> 当前代码下不该出现，见模块 docstring
LONG_SUSPECT_S = 20 * 60                  # 20 分钟以上 -> 疑似断档

DATE_PREFIX = "date="
STATUS_KEYS = (
    "last_snapshot", "last_success_at", "last_attempt_at",
    "last_success_date", "last_error",
)
#: ``_intraday_due()`` 认这两个字段（前者优先）。
DUE_KEYS = ("last_snapshot", "last_success_at")


@dataclass
class DayFinding:
    """单个交易日的快照与间隔。"""

    trade_date: str
    captures: list[datetime] = field(default_factory=list)
    intervals_s: list[float] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def suspect_short(self) -> list[float]:
        return [s for s in self.intervals_s if s < SHORT_SUSPECT_S]

    @property
    def suspect_long(self) -> list[float]:
        return [s for s in self.intervals_s if s > LONG_SUSPECT_S]


@dataclass
class GapReport:
    """一次观测的完整结果。"""

    root: Path | None
    gap_s: float = DEFAULT_GAP_S
    findings: list[DayFinding] = field(default_factory=list)
    state: dict = field(default_factory=dict)
    error: str = ""
    total_dates: int = 0

    @property
    def scanned(self) -> list[DayFinding]:
        return [f for f in self.findings if f.captures]

    @property
    def short_count(self) -> int:
        return sum(len(f.suspect_short) for f in self.scanned)

    @property
    def long_count(self) -> int:
        return sum(len(f.suspect_long) for f in self.scanned)

    @property
    def problems(self) -> list[str]:
        return [p for f in self.findings for p in f.problems]

    @property
    def verdict(self) -> str:
        """``ok`` / ``suspect`` / ``nodata``。"""
        if not self.scanned:
            return "nodata"
        return "suspect" if (self.short_count or self.long_count) else "ok"

    @property
    def exit_code(self) -> int:
        return {"ok": 0, "suspect": 1, "nodata": 2}[self.verdict]

    @property
    def writing(self) -> bool:
        """这台实例到底有没有在写盘中快照。"""
        if self.scanned:
            return True
        return bool(self.state.get("last_snapshot") or self.state.get("last_success_at"))

    @property
    def due_stamp(self) -> datetime | None:
        """``_intraday_due()`` 实际会读的那个时刻（``last_snapshot`` 优先）。"""
        for key in DUE_KEYS:
            stamp = parse_captured(self.state.get(key))
            if stamp is not None:
                return stamp
        return None


def parse_captured(value: object) -> datetime | None:
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


def partition_dates(root: Path) -> list[str]:
    """列出 ``date=YYYY-MM-DD`` 分区日，升序。"""
    found = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name.startswith(DATE_PREFIX):
            found.append(child.name[len(DATE_PREFIX):])
    return found


def inspect_day(root: Path, trade_date: str) -> DayFinding:
    """读一个交易日的 ``captured_at`` 并算相邻间隔。坏文件记问题，不抛。"""
    finding = DayFinding(trade_date=trade_date)
    path = root / f"{DATE_PREFIX}{trade_date}" / "part.parquet"
    if not path.exists():
        finding.problems.append("缺少 part.parquet")
        return finding
    try:
        frame = pl.read_parquet(path, columns=["captured_at"])
    except Exception as exc:  # noqa: BLE001 - 坏分区要记入诊断，不能中断整轮扫描
        finding.problems.append(f"无法读取: {type(exc).__name__}: {exc}")
        return finding
    if "captured_at" not in frame.columns:
        finding.problems.append("缺少 captured_at 列")
        return finding

    stamps = sorted(
        {parsed for value in frame["captured_at"].to_list() if (parsed := parse_captured(value))}
    )
    finding.captures = stamps
    if not stamps:
        finding.problems.append("captured_at 全为空或无法解析")
        return finding
    finding.intervals_s = [
        (later - earlier).total_seconds() for earlier, later in pairwise(stamps)
    ]
    return finding


def read_kind_state(data_dir: Path, kind: str = "stock_intraday") -> dict:
    """读 ``fund_flow/_state/status.json`` 里某个 kind 的状态。读不到返回 ``{}``。"""
    path = Path(data_dir) / "fund_flow" / "_state" / "status.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    kinds = payload.get("kinds")
    if not isinstance(kinds, dict):
        return {}
    raw = kinds.get(kind)
    return raw if isinstance(raw, dict) else {}


def default_root(data_dir: Path | None = None) -> Path | None:
    """``data_dir`` / fund_flow / stock_intraday。无参时取 ``settings.data_dir``。"""
    if data_dir is not None:
        return Path(data_dir) / "fund_flow" / "stock_intraday"
    try:
        from app.config import settings
    except Exception:  # noqa: BLE001 - 只读工具，装不上就让人用 --path
        return None
    return Path(settings.data_dir) / "fund_flow" / "stock_intraday"


def state_data_dir(root: Path) -> Path | None:
    """从分区根目录反推 ``data_dir``（``.../fund_flow/stock_intraday`` -> 上一层）。"""
    if root.parent.name == "fund_flow":
        return root.parent.parent
    return None


def analyze(
    root: Path | None,
    *,
    days: int = 5,
    data_dir: Path | None = None,
    gap_s: float = DEFAULT_GAP_S,
) -> GapReport:
    """读分区与状态，产出报告。不写盘。"""
    report = GapReport(root=root, gap_s=gap_s)
    if root is None:
        report.error = "无法确定数据目录"
        return report
    if not root.exists():
        report.error = "目录不存在"
        return report

    dates = partition_dates(root)
    if not dates:
        report.error = "没有找到任何 date=YYYY-MM-DD 分区"
        return report

    recent = dates[-max(1, days):]
    report.total_dates = len(dates)
    report.findings = [inspect_day(root, day) for day in recent]

    resolved = data_dir if data_dir is not None else state_data_dir(root)
    if resolved is not None:
        report.state = read_kind_state(resolved)
    return report


def _fmt_seconds(value: float) -> str:
    return f"{value:,.0f}s ({value / 60:.1f}分)"


def _render_day(finding: DayFinding, *, verbose: bool) -> list[str]:
    out = [f"  {finding.trade_date}  快照 {len(finding.captures)} 份"]
    for item in finding.problems:
        out.append(f"      问题: {item}")
    if not finding.intervals_s:
        return out
    gaps = finding.intervals_s
    out.append(
        f"      间隔: 最小 {_fmt_seconds(min(gaps))}"
        f" · 中位 {_fmt_seconds(statistics.median(gaps))}"
        f" · 均值 {_fmt_seconds(statistics.fmean(gaps))}"
        f" · 最大 {_fmt_seconds(max(gaps))}"
    )
    span = finding.captures[-1] - finding.captures[0]
    out.append(
        f"      首份 {finding.captures[0].strftime('%H:%M:%S')}"
        f" · 末份 {finding.captures[-1].strftime('%H:%M:%S')}"
        f" · 跨度 {_fmt_seconds(span.total_seconds())}"
    )
    if finding.suspect_short:
        out.append(
            f"      ⚠ 低于 3 分钟的间隔 {len(finding.suspect_short)} 个"
            f"（最短 {_fmt_seconds(min(finding.suspect_short))}）"
            " — 见文末「为什么短间隔不该出现」"
        )
    if finding.suspect_long:
        out.append(
            f"      ⚠ 超过 20 分钟的间隔 {len(finding.suspect_long)} 个"
            f"（最长 {_fmt_seconds(max(finding.suspect_long))}）"
            " — 疑似断档，查 last_error / 熔断 / 让路 / 旁路线程卡死"
        )
    if verbose:
        for earlier, later in pairwise(finding.captures):
            out.append(
                f"        {earlier.strftime('%H:%M:%S')} -> {later.strftime('%H:%M:%S')}"
                f"  {_fmt_seconds((later - earlier).total_seconds())}"
            )
    return out


def _render_state(report: GapReport) -> list[str]:
    out = ["", "调度状态（fund_flow/_state/status.json）"]
    state = report.state
    if not state:
        out.append("  未读到 stock_intraday 状态（还没跑过，或状态文件不在预期位置）")
        return out
    now = datetime.now(CN_TZ)
    for key in STATUS_KEYS:
        raw = state.get(key)
        if not raw:
            continue
        if key == "last_error":
            out.append(f"  {key:<18} {str(raw)[:160]}")
            continue
        if key == "last_success_date":
            out.append(f"  {key:<18} {raw}")
            continue
        stamp = parse_captured(raw)
        if stamp is None:
            out.append(f"  {key:<18} {raw}  (无法解析)")
            continue
        age = (now - stamp).total_seconds()
        note = {
            "last_snapshot": "  <- _intraday_due 首要依据",
            "last_success_at": "  <- _intraday_due 回退依据",
            "last_attempt_at": "  （_intraday_due 不看这个字段）",
        }.get(key, "")
        out.append(
            f"  {key:<18} {stamp.strftime('%Y-%m-%d %H:%M:%S')}"
            f"  距今 {_fmt_seconds(age)}{note}"
        )
    if not any(state.get(k) for k in DUE_KEYS):
        out.append("  ⚠ 没有 last_snapshot 也没有 last_success_at：")
        out.append("      _intraday_due 会一直视为「首轮」，间隔判定失效（每轮都该拉）。")
    return out


def render(report: GapReport, *, verbose: bool = False) -> str:
    """把报告渲染成给人看的文本。"""
    out = [
        "=" * 68,
        "盘中个股资金流间隔观测（只读）",
        "=" * 68,
    ]
    if report.root is not None:
        out.append(f"分区根目录: {report.root}")
    out.append(f"设定间隔  : {_fmt_seconds(report.gap_s)}（{GAP_ENV_HINT}）")
    out.append("")

    if report.error:
        if report.error == "目录不存在":
            out.append("目录不存在 —— 这台实例还没落过盘中快照，或路径不对。")
        else:
            out.append(report.error + "。")
        return "\n".join(out)

    total = report.total_dates or len(report.findings)
    out.append(f"共 {total} 个交易日分区，查看最近 {len(report.findings)} 个：")
    out.append("")
    for finding in report.findings:
        out.extend(_render_day(finding, verbose=verbose))
    out.extend(_render_state(report))

    out.extend(["", "=" * 68])
    if report.verdict == "nodata":
        tail = "（有分区但缺 captured_at）" if report.problems else ""
        out.append("结论: 没有可判读的快照数据" + tail)
        if not report.writing:
            out.append("  这台实例看起来**没有在写**盘中快照：既无有效分区，状态里也没有成功时刻。")
        return "\n".join(out)
    out.append(f"结论: 判读 {len(report.scanned)} 个交易日")
    if report.short_count:
        out.append(
            f"  ⚠ {report.short_count} 个间隔低于 3 分钟 —— 当前代码下不该出现，"
            "优先查 last_snapshot/last_success_at 是否写入、是否有多实例共写同一 data/"
        )
    if report.long_count:
        out.append(f"  ⚠ {report.long_count} 个间隔超过 20 分钟 —— 疑似断档，查 last_error")
    if report.verdict == "ok":
        out.append("  ✅ 未发现明显异常：间隔集中在设定值附近")
    if report.problems:
        out.append(f"  另有 {len(report.problems)} 处数据问题（见上）")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.fund_flow.intraday_gap",
        description="只读观测：盘中个股资金流快照的实际间隔分布。",
    )
    parser.add_argument(
        "--path",
        type=Path,
        help="stock_intraday 分区根目录（含 date=YYYY-MM-DD/part.parquet）。"
             "默认取 TSP data_dir 下的 fund_flow/stock_intraday",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="TSP 数据目录（自动拼 fund_flow/stock_intraday）。容器内一般是 /app/data",
    )
    parser.add_argument("--days", type=int, default=5, help="只看最近 N 个交易日分区，默认 5")
    parser.add_argument("--verbose", action="store_true", help="打印每一个间隔")
    parser.add_argument(
        "--gap",
        type=float,
        default=DEFAULT_GAP_S,
        help=f"设定间隔秒数，默认 {DEFAULT_GAP_S}（6 分钟），仅用于提示文字",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.path if args.path is not None else default_root(args.data_dir)
    report = analyze(root, days=args.days, data_dir=args.data_dir, gap_s=args.gap)
    print(render(report, verbose=args.verbose))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
