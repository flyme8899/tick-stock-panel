#!/usr/bin/env python3
r"""可选修复分钟 K 分区。默认只打印计划，不改任何数据文件。

坏分区多半是 volume 为 Int64，而 kline_sync 写入的是 Float64。
_write_minute_partition 会把已有文件和新数据纵向拼接，类型不一致时会失败，
所以要先把坏的 part.parquet 移走，再重拉。

TickFlow 付费说明写的是「一年分钟级历史」，单次 count 最大 10000
（大约 41 个交易日）。一年窗口内的日期计划重拉；更早的不重拉。
重拉没有写回某一天时，用备份做类型转换写回，不留空洞。
仅转换类型补不回被整日覆盖丢掉的股票。没有用本机 key 探测过接口。
自定义分钟源能回溯多久，TSP 里没有单独的保留期，按同一年窗口计划。

不调用 sync_and_persist_minute：那个入口会先做空 datetime 清理和
旧 symbol= 分区迁移，可能改到别的日期。

开发环境（与 ./dev.sh 同一套后端虚拟环境，在仓库根目录执行）。
先看计划，确认后再执行。执行前停掉 ./dev.sh，避免修到一半又有写入。

    PYTHONPATH=backend backend/.venv/bin/python scripts/repair_minute_kline.py
    PYTHONPATH=backend backend/.venv/bin/python scripts/repair_minute_kline.py --apply
    PYTHONPATH=backend backend/.venv/bin/python scripts/repair_minute_kline.py --rollback data/backup/kline_minute_repair_<时间>

Windows（dev.ps1 同一套环境）:

    $env:PYTHONPATH = "backend"
    backend\.venv\Scripts\python scripts\repair_minute_kline.py

Docker 镜像不含 scripts/。compose 把宿主机 ./data 挂到容器 /app/data，
并强制 DATA_DIR=/app/data。修复脚本会按同目录加载 check_minute_kline.py，
两个文件都要挂进 /tmp。docker compose run --no-deps 里的 127.0.0.1
不是正在跑的 app 容器，探针用 http://app:3018（app 需要已经在跑）。

    docker compose run --rm --no-deps --entrypoint uv \
      -v "$PWD/scripts/check_minute_kline.py:/tmp/check_minute_kline.py:ro" \
      -v "$PWD/scripts/repair_minute_kline.py:/tmp/repair_minute_kline.py:ro" \
      -e TSP_API_BASE=http://app:3018 \
      app run --no-sync python /tmp/repair_minute_kline.py

    同样把 --apply、--yes 或 --rollback <备份目录> 加在脚本参数最后。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl

# 付费文档「一年分钟级历史」。超过这个自然日数的分区不计划重拉。
REFETCH_LOOKBACK_DAYS = 365
# job_store 里 pending/running 超过这个时间，当成上次进程留下的记录，不因此拒绝。
ACTIVE_JOB_MAX_AGE_S = 6 * 3600
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FLOAT_COLUMNS = ("open", "high", "low", "close", "volume", "amount")
_CN_TZ = timezone(timedelta(hours=8))

FetchJson = Callable[[str], dict[str, Any]]
RefetchFn = Callable[["RefetchRequest"], None]
ConfirmFn = Callable[[], bool]


def _load_checker():
    path = Path(__file__).with_name("check_minute_kline.py")
    if not path.is_file():
        raise SystemExit(
            f"找不到 {path.name}。它必须和本脚本在同一目录。"
            "Docker 里请把两个脚本都挂到 /tmp。"
        )
    spec = importlib.util.spec_from_file_location("tsp_check_minute_kline", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


@dataclass
class WriterStatus:
    active: bool
    http_unknown: bool
    reasons: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    api_base: str = ""


@dataclass
class DatePlan:
    date: str
    path: Path
    size_bytes: int
    rows: int | None
    stocks: int | None
    problems: list[str]
    action: str  # refetch | cast
    reason: str


@dataclass
class RepairPlan:
    minute_root: Path
    data_dir: Path
    dates: list[DatePlan]
    symbols: list[str]
    symbol_note: str
    batch: int
    rpm: int
    limit_note: str
    segment_days: int
    segments: int
    steps: int
    eta_s: float
    window_start: datetime | None
    window_end: datetime | None
    resumable: Path | None
    asset_type: str


@dataclass
class RefetchRequest:
    minute_root: Path
    data_dir: Path
    dates: list[str]
    symbols: list[str]
    start: datetime
    end: datetime
    batch: int
    rpm: int
    segment_days: int
    asset_type: str


def _beijing_now() -> datetime:
    try:
        from app.market_time import cn_now

        return cn_now()
    except ImportError:
        return datetime.now(_CN_TZ)


def _default_api_base() -> str:
    env = os.environ.get("TSP_API_BASE")
    if env:
        return env.rstrip("/")
    port = os.environ.get("PORT", "3018")
    return f"http://127.0.0.1:{port}"


def _http_get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=2) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("分钟刷新状态不是 JSON 对象")
    return payload


def detect_writers(
    data_dir: Path,
    api_base: str | None = None,
    *,
    fetch: FetchJson | None = None,
    now_ts: float | None = None,
) -> WriterStatus:
    """看分钟写入是否还在进行。

    不构造 JobStore：它的初始化会把别的进程留下的 pending/running 改成 failed。
    这里只读 job_store/*.json。分钟增量刷新是线程，本进程看不到，改问正在跑的后端。
    """
    base = (api_base or _default_api_base()).rstrip("/")
    status = WriterStatus(active=False, http_unknown=False, api_base=base)
    moment = time.time() if now_ts is None else now_ts
    job_dir = data_dir / "job_store"
    if job_dir.is_dir():
        for path in sorted(job_dir.glob("*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") not in ("pending", "running"):
                continue
            age = moment - path.stat().st_mtime
            label = f"{job.get('id') or path.name} 状态 {job.get('status')}"
            if age <= ACTIVE_JOB_MAX_AGE_S:
                status.active = True
                status.reasons.append(f"job_store {label}，{int(age)} 秒内有更新")
            else:
                status.stale.append(f"job_store {label}，已 {int(age)} 秒没更新，不因此拒绝")

    fetch = fetch or _http_get_json
    url = base + "/api/settings/minute-refresh/status"
    try:
        payload = fetch(url)
    except Exception as exc:  # noqa: BLE001 - 探不到本身就是结论，不能当成空闲
        status.http_unknown = True
        status.reasons.append(
            f"无法确认分钟刷新状态（{type(exc).__name__}: {exc}）。后端没开、或地址不对时会这样。"
        )
        return status
    if payload.get("running"):
        status.active = True
        status.reasons.append(f"{url} 返回 running=true，分钟增量刷新线程还在跑")
    return status


def writer_block_reason(status: WriterStatus, force: bool) -> str | None:
    if force:
        return None
    if status.active:
        return "检测到分钟同步或实时分钟写入正在进行"
    if status.http_unknown:
        return "无法确认分钟刷新是否在跑"
    return None


def data_dir_of(minute_root: Path) -> Path:
    return minute_root.parent


def partition_file(minute_root: Path, day: str) -> Path:
    return minute_root / f"date={day}" / "part.parquet"


def backup_copy_of(backup_dir: Path, day: str) -> Path:
    return backup_dir / f"date={day}" / "part.parquet"


def displaced_copy_of(backup_dir: Path, day: str) -> Path:
    return backup_dir / "displaced" / f"date={day}" / "part.parquet"


def _parse_day(text: str) -> date:
    return date.fromisoformat(text)


def action_for(day: date, today: date) -> tuple[str, str]:
    age = (today - day).days
    if age <= REFETCH_LOOKBACK_DAYS:
        return (
            "refetch",
            f"距今 {age} 天，在 TickFlow 付费文档「一年分钟级历史」窗口内，计划重拉",
        )
    return (
        "cast",
        f"距今 {age} 天，超过 {REFETCH_LOOKBACK_DAYS} 天，不计划重拉，只做类型转换",
    )


def _read_symbol_column(path: Path) -> list[str] | None:
    try:
        frame = pl.read_parquet(path, columns=["symbol"])
    except Exception:  # noqa: BLE001 - 维表或分区读失败就退回文件里已有的代码
        return None
    return [str(item) for item in frame.get_column("symbol").drop_nulls().to_list()]


def load_symbols(data_dir: Path, minute_root: Path, days: list[str], asset_type: str) -> tuple[list[str], str]:
    """重拉用的标的。股票优先用 instruments 去掉指数，再并上坏文件里的代码。"""
    present: set[str] = set()
    for day in days:
        for path in (partition_file(minute_root, day),):
            if not path.is_file():
                continue
            symbols = _read_symbol_column(path)
            if symbols:
                present.update(symbols)

    if asset_type == "etf":
        ordered = sorted(present)
        return ordered, (
            f"这是 ETF 分钟目录，重拉 {len(ordered)} 只坏文件里已有的代码，"
            "不用股票 instruments 维表。"
        )

    inst = data_dir / "instruments" / "instruments.parquet"
    universe: list[str] | None = None
    if inst.is_file():
        universe = _read_symbol_column(inst)
    if not universe:
        ordered = sorted(present)
        return ordered, (
            f"没有可读的 instruments 维表，重拉 {len(ordered)} 只坏文件里已有的股票。"
            "被整日覆盖丢掉、文件里已经没有的股票回不来。"
        )

    index_symbols: set[str] = set()
    index_root = data_dir / "instruments_index"
    if index_root.exists():
        for path in index_root.rglob("*.parquet"):
            found = _read_symbol_column(path)
            if found:
                index_symbols.update(found)
    kept = sorted({item for item in universe if item not in index_symbols} | present)
    return kept, (
        f"重拉 {len(kept)} 只：instruments 去掉 {len(index_symbols)} 只指数后，"
        f"并上坏文件里的 {len(present)} 只。丢掉的股票可以靠这次重拉回来。"
    )


def resolve_batch_rpm(data_dir: Path) -> tuple[int, int, str]:
    """与 sync_and_persist_minute 相同的默认值，rpm 再乘 0.8。能读到能力缓存就用缓存。"""
    fallback = (
        "没有可用的 capabilities.json，使用与 sync_and_persist_minute 相同的默认值："
        "batch 100、标称 rpm 30，再乘 0.8 得到 rpm 24"
    )
    path = data_dir / "capabilities.json"
    if not path.is_file():
        return 100, 24, fallback
    try:
        from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
        from app.tickflow.rate_limits import resolve_limit

        data = json.loads(path.read_text(encoding="utf-8"))
        caps: dict[Any, Any] = {}
        for name, lim in data.get("capabilities", {}).items():
            try:
                cap = Cap(name)
            except ValueError:
                continue
            if not isinstance(lim, dict):
                continue
            caps[cap] = CapabilityLimits(
                rpm=lim.get("rpm"),
                batch=lim.get("batch"),
                subscribe=lim.get("subscribe"),
            )
        capset = CapabilitySet(caps)
        if not capset.has(Cap.KLINE_MINUTE_BATCH):
            return 100, 24, fallback + "（缓存里没有 kline.minute.batch）"
        limit = resolve_limit(
            capset,
            Cap.KLINE_MINUTE_BATCH,
            default_batch=100,
            default_rpm=30,
            default_rpm_when_unset=False,
        )
        batch = int(limit.batch or 100)
        rpm = int(limit.rpm or 24)
        return batch, rpm, f"来自 capabilities.json 的 kline.minute.batch，rpm 已按 0.8 安全系数换算为 {rpm}"
    except Exception as exc:  # noqa: BLE001
        return 100, 24, f"读取 capabilities.json 失败（{type(exc).__name__}: {exc}）。改用 batch 100、rpm 24"


def resolve_segment_days() -> int:
    try:
        from app.services import preferences

        return int(preferences.get_minute_sync_segment_days())
    except Exception:  # noqa: BLE001
        return 20


def segment_count(start: datetime, end: datetime, segment_trading_days: int) -> int:
    """与 sync_minute_batch 相同的自然日切段。"""
    if start >= end:
        return 0
    seg_calendar_days = max(1, int(segment_trading_days * 7 / 5))
    chunk = timedelta(days=seg_calendar_days)
    count = 0
    cursor = start
    while cursor < end:
        count += 1
        nxt = min(cursor + chunk, end)
        if nxt <= cursor:
            break
        cursor = nxt
    return count


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_CN_TZ)
    return moment


def build_plan(
    minute_root: Path,
    days: list[str],
    report,
    *,
    today: date,
    now: datetime,
    resumable: Path | None,
    batch: int | None = None,
    rpm: int | None = None,
    segment_days: int | None = None,
) -> RepairPlan:
    data_dir = data_dir_of(minute_root)
    by_date = {item.date: item for item in report.partitions}
    chosen: list[DatePlan] = []
    for day in days:
        path = partition_file(minute_root, day)
        finding = by_date.get(day)
        size = path.stat().st_size if path.is_file() else 0
        action, reason = action_for(_parse_day(day), today)
        chosen.append(
            DatePlan(
                date=day,
                path=path,
                size_bytes=size,
                rows=None if finding is None else finding.rows,
                stocks=None if finding is None else finding.stocks,
                problems=list(finding.problems) if finding is not None else [],
                action=action,
                reason=reason,
            )
        )
    asset_type = "etf" if minute_root.name == "kline_etf_minute" else "stock"
    refetch_days = [item.date for item in chosen if item.action == "refetch"]
    symbols, symbol_note = load_symbols(data_dir, minute_root, refetch_days, asset_type)
    if batch is None or rpm is None:
        resolved_batch, resolved_rpm, limit_note = resolve_batch_rpm(data_dir)
        batch = resolved_batch if batch is None else batch
        rpm = resolved_rpm if rpm is None else rpm
    else:
        limit_note = f"batch {batch}，rpm {rpm}"
    segment_days = resolve_segment_days() if segment_days is None else segment_days
    window_start: datetime | None = None
    window_end: datetime | None = None
    segments = 0
    steps = 0
    eta = 0.0
    if refetch_days and symbols:
        window_start = datetime.combine(_parse_day(min(refetch_days)), datetime.min.time()).replace(
            hour=9, minute=25, tzinfo=_CN_TZ
        )
        window_end = _aware(now)
        segments = segment_count(window_start, window_end, segment_days)
        per_segment = max(1, math.ceil(len(symbols) / batch)) if batch else 1
        steps = segments * per_segment
        if rpm and steps > 1:
            eta = (steps - 1) * (60.0 / rpm)
    return RepairPlan(
        minute_root=minute_root,
        data_dir=data_dir,
        dates=chosen,
        symbols=symbols,
        symbol_note=symbol_note,
        batch=batch,
        rpm=rpm,
        limit_note=limit_note,
        segment_days=segment_days,
        segments=segments,
        steps=steps,
        eta_s=eta,
        window_start=window_start,
        window_end=window_end,
        resumable=resumable,
        asset_type=asset_type,
    )


def _fmt_int(value: int | None) -> str:
    return "—" if value is None else str(value)


def _fmt_eta(seconds: float) -> str:
    if seconds < 90:
        return f"约 {seconds:.0f} 秒"
    return f"约 {seconds / 60:.0f} 分钟"


def format_plan(plan: RepairPlan, writer: WriterStatus, block: str | None, *, apply: bool) -> str:
    refetch_n = sum(1 for item in plan.dates if item.action == "refetch")
    cast_n = sum(1 for item in plan.dates if item.action == "cast")
    lines = [
        "分钟 K 修复计划" + ("（即将按这个计划改数据）" if apply else "（这次只打印，不修改数据）"),
        f"目录: {plan.minute_root}",
        f"日期: {len(plan.dates)} 个，其中计划重拉 {refetch_n} 个，计划仅转换类型 {cast_n} 个",
    ]
    if plan.resumable is not None:
        lines.append(f"未完成的备份: {plan.resumable}（--apply 会接着做；--fresh 另开一份）")
    for item in plan.dates:
        label = "重拉" if item.action == "refetch" else "仅转换类型"
        detail = "；".join(item.problems) if item.problems else "当前分区类型检查没有报错"
        lines.append(
            f"date={item.date}  文件 {item.size_bytes} 字节  行 {_fmt_int(item.rows)}"
            f"  股票 {_fmt_int(item.stocks)}  计划: {label}"
        )
        lines.append(f"  {detail}")
        lines.append(f"  {item.reason}")
    backup_hint = plan.data_dir / "backup" / "kline_minute_repair_<启动时刻>"
    lines.append(f"备份目录: {backup_hint}")
    lines.append(
        "  在 kline_minute 外面。先复制 part.parquet 并核对 schema 和行数，"
        "再把坏文件移到 displaced/，不删除。"
        "计划仅转换类型的日期会马上把转换结果写回，不把空洞留到最后。"
    )
    if refetch_n:
        start = plan.window_start.strftime("%Y-%m-%d %H:%M") if plan.window_start else "—"
        end = plan.window_end.strftime("%Y-%m-%d %H:%M") if plan.window_end else "—"
        lines.append(
            "重拉调用: kline_sync.sync_minute_batch，段末回调 _write_minute_partition"
            "（先写 .tmp 再替换，与现有同步相同）。"
        )
        lines.append("  不调用 sync_and_persist_minute，避免空时间清理和旧分区迁移改到其他日期。")
        lines.append(f"窗口: {start} 北京时间 → {end} 北京时间")
        lines.append(f"标的: {plan.symbol_note}")
        lines.append(
            f"分段: {plan.segment_days} 个交易日一段（自然日约 {max(1, int(plan.segment_days * 7 / 5))} 天），"
            f"约 {plan.segments} 段；batch {plan.batch}；rpm {plan.rpm}。"
        )
        lines.append(f"  {plan.limit_note}")
        lines.append(
            f"预计限速等待: {_fmt_eta(plan.eta_s)}（{plan.steps} 步，除第一步外每步约 60/{plan.rpm or 1} 秒）。"
            "接口本身的耗时另计。"
        )
    lines.append(
        "数据源: TickFlow 付费说明写的是「一年分钟级历史」，单次 count 最大 10000"
        "（大约 41 个交易日），所以上面一年内的日期计划重拉，更早的计划仅转换类型。"
        "自定义分钟源的保留期未知，重拉结果为空就改成仅转换类型。"
        "没有用本机 key 去探测接口。"
    )
    lines.append("仅转换类型只改列的 dtype（例如 Int64 → Float64），补不回被整日覆盖丢掉的股票。")
    lines.append("中断后续跑: 每个日期的状态写在备份目录的 status.json。再次 --apply 会从上次停下的地方继续。")
    if writer.stale:
        for item in writer.stale:
            lines.append(f"写入检查: {item}")
    if block:
        lines.append(f"写入检查: {block}。{'；'.join(writer.reasons)}")
        lines.append("不加 --force 时，--apply 会拒绝并返回退出码 3。")
    elif writer.reasons:
        lines.append("写入检查: " + "；".join(writer.reasons))
    else:
        lines.append("写入检查: 没有看到近期的 pending/running 任务，分钟刷新状态也不是 running。")
    if apply and not block:
        lines.append("确认: 交互终端需要输入 REPAIR；非交互环境请加 --yes。")
    else:
        lines.append("执行: 加上 --apply，并输入 REPAIR，或同时加 --yes。执行前先停掉会写分钟分区的进程。")
    return "\n".join(lines)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _row_count(path: Path) -> int:
    return int(pl.scan_parquet(str(path)).select(pl.len()).collect().item())


def verify_backup(src: Path, dst: Path) -> None:
    if not dst.is_file():
        raise RuntimeError(f"备份不存在: {dst}")
    if src.stat().st_size != dst.stat().st_size:
        raise RuntimeError(f"备份大小与原文件不一致: {src.name}")
    src_schema = pl.read_parquet_schema(src)
    dst_schema = pl.read_parquet_schema(dst)
    if dict(src_schema) != dict(dst_schema):
        raise RuntimeError(f"备份 schema 与原文件不一致: {src.name}")
    if _row_count(src) != _row_count(dst):
        raise RuntimeError(f"备份行数与原文件不一致: {src.name}")


def _atomic_copy(src: Path, dst: Path) -> None:
    from app.tickflow.repository import replace_with_retry

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    shutil.copy2(src, tmp)
    replace_with_retry(tmp, dst)


def cast_frame(df: pl.DataFrame) -> pl.DataFrame:
    columns = list(checker.CANONICAL_COLUMNS)
    missing = [name for name in columns if name not in df.columns]
    if missing:
        raise RuntimeError("备份缺少列: " + ", ".join(missing))
    df = df.select(columns)
    dtype = df.schema["datetime"]
    if isinstance(dtype, pl.Datetime) and dtype.time_zone is not None:
        df = df.with_columns(
            pl.col("datetime").dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
        )
    return df.with_columns(
        [
            pl.col("symbol").cast(pl.String),
            pl.col("datetime").cast(pl.Datetime("us"), strict=False),
            *[pl.col(name).cast(pl.Float64, strict=False) for name in _FLOAT_COLUMNS],
        ]
    ).sort("symbol", "datetime")


def write_cast(backup_file: Path, dest: Path) -> int:
    frame = cast_frame(pl.read_parquet(backup_file))
    from app.services.kline_sync import _atomic_write_parquet

    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_parquet(frame, dest)
    schema = pl.read_parquet_schema(dest)
    if schema.get("volume") != pl.Float64:
        raise RuntimeError(f"类型转换后 volume 仍是 {schema.get('volume')}")
    return frame.height


def is_good_float(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        schema = pl.read_parquet_schema(path)
    except Exception:  # noqa: BLE001
        return False
    if schema.get("volume") != pl.Float64:
        return False
    try:
        return _row_count(path) > 0
    except Exception:  # noqa: BLE001
        return False


def new_backup_dir(data_dir: Path, now: datetime) -> Path:
    stamp = _aware(now).strftime("%Y%m%dT%H%M%S")
    root = data_dir / "backup"
    candidate = root / f"kline_minute_repair_{stamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"kline_minute_repair_{stamp}_{suffix}"
    return candidate


def find_resumable(data_dir: Path, minute_root: Path) -> Path | None:
    root = data_dir / "backup"
    if not root.is_dir():
        return None
    found: Path | None = None
    for directory in sorted(path for path in root.glob("kline_minute_repair_*") if path.is_dir()):
        status_path = directory / "status.json"
        if not status_path.is_file():
            continue
        try:
            status = _read_json(status_path)
        except (OSError, json.JSONDecodeError):
            continue
        recorded = status.get("minute_root")
        if not recorded or Path(recorded).resolve() != minute_root.resolve():
            continue
        dates = status.get("dates") or {}
        if any(item.get("state") != "done" for item in dates.values()):
            found = directory
    return found


def _blank_date_status(item: DatePlan) -> dict[str, Any]:
    return {
        "state": "pending",
        "planned": item.action,
        "mode": None,
        "rows_before": item.rows,
        "stocks_before": item.stocks,
        "bytes_before": item.size_bytes,
        "rows_after": None,
        "note": "",
    }


def backup_and_displace(minute_root: Path, backup_dir: Path, day: str, item: dict[str, Any]) -> None:
    """复制并核对备份，再把坏文件移出分区。已经移走过的日期不会再移动。"""
    live = partition_file(minute_root, day)
    copied = backup_copy_of(backup_dir, day)
    displaced = displaced_copy_of(backup_dir, day)
    state = item.get("state")
    if state in ("displaced", "provisional", "done"):
        if not copied.is_file():
            raise RuntimeError(f"date={day} 的状态是 {state}，但备份文件不在 {copied}")
        return
    if not copied.is_file():
        if not live.is_file():
            raise RuntimeError(f"date={day} 没有 part.parquet，也无法从备份继续")
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live, copied)
        try:
            verify_backup(live, copied)
        except Exception:
            copied.unlink(missing_ok=True)
            raise
        item["state"] = "backed_up"
    elif live.is_file():
        verify_backup(live, copied)
    if live.is_file():
        if displaced.is_file():
            raise RuntimeError(f"date={day} 的原文件和 displaced 同时存在，已停止，避免盖掉备份")
        displaced.parent.mkdir(parents=True, exist_ok=True)
        live.replace(displaced)
    elif not displaced.is_file():
        raise RuntimeError(f"date={day} 既没有分区文件，也没有 displaced 副本")
    item["state"] = "displaced"


def default_refetch(request: RefetchRequest) -> None:
    """只拉分钟并按日期原子写入。不做 sync_and_persist_minute 里的迁移。"""
    import threading

    from app.services.kline_sync import _write_minute_partition, sync_minute_batch
    from app.services.minute_adjust import minute_basis_is_raw

    if not request.symbols:
        raise RuntimeError("没有可重拉的标的")
    lock = threading.Lock()

    def _persist(frame: pl.DataFrame) -> None:
        with lock:
            _write_minute_partition(frame, request.minute_root)

    def _progress(cur: int, total: int, label: str) -> None:
        print(f"  重拉进度 {cur}/{total} {label}", flush=True)

    print(
        f"开始重拉 {len(request.dates)} 个日期、{len(request.symbols)} 只，"
        f"batch {request.batch}，rpm {request.rpm}",
        flush=True,
    )
    sync_minute_batch(
        request.symbols,
        start_time=request.start,
        end_time=request.end,
        batch_size=request.batch,
        rpm=request.rpm,
        on_chunk_done=_progress,
        segment_trading_days=request.segment_days,
        on_segment=_persist,
        asset_type=request.asset_type,
        raw_basis=minute_basis_is_raw(request.data_dir),
    )


def _close_hole(minute_root: Path, backup_dir: Path, day: str) -> str:
    """分区里没有文件时，用备份做类型转换写回；转换失败则按原字节写回。"""
    live = partition_file(minute_root, day)
    if live.is_file():
        return "kept"
    copied = backup_copy_of(backup_dir, day)
    if not copied.is_file():
        raise RuntimeError(f"date={day} 没有备份，不能补洞")
    try:
        write_cast(copied, live)
        return "cast"
    except Exception as exc:
        _atomic_copy(copied, live)
        raise RuntimeError(f"date={day} 类型转换失败，已按原字节写回备份: {exc}") from exc


def run_apply(plan: RepairPlan, backup_dir: Path, *, refetch: RefetchFn, now: datetime) -> int:
    status_path = backup_dir / "status.json"
    if status_path.is_file():
        status = _read_json(status_path)
    else:
        backup_dir.mkdir(parents=True, exist_ok=False)
        status = {
            "version": 1,
            "minute_root": str(plan.minute_root),
            "created_at": _aware(now).isoformat(timespec="seconds"),
            "dates": {},
        }
    by_plan = {item.date: item for item in plan.dates}
    for item in plan.dates:
        status["dates"].setdefault(item.date, _blank_date_status(item))
        # 计划动作以这次的窗口判断为准，已完成的不改。
        if status["dates"][item.date].get("state") != "done":
            status["dates"][item.date]["planned"] = item.action
    _write_json(status_path, status)

    try:
        for day, entry in list(status["dates"].items()):
            if day not in by_plan or entry.get("state") == "done":
                continue
            print(f"备份 date={day}", flush=True)
            backup_and_displace(plan.minute_root, backup_dir, day, entry)
            _write_json(status_path, status)
            if entry.get("planned") == "cast":
                rows = write_cast(backup_copy_of(backup_dir, day), partition_file(plan.minute_root, day))
                entry["state"] = "done"
                entry["mode"] = "cast"
                entry["rows_after"] = rows
                entry["note"] = "超过重拉窗口，只做了类型转换。补不回被整日覆盖丢掉的股票。"
                _write_json(status_path, status)
    except Exception as exc:  # noqa: BLE001
        for day, entry in status["dates"].items():
            live = partition_file(plan.minute_root, day)
            if entry.get("state") == "displaced" and not live.is_file():
                try:
                    how = _close_hole(plan.minute_root, backup_dir, day)
                except Exception as inner:  # noqa: BLE001
                    entry["note"] = f"补洞失败: {inner}"
                    continue
                if how == "cast":
                    entry["mode"] = "cast"
                    entry["rows_after"] = _row_count(live)
                    entry["state"] = "provisional"
                    entry["note"] = "写回中断，已用备份做类型转换补上空洞。再次 --apply 会继续。"
        _write_json(status_path, status)
        print(f"备份阶段失败，已停止: {exc}")
        print(f"状态: {status_path}")
        print("被移走的日期已尽量用备份补上。重跑同一命令会从 status.json 继续。")
        return 1

    refetch_days = [
        day
        for day, entry in status["dates"].items()
        if day in by_plan and entry.get("state") != "done" and entry.get("planned") == "refetch"
    ]
    refetch_error: Exception | None = None
    before_rows: dict[str, int] = {}
    if refetch_days:
        for day in refetch_days:
            live = partition_file(plan.minute_root, day)
            before_rows[day] = _row_count(live) if live.is_file() else 0
        if not plan.symbols:
            print("没有可重拉的标的，这些日期改为类型转换。被整日覆盖丢掉的股票回不来。")
        else:
            request = RefetchRequest(
                minute_root=plan.minute_root,
                data_dir=plan.data_dir,
                dates=refetch_days,
                symbols=plan.symbols,
                start=plan.window_start or _aware(now),
                end=plan.window_end or _aware(now),
                batch=plan.batch,
                rpm=plan.rpm,
                segment_days=plan.segment_days,
                asset_type=plan.asset_type,
            )
            try:
                refetch(request)
            except Exception as exc:  # noqa: BLE001
                refetch_error = exc
                print(f"重拉中断: {type(exc).__name__}: {exc}")
                print("还没写回的日期会用备份做类型转换，避免留下空洞。再次 --apply 会继续重拉。")

    for day in refetch_days:
        entry = status["dates"][day]
        live = partition_file(plan.minute_root, day)
        try:
            if not is_good_float(live):
                how = _close_hole(plan.minute_root, backup_dir, day)
                entry["mode"] = "cast" if how == "cast" else entry.get("mode")
                entry["rows_after"] = _row_count(live) if live.is_file() else None
                if refetch_error is not None:
                    entry["state"] = "provisional"
                    entry["note"] = "重拉中断，已用备份做类型转换避免空洞。再次 --apply 会继续重拉。"
                else:
                    entry["state"] = "done"
                    entry["note"] = "重拉没有写回这一天，已用备份做类型转换。补不回被整日覆盖丢掉的股票。"
            else:
                after = _row_count(live)
                entry["rows_after"] = after
                changed = before_rows.get(day, 0) == 0 or after != before_rows.get(day, 0)
                if refetch_error is not None:
                    entry["state"] = "provisional"
                    entry["mode"] = "refetch" if changed else "cast"
                    entry["note"] = "重拉中断。再次 --apply 会继续重拉这一天。"
                elif changed:
                    entry["state"] = "done"
                    entry["mode"] = "refetch"
                    entry["note"] = "已重拉并原子写回。"
                else:
                    entry["state"] = "done"
                    entry["mode"] = "cast"
                    entry["note"] = "重拉没有改变行数，保留类型转换结果。补不回备份里没有的股票。"
        except Exception as exc:  # noqa: BLE001
            entry["state"] = "provisional" if refetch_error is not None else entry.get("state")
            entry["note"] = f"写回失败: {exc}"
            _write_json(status_path, status)
            print(f"date={day} 写回失败: {exc}")
            return 1
        _write_json(status_path, status)

    print("")
    print(f"备份: {backup_dir}")
    provisional = False
    for day, entry in status["dates"].items():
        if day not in by_plan:
            continue
        mode = "重拉" if entry.get("mode") == "refetch" else "仅转换类型"
        if entry.get("state") == "provisional":
            provisional = True
            mode = mode + "（未完成，可再跑 --apply）"
        rows_before = entry.get("rows_before")
        rows_after = entry.get("rows_after")
        print(
            f"date={day}  {mode}  行 {_fmt_int(rows_before)} → {_fmt_int(rows_after)}"
            + (f"  {entry.get('note')}" if entry.get("note") else "")
        )
    print("")
    report = checker.check_store(plan.minute_root)
    print(checker.format_report(report))
    if provisional:
        print("有日期停在未完成的重拉。上面的类型转换只是为了不留空洞，再次 --apply 会继续。")
        return 1
    return 0 if report.ok else 1


def run_rollback(
    backup_dir: Path,
    minute_root: Path | None,
    writer: WriterStatus,
    force: bool,
) -> int:
    if not backup_dir.is_dir():
        print(f"备份目录不存在: {backup_dir}")
        return 2
    status_path = backup_dir / "status.json"
    recorded_root: Path | None = None
    if status_path.is_file():
        try:
            recorded = _read_json(status_path).get("minute_root")
        except (OSError, json.JSONDecodeError):
            recorded = None
        if recorded:
            recorded_root = Path(recorded)
    root = minute_root or recorded_root
    if root is None:
        print("没有 --path，备份里也没有 minute_root，不能确定写回哪里。")
        return 2
    copies = sorted(path for path in backup_dir.glob("date=*/part.parquet") if path.is_file())
    if not copies:
        print(f"备份里没有 date=*/part.parquet: {backup_dir}")
        return 2
    block = writer_block_reason(writer, force)
    if block:
        print(f"已拒绝回滚: {block}。{'；'.join(writer.reasons)}")
        print("确认没有分钟写入后重跑，或显式加 --force。")
        return 3
    print(f"从 {backup_dir} 恢复到 {root}")
    for src in copies:
        day = src.parent.name.removeprefix("date=")
        dest = partition_file(root, day)
        _atomic_copy(src, dest)
        print(f"已恢复 date={day}（备份仍保留）")
    report = checker.check_store(root)
    print("")
    print(checker.format_report(report))
    return 0 if report.ok else 1


