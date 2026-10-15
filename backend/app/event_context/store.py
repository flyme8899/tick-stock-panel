"""``data/event_context/<kind>/date=YYYY-MM-DD/part.parquet``。

写入走临时文件替换。空列表不覆盖已有分区，上次成功的结果留在盘上。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet, atomic_write_text

KINDS = frozenset({"lhb", "margin", "qa", "notice", "changes"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_KEYS: dict[str, list[str]] = {
    "lhb": ["code", "trade_date", "broker", "side"],
    "margin": ["code", "trade_date"],
    "qa": ["question_id"],
    "notice": ["art_code"],
    "changes": ["code", "trade_date", "kind", "at"],
}

_SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "lhb": {
        "code": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "net_buy": pl.Float64,
        "change_pct_points": pl.Float64,
        "reason": pl.Utf8,
        "broker": pl.Utf8,
        "broker_net": pl.Float64,
        "side": pl.Utf8,
        "source": pl.Utf8,
    },
    "margin": {
        "code": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "market": pl.Utf8,
        "margin_balance": pl.Float64,
        "short_balance": pl.Float64,
        "margin_buy": pl.Float64,
        "source": pl.Utf8,
    },
    "qa": {
        "question_id": pl.Utf8,
        "code": pl.Utf8,
        "name": pl.Utf8,
        "published_at": pl.Utf8,
        "question": pl.Utf8,
        "answer": pl.Utf8,
        "source": pl.Utf8,
    },
    "notice": {
        "art_code": pl.Utf8,
        "code": pl.Utf8,
        "name": pl.Utf8,
        "title": pl.Utf8,
        "column_name": pl.Utf8,
        "notice_date": pl.Utf8,
        "url": pl.Utf8,
        "source": pl.Utf8,
        "published_at": pl.Utf8,
        "time_precision": pl.Utf8,
        "available_from": pl.Utf8,
    },
    "changes": {
        "code": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "kind": pl.Utf8,
        "at": pl.Utf8,
        "info": pl.Utf8,
        "source": pl.Utf8,
    },
}


def root(data_dir: Path) -> Path:
    return Path(data_dir) / "event_context"


def state_path(data_dir: Path) -> Path:
    return root(data_dir) / "state.json"


def _check(kind: str, day: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"未知事件上下文分区: {kind}")
    if not _DATE_RE.match(day):
        raise ValueError(f"交易日格式应为 YYYY-MM-DD: {day}")


def partition_file(data_dir: Path, kind: str, day: str) -> Path:
    _check(kind, day)
    return root(data_dir) / kind / f"date={day}" / "part.parquet"


def empty_frame(kind: str) -> pl.DataFrame:
    return pl.DataFrame(schema=_SCHEMAS[kind])


def frame_of(kind: str, rows: list[dict]) -> pl.DataFrame:
    schema = _SCHEMAS[kind]
    if not rows:
        return empty_frame(kind)
    df = pl.DataFrame(rows)
    exprs = []
    for name, dtype in schema.items():
        if name in df.columns:
            exprs.append(pl.col(name).cast(dtype, strict=False))
        else:
            exprs.append(pl.lit(None).cast(dtype).alias(name))
    return df.select(exprs)


def align_frame(kind: str, frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty():
        return empty_frame(kind)
    return frame_of(kind, frame.to_dicts())


def write_rows(data_dir: Path, kind: str, day: str, rows: list[dict]) -> int:
    """合并写入。空列表不创建、也不清空已有分区。"""
    _check(kind, day)
    path = partition_file(data_dir, kind, day)
    if not rows:
        if path.exists():
            return align_frame(kind, pl.read_parquet(path)).height
        return 0
    incoming = frame_of(kind, rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = align_frame(kind, pl.read_parquet(path))
        merged = pl.concat([previous, incoming], how="vertical_relaxed")
    else:
        merged = incoming
    merged = merged.unique(subset=_KEYS[kind], keep="last", maintain_order=True)
    atomic_write_parquet(merged, path)
    return merged.height


def read_partition(data_dir: Path, kind: str, day: str) -> pl.DataFrame:
    path = partition_file(data_dir, kind, day)
    if not path.exists():
        return empty_frame(kind)
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return empty_frame(kind)
    return align_frame(kind, frame)


def list_dates(data_dir: Path, kind: str) -> list[str]:
    if kind not in KINDS:
        raise ValueError(f"未知事件上下文分区: {kind}")
    base = root(data_dir) / kind
    if not base.exists():
        return []
    found = []
    for path in base.glob("date=*"):
        if path.is_dir() and (path / "part.parquet").exists():
            found.append(path.name.removeprefix("date="))
    return sorted(found)


def latest_date(data_dir: Path, kind: str) -> str | None:
    dates = list_dates(data_dir, kind)
    return dates[-1] if dates else None


def load_state(data_dir: Path) -> dict:
    path = state_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(data_dir: Path, state: dict) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))
