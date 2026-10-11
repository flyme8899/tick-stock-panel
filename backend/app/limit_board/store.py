"""``data/limit_board/<kind>/date=YYYY-MM-DD/part.parquet``。

写入先落临时文件再替换。同一主键后写覆盖先写。金额单位是元。
``pct`` / ``turnover`` 沿用东财和同花顺的百分数口径（10 表示 10%）。
``break_rate`` 和 ``seal_rate`` 是 0–1 的比例。
"""
from __future__ import annotations

import re
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet

KINDS = frozenset({"zt", "zb", "dt", "yzt", "sentiment", "ths"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

KEYS: dict[str, list[str]] = {
    "zt": ["code", "trade_date"],
    "zb": ["code", "trade_date"],
    "dt": ["code", "trade_date"],
    "yzt": ["code", "trade_date"],
    "sentiment": ["trade_date"],
    "ths": ["code", "trade_date"],
}

_SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "zt": {
        "code": pl.Utf8,
        "symbol": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "price": pl.Float64,
        "pct": pl.Float64,
        "amount": pl.Float64,
        "float_cap": pl.Float64,
        "turnover": pl.Float64,
        "limit_days": pl.Int64,
        "first_seal": pl.Utf8,
        "last_seal": pl.Utf8,
        "seal_fund": pl.Float64,
        "break_times": pl.Int64,
        "industry": pl.Utf8,
        "zt_stat": pl.Utf8,
        "source": pl.Utf8,
    },
    "zb": {
        "code": pl.Utf8,
        "symbol": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "price": pl.Float64,
        "limit_price": pl.Float64,
        "pct": pl.Float64,
        "turnover": pl.Float64,
        "first_seal": pl.Utf8,
        "break_times": pl.Int64,
        "amplitude": pl.Float64,
        "speed": pl.Float64,
        "industry": pl.Utf8,
        "zt_stat": pl.Utf8,
        "source": pl.Utf8,
    },
    "dt": {
        "code": pl.Utf8,
        "symbol": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "price": pl.Float64,
        "pct": pl.Float64,
        "turnover": pl.Float64,
        "pe": pl.Float64,
        "seal_fund": pl.Float64,
        "last_seal": pl.Utf8,
        "board_amount": pl.Float64,
        "dt_days": pl.Int64,
        "open_times": pl.Int64,
        "industry": pl.Utf8,
        "source": pl.Utf8,
    },
    "yzt": {
        "code": pl.Utf8,
        "symbol": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "price": pl.Float64,
        "pct": pl.Float64,
        "turnover": pl.Float64,
        "amplitude": pl.Float64,
        "speed": pl.Float64,
        "y_first_seal": pl.Utf8,
        "y_limit_days": pl.Int64,
        "industry": pl.Utf8,
        "zt_stat": pl.Utf8,
        "source": pl.Utf8,
    },
    "sentiment": {
        "trade_date": pl.Utf8,
        "zt_count": pl.Int64,
        "zb_count": pl.Int64,
        "dt_count": pl.Int64,
        "yzt_count": pl.Int64,
        "break_rate": pl.Float64,
        "max_height": pl.Int64,
        "ladder_json": pl.Utf8,
        "advance_count": pl.Int64,
        "source": pl.Utf8,
    },
    "ths": {
        "code": pl.Utf8,
        "symbol": pl.Utf8,
        "name": pl.Utf8,
        "trade_date": pl.Utf8,
        "price": pl.Float64,
        "pct": pl.Float64,
        "reason": pl.Utf8,
        "themes": pl.Utf8,
        "board_type": pl.Utf8,
        "seal_rate": pl.Float64,
        "break_times": pl.Int64,
        "seal_amount": pl.Float64,
        "high_days": pl.Utf8,
        "first_time": pl.Utf8,
        "is_again": pl.Int64,
        "source": pl.Utf8,
    },
}


def root(data_dir: Path) -> Path:
    return Path(data_dir) / "limit_board"


def _check(kind: str, trade_date: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"未知打板分区: {kind}")
    if not _DATE_RE.match(trade_date):
        raise ValueError(f"交易日格式应为 YYYY-MM-DD: {trade_date}")


def partition_file(data_dir: Path, kind: str, trade_date: str) -> Path:
    _check(kind, trade_date)
    return root(data_dir) / kind / f"date={trade_date}" / "part.parquet"


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


def write_rows(data_dir: Path, kind: str, trade_date: str, rows: list[dict]) -> int:
    """整表替换当天分区。池子是快照，开板的股票不能留在上一份涨停池里。

    空列表会写成空分区，用来记下「当天确认没有记录」。调用方只有在接口明确
    给出空池时才传空列表；请求失败不要调用这里。
    """
    _check(kind, trade_date)
    frame = frame_of(kind, rows)
    if not frame.is_empty():
        keys = [key for key in KEYS[kind] if key in frame.columns]
        frame = frame.unique(subset=keys, keep="last", maintain_order=True)
    path = partition_file(data_dir, kind, trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(frame, path)
    return frame.height


def align_frame(kind: str, frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty():
        return empty_frame(kind)
    return frame_of(kind, frame.to_dicts())


def read_partition(data_dir: Path, kind: str, trade_date: str) -> pl.DataFrame:
    path = partition_file(data_dir, kind, trade_date)
    if not path.exists():
        return empty_frame(kind)
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return empty_frame(kind)
    return align_frame(kind, frame)


def list_dates(data_dir: Path, kind: str) -> list[str]:
    if kind not in KINDS:
        raise ValueError(f"未知打板分区: {kind}")
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
