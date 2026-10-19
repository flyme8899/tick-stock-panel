"""回补仍在上市、但本地股本序列断更或滞后的股票。

巨潮（akshare ``stock_share_change_cninfo``）是主源，adata
``stock.info.get_stock_shares(is_history=True)`` 做交叉校验。只核对两边都有的
变动日；对不上的那一个点进复核，其余点仍可写。巨潮独有、且晚于 adata 最新
变动日的更新允许写入，``source`` 标 ``cninfo_only``。

巨潮的总股本、人民币普通股、已流通股份、流通受限股份单位都是万股，映射时
固定乘 10000 转成股。adata 的 ``total_shares`` / ``list_a_shares`` 已经是股。
换算后仍相差超过 1% 的点进复核，不根据数量级猜单位。``period_end`` 用变动日，
``announce_date`` 用巨潮公告日。两边都确认的行 ``source`` 为 ``cninfo+adata``。
已有列（含 ``source``）原样保留，只改被追加或修正的行。

现网 ``instruments.list_status`` 全部为空，退市状态列判断不到任何股票。是否
仍在上市实际只看近 ``--active-days``（默认 30）个自然日的日线成交量是否大于 0。
因此 600065、603157 这类近端没有成交的股票，默认模式和 ``--symbols`` 都不会入选。

默认 dry-run。``--apply`` 才写 ``financials/shares/part.parquet``，写之前把分区
复制到 ``financials/shares/backups/``。请求间隔约 1.2 秒，失败重试；北京时间
23:50–00:10 暂停，避开 23:55 的定时任务。

容器内（数据目录 ``/app/data``）::

    uv run --no-sync python -m app.services.shares_backfill --data-dir /app/data
    uv run --no-sync python -m app.services.shares_backfill --data-dir /app/data --apply

``--scan-values`` 会把仍有成交的股票都拿去和外部最新值比较，用来抓最后一期
并不早、但股本已经差超过 1% 的标的。
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_now

logger = logging.getLogger(__name__)

SOURCE_TAG = "cninfo+adata"
SOURCE_CNINFO_ONLY = "cninfo_only"
_CNINFO_WAN_TO_SHARES = 10_000
DEFAULT_STALE_BEFORE = date(2025, 1, 1)
DEFAULT_ACTIVE_DAYS = 30
DEFAULT_SPACING_S = 1.2
DEFAULT_RETRIES = 3
REL_TOL = 0.01
_PAUSE_START_S = 23 * 3600 + 50 * 60
_PAUSE_END_S = 10 * 60
_DELISTED_STATUS = frozenset({"D", "DELISTED", "退市", "已退市", "终止上市"})

Clock = Callable[[], float]
Now = Callable[[], datetime]
Sleeper = Callable[[float], None]
Fetcher = Callable[[str], list["ShareEvent"]]


@dataclass(frozen=True)
class ShareEvent:
    period_end: date
    announce_date: date | None
    total_shares: float
    float_shares: float | None
    source: str = SOURCE_TAG


@dataclass(frozen=True)
class LocalRow:
    period_end: date
    announce_date: date | None
    total_shares: float | None
    float_shares: float | None
    source: str | None


@dataclass(frozen=True)
class Change:
    symbol: str
    period_end: date
    action: str
    announce_date_old: date | None
    announce_date_new: date | None
    total_old: float | None
    total_new: float
    float_old: float | None
    float_new: float | None
    source_old: str | None
    source_new: str


@dataclass(frozen=True)
class ReviewItem:
    symbol: str
    reason: str
    cninfo_latest_date: str = ""
    cninfo_total: str = ""
    adata_latest_date: str = ""
    adata_total: str = ""
    local_latest_date: str = ""
    local_total: str = ""


@dataclass
class BackfillResult:
    candidates: list[str] = field(default_factory=list)
    skipped_delisted: list[str] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    reviews: list[ReviewItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    wrote: bool = False
    backup: Path | None = None
    diff_report: Path | None = None
    review_report: Path | None = None


def shares_path(data_dir: Path) -> Path:
    return data_dir / "financials" / "shares" / "part.parquet"


def rel_diff(left: float, right: float) -> float:
    denom = max(abs(left), abs(right))
    if denom == 0:
        return 0.0
    return abs(left - right) / denom


def needs_share_backfill(
    *,
    last_period: date | None,
    stale_before: date,
    local_total: float | None,
    external_total: float | None,
) -> bool:
    """断更（最后一期早于阈值）或最新股本与外部值相差超过 1%。"""
    if last_period is None or last_period < stale_before:
        return True
    if local_total is None or external_total is None:
        return False
    return rel_diff(local_total, external_total) > REL_TOL


def _as_cn(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=CN_TZ)
    return now.astimezone(CN_TZ)


def seconds_until_clear(now: datetime) -> float:
    """北京时间 23:50（含）到次日 00:10（不含）需要暂停的秒数。"""
    local = _as_cn(now)
    stamp = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
    if stamp >= _PAUSE_START_S:
        return (24 * 3600 - stamp) + _PAUSE_END_S
    if stamp < _PAUSE_END_S:
        return _PAUSE_END_S - stamp
    return 0.0


class Pacer:
    """每次外部请求前先避开停机窗口，再保证最小间隔。间隔从请求开始算。"""

    def __init__(self, spacing_s: float, sleeper: Sleeper, monotonic: Clock, now: Now) -> None:
        self.spacing_s = spacing_s
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.now = now
        self._last: float | None = None

    def wait(self) -> None:
        pause = seconds_until_clear(self.now())
        if pause > 0:
            logger.info("股本回补暂停 %.0f 秒，避开 23:55 定时任务", pause)
            self.sleeper(pause)
        if self._last is not None and self.spacing_s > 0:
            gap = self.spacing_s - (self.monotonic() - self._last)
            if gap > 0:
                self.sleeper(gap)
        self._last = self.monotonic()


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value or value.lower() in {"nan", "none", "null", "--"}:
            return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number <= 0:
        return None
    return number


def _as_date(value: object) -> date | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "nat", "--"}:
        return None
    text = text[:10].replace("/", "-")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _frame_records(frame: object) -> list[dict]:
    if frame is None:
        return []
    if isinstance(frame, pl.DataFrame):
        return [] if frame.is_empty() else frame.to_dicts()
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        try:
            records = to_dict(orient="records")
        except TypeError:
            records = to_dict()
        if isinstance(records, list):
            return [dict(row) for row in records]
    return [dict(row) for row in frame]  # type: ignore[arg-type]


def _code_matches(row_code: object, symbol: str) -> bool:
    if row_code is None:
        return True
    text = str(row_code).strip().upper()
    if not text or text in {"NAN", "NONE"}:
        return True
    return text.split(".", 1)[0] == symbol.split(".", 1)[0]


def _collapse(events: list[ShareEvent]) -> dict[date, ShareEvent]:
    folded: dict[date, ShareEvent] = {}
    for event in events:
        folded[event.period_end] = event
    return folded


def _first_float(row: dict, *names: str) -> float | None:
    for name in names:
        if name not in row:
            continue
        number = _as_float(row.get(name))
        if number is not None:
            return number
    return None


def _cninfo_shares(row: dict, *names: str) -> float | None:
    """巨潮股本列是万股。固定乘 10000，不按数量级猜测。"""
    number = _first_float(row, *names)
    if number is None:
        return None
    return number * _CNINFO_WAN_TO_SHARES


def map_cninfo(symbol: str, frame: object) -> list[ShareEvent]:
    """巨潮股本变动 → 内部事件，单位换成股。

    ``总股本``、``人民币普通股``、``已流通股份``、``流通受限股份`` 都是万股。
    入库的流通股本优先用人民币普通股，否则用已流通股份。流通受限股份没有对应列。
    """
    events: list[ShareEvent] = []
    for row in _frame_records(frame):
        if not _code_matches(row.get("证券代码"), symbol):
            continue
        period = _as_date(row.get("变动日期"))
        total = _cninfo_shares(row, "总股本")
        if period is None or total is None:
            continue
        events.append(ShareEvent(
            period_end=period,
            announce_date=_as_date(row.get("公告日期")),
            total_shares=total,
            float_shares=_cninfo_shares(row, "人民币普通股", "已流通股份"),
        ))
    return events


def map_adata(symbol: str, frame: object) -> list[ShareEvent]:
    """adata 历史股本 → 内部事件。``total_shares`` / ``list_a_shares`` 单位为股。"""
    events: list[ShareEvent] = []
    for row in _frame_records(frame):
        if not _code_matches(row.get("stock_code"), symbol):
            continue
        period = _as_date(row.get("change_date"))
        total = _first_float(row, "total_shares")
        if period is None or total is None:
            continue
        events.append(ShareEvent(
            period_end=period,
            announce_date=None,
            total_shares=total,
            float_shares=_first_float(row, "list_a_shares"),
        ))
    return events


def reconcile(
    cninfo: list[ShareEvent],
    adata: list[ShareEvent],
) -> tuple[list[ShareEvent], list[str], str]:
    """核对两边都有的变动日，并收下巨潮晚于 adata 的更新。

    返回 ``(可写入事件, 单点复核原因, 整只股票的拒写原因)``。数量以巨潮为准，
    公告日只取巨潮。重合日总股本或流通股本相差超过 1% 时，只把该日记成
    ``total_mismatch:YYYY-MM-DD`` 或 ``float_mismatch:YYYY-MM-DD``，不因此拒写
    其他日期，也不改单位。巨潮独有且晚于 adata 最新变动日的行 ``source`` 为
    ``cninfo_only``；两边都有的行为 ``cninfo+adata``。更早的单边变动日不写、
    也不复核。任一来源为空则整只不写。没有任何可写事件、也没有单点复核时，
    原因是 ``no_overlap``。
    """
    if not cninfo or not adata:
        return [], [], "empty_source"
    left = _collapse(cninfo)
    right = _collapse(adata)
    adata_latest = max(right)
    agreed: list[ShareEvent] = []
    point_reasons: list[str] = []
    for day in sorted(set(left) & set(right)):
        primary, other = left[day], right[day]
        if rel_diff(primary.total_shares, other.total_shares) > REL_TOL:
            point_reasons.append(f"total_mismatch:{day.isoformat()}")
            continue
        if (
            primary.float_shares is not None
            and other.float_shares is not None
            and rel_diff(primary.float_shares, other.float_shares) > REL_TOL
        ):
            point_reasons.append(f"float_mismatch:{day.isoformat()}")
            continue
        agreed.append(ShareEvent(
            period_end=day,
            announce_date=primary.announce_date,
            total_shares=primary.total_shares,
            float_shares=primary.float_shares if primary.float_shares is not None else other.float_shares,
            source=SOURCE_TAG,
        ))
    for day in sorted(set(left) - set(right)):
        if day <= adata_latest:
            continue
        primary = left[day]
        agreed.append(ShareEvent(
            period_end=day,
            announce_date=primary.announce_date,
            total_shares=primary.total_shares,
            float_shares=primary.float_shares,
            source=SOURCE_CNINFO_ONLY,
        ))
    if not agreed and not point_reasons:
        return [], [], "no_overlap"
    return agreed, point_reasons, ""


def plan_changes(symbol: str, local_rows: list[LocalRow], agreed: list[ShareEvent]) -> list[Change]:
    """只追加本地没有的变动日，或修正相差超过 1% 的股本 / 不一致的公告日。"""
    local_by = {row.period_end: row for row in local_rows}
    changes: list[Change] = []
    for event in sorted(agreed, key=lambda item: item.period_end):
        old = local_by.get(event.period_end)
        if old is None:
            changes.append(Change(
                symbol=symbol,
                period_end=event.period_end,
                action="append",
                announce_date_old=None,
                announce_date_new=event.announce_date,
                total_old=None,
                total_new=event.total_shares,
                float_old=None,
                float_new=event.float_shares,
                source_old=None,
                source_new=event.source,
            ))
            continue
        total_drift = old.total_shares is None or rel_diff(old.total_shares, event.total_shares) > REL_TOL
        float_drift = (
            event.float_shares is not None
            and (old.float_shares is None or rel_diff(old.float_shares, event.float_shares) > REL_TOL)
        )
        announce_drift = event.announce_date is not None and event.announce_date != old.announce_date
        if not (total_drift or float_drift or announce_drift):
            continue
        total_new = event.total_shares if total_drift or old.total_shares is None else old.total_shares
        changes.append(Change(
            symbol=symbol,
            period_end=event.period_end,
            action="correct",
            announce_date_old=old.announce_date,
            announce_date_new=event.announce_date if announce_drift else old.announce_date,
            total_old=old.total_shares,
            total_new=total_new,
            float_old=old.float_shares,
            float_new=event.float_shares if float_drift else old.float_shares,
            source_old=old.source,
            source_new=event.source,
        ))
    return changes


def _date_expr(column: str, dtype: pl.DataType) -> pl.Expr:
    if dtype == pl.Date:
        return pl.col(column)
    if isinstance(dtype, pl.Datetime):
        return pl.col(column).dt.date()
    return pl.col(column).cast(pl.Utf8, strict=False).str.slice(0, 10).str.to_date(strict=False)


def _format_date(column: str, current: pl.DataType, target: pl.DataType) -> pl.Expr:
    if target in (pl.Utf8, pl.String) and current == pl.Date:
        return pl.col(column).dt.strftime("%Y-%m-%d")
    if target == pl.Date and current in (pl.Utf8, pl.String):
        return pl.col(column).str.slice(0, 10).str.to_date(strict=False)
    if current == target:
        return pl.col(column)
    return pl.col(column).cast(target, strict=False)


def _with_period_key(frame: pl.DataFrame) -> pl.DataFrame:
    dtype = frame.schema.get("period_end", pl.Utf8)
    return frame.with_columns(_date_expr("period_end", dtype).alias("_pe"))


def _changes_frame(changes: list[Change]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [item.symbol for item in changes],
        "period_end": [item.period_end.isoformat() for item in changes],
        "announce_date": [
            item.announce_date_new.isoformat() if item.announce_date_new else None
            for item in changes
        ],
        "total_shares": [item.total_new for item in changes],
        "float_shares": [item.float_new for item in changes],
        "source": [item.source_new for item in changes],
        "action": [item.action for item in changes],
    }, schema={
        "symbol": pl.Utf8,
        "period_end": pl.Utf8,
        "announce_date": pl.Utf8,
        "total_shares": pl.Float64,
        "float_shares": pl.Float64,
        "source": pl.Utf8,
        "action": pl.Utf8,
    })


def _cast_like(frame: pl.DataFrame, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    exprs: list[pl.Expr] = []
    for name, dtype in schema.items():
        if name not in frame.columns:
            exprs.append(pl.lit(None).cast(dtype, strict=False).alias(name))
            continue
        exprs.append(_format_date(name, frame.schema[name], dtype).alias(name))
    return frame.select(exprs)


def apply_changes(existing: pl.DataFrame, changes: list[Change]) -> pl.DataFrame:
    """用变动替换同一 (symbol, period_end)，其余行和其余列保持不动。"""
    if not changes:
        return existing
    updates = _with_period_key(_changes_frame(changes))
    if existing.is_empty():
        appended = updates.filter(pl.col("action") == "append").drop("action")
        return appended.sort(["symbol", "_pe"]).drop("_pe")

    existing_keyed = _with_period_key(existing)
    for name, dtype in (
        ("announce_date", pl.Utf8),
        ("total_shares", pl.Float64),
        ("float_shares", pl.Float64),
        ("source", pl.Utf8),
    ):
        if name not in existing_keyed.columns:
            existing_keyed = existing_keyed.with_columns(pl.lit(None).cast(dtype, strict=False).alias(name))
    schema = {name: dtype for name, dtype in existing_keyed.schema.items() if name != "_pe"}
    keys = updates.select("symbol", "_pe")
    kept = existing_keyed.join(keys, on=["symbol", "_pe"], how="anti")

    correcting = updates.filter(pl.col("action") == "correct")
    old = existing_keyed.join(correcting.select("symbol", "_pe"), on=["symbol", "_pe"], how="inner")
    overlay = old.join(
        correcting.select(
            "symbol", "_pe",
            pl.col("period_end").alias("period_end_new"),
            pl.col("announce_date").alias("announce_date_new"),
            pl.col("total_shares").alias("total_shares_new"),
            pl.col("float_shares").alias("float_shares_new"),
            pl.col("source").alias("source_new"),
        ),
        on=["symbol", "_pe"],
        how="left",
    )
    overlay = overlay.with_columns(
        pl.col("period_end_new").alias("period_end"),
        pl.col("announce_date_new").alias("announce_date"),
        pl.col("total_shares_new").alias("total_shares"),
        pl.col("float_shares_new").alias("float_shares"),
        pl.col("source_new").alias("source"),
    ).drop([
        "period_end_new", "announce_date_new", "total_shares_new",
        "float_shares_new", "source_new",
    ])
    overlay = _cast_like(overlay.drop("_pe"), schema).with_columns(
        _date_expr("period_end", schema["period_end"]).alias("_pe")
    )

    appended = _cast_like(
        updates.filter(pl.col("action") == "append").drop(["action", "_pe"]),
        schema,
    ).with_columns(_date_expr("period_end", schema["period_end"]).alias("_pe"))
    out = pl.concat([kept, overlay, appended], how="diagonal_relaxed")
    return out.sort(["symbol", "_pe"]).drop("_pe")


def _read_shares(data_dir: Path) -> pl.DataFrame:
    path = shares_path(data_dir)
    if not path.exists():
        return pl.DataFrame()
    return pl.read_parquet(path)


def _local_rows(frame: pl.DataFrame) -> dict[str, list[LocalRow]]:
    if frame.is_empty() or "symbol" not in frame.columns or "period_end" not in frame.columns:
        return {}
    keyed = _with_period_key(frame).filter(pl.col("symbol").is_not_null() & pl.col("_pe").is_not_null())
    grouped: dict[str, list[LocalRow]] = {}
    has_total = "total_shares" in keyed.columns
    has_float = "float_shares" in keyed.columns
    has_announce = "announce_date" in keyed.columns
    has_source = "source" in keyed.columns
    columns = ["symbol", "_pe"]
    for name, present in (
        ("total_shares", has_total),
        ("float_shares", has_float),
        ("announce_date", has_announce),
        ("source", has_source),
    ):
        if present:
            columns.append(name)
    for row in keyed.select(columns).iter_rows(named=True):
        grouped.setdefault(str(row["symbol"]), []).append(LocalRow(
            period_end=row["_pe"],
            announce_date=_as_date(row["announce_date"]) if has_announce else None,
            total_shares=_as_float(row["total_shares"]) if has_total else None,
            float_shares=_as_float(row["float_shares"]) if has_float else None,
            source=None if not has_source or row["source"] is None else str(row["source"]),
        ))
    for rows in grouped.values():
        rows.sort(key=lambda item: item.period_end)
    return grouped


def recent_traded_symbols(data_dir: Path, *, as_of: date, within_days: int) -> set[str]:
    """近端日线里成交量大于 0 的股票。

    现网 ``instruments.list_status`` 全部为空，退市状态列判断不到任何股票。
    是否仍在上市实际只看这里：近 ``within_days`` 个自然日没有成交，就不回补。
    """
    root = data_dir / "kline_daily"
    if not root.exists():
        return set()
    start = as_of - timedelta(days=within_days)
    found: set[str] = set()
    for path in sorted(root.glob("date=*/*.parquet")):
        try:
            day = date.fromisoformat(path.parent.name.split("=", 1)[-1][:10])
        except ValueError:
            continue
        if day < start or day > as_of:
            continue
        try:
            schema = pl.read_parquet_schema(path)
            if "symbol" not in schema:
                continue
            columns = ["symbol"] + (["volume"] if "volume" in schema else [])
            frame = pl.read_parquet(path, columns=columns)
        except (OSError, pl.exceptions.PolarsError) as exc:
            logger.debug("读取日线 %s 失败: %s", path, exc)
            continue
        if "volume" in frame.columns:
            frame = frame.filter(pl.col("volume").cast(pl.Float64, strict=False).fill_null(0) > 0)
        found.update(frame["symbol"].drop_nulls().cast(pl.Utf8).to_list())
    return found


def delisted_symbols(data_dir: Path) -> set[str]:
    """``list_status`` 已标成退市的股票。

    现网该列全部为空，这个集合通常是空的，不能靠它识别 600065、603157 这类
    已无成交的股票。没有近端成交的由 ``recent_traded_symbols`` 排除。
    """
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return set()
    try:
        schema = pl.read_parquet_schema(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        logger.warning("读取 instruments 失败: %s", exc)
        return set()
    if "symbol" not in schema or "list_status" not in schema:
        return set()
    frame = pl.read_parquet(path, columns=["symbol", "list_status"])
    status = (
        frame["list_status"].cast(pl.Utf8, strict=False).fill_null("").str.strip_chars().str.to_uppercase()
    )
    return set(frame.filter(status.is_in(list(_DELISTED_STATUS)))["symbol"].cast(pl.Utf8).to_list())


def normalize_symbol(raw: str) -> str:
    text = raw.strip().upper()
    if not text:
        return ""
    if "." in text:
        return text
    if text.startswith(("6", "9")):
        return f"{text}.SH"
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    return f"{text}.SZ"


def select_targets(
    data_dir: Path,
    *,
    as_of: date,
    stale_before: date,
    active_days: int,
    symbols: list[str] | None = None,
    scan_values: bool = False,
) -> tuple[list[str], list[str]]:
    """返回 (待拉外部源的股票, 跳过的退市股)。

    默认只含最后一期早于阈值、且近端仍有成交的股票。现网 ``list_status`` 全空，
    退市实际只靠近 ``active_days``（默认 30）个自然日无成交，所以近端没有成交的
    600065、603157 默认不会入选。``symbols`` 同样要求近端有成交；``list_status``
    已标退市时再额外跳过。``scan_values`` 改为扫描全部近端仍有成交的股票。
    """
    traded = recent_traded_symbols(data_dir, as_of=as_of, within_days=active_days)
    delisted = delisted_symbols(data_dir)
    explicit = {normalize_symbol(item) for item in symbols or [] if normalize_symbol(item)}
    local = _local_rows(_read_shares(data_dir))
    targets: list[str] = []
    skipped: list[str] = []
    pool = explicit or traded
    # 显式代码即使近端日线里没有，也要能看出是退市跳过还是没有成交。
    if explicit:
        pool = explicit
    for symbol in sorted(pool):
        if symbol in delisted:
            skipped.append(symbol)
            continue
        if symbol not in traded:
            continue
        rows = local.get(symbol, [])
        last_period = rows[-1].period_end if rows else None
        if explicit or scan_values or last_period is None or last_period < stale_before:
            targets.append(symbol)
    return targets, skipped


def _latest_fields(events: list[ShareEvent]) -> tuple[str, str]:
    if not events:
        return "", ""
    latest = max(events, key=lambda item: item.period_end)
    return latest.period_end.isoformat(), f"{latest.total_shares:.4f}".rstrip("0").rstrip(".")


def _local_latest_fields(rows: list[LocalRow]) -> tuple[str, str]:
    if not rows:
        return "", ""
    latest = rows[-1]
    total = "" if latest.total_shares is None else f"{latest.total_shares:.4f}".rstrip("0").rstrip(".")
    return latest.period_end.isoformat(), total


def _attention(
    *,
    stale: bool,
    local_rows: list[LocalRow],
    cninfo: list[ShareEvent],
    adata: list[ShareEvent],
) -> bool:
    """全市场扫描时，只有断更或和外部值对不上的股票才进复核。"""
    if stale:
        return True
    local_total = local_rows[-1].total_shares if local_rows else None
    for events in (cninfo, adata):
        if not events:
            continue
        latest = max(events, key=lambda item: item.period_end)
        if local_total is None or rel_diff(local_total, latest.total_shares) > REL_TOL:
            return True
    left, right = _collapse(cninfo), _collapse(adata)
    for row in local_rows:
        if row.total_shares is None:
            continue
        primary, other = left.get(row.period_end), right.get(row.period_end)
        if primary is None or other is None:
            continue
        if rel_diff(primary.total_shares, other.total_shares) > REL_TOL:
            continue
        if rel_diff(row.total_shares, primary.total_shares) > REL_TOL:
            return True
    return False


def _retry(call: Callable[[], list[ShareEvent]], *, retries: int, pace: Pacer) -> list[ShareEvent]:
    last: Exception | None = None
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        pace.wait()
        try:
            return call()
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("股本回补请求失败 %s/%s: %s", attempt, attempts, exc)
    assert last is not None
    raise last


def _review(
    symbol: str,
    reason: str,
    local_rows: list[LocalRow],
    cninfo: list[ShareEvent],
    adata: list[ShareEvent],
) -> ReviewItem:
    cninfo_day, cninfo_total = _latest_fields(cninfo)
    adata_day, adata_total = _latest_fields(adata)
    local_day, local_total = _local_latest_fields(local_rows)
    return ReviewItem(
        symbol=symbol,
        reason=reason,
        cninfo_latest_date=cninfo_day,
        cninfo_total=cninfo_total,
        adata_latest_date=adata_day,
        adata_total=adata_total,
        local_latest_date=local_day,
        local_total=local_total,
    )


def backup_partition(path: Path, now: datetime) -> Path:
    if not path.exists():
        raise FileNotFoundError(path)
    dest_dir = path.parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"part-{_as_cn(now).strftime('%Y%m%dT%H%M%S')}.parquet"
    shutil.copy2(path, dest)
    if not dest.exists() or dest.stat().st_size <= 0:
        raise OSError(f"股本分区备份失败: {dest}")
    return dest


def _write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_reports(
    report_dir: Path,
    changes: list[Change],
    reviews: list[ReviewItem],
    stamp: str,
) -> tuple[Path, Path]:
    diff_path = report_dir / f"diff-{stamp}.csv"
    review_path = report_dir / f"review-{stamp}.csv"
    _write_csv(diff_path, [
        "symbol", "action", "period_end",
        "announce_date_old", "announce_date_new",
        "total_shares_old", "total_shares_new",
        "float_shares_old", "float_shares_new",
        "source_old", "source_new",
    ], [
        [
            item.symbol, item.action, item.period_end.isoformat(),
            _iso(item.announce_date_old), _iso(item.announce_date_new),
            _num(item.total_old), _num(item.total_new),
            _num(item.float_old), _num(item.float_new),
            item.source_old or "", item.source_new,
        ]
        for item in changes
    ])
    _write_csv(review_path, [
        "symbol", "reason",
        "cninfo_latest_date", "cninfo_total",
        "adata_latest_date", "adata_total",
        "local_latest_date", "local_total",
    ], [
        [
            item.symbol, item.reason,
            item.cninfo_latest_date, item.cninfo_total,
            item.adata_latest_date, item.adata_total,
            item.local_latest_date, item.local_total,
        ]
        for item in reviews
    ])
    return diff_path, review_path


def _iso(value: date | None) -> str:
    return "" if value is None else value.isoformat()


def _num(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.4f}".rstrip("0").rstrip(".")


def fetch_cninfo_events(symbol: str) -> list[ShareEvent]:
    import akshare as ak

    code = symbol.split(".", 1)[0]
    frame = ak.stock_share_change_cninfo(symbol=code, start_date="19900101", end_date="20991231")
    return map_cninfo(symbol, frame)


def fetch_adata_events(symbol: str) -> list[ShareEvent]:
    import adata

    code = symbol.split(".", 1)[0]
    frame = adata.stock.info.get_stock_shares(stock_code=code, is_history=True)
    return map_adata(symbol, frame)


def run_backfill(
    data_dir: Path,
    *,
    apply: bool = False,
    stale_before: date = DEFAULT_STALE_BEFORE,
    active_days: int = DEFAULT_ACTIVE_DAYS,
    symbols: list[str] | None = None,
    scan_values: bool = False,
    spacing_s: float = DEFAULT_SPACING_S,
    retries: int = DEFAULT_RETRIES,
    as_of: date | None = None,
    report_dir: Path | None = None,
    fetch_cninfo: Fetcher | None = None,
    fetch_adata: Fetcher | None = None,
    sleeper: Sleeper | None = None,
    monotonic: Clock | None = None,
    now: Now | None = None,
) -> BackfillResult:
    """识别断更或滞后、且近端仍有成交的股票。默认不写分区。

    现网 ``list_status`` 全空，仍在上市只表示近端日线有成交。重合变动日一致的
    才写入；巨潮晚于 adata 最新日的独有更新以 ``cninfo_only`` 写入。单个旧变动日
    对不上只进复核。
    """
    clock_now = now or cn_now
    as_of = as_of or _as_cn(clock_now()).date()
    targets, skipped = select_targets(
        data_dir,
        as_of=as_of,
        stale_before=stale_before,
        active_days=active_days,
        symbols=symbols,
        scan_values=scan_values,
    )
    result = BackfillResult(candidates=targets, skipped_delisted=skipped)
    cninfo_fetch = fetch_cninfo or fetch_cninfo_events
    adata_fetch = fetch_adata or fetch_adata_events
    pace = Pacer(spacing_s, sleeper or time.sleep, monotonic or time.monotonic, clock_now)
    existing = _read_shares(data_dir)
    local = _local_rows(existing)
    for symbol in targets:
        rows = local.get(symbol, [])
        last_period = rows[-1].period_end if rows else None
        stale = last_period is None or last_period < stale_before
        cninfo: list[ShareEvent] = []
        adata_rows: list[ShareEvent] = []
        try:
            cninfo = _retry(partial(cninfo_fetch, symbol), retries=retries, pace=pace)
            adata_rows = _retry(partial(adata_fetch, symbol), retries=retries, pace=pace)
        except Exception as exc:  # noqa: BLE001
            reason = f"fetch_failed:{exc}"
            result.errors.append(f"{symbol}: {reason}")
            result.reviews.append(_review(symbol, reason[:200], rows, cninfo, adata_rows))
            logger.warning("股本回补 %s 拉取失败: %s", symbol, exc)
            continue
        agreed, point_reasons, reason = reconcile(cninfo, adata_rows)
        notable = (not scan_values) or _attention(
            stale=stale, local_rows=rows, cninfo=cninfo, adata=adata_rows,
        )
        if reason:
            if not notable:
                logger.info("股本回补 %s 两源无法核对但本地未滞后，跳过复核", symbol)
                continue
            result.reviews.append(_review(symbol, reason, rows, cninfo, adata_rows))
            logger.info("股本回补 %s 两源无法核对(%s)，不写入", symbol, reason)
            continue
        if point_reasons and notable:
            for point_reason in point_reasons:
                result.reviews.append(_review(symbol, point_reason, rows, cninfo, adata_rows))
        changes = plan_changes(symbol, rows, agreed)
        if not changes:
            if point_reasons:
                logger.info(
                    "股本回补 %s 部分变动日对不上(%s)，其余无需写入",
                    symbol,
                    ",".join(point_reasons),
                )
            else:
                logger.info("股本回补 %s 与两源一致，无需写入", symbol)
            continue
        result.changes.extend(changes)
        logger.info(
            "股本回补 %s 追加 %d 修正 %d",
            symbol,
            sum(item.action == "append" for item in changes),
            sum(item.action == "correct" for item in changes),
        )

    stamp = _as_cn(clock_now()).strftime("%Y%m%dT%H%M%S")
    reports = report_dir or (data_dir / "financials" / "shares" / "reports")
    result.diff_report, result.review_report = write_reports(
        reports, result.changes, result.reviews, stamp,
    )
    if not apply or not result.changes:
        return result

    path = shares_path(data_dir)
    merged = apply_changes(existing, result.changes)
    if path.exists():
        # 备份失败就停，避免分区被改掉后没有原件。
        result.backup = backup_partition(path, clock_now())
    path.parent.mkdir(parents=True, exist_ok=True)
    from app.services.fs_utils import atomic_write_parquet

    atomic_write_parquet(merged, path)
    result.wrote = True
    logger.info("股本回补已写入 %s 行差异，备份 %s", len(result.changes), result.backup)
    return result


def warn_lagging_listed_shares(
    data_dir: Path,
    *,
    as_of: date | None = None,
    stale_before: date = DEFAULT_STALE_BEFORE,
    active_days: int = DEFAULT_ACTIVE_DAYS,
) -> list[str]:
    """近端仍有成交、且最后一期早于阈值时打 WARNING。失败只记日志，返回空列表。

    现网 ``list_status`` 全空，这里的「在市」就是近 ``active_days`` 日成交量大于 0。
    """
    try:
        day = as_of or cn_now().date()
        targets, _skipped = select_targets(
            data_dir,
            as_of=day,
            stale_before=stale_before,
            active_days=active_days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("股本滞后检查失败(不阻断): %s", exc)
        return []
    if targets:
        logger.warning(
            "股本滞后: %d 只近端仍有成交的股票最后一期早于 %s，近 %d 日日线有成交（样例: %s）。"
            "现网 list_status 全空，退市实际只靠这段窗口内无成交。"
            "回测市值按当时股本计算，断更会偏低。只告警，不阻断盘后管道。",
            len(targets),
            stale_before.isoformat(),
            active_days,
            ",".join(targets[:10]),
        )
    return targets


def _data_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw)
    from app.config import settings

    return Path(settings.data_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回补仍在上市但股本序列断更或滞后的股票")
    parser.add_argument("--data-dir", default=None, help="数据目录，容器内为 /app/data")
    parser.add_argument("--apply", action="store_true", help="写入股本分区。默认只输出差异")
    parser.add_argument("--dry-run", action="store_true", help="只输出差异，不写分区（默认）")
    parser.add_argument("--stale-before", default=DEFAULT_STALE_BEFORE.isoformat(), help="最后一期早于该日视为断更")
    parser.add_argument(
        "--active-days",
        type=int,
        default=DEFAULT_ACTIVE_DAYS,
        help="近端多少个自然日内有成交算仍在上市。现网 list_status 全空，退市实际只靠这段窗口内无成交",
    )
    parser.add_argument("--symbols", default="", help="只处理这些代码，逗号分隔。近端无成交的会跳过；list_status 已标退市的也跳过")
    parser.add_argument(
        "--scan-values",
        action="store_true",
        help="扫描全部仍有成交的股票，最新股本与外部源相差超过百分之一或历史值不一致时才写入",
    )
    parser.add_argument("--spacing", type=float, default=DEFAULT_SPACING_S, help="外部请求最小间隔（秒）")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="单次请求失败后的尝试次数")
    parser.add_argument("--report-dir", default=None, help="差异和复核 CSV 目录")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.apply and args.dry_run:
        print("--apply 和 --dry-run 不能同时使用")
        return 2
    if args.active_days < 0 or args.spacing < 0 or args.retries < 1:
        print("--active-days、--spacing 不能为负，--retries 至少为 1")
        return 2
    try:
        stale_before = date.fromisoformat(args.stale_before)
    except ValueError:
        print("--stale-before 需要 YYYY-MM-DD")
        return 2
    data_dir = _data_dir(args.data_dir)
    symbols = [item for item in args.symbols.split(",") if item.strip()]
    print(
        f"{'写入' if args.apply else 'dry-run'} 股本回补 -> {data_dir} "
        f"断更早于 {stale_before.isoformat()} 近端 {args.active_days} 日"
        f"{' 全市场最新值' if args.scan_values else ''}",
        flush=True,
    )
    result = run_backfill(
        data_dir,
        apply=args.apply,
        stale_before=stale_before,
        active_days=args.active_days,
        symbols=symbols or None,
        scan_values=args.scan_values,
        spacing_s=args.spacing,
        retries=args.retries,
        report_dir=Path(args.report_dir) if args.report_dir else None,
    )
    print(
        f"候选 {len(result.candidates)} 退市跳过 {len(result.skipped_delisted)} "
        f"差异 {len(result.changes)} 复核 {len(result.reviews)} 失败 {len(result.errors)}",
        flush=True,
    )
    if result.diff_report:
        print(f"差异报告 {result.diff_report}", flush=True)
    if result.review_report:
        print(f"复核清单 {result.review_report}", flush=True)
    if result.backup:
        print(f"备份 {result.backup}", flush=True)
    if result.wrote:
        print(f"已写入 {shares_path(data_dir)}", flush=True)
    elif args.apply:
        print("没有需要写入的差异", flush=True)
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
