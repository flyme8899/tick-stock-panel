#!/usr/bin/env python3
r"""只读检查分钟 K 分区是否与 kline_sync 的落盘类型一致。

不创建、不修改、不删除任何数据文件。发现问题只打印说明。

kline_sync._normalize_minute / _write_minute_partition 写入的列:

    symbol    String
    datetime  Datetime(us)、无时区（北京时间墙钟）
    open/high/low/close/volume/amount    Float64

开发环境（与 ./dev.sh 同一套后端虚拟环境，在仓库根目录执行）:

    PYTHONPATH=backend backend/.venv/bin/python scripts/check_minute_kline.py

Windows（dev.ps1 同一套环境）:

    $env:PYTHONPATH = "backend"
    backend\.venv\Scripts\python scripts\check_minute_kline.py

Docker 镜像不含 scripts/。compose 把宿主机 ./data 挂到容器 /app/data，
并强制 DATA_DIR=/app/data。在仓库根目录执行:

    docker compose run --rm --no-deps --entrypoint uv -v "$PWD/scripts/check_minute_kline.py:/tmp/check_minute_kline.py:ro" app run --no-sync python /tmp/check_minute_kline.py

指定分区根目录（里面应有 date=YYYY-MM-DD/part.parquet）:

    ... --path /path/to/kline_minute

ETF 分钟在 data/kline_etf_minute，需要时用 --path 指向它。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

CANONICAL_COLUMNS = (
    "symbol",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)
_FLOAT_COLUMNS = ("open", "high", "low", "close", "volume", "amount")

REPAIR_TEXT = """\
修复（本脚本不会执行这些步骤）:
1. 先停掉会写分钟分区的进程（实时全量分钟、正在跑的 sync_minute），避免边修边写。
2. 把有问题的 date=YYYY-MM-DD 整目录复制到 kline_minute 外面做备份。
   不要把备份留在 kline_minute 里面，否则 **/*.parquet 视图会把它扫进去。
3. kline_sync._write_minute_partition 会先读入已有 part.parquet，再与新数据纵向拼接。
   volume 为 Int64 时，和新的 Float64 拼在一起会失败。坏文件还在原处时，
   直接重拉通常修不好类型。
4. 备份确认可读之后，把该日的 part.parquet 移出分区目录，再用现有接口重拉
   覆盖这些日期的窗口:
   - 全市场: POST /api/kline/sync_minute ，body {"days": N}
   - 单只: POST /api/kline/sync_minute_single ，days 为 1 到 30，只补这一只
   整日文件被覆盖过时，缺掉的股票要靠重拉回来。只把 Int64 转成 Float64
   补不回被盖掉的行。
5. 重拉后再运行本脚本，结论应为 OK。
   带确认开关的修复在 scripts/repair_minute_kline.py：默认只打印计划；
   --apply 才会备份、移走坏文件并重拉。重拉没有写回、或日期超出大约一年的，
   用备份做类型转换写回，不留空洞。仅转换类型补不回被整日覆盖丢掉的股票。
   不要把修复放进这个诊断脚本。
"""


@dataclass
class PartitionFinding:
    date: str
    rows: int | None = None
    stocks: int | None = None
    duplicate_keys: int = 0
    extra_rows: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and self.duplicate_keys == 0 and self.extra_rows == 0


@dataclass
class Report:
    root: Path
    missing_root: bool = False
    partitions: list[PartitionFinding] = field(default_factory=list)
    tmp_files: list[str] = field(default_factory=list)
    scan_ok: bool | None = None
    scan_error: str = ""

    @property
    def ok(self) -> bool:
        return (
            not self.missing_root
            and bool(self.partitions)
            and all(item.ok for item in self.partitions)
            and not self.tmp_files
            and self.scan_ok is True
        )


def default_minute_root() -> Path:
    """TSP 的数据目录约定: settings.data_dir / kline_minute。"""
    try:
        from app.config import settings
    except ImportError as exc:
        raise SystemExit(
            "无法导入 app.config。请在仓库根目录用后端虚拟环境执行，"
            "并设置 PYTHONPATH=backend。Docker 里 PYTHONPATH 已是 /app。\n"
            f"({exc})"
        ) from exc
    return Path(settings.data_dir) / "kline_minute"


def _dtype_problem(name: str, dtype: pl.DataType) -> str | None:
    if name == "symbol":
        if dtype == pl.String:
            return None
        return f"symbol={dtype}（应为 String）"
    if name == "datetime":
        if (
            isinstance(dtype, pl.Datetime)
            and dtype.time_unit == "us"
            and dtype.time_zone is None
        ):
            return None
        return f"datetime={dtype}（应为 Datetime(us)、无时区）"
    if name in _FLOAT_COLUMNS:
        if dtype == pl.Float64:
            return None
        return f"{name}={dtype}（应为 Float64）"
    return None


def _inspect_partition(path: Path) -> PartitionFinding:
    date = path.parent.name.removeprefix("date=")
    finding = PartitionFinding(date=date)
    try:
        schema = pl.read_parquet_schema(path)
    except Exception as exc:  # noqa: BLE001 - 坏文件要记入诊断，不能中断扫描
        finding.problems.append(f"无法读取: {type(exc).__name__}: {exc}")
        return finding

    missing = [name for name in CANONICAL_COLUMNS if name not in schema]
    if missing:
        finding.problems.append("缺少列: " + ", ".join(missing))
    extra = [name for name in schema if name not in CANONICAL_COLUMNS]
    if extra:
        finding.problems.append("多余列: " + ", ".join(extra))
    for name in CANONICAL_COLUMNS:
        if name not in schema:
            continue
        problem = _dtype_problem(name, schema[name])
        if problem:
            finding.problems.append(problem)

    key_cols = [name for name in ("symbol", "datetime") if name in schema]
    if len(key_cols) < 2:
        return finding
    try:
        frame = pl.read_parquet(path, columns=key_cols)
    except Exception as exc:  # noqa: BLE001
        finding.problems.append(f"无法统计行数: {type(exc).__name__}: {exc}")
        return finding
    finding.rows = frame.height
    finding.stocks = frame.get_column("symbol").n_unique()
    grouped = frame.group_by(key_cols).len()
    duplicated = grouped.filter(pl.col("len") > 1)
    finding.duplicate_keys = duplicated.height
    if finding.duplicate_keys:
        extra = duplicated.select((pl.col("len") - 1).sum()).item()
        finding.extra_rows = int(extra or 0)
        finding.problems.append(
            f"重复 (symbol, datetime) {finding.duplicate_keys} 组，多出 {finding.extra_rows} 行"
        )
    return finding


def _cross_scan(files: list[Path]) -> tuple[bool, str]:
    """按默认严格类型读完全部分区的规范列。len() 发现不了 Int64/Float64 冲突。"""
    try:
        (
            pl.scan_parquet([str(path) for path in files])
            .select([pl.col(name).null_count().alias(name) for name in CANONICAL_COLUMNS])
            .collect()
        )
    except Exception as exc:  # noqa: BLE001 - 扫描失败本身就是诊断结果
        text = f"{type(exc).__name__}: {exc}"
        return False, text.replace("\n", " ")[:500]
    return True, ""


def check_store(root: Path) -> Report:
    report = Report(root=root)
    if not root.is_dir():
        report.missing_root = True
        report.scan_ok = False
        report.scan_error = "目录不存在"
        return report

    report.tmp_files = sorted(
        path.name for path in root.glob("date=*/*.tmp") if path.is_file()
    )
    files = sorted(path for path in root.glob("date=*/part.parquet") if path.is_file())
    report.partitions = [_inspect_partition(path) for path in files]
    if not files:
        report.scan_ok = False
        report.scan_error = "没有 date=*/part.parquet"
        return report
    report.scan_ok, report.scan_error = _cross_scan(files)
    return report


