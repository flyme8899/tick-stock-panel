"""``data/research/<kind>/date=YYYY-MM-DD/part.parquet``，按发布日分区。

同一 ``info_code`` 后写覆盖先写。只存 PDF 地址，不存文件。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet, atomic_write_text, partition_lock

KINDS = frozenset({"stock", "industry"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "stock": {
        "info_code": pl.Utf8,
        "code": pl.Utf8,
        "symbol": pl.Utf8,
        "title": pl.Utf8,
        "broker": pl.Utf8,
        "rating": pl.Utf8,
        "eps_this": pl.Float64,
        "eps_next": pl.Float64,
        "eps_two": pl.Float64,
        "publish_date": pl.Utf8,
        "pdf_url": pl.Utf8,
        "industry_name": pl.Utf8,
        "kind": pl.Utf8,
        "source": pl.Utf8,
    },
    "industry": {
        "info_code": pl.Utf8,
        "industry_code": pl.Utf8,
        "industry_name": pl.Utf8,
        "title": pl.Utf8,
        "broker": pl.Utf8,
        "rating": pl.Utf8,
        "publish_date": pl.Utf8,
        "pdf_url": pl.Utf8,
        "kind": pl.Utf8,
        "source": pl.Utf8,
    },
}


def root(data_dir: Path) -> Path:
    return Path(data_dir) / "research"


def state_path(data_dir: Path) -> Path:
    return root(data_dir) / "state.json"


def _check(kind: str, publish_date: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"未知研报分区: {kind}")
    if not _DATE_RE.match(publish_date):
        raise ValueError(f"发布日格式应为 YYYY-MM-DD: {publish_date}")


def partition_file(data_dir: Path, kind: str, publish_date: str) -> Path:
    _check(kind, publish_date)
    return root(data_dir) / kind / f"date={publish_date}" / "part.parquet"


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


def write_rows(data_dir: Path, kind: str, publish_date: str, rows: list[dict]) -> int:
    """按发布日合并。空列表不创建分区。

    「读旧分区 → 合并 → 写回」整段在同一分区的锁里完成，否则并发写会互相覆盖:
    两个线程都读到同一份旧内容，后落盘的那份把先落盘的行冲掉，且不报错。
    """
    _check(kind, publish_date)
    path = partition_file(data_dir, kind, publish_date)
    with partition_lock(f"{kind}:{publish_date}"):
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
        merged = merged.unique(subset=["info_code"], keep="last", maintain_order=True)
        atomic_write_parquet(merged, path)
        return merged.height


def align_frame(kind: str, frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty():
        return empty_frame(kind)
    return frame_of(kind, frame.to_dicts())


def read_partition(data_dir: Path, kind: str, publish_date: str) -> pl.DataFrame:
    path = partition_file(data_dir, kind, publish_date)
    if not path.exists():
        return empty_frame(kind)
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return empty_frame(kind)
    return align_frame(kind, frame)


def list_dates(data_dir: Path, kind: str) -> list[str]:
    if kind not in KINDS:
        raise ValueError(f"未知研报分区: {kind}")
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


def known_codes(state: dict, bucket: str) -> set[str]:
    raw = state.get(bucket)
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if item}


def remember_codes(state: dict, bucket: str, codes: list[str], *, cap: int) -> None:
    current = [str(item) for item in (state.get(bucket) or []) if item]
    for code in codes:
        if code and code not in current:
            current.append(code)
    state[bucket] = current[-cap:]