def _select_dates(
    report,
    minute_root: Path,
    override: list[str] | None,
    resumable: Path | None,
) -> tuple[list[str] | None, str | None]:
    """返回 (dates, error)。dates 为 None 表示没有要做的事。"""
    if override:
        missing = []
        for day in override:
            live = partition_file(minute_root, day)
            backed = False
            if resumable is not None and backup_copy_of(resumable, day).is_file():
                backed = True
            if not live.is_file() and not backed:
                missing.append(day)
        if missing:
            return None, "这些日期没有 part.parquet: " + ", ".join(missing)
        return override, None

    unfinished: list[str] = []
    if resumable is not None:
        status = _read_json(resumable / "status.json")
        unfinished = [
            day for day, item in (status.get("dates") or {}).items() if item.get("state") != "done"
        ]
    bad = [item.date for item in report.partitions if not item.ok]
    if not bad and not unfinished:
        return None, None
    merged: list[str] = []
    for day in [*bad, *unfinished]:
        if day not in merged:
            merged.append(day)
    return merged, None


def execute(
    argv: list[str] | None = None,
    *,
    writer: WriterStatus | None = None,
    refetch: RefetchFn | None = None,
    today: date | None = None,
    now: datetime | None = None,
    confirm: ConfirmFn | None = None,
    fetch: FetchJson | None = None,
    batch: int | None = None,
    rpm: int | None = None,
    segment_days: int | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="可选修复分钟 K 分区。默认只打印计划；--apply 才改数据。",
    )
    parser.add_argument("--path", help="分区根目录，默认是 TSP data_dir 下的 kline_minute")
    parser.add_argument("--dates", help="逗号分隔的 YYYY-MM-DD。默认用诊断脚本标出的问题日期")
    parser.add_argument("--apply", action="store_true", help="真正备份、移走坏文件并重拉或做类型转换")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认。必须和 --apply 一起才执行")
    parser.add_argument("--force", action="store_true", help="写入检查不通过时仍继续")
    parser.add_argument("--fresh", action="store_true", help="不接着上次未完成的备份，另开一份")
    parser.add_argument("--resume", help="接着指定的备份目录做")
    parser.add_argument("--rollback", help="把该备份目录里的原 part.parquet 写回分区")
    parser.add_argument("--api-base", help="分钟刷新状态的后端地址，默认 TSP_API_BASE 或 http://127.0.0.1:$PORT")
    args = parser.parse_args(argv)

    if args.path:
        minute_root = Path(args.path).expanduser().resolve()
    else:
        minute_root = checker.default_minute_root()
    data_dir = data_dir_of(minute_root)
    moment = now or _beijing_now()
    today = today or _aware(moment).date()

    if args.rollback and args.resume:
        print("--rollback 和 --resume 不能同时用。")
        return 2

    if writer is None:
        writer = detect_writers(data_dir, args.api_base, fetch=fetch)

    if args.rollback:
        return run_rollback(Path(args.rollback).expanduser().resolve(), minute_root if args.path else None, writer, args.force)

    if not minute_root.is_dir():
        print(f"目录不存在: {minute_root}")
        return 2

    override: list[str] | None = None
    if args.dates:
        override = [item.strip() for item in args.dates.split(",") if item.strip()]
        bad_tokens = [item for item in override if not _DATE_RE.fullmatch(item)]
        if bad_tokens:
            print("日期格式应为 YYYY-MM-DD: " + ", ".join(bad_tokens))
            return 2

    resumable: Path | None = None
    if args.resume:
        resumable = Path(args.resume).expanduser().resolve()
        if not (resumable / "status.json").is_file():
            print(f"找不到状态日志: {resumable / 'status.json'}")
            return 2
    elif not args.fresh:
        resumable = find_resumable(data_dir, minute_root)

    report = checker.check_store(minute_root)
    dates, error = _select_dates(report, minute_root, override, resumable)
    if error:
        print(error)
        return 2
    if not dates:
        print("没有需要修复的日期分区。")
        if not report.ok:
            print(checker.format_report(report))
            return 1
        return 0

    plan = build_plan(
        minute_root,
        dates,
        report,
        today=today,
        now=moment,
        resumable=resumable,
        batch=batch,
        rpm=rpm,
        segment_days=segment_days,
    )
    block = writer_block_reason(writer, args.force)
    print(format_plan(plan, writer, block, apply=args.apply))
    if not args.apply:
        if args.yes:
            print("--yes 只有和 --apply 一起才会改数据。这次仍然只打印计划。")
        return 0
    if block:
        print("已拒绝执行，没有改分区。")
        return 3
    if args.force and (writer.active or writer.http_unknown):
        print("已按 --force 跳过写入检查。请确认没有别的进程在写这些分区。")
    if not args.yes:
        accepted = confirm() if confirm is not None else _prompt_confirm()
        if not accepted:
            print("已取消，没有改分区。")
            return 2

    backup_dir = resumable if resumable is not None else new_backup_dir(data_dir, moment)
    try:
        return run_apply(plan, backup_dir, refetch=refetch or default_refetch, now=moment)
    except KeyboardInterrupt:
        print("已中断。重跑同一命令会从备份目录的 status.json 继续。")
        return 1


def _prompt_confirm() -> bool:
    if not sys.stdin.isatty():
        print("非交互环境请加 --yes。")
        return False
    print("将修改分钟分区。输入 REPAIR 确认，其他输入取消。")
    try:
        answer = input().strip()
    except EOFError:
        return False
    return answer == "REPAIR"


def main(argv: list[str] | None = None, **kwargs: Any) -> int:
    return execute(argv, **kwargs)


if __name__ == "__main__":
    sys.exit(main())