def format_report(report: Report) -> str:
    lines = [
        "分钟 K 诊断（只读，不修改数据）",
        f"目录: {report.root}",
    ]
    if report.missing_root:
        lines.append("结论: 有问题")
        lines.append("目录不存在。可用 --path 指向 data/kline_minute。")
        lines.append("")
        lines.append(REPAIR_TEXT.rstrip())
        return "\n".join(lines)

    lines.append(f"分区: {len(report.partitions)}")
    if report.tmp_files:
        lines.append("发现临时文件（诊断不读取、不删除）: " + ", ".join(report.tmp_files))
    for item in report.partitions:
        rows = "—" if item.rows is None else str(item.rows)
        stocks = "—" if item.stocks is None else str(item.stocks)
        detail = "；".join(item.problems) if item.problems else "类型 OK，无重复键"
        lines.append(f"date={item.date}  行 {rows}  股票 {stocks}  {detail}")

    if report.scan_ok:
        lines.append("跨分区 scan_parquet: 成功")
    else:
        lines.append("跨分区 scan_parquet: 失败")
        if report.scan_error:
            lines.append(report.scan_error)

    if report.ok:
        total_rows = sum(item.rows or 0 for item in report.partitions)
        total_note = f"共 {len(report.partitions)} 个分区、{total_rows} 行。"
        lines.append("结论: OK")
        lines.append(total_note + "类型与 kline_sync 一致，跨分区读取成功，无重复键。")
        return "\n".join(lines)

    dates = [item.date for item in report.partitions if not item.ok]
    lines.append("结论: 有问题")
    if not report.partitions:
        lines.append("没有 date=*/part.parquet。")
    elif dates:
        lines.append("问题日期: " + ", ".join(dates))
    elif report.tmp_files:
        lines.append("问题: 分区类型正常，但存在未完成的临时文件。")
    elif report.scan_ok is False:
        lines.append("问题: 跨分区读取失败。")
    lines.append("")
    lines.append(REPAIR_TEXT.rstrip())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读检查分钟 K 分区的列类型、重复键和跨分区 scan_parquet。不写数据。",
    )
    parser.add_argument(
        "--path",
        help="分区根目录，默认是 TSP data_dir 下的 kline_minute",
    )
    args = parser.parse_args(argv)
    root = Path(args.path).expanduser() if args.path else default_minute_root()
    if args.path:
        root = root.resolve()
    report = check_store(root)
    print(format_report(report))
    if report.ok:
        return 0
    if report.missing_root:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
