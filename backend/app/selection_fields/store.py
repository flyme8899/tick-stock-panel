"""选股补充字段的落盘。

失败只改 meta，不把已有 parquet 换成空表。PE 按 symbol 合并，避免每只股票重写全市场。
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet, atomic_write_text, partition_writer

KINDS = ("northbound", "holders", "index_members", "unlock", "pe_ttm")

SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "northbound": {
        "symbol": pl.Utf8,
        "northbound_hold_pct": pl.Float64,
        "trade_date": pl.Utf8,
    },
    "holders": {
        "symbol": pl.Utf8,
        "holder_num": pl.Int64,
        "holder_change_pct": pl.Float64,
        "report_date": pl.Utf8,
    },
    "index_members": {
        "symbol": pl.Utf8,
        "index_membership": pl.Utf8,
    },
    "unlock": {
        "symbol": pl.Utf8,
        "next_unlock_date": pl.Utf8,
        "unlock_float_ratio": pl.Float64,
    },
    "pe_ttm": {
        "symbol": pl.Utf8,
        "pe_ttm": pl.Float64,
        "as_of": pl.Utf8,
    },
}


def root(data_dir: Path | str) -> Path:
    return Path(data_dir) / "selection_fields"


def part_path(data_dir: Path | str, kind: str) -> Path:
    return root(data_dir) / kind / "part.parquet"


def meta_path(data_dir: Path | str, kind: str) -> Path:
    return root(data_dir) / kind / "meta.json"


def checkpoint_path(data_dir: Path | str) -> Path:
    return root(data_dir) / "pe_ttm" / "checkpoint.json"


def read_meta(data_dir: Path | str, kind: str) -> dict:
    path = meta_path(data_dir, kind)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def read_frame(data_dir: Path | str, kind: str) -> pl.DataFrame:
    return _read_schema(part_path(data_dir, kind), SCHEMAS[kind])


def index_part_path(data_dir: Path | str, code: str) -> Path:
    _check_index_code(code)
    return root(data_dir) / "index_members" / f"{code}.parquet"


def index_meta_path(data_dir: Path | str, code: str) -> Path:
    _check_index_code(code)
    return root(data_dir) / "index_members" / f"{code}.json"


def read_index_part(data_dir: Path | str, code: str) -> pl.DataFrame:
    return _read_schema(index_part_path(data_dir, code), SCHEMAS["index_members"])


def read_index_meta(data_dir: Path | str, code: str) -> dict:
    path = index_meta_path(data_dir, code)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def commit_index(
    data_dir: Path | str,
    code: str,
    rows: list[dict],
    *,
    ok: bool,
    error: str = "",
    note: str = "",
    as_of: str = "",
) -> None:
    """一个指数一份 parquet。失败不替换该指数已有的成分。"""
    if ok:
        frame = _frame("index_members", rows)
        path = index_part_path(data_dir, code)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(frame, path)
        _write_index_meta(data_dir, code, status="ok", error="", note=note, as_of=as_of)
        return
    _write_index_meta(data_dir, code, status="error", error=error, note=note, as_of="")


def update_meta(
    data_dir: Path | str,
    kind: str,
    *,
    status: str,
    error: str = "",
    note: str = "",
) -> None:
    """只写 meta。已有 parquet 保持不动。"""
    path = meta_path(data_dir, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "error": error[:300], "note": note[:300]}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def commit(
    data_dir: Path | str,
    kind: str,
    rows: list[dict],
    *,
    ok: bool,
    error: str = "",
    note: str = "",
) -> None:
    """成功才替换 parquet。失败只记录 error，上次成功的表还在。"""
    if ok:
        frame = _frame(kind, rows)
        path = part_path(data_dir, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 整表覆盖也要进锁: 与 upsert_pe_rows 的读改写共用 `pe_ttm` 这把锁, 否则
        # 一次全量 commit 与一次增量 upsert 交错时, 全量落盘的那份会把增量行冲掉。
        # 跨进程同理 —— 全量刷新与增量补数可能分别在 web 进程和脚本里跑。
        with partition_writer(Path(data_dir), kind, retry_s=0.2, attempts=10, on_busy="info"):
            atomic_write_parquet(frame, path)
            update_meta(data_dir, kind, status="ok", error="", note=note)
        return
    update_meta(data_dir, kind, status="error", error=error, note=note)


def upsert_pe_rows(data_dir: Path | str, rows: list[dict]) -> None:
    """按 symbol 增量合并 PE。

    「读旧表 → 合并 → 写回」必须在同一把锁里, 否则两个线程都读到同一份旧表, 各自
    合并后分别落盘, 后落盘的那份把先落盘的新行整片覆盖 —— 没有异常, 数据静默丢失。
    跨进程 (定时增量 vs 手动补数) 由 ``partition_writer`` 的文件锁兜住。
    """
    if not rows:
        return
    fresh = _frame("pe_ttm", rows)
    path = part_path(data_dir, "pe_ttm")
    path.parent.mkdir(parents=True, exist_ok=True)
    with partition_writer(Path(data_dir), "pe_ttm", retry_s=0.2, attempts=10, on_busy="info"):
        current = read_frame(data_dir, "pe_ttm")
        merged = (
            pl.concat([current, fresh], how="vertical")
            .unique(subset=["symbol"], keep="last")
            .sort("symbol")
        )
        atomic_write_parquet(merged, path)
        update_meta(data_dir, "pe_ttm", status="ok", error="", note="")


def load_checkpoint(data_dir: Path | str) -> dict:
    path = checkpoint_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_checkpoint(data_dir: Path | str, payload: dict) -> None:
    path = checkpoint_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _check_index_code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"指数代码无效: {code}")


def _read_schema(path: Path, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    if not path.exists():
        return pl.DataFrame(schema=schema)
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001 — 坏文件当成没有，避免读侧把结果清空
        return pl.DataFrame(schema=schema)
    for name, dtype in schema.items():
        if name not in frame.columns:
            frame = frame.with_columns(pl.lit(None).cast(dtype).alias(name))
    return frame.select(list(schema)).cast(schema)


def _write_index_meta(
    data_dir: Path | str,
    code: str,
    *,
    status: str,
    error: str,
    note: str,
    as_of: str,
) -> None:
    path = index_meta_path(data_dir, code)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "error": error[:300],
        "note": note[:300],
        "as_of": as_of,
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _frame(kind: str, rows: list[dict]) -> pl.DataFrame:
    schema = SCHEMAS[kind]
    if not rows:
        return pl.DataFrame(schema=schema)
    frame = pl.DataFrame(rows)
    for name, dtype in schema.items():
        if name not in frame.columns:
            frame = frame.with_columns(pl.lit(None).cast(dtype).alias(name))
    return frame.select(list(schema)).cast(schema)
