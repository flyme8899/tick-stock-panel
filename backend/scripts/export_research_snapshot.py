"""导出研究用只读数据快照：research/_snapshots/YYYYMMDD/。

研究脚本只读这个快照，不连生产库、不在生产容器里跑研究（见《TSP 研究统一规范》）。
本脚本只读生产数据：Parquet 只读打开，SQLite 用 ``mode=ro`` URI 打开，不写 data/ 下任何文件。

服务器上的用法（容器镜像里没有 scripts/，需挂进去；快照目录挂到 /snapshots）：

    cd ~/tick-stock-panel && mkdir -p research/_snapshots
    nice -n 19 ionice -c3 docker compose run --rm --no-deps -T \\
        -v "$PWD/backend/scripts:/app/scripts:ro" \\
        -v "$PWD/research/_snapshots:/snapshots" \\
        -e SNAPSHOT_GIT_COMMIT="$(git rev-parse HEAD)" \\
        app uv run --no-sync python -m scripts.export_research_snapshot \\
        --out /snapshots --dry-run

去掉 ``--dry-run`` 即真正导出。常用参数：

    --start/--end YYYY-MM-DD   日期范围（默认截至今天的近 3 年），作用于按日分区的表和资讯表
    --tables a,b,c             只导出部分表（--list-tables 看全部）
    --tag YYYYMMDD             快照目录名，默认今天（北京时间）
    --force                    目录已存在时覆盖（默认拒绝）
    --min-free-gb 5            导出后剩余空间低于此值则不导出
    --no-nice                  不自动降低进程 CPU/IO 优先级
    --no-csv                   不额外生成 CSV（默认生成，见下）
    --csv-only                 不重新导出，只给已有快照 <tag> 补生成 CSV

默认同时在快照目录下生成 csv/：每张表一个 <表名>.csv.gz（UTF-8、带表头、日期 YYYY-MM-DD、
文本字段一律加引号，股票/基金代码保留前导零），外加 csv_manifest.json / csv_manifest.txt
（行数、md5）。给装不了 pyarrow/polars 的环境用，标准库 csv + gzip 就能读。

脚本启动时会自己 ``nice 19`` + ``ionice idle``，外层的 nice/ionice 只是双保险。
数据逐个分区/逐批读取后流式写出，内存只占一个批次。先写到临时目录，全部成功后才改名，
中途失败不会留下半成品快照。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))
GB = 1024**3
_PART_RE = re.compile(r"^(date|report_period)=(\d{4}-\d{2}-\d{2})$")
# SQLite 文本（按 UTF-8 字节计）经 zstd 压缩后通常只剩 1/3 左右，按 1/2 估算留余量。
_SQLITE_TEXT_RATIO = 0.5
# Parquet 源已是压缩格式，重写为 zstd 后一般不会更大；留 20% 余量。
_PARQUET_RATIO = 1.2


@dataclass(frozen=True)
class TableSpec:
    name: str
    kind: str  # "partitioned" | "file" | "sqlite"
    path: str  # 相对 data_dir
    desc: str
    date_filtered: bool = True
    part_key: str = "date"  # 分区目录名前缀：date=YYYY-MM-DD 或 report_period=YYYY-MM-DD
    # sqlite 专用
    table: str = ""
    columns: tuple[str, ...] = ()
    int_columns: tuple[str, ...] = ("id",)
    date_column: str = ""
    where: str = ""
    size_columns: tuple[str, ...] = ()


TABLES: tuple[TableSpec, ...] = (
    TableSpec("kline_daily", "partitioned", "kline_daily",
              "A 股日 K（不复权），按交易日分区，配合 adj_factor 复权"),
    TableSpec("adj_factor", "file", "adj_factor/all.parquet",
              "复权因子（除权除息事件），全量，不按日期截取", date_filtered=False),
    TableSpec("kline_index_daily", "partitioned", "kline_index_daily", "指数日 K（基准用）"),
    TableSpec("kline_etf_daily", "partitioned", "kline_etf_daily", "ETF 日 K"),
    TableSpec("fund_flow_stock", "partitioned", "fund_flow/stock",
              "个股主力资金流（东财，日度，单位元）"),
    TableSpec("etf_shares", "partitioned", "fund_flow/etf_shares", "ETF 份额（日度）"),
    TableSpec("etf_flow_news", "sqlite", "news/news.sqlite",
              "ETF领航者申赎文章（news.sqlite 中 source=etf_flow，不受 45 天清理）",
              table="news_items",
              columns=("id", "source", "source_id", "published_at", "title",
                       "clean_text", "url", "extra_json", "ingested_at"),
              date_column="published_at", where="source = 'etf_flow'",
              size_columns=("title", "clean_text", "extra_json")),
    TableSpec("news_archive", "sqlite", "news_archive.sqlite",
              "历史快讯归档（东财 7×24、财联社），stock_codes 为 JSON 文本",
              table="news_archive",
              columns=("id", "source", "source_id", "published_at", "title", "content",
                       "level", "stock_codes", "url", "ingested_at"),
              date_column="published_at",
              size_columns=("title", "content", "stock_codes", "url")),
    TableSpec("instruments", "file", "instruments/instruments.parquet",
              "A 股股票列表（当前快照，含总股本/流通股本）", date_filtered=False),
    TableSpec("instruments_etf", "file", "instruments_etf/instruments_etf.parquet",
              "ETF 列表", date_filtered=False),
    TableSpec("instruments_index", "file", "instruments_index/instruments_index.parquet",
              "指数列表", date_filtered=False),
    TableSpec("industry_ths", "file", "ext_data/ext_hy_ths/part.parquet",
              "同花顺行业归属（当前快照）", date_filtered=False),
    TableSpec("concept_ths", "file", "ext_data/ext_gn_ths/part.parquet",
              "同花顺概念归属（当前快照）", date_filtered=False),
    TableSpec("financial_forecast", "partitioned", "financials/em_forecast",
              "业绩预告（东财，按报告期分区，含公告日；一份预告按指标拆多行），全部报告期",
              date_filtered=False, part_key="report_period"),
    TableSpec("financial_express", "partitioned", "financials/em_express",
              "业绩快报（东财，库内当前版本，正式报告发布后可能被改写），全部报告期",
              date_filtered=False, part_key="report_period"),
    TableSpec("financial_report", "partitioned", "financials/em_report",
              "业绩报表（东财，含公告日与更新日），全部报告期",
              date_filtered=False, part_key="report_period"),
    TableSpec("financials_metrics", "file", "financials/metrics/part.parquet",
              "TickFlow 核心财务指标（symbol+period_end，含 announce_date），全量", date_filtered=False),
    TableSpec("financials_income", "file", "financials/income/part.parquet",
              "TickFlow 利润表，全量", date_filtered=False),
    TableSpec("financials_balance_sheet", "file", "financials/balance_sheet/part.parquet",
              "TickFlow 资产负债表，全量", date_filtered=False),
    TableSpec("financials_cash_flow", "file", "financials/cash_flow/part.parquet",
              "TickFlow 现金流量表，全量", date_filtered=False),
    TableSpec("financials_shares", "file", "financials/shares/part.parquet",
              "TickFlow 历史股本，全量", date_filtered=False),
    TableSpec("hot_event_rank", "partitioned", "news/hot_event_rank",
              "热点事件榜按日快照（preopen/postclose/nextday 各档，slot 列区分；"
              "nextday 分区日是目标开市日，可能先于当天写入），全部历史", date_filtered=False),
)
TABLE_BY_NAME = {t.name: t for t in TABLES}


class SnapshotError(RuntimeError):
    pass


@dataclass
class Plan:
    spec: TableSpec
    status: str = "ok"  # ok | missing
    files: list[Path] = field(default_factory=list)
    rows: int = 0
    date_min: str | None = None
    date_max: str | None = None
    est_bytes: int = 0
    source: str = ""


# ---------- 只读访问 ----------

def connect_ro(path: Path) -> sqlite3.Connection:
    """只读打开 SQLite。WAL 库照常能读到最新已提交数据；任何写操作都会报错。"""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _date_bounds(start: date, end: date) -> tuple[str, str]:
    """[start, end] 闭区间，转成 ISO 文本比较用的 [lo, hi)。published_at 都是 +08:00 的 ISO 文本。"""
    return start.isoformat(), (end + timedelta(days=1)).isoformat()


def _partitions(root: Path, start: date | None, end: date | None,
                key: str = "date") -> list[tuple[str, Path]]:
    out = []
    if not root.is_dir():
        return out
    for child in root.iterdir():
        m = _PART_RE.match(child.name)
        if not m or not child.is_dir() or m.group(1) != key:
            continue
        d = m.group(2)
        if start and d < start.isoformat():
            continue
        if end and d > end.isoformat():
            continue
        for f in sorted(child.glob("*.parquet")):
            out.append((d, f))
    out.sort(key=lambda x: (x[0], x[1].name))
    return out


def _sqlite_where(spec: TableSpec, start: date | None, end: date | None) -> tuple[str, list]:
    conds, params = [], []
    if spec.where:
        conds.append(f"({spec.where})")
    if spec.date_filtered and spec.date_column and start and end:
        lo, hi = _date_bounds(start, end)
        conds.append(f'"{spec.date_column}" >= ? AND "{spec.date_column}" < ?')
        params += [lo, hi]
    return (" WHERE " + " AND ".join(conds)) if conds else "", params


def plan_table(spec: TableSpec, data_dir: Path, start: date, end: date) -> Plan:
    import pyarrow.parquet as pq

    src = data_dir / spec.path
    plan = Plan(spec=spec, source=str(src))
    if spec.kind == "partitioned":
        parts = _partitions(src, start if spec.date_filtered else None,
                            end if spec.date_filtered else None, spec.part_key)
        if not parts:
            plan.status = "missing"
            return plan
        plan.files = [f for _, f in parts]
        plan.rows = sum(pq.ParquetFile(f).metadata.num_rows for f in plan.files)
        plan.date_min, plan.date_max = parts[0][0], parts[-1][0]
        plan.est_bytes = int(sum(f.stat().st_size for f in plan.files) * _PARQUET_RATIO)
    elif spec.kind == "file":
        if not src.is_file():
            plan.status = "missing"
            return plan
        plan.files = [src]
        plan.rows = pq.ParquetFile(src).metadata.num_rows
        plan.est_bytes = int(src.stat().st_size * _PARQUET_RATIO)
    elif spec.kind == "sqlite":
        if not src.is_file():
            plan.status = "missing"
            return plan
        plan.files = [src]
        conn = connect_ro(src)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (spec.table,)
            ).fetchone()
            if not exists:
                plan.status = "missing"
                return plan
            where, params = _sqlite_where(spec, start, end)
            size_expr = " + ".join(f'COALESCE(LENGTH(CAST("{c}" AS BLOB)), 0)' for c in spec.size_columns) or "0"
            dc = spec.date_column
            date_sel = f', MIN(SUBSTR("{dc}", 1, 10)), MAX(SUBSTR("{dc}", 1, 10))' if dc else ", NULL, NULL"
            n, size, dmin, dmax = conn.execute(
                f'SELECT COUNT(*), COALESCE(SUM({size_expr}), 0){date_sel} FROM "{spec.table}"{where}',
                params,
            ).fetchone()
        finally:
            conn.close()
        plan.rows, plan.date_min, plan.date_max = int(n), dmin, dmax
        plan.est_bytes = int((size + 64 * n) * _SQLITE_TEXT_RATIO)
    else:  # pragma: no cover
        raise SnapshotError(f"未知表类型 {spec.kind}")
    return plan


# ---------- 写出 ----------

def _unified_schema(files: list[Path]):
    import pyarrow as pa
    import pyarrow.parquet as pq

    schemas = [pq.read_schema(f) for f in files]
    schemas = [s.remove_metadata() for s in schemas]
    try:
        return pa.unify_schemas(schemas, promote_options="permissive")
    except (pa.ArrowInvalid, pa.ArrowTypeError) as e:
        raise SnapshotError(f"分区间字段类型无法合并：{e}") from e


def _conform(table, schema):
    import pyarrow as pa

    cols = []
    for fld in schema:
        if fld.name in table.column_names:
            col = table.column(fld.name)
            if col.type != fld.type:
                col = col.cast(fld.type)
            cols.append(col)
        else:
            cols.append(pa.nulls(table.num_rows, type=fld.type))
    return pa.Table.from_arrays(cols, schema=schema)


def _iter_parquet(plan: Plan, schema, chunk_rows: int) -> Iterator:
    import pyarrow as pa
    import pyarrow.parquet as pq

    buf, n = [], 0
    for f in plan.files:
        pf = pq.ParquetFile(f)
        for rg in range(pf.num_row_groups):
            t = _conform(pf.read_row_group(rg).replace_schema_metadata(None), schema)
            buf.append(t)
            n += t.num_rows
            if n >= chunk_rows:
                yield pa.concat_tables(buf)
                buf, n = [], 0
    if buf:
        yield pa.concat_tables(buf)


def _sqlite_schema(spec: TableSpec):
    import pyarrow as pa

    return pa.schema([
        (c, pa.int64() if c in spec.int_columns else pa.string()) for c in spec.columns
    ])


def _iter_sqlite(plan: Plan, start: date, end: date, chunk_rows: int) -> Iterator:
    import pyarrow as pa

    spec = plan.spec
    schema = _sqlite_schema(spec)
    where, params = _sqlite_where(spec, start, end)
    cols = ", ".join(f'"{c}"' for c in spec.columns)
    conn = connect_ro(plan.files[0])
    try:
        # 一个读事务内分批取，前后一致；WAL 下不阻塞线上写入。
        conn.execute("BEGIN")
        cur = conn.execute(f'SELECT {cols} FROM "{spec.table}"{where} ORDER BY id', params)
        while True:
            rows = cur.fetchmany(chunk_rows)
            if not rows:
                break
            arrays = list(zip(*rows, strict=True))
            yield pa.Table.from_arrays(
                [pa.array(a, type=fld.type) for a, fld in zip(arrays, schema, strict=True)],
                schema=schema,
            )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def export_table(plan: Plan, out_dir: Path, start: date, end: date, chunk_rows: int) -> dict:
    import pyarrow.parquet as pq

    spec = plan.spec
    if spec.kind == "sqlite":
        schema = _sqlite_schema(spec)
        chunks = _iter_sqlite(plan, start, end, chunk_rows)
    else:
        schema = _unified_schema(plan.files)
        chunks = _iter_parquet(plan, schema, chunk_rows)
    target = out_dir / f"{spec.name}.parquet"
    rows = 0
    dmin = dmax = None
    date_col = spec.date_column or None
    with pq.ParquetWriter(target, schema, compression="zstd") as w:
        for chunk in chunks:
            w.write_table(chunk, row_group_size=chunk_rows)
            rows += chunk.num_rows
            if date_col and chunk.num_rows:
                vals = [v[:10] for v in chunk.column(date_col).to_pylist() if v]
                if vals:
                    lo, hi = min(vals), max(vals)
                    dmin = lo if dmin is None or lo < dmin else dmin
                    dmax = hi if dmax is None or hi > dmax else dmax
    if spec.kind != "sqlite":
        dmin, dmax = plan.date_min, plan.date_max
    return {
        "file": target.name,
        "rows": rows,
        "date_min": dmin,
        "date_max": dmax,
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
        "columns": {f.name: str(f.type) for f in schema},
    }


# ---------- CSV ----------

CSV_DIR = "csv"
CSV_FORMAT = ("gzip 压缩 CSV，UTF-8（无 BOM），逗号分隔，首行表头；日期 YYYY-MM-DD；"
              "文本字段一律加双引号（代码保留前导零）；空值为空字段")


def _is_string_list(t) -> bool:
    import pyarrow as pa

    return ((pa.types.is_list(t) or pa.types.is_large_list(t))
            and (pa.types.is_string(t.value_type) or pa.types.is_large_string(t.value_type)))


def _list_to_json_text(col):
    """文本列表转 JSON 数组文本，如 ["000001.SZ","600000.SH"]；空值留空，空列表写 []。"""
    import pyarrow as pa

    return pa.array(
        [None if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"))
         for v in col.to_pylist()],
        type=pa.string(),
    )


def _csv_ready(batch):
    """把 CSV 写不了或写出来不直观的类型转成文本：时间戳转 ISO 文本，空类型转字符串，
    文本列表转 JSON 数组文本。其它嵌套类型仍然报错。"""
    import pyarrow as pa
    import pyarrow.compute as pc

    cols, fields = [], []
    for fld, col in zip(batch.schema, batch.columns, strict=True):
        t = fld.type
        if pa.types.is_timestamp(t):
            col = pc.strftime(col, format="%Y-%m-%dT%H:%M:%S")
        elif pa.types.is_null(t):
            col = col.cast(pa.string())
        elif pa.types.is_dictionary(t):
            col = col.cast(t.value_type)
        elif _is_string_list(t):
            col = _list_to_json_text(col)
        elif pa.types.is_nested(t):
            raise SnapshotError(f"字段 {fld.name} 是嵌套类型 {t}，CSV 无法表示")
        cols.append(col)
        fields.append(pa.field(fld.name, col.type))
    return pa.RecordBatch.from_arrays(cols, schema=pa.schema(fields))


def write_csv_table(parquet_path: Path, csv_path: Path, batch_rows: int = 200_000) -> dict:
    """流式把一个 parquet 写成 csv.gz，返回行数、大小、md5。内存只占一个批次。"""
    import pyarrow as pa
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(parquet_path)
    rows = 0
    writer = None
    sink = pa.CompressedOutputStream(str(csv_path), "gzip")
    try:
        for batch in pf.iter_batches(batch_size=batch_rows):
            batch = _csv_ready(batch)
            if writer is None:
                writer = pacsv.CSVWriter(sink, batch.schema,
                                         write_options=pacsv.WriteOptions(quoting_style="needed"))
            writer.write_batch(batch)
            rows += batch.num_rows
        if writer is None:  # 空表也写表头
            empty = _csv_ready(pa.RecordBatch.from_pylist([], schema=pf.schema_arrow))
            writer = pacsv.CSVWriter(sink, empty.schema,
                                     write_options=pacsv.WriteOptions(quoting_style="needed"))
        writer.close()
    finally:
        sink.close()
    expected = pf.metadata.num_rows
    if rows != expected:
        raise SnapshotError(f"{csv_path.name} 写出 {rows} 行，parquet 有 {expected} 行")
    return {
        "file": csv_path.name,
        "rows": rows,
        "bytes": csv_path.stat().st_size,
        "md5": _md5(csv_path),
        "columns": {f.name: str(f.type) for f in pf.schema_arrow},
    }


def export_csv(snapshot_dir: Path, csv_dir: Path, *, snapshot: str, created: datetime,
               batch_rows: int = 200_000, log: Callable[[str], None] = print) -> dict:
    """把 snapshot_dir 下 MANIFEST 里状态 ok 的表逐张写成 csv_dir/<表>.csv.gz，并写 csv 清单。"""
    manifest = json.loads((snapshot_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    csv_dir.mkdir(parents=True, exist_ok=True)
    tables = {}
    for name, t in manifest["tables"].items():
        if t.get("status") != "ok":
            continue
        log(f"CSV {name} ...")
        info = write_csv_table(snapshot_dir / t["file"], csv_dir / f"{name}.csv.gz", batch_rows)
        if info["rows"] != t["rows"]:
            raise SnapshotError(f"{name} CSV {info['rows']} 行，与 MANIFEST 的 {t['rows']} 行不一致")
        tables[name] = info
    csv_manifest = {
        "snapshot": snapshot,
        "created_at": created.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "format": CSV_FORMAT,
        "git_commit": manifest.get("git_commit"),
        "params": manifest.get("params"),
        "tables": tables,
        "total_bytes": sum(t["bytes"] for t in tables.values()),
    }
    (csv_dir / "csv_manifest.json").write_text(
        json.dumps(csv_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# 快照 {snapshot} CSV 清单；{CSV_FORMAT}", "file\trows\tbytes\tmd5"]
    lines += [f"{t['file']}\t{t['rows']}\t{t['bytes']}\t{t['md5']}" for t in tables.values()]
    (csv_dir / "csv_manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_manifest


def csv_only(*, out_root: Path, tag: str, force: bool = False, batch_rows: int = 200_000,
             now: Callable[[], datetime] = lambda: datetime.now(CN_TZ),
             min_free_gb: float = 5.0, log: Callable[[str], None] = print) -> dict:
    """给已有快照补生成 csv/。先写临时目录，成功后再改名；已有 csv/ 需 --force。"""
    snap_dir = out_root / tag
    if not (snap_dir / "MANIFEST.json").is_file():
        raise SnapshotError(f"快照不存在或缺 MANIFEST.json：{snap_dir}")
    target = snap_dir / CSV_DIR
    if target.exists() and not force:
        raise SnapshotError(f"CSV 目录已存在：{target}（确认覆盖请加 --force）")
    manifest = json.loads((snap_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    est = int(manifest.get("total_bytes", 0) * 2)  # csv.gz 一般不超过 parquet 的 2 倍
    free = shutil.disk_usage(snap_dir).free
    if free - est < min_free_gb * GB:
        raise SnapshotError(f"空间不足：预估 {_fmt_gb(est)}，剩 {_fmt_gb(free)}，门槛 {min_free_gb:g}G")
    tmp = snap_dir / f".tmp-csv-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        result = export_csv(snap_dir, tmp, snapshot=tag, created=now(),
                            batch_rows=batch_rows, log=log)
        old = None
        if target.exists():
            old = snap_dir / f".old-csv-{os.getpid()}"
            target.rename(old)
        tmp.rename(target)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log(f"完成：{target}，合计 {_fmt_gb(result['total_bytes'])}")
    return result


# ---------- 辅助 ----------

def lower_priority() -> list[str]:
    done = []
    try:
        os.nice(19 - os.nice(0))
        done.append("nice=19")
    except OSError:
        pass
    try:
        import psutil

        if hasattr(psutil, "IOPRIO_CLASS_IDLE"):
            psutil.Process().ionice(psutil.IOPRIO_CLASS_IDLE)
            done.append("ionice=idle")
    except Exception:  # noqa: BLE001  ionice 不可用时不影响导出
        pass
    return done


def git_commit(explicit: str = "") -> str:
    if explicit:
        return explicit
    env = os.environ.get("SNAPSHOT_GIT_COMMIT", "").strip()
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def default_data_dir() -> Path:
    env = os.environ.get("DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "data"


def default_out_dir() -> Path:
    env = os.environ.get("SNAPSHOT_OUT_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "research" / "_snapshots"


def three_years_before(d: date) -> date:
    try:
        return d.replace(year=d.year - 3)
    except ValueError:  # 2 月 29 日
        return d.replace(year=d.year - 3, day=28)


def _fmt_gb(n: int | float) -> str:
    return f"{n / GB:.2f}G" if n >= GB / 10 else f"{n / 1024**2:.1f}M"


def render_readme(manifest: dict) -> str:
    lines = [
        f"# 研究数据快照 {manifest['snapshot']}",
        "",
        f"- 生成时间：{manifest['created_at']}（北京时间）",
        f"- 代码版本：{manifest['git_commit']}",
        f"- 日期范围：{manifest['params']['start']} ~ {manifest['params']['end']}"
        "（只作用于按日分区的表和资讯表；列表/行业/复权因子/财务表为全量或当前快照，财务表的日期范围是报告期）",
        f"- 来源：{manifest['source']['data_dir']}（只读导出）",
        "",
        "本目录只读。研究脚本只读这里的文件，不连生产库。校验见 MANIFEST.json 的 sha256。",
        "",
        "| 表 | 行数 | 日期范围 | 大小 | 说明 |",
        "|---|---:|---|---:|---|",
    ]
    for name, t in manifest["tables"].items():
        if t.get("status") != "ok":
            lines.append(f"| {name} | - | - | - | 源数据不存在，未导出 |")
            continue
        rng = f"{t['date_min']} ~ {t['date_max']}" if t.get("date_min") else "-"
        lines.append(
            f"| {name} | {t['rows']:,} | {rng} | {_fmt_gb(t['bytes'])} | {t['desc']} |"
        )
    lines += [
        "",
        "读取示例：",
        "",
        "```python",
        "import polars as pl",
        f"kl = pl.read_parquet('research/_snapshots/{manifest['snapshot']}/kline_daily.parquet')",
        "```",
        "",
        "没有 pyarrow/polars 的环境可读 csv/ 下的 <表名>.csv.gz（如有），"
        "例如 `pandas.read_csv(path, dtype=str)`，行数和 md5 见 csv/csv_manifest.json。",
        "",
        "口径提醒：K 线不复权，需要时用 adj_factor 自行复权；资讯 published_at 为 +08:00 ISO 文本；"
        "股票列表、行业、概念是导出当天的状态，回测有幸存者偏差，请在报告里说明。",
        "",
    ]
    return "\n".join(lines)


# ---------- 主流程 ----------

def run(
    *,
    data_dir: Path,
    out_root: Path,
    tag: str,
    start: date,
    end: date,
    tables: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    min_free_gb: float = 5.0,
    chunk_rows: int = 200_000,
    commit: str = "",
    csv: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(CN_TZ),
    log: Callable[[str], None] = print,
) -> dict:
    if start > end:
        raise SnapshotError(f"--start {start} 晚于 --end {end}")
    if not data_dir.is_dir():
        raise SnapshotError(f"数据目录不存在：{data_dir}")
    names = tables or [t.name for t in TABLES]
    unknown = [n for n in names if n not in TABLE_BY_NAME]
    if unknown:
        raise SnapshotError(f"未知表：{', '.join(unknown)}；可选：{', '.join(TABLE_BY_NAME)}")
    if not re.fullmatch(r"[0-9A-Za-z_-]+", tag):
        raise SnapshotError(f"非法快照名：{tag}")

    target = out_root / tag
    if target.exists() and not force:
        raise SnapshotError(f"快照目录已存在：{target}（确认覆盖请加 --force）")

    plans = [plan_table(TABLE_BY_NAME[n], data_dir, start, end) for n in names]
    est = sum(p.est_bytes for p in plans if p.status == "ok")
    if csv:  # csv.gz 一般不超过 parquet 的 2 倍
        est *= 3
    probe = out_root if out_root.exists() else out_root.parent
    if not probe.exists():
        raise SnapshotError(f"输出目录的上级不存在：{probe}（请用 --out 指定已挂载的目录）")
    free = shutil.disk_usage(probe).free
    after = free - est
    fits = after >= min_free_gb * GB

    log(f"{'表':<26}{'行数':>12}  {'日期范围':<23}{'预估大小':>10}")
    for p in plans:
        if p.status != "ok":
            log(f"{p.spec.name:<26}{'-':>12}  {'(源不存在，跳过)':<23}")
            continue
        rng = f"{p.date_min}~{p.date_max}" if p.date_min else "-"
        log(f"{p.spec.name:<26}{p.rows:>12,}  {rng:<23}{_fmt_gb(p.est_bytes):>10}")
    log(f"预估合计 {_fmt_gb(est)}；当前剩余 {_fmt_gb(free)}；导出后约剩 {_fmt_gb(after)}"
        f"（门槛 {min_free_gb:g}G）→ {'可以导出' if fits else '空间不足'}")

    summary = {
        "dry_run": dry_run,
        "target": str(target),
        "estimated_bytes": est,
        "free_bytes": free,
        "fits": fits,
        "tables": {
            p.spec.name: {"status": p.status, "rows": p.rows, "date_min": p.date_min,
                          "date_max": p.date_max, "estimated_bytes": p.est_bytes}
            for p in plans
        },
    }
    if dry_run:
        return summary
    if not fits:
        raise SnapshotError(
            f"空间不足：预估 {_fmt_gb(est)}，导出后剩 {_fmt_gb(after)} < {min_free_gb:g}G，已放弃"
        )

    out_root.mkdir(parents=True, exist_ok=True)
    tmp = out_root / f".tmp-{tag}-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    created = now()
    try:
        results = {}
        for p in plans:
            entry = {"status": p.status, "desc": p.spec.desc, "source": p.source,
                     "kind": p.spec.kind, "date_filtered": p.spec.date_filtered}
            if p.status == "ok":
                log(f"导出 {p.spec.name} ...")
                entry.update(export_table(p, tmp, start, end, chunk_rows))
                entry["source_files"] = len(p.files)
                if p.spec.where:
                    entry["filter"] = p.spec.where
            results[p.spec.name] = entry
        manifest = {
            "snapshot": tag,
            "created_at": created.isoformat(timespec="seconds"),
            "timezone": "Asia/Shanghai",
            "git_commit": git_commit(commit),
            "source": {"data_dir": str(data_dir), "host": os.uname().nodename,
                       "access": "read-only"},
            "params": {"start": start.isoformat(), "end": end.isoformat(), "tables": names,
                       "chunk_rows": chunk_rows},
            "tables": results,
            "total_bytes": sum(r.get("bytes", 0) for r in results.values()),
        }
        (tmp / "MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if csv:
            cm = export_csv(tmp, tmp / CSV_DIR, snapshot=tag, created=created,
                            batch_rows=chunk_rows, log=log)
            manifest["csv"] = {
                "dir": CSV_DIR,
                "manifest": f"{CSV_DIR}/csv_manifest.json",
                "total_bytes": cm["total_bytes"],
                "tables": {n: {"file": f"{CSV_DIR}/{t['file']}", "rows": t["rows"], "md5": t["md5"]}
                           for n, t in cm["tables"].items()},
            }
        (tmp / "MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (tmp / "README.md").write_text(render_readme(manifest), encoding="utf-8")
        old = None
        if target.exists():
            old = out_root / f".old-{tag}-{os.getpid()}"
            target.rename(old)
        tmp.rename(target)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log(f"完成：{target}，合计 {_fmt_gb(manifest['total_bytes'])}")
    summary["manifest"] = manifest
    return summary


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出研究用只读数据快照")
    ap.add_argument("--data-dir", type=Path, default=None, help="TSP data 目录，默认 DATA_DIR 或 ./data")
    ap.add_argument("--out", type=Path, default=None,
                    help="快照根目录，默认 SNAPSHOT_OUT_DIR 或 <repo>/research/_snapshots")
    ap.add_argument("--tag", default="", help="快照目录名，默认今天 YYYYMMDD（北京时间）")
    ap.add_argument("--start", type=_parse_date, default=None)
    ap.add_argument("--end", type=_parse_date, default=None)
    ap.add_argument("--tables", default="", help="逗号分隔的表名子集")
    ap.add_argument("--list-tables", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只统计行数和预估大小，不写文件")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的同名快照")
    ap.add_argument("--min-free-gb", type=float, default=5.0)
    ap.add_argument("--chunk-rows", type=int, default=200_000)
    ap.add_argument("--git-commit", default="")
    ap.add_argument("--no-nice", action="store_true")
    ap.add_argument("--csv", action=argparse.BooleanOptionalAction, default=True,
                    help="同时生成 csv/ 下的 csv.gz 和 csv 清单（默认开，--no-csv 关闭）")
    ap.add_argument("--csv-only", action="store_true",
                    help="不重新导出，只给已有快照 --tag 补生成 csv/（已有 csv/ 需 --force）")
    args = ap.parse_args(argv)

    if args.list_tables:
        for t in TABLES:
            print(f"{t.name:<26}{t.desc}")
        return 0
    if not args.no_nice:
        pr = lower_priority()
        if pr:
            print("已降低优先级：" + ", ".join(pr))
    today = datetime.now(CN_TZ).date()
    end = args.end or today
    start = args.start or three_years_before(end)
    tables = [s.strip() for s in args.tables.split(",") if s.strip()] or None
    try:
        if args.csv_only:
            if not args.tag:
                raise SnapshotError("--csv-only 需要用 --tag 指定已有快照")
            csv_only(out_root=args.out or default_out_dir(), tag=args.tag, force=args.force,
                     batch_rows=max(1000, args.chunk_rows), min_free_gb=args.min_free_gb)
            return 0
        run(
            data_dir=args.data_dir or default_data_dir(),
            out_root=args.out or default_out_dir(),
            tag=args.tag or today.strftime("%Y%m%d"),
            start=start, end=end, tables=tables, dry_run=args.dry_run,
            force=args.force, min_free_gb=args.min_free_gb,
            chunk_rows=max(1000, args.chunk_rows), commit=args.git_commit, csv=args.csv,
        )
    except SnapshotError as e:
        print(f"失败：{e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
