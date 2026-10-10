"""按日落盘的 parquet。

``data/fund_flow/<kind>/date=YYYY-MM-DD/part.parquet``。写入走临时文件替换。
同一分区再次写入时按主键合并，后写入的行覆盖同一主键。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet, atomic_write_text

KINDS = frozenset({
    "stock",
    "industry",
    "concept",
    "margin",
    "etf_shares",
    "southbound",
    "northbound_turnover",
    "lhb",
})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 合并主键。后写覆盖先写。
KEYS: dict[str, list[str]] = {
    "stock": ["code", "trade_date"],
    "industry": ["name", "snapshot", "captured_at"],
    "concept": ["name", "snapshot", "captured_at"],
    "margin": ["market", "symbol", "row_kind", "trade_date"],
    "etf_shares": ["code", "trade_date"],
    "southbound": ["trade_date"],
    "northbound_turnover": ["trade_date"],
    "lhb": ["code", "trade_date"],
}

_SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "stock": {
        "symbol": pl.Utf8,
        "code": pl.Utf8,
        "trade_date": pl.Utf8,
        "main_net": pl.Float64,
        "large_net": pl.Float64,
        "super_net": pl.Float64,
        "source": pl.Utf8,
    },
    "industry": {
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "captured_at": pl.Utf8,
        "snapshot": pl.Utf8,
        "net_inflow": pl.Float64,
        "rank": pl.Int64,
        "source": pl.Utf8,
    },
    "concept": {
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "captured_at": pl.Utf8,
        "snapshot": pl.Utf8,
        "net_inflow": pl.Float64,
        "rank": pl.Int64,
        "source": pl.Utf8,
    },
    "margin": {
        "market": pl.Utf8,
        "symbol": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "row_kind": pl.Utf8,
        "margin_balance": pl.Float64,
        "short_balance": pl.Float64,
        "margin_buy": pl.Float64,
        "source": pl.Utf8,
    },
    "etf_shares": {
        "symbol": pl.Utf8,
        "code": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "shares": pl.Float64,
        "source": pl.Utf8,
    },
    "southbound": {
        "trade_date": pl.Utf8,
        "net_flow": pl.Float64,
        "source": pl.Utf8,
    },
    "northbound_turnover": {
        "trade_date": pl.Utf8,
        "turnover": pl.Float64,
        "source": pl.Utf8,
    },
    "lhb": {
        "symbol": pl.Utf8,
        "code": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "net_buy": pl.Float64,
        "change_pct_points": pl.Float64,
        "source": pl.Utf8,
    },
}


def root(data_dir: Path) -> Path:
    return Path(data_dir) / "fund_flow"


def _check(kind: str, trade_date: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"未知资金进出分区: {kind}")
    if not _DATE_RE.match(trade_date):
        raise ValueError(f"交易日格式应为 YYYY-MM-DD: {trade_date}")


def partition_file(data_dir: Path, kind: str, trade_date: str) -> Path:
    _check(kind, trade_date)
    return root(data_dir) / kind / f"date={trade_date}" / "part.parquet"


def empty_frame(kind: str) -> pl.DataFrame:
    schema = _SCHEMAS[kind]
    return pl.DataFrame(schema=schema)


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


def write_rows(data_dir: Path, kind: str, trade_date: str, rows: list[dict]) -> int:
    """合并写入一个交易日分区。返回该分区合并后的行数。"""
    _check(kind, trade_date)
    incoming = frame_of(kind, rows)
    if incoming.is_empty():
        path = partition_file(data_dir, kind, trade_date)
        if path.exists():
            return pl.read_parquet(path).height
        return 0
    path = partition_file(data_dir, kind, trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = pl.read_parquet(path)
        merged = pl.concat([frame_of(kind, previous.to_dicts()), incoming], how="vertical_relaxed")
    else:
        merged = incoming
    keys = [key for key in KEYS[kind] if key in merged.columns]
    merged = merged.unique(subset=keys, keep="last")
    atomic_write_parquet(merged, path)
    return merged.height


def read_partition(data_dir: Path, kind: str, trade_date: str) -> pl.DataFrame:
    path = partition_file(data_dir, kind, trade_date)
    if not path.exists():
        return empty_frame(kind)
    return pl.read_parquet(path)


def list_dates(data_dir: Path, kind: str) -> list[str]:
    if kind not in KINDS:
        raise ValueError(f"未知资金进出分区: {kind}")
    base = root(data_dir) / kind
    if not base.exists():
        return []
    found = []
    for path in base.glob("date=*"):
        if path.is_dir() and (path / "part.parquet").exists():
            found.append(path.name.removeprefix("date="))
    return sorted(found)


def read_range(data_dir: Path, kind: str, *, start: str | None = None, end: str | None = None) -> pl.DataFrame:
    dates = [
        item for item in list_dates(data_dir, kind)
        if (start is None or item >= start) and (end is None or item <= end)
    ]
    if not dates:
        return empty_frame(kind)
    frames = [read_partition(data_dir, kind, item) for item in dates]
    return pl.concat(frames, how="vertical_relaxed")


def latest_date(data_dir: Path, kind: str) -> str | None:
    dates = list_dates(data_dir, kind)
    return dates[-1] if dates else None


def state_path(data_dir: Path) -> Path:
    return root(data_dir) / "_state" / "status.json"


def load_state(data_dir: Path) -> dict:
    path = state_path(data_dir)
    if not path.exists():
        return {"kinds": {}, "sources": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"kinds": {}, "sources": {}}
    if not isinstance(payload, dict):
        return {"kinds": {}, "sources": {}}
    payload.setdefault("kinds", {})
    payload.setdefault("sources", {})
    return payload


def save_state(data_dir: Path, payload: dict) -> None:
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def update_kind_state(data_dir: Path, kind: str, **fields: object) -> dict:
    payload = load_state(data_dir)
    current = dict(payload["kinds"].get(kind) or {})
    current.update(fields)
    current["updated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["kinds"][kind] = current
    save_state(data_dir, payload)
    return current
