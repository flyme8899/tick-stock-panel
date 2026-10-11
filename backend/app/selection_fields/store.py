"""选股补充字段的落盘。

失败只改 meta，不把已有 parquet 换成空表。PE 按 symbol 合并，避免每只股票重写全市场。
"""
from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet, atomic_write_text

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
    path = part_path(data_dir, kind)
    schema = SCHEMAS[kind]
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
        atomic_write_parquet(frame, path)
        update_meta(data_dir, kind, status="ok", error="", note=note)
        return
    update_meta(data_dir, kind, status="error", error=error, note=note)


def upsert_pe_rows(data_dir: Path | str, rows: list[dict]) -> None:
    if not rows:
        return
    fresh = _frame("pe_ttm", rows)
    current = read_frame(data_dir, "pe_ttm")
    merged = (
        pl.concat([current, fresh], how="vertical")
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )
    path = part_path(data_dir, "pe_ttm")
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _frame(kind: str, rows: list[dict]) -> pl.DataFrame:
    schema = SCHEMAS[kind]
    if not rows:
        return pl.DataFrame(schema=schema)
    frame = pl.DataFrame(rows)
    for name, dtype in schema.items():
        if name not in frame.columns:
            frame = frame.with_columns(pl.lit(None).cast(dtype).alias(name))
    return frame.select(list(schema)).cast(schema)
