"""A 股 ST / *ST 区间同步。

回测不能用 instruments 里「今天的名称」判断历史某一天是否为风险警示:
那张表每个标的只有一行, as_of 是同步当天。本模块把 baostock 日线
``isST`` 收成半开区间 ``[start_date, end_date)``, 写入

    data/st_history/intervals.parquet

字段: symbol, start_date, end_date, st_type, source。
``end_date`` 为空表示区间仍未结束 (矩阵条件为 start <= d < end, 空 end 视为无穷)。

主源是 baostock (免费、无 token)。``--spot-check N`` 再用 akshare
``stock_zh_a_disclosure_report_cninfo`` 按「风险警示」抽查, 只记差异, 不改区间。

首次回补 (服务器, 数据目录已挂到 /app/data)::

    docker compose exec app uv run --no-sync python -m app.services.st_history_sync --data-dir /app/data

开发机::

    cd backend && uv run python -m app.services.st_history_sync --data-dir ../data
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path

import numpy as np
import polars as pl

from app.price_limits import parse_listing_date, polars_is_risk_warning_name

logger = logging.getLogger(__name__)

ST_HISTORY_COLUMNS = ("symbol", "start_date", "end_date", "st_type", "source")
ST_HISTORY_SCHEMA = {
    "symbol": pl.Utf8,
    "start_date": pl.Date,
    "end_date": pl.Date,
    "st_type": pl.Utf8,
    "source": pl.Utf8,
}
_SOURCE_BAOSTOCK = "baostock"
_ST_TYPE = "ST"
_DEFAULT_START = date(1990, 12, 19)
_FLUSH_EVERY = 100

FlagRow = tuple[date, bool]
FetchFlags = Callable[[str, date, date], list[FlagRow]]
FetchAnnouncements = Callable[[str, date, date], list[tuple[date, str]]]

_MISSING_WARNED: set[str] = set()


def st_history_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "st_history"


def st_history_path(data_dir: Path) -> Path:
    return st_history_dir(data_dir) / "intervals.parquet"


def st_history_fingerprint(data_dir: Path) -> str:
    """文件身份。缺失与「空表」分开, 回补完成后缓存和 enriched 会失效。"""
    path = st_history_path(data_dir)
    if not path.exists():
        return "missing"
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def empty_st_history() -> pl.DataFrame:
    return pl.DataFrame(schema=ST_HISTORY_SCHEMA)


def load_st_history(data_dir: Path | None, *, warn: bool = True) -> pl.DataFrame | None:
    """读取区间表。文件不存在返回 None (调用方回退当前名称)。"""
    if data_dir is None:
        return None
    path = st_history_path(data_dir)
    if not path.exists():
        if warn:
            _warn_missing(path)
        return None
    frame = pl.read_parquet(path)
    missing = [name for name in ST_HISTORY_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError(f"st_history 缺少字段 {missing}: {path}")
    return frame.select(list(ST_HISTORY_COLUMNS))


def _warn_missing(path: Path) -> None:
    key = str(path)
    if key in _MISSING_WARNED:
        return
    _MISSING_WARNED.add(key)
    logger.warning(
        "未找到 ST 区间 %s, 回测过滤与涨跌停回退到当前证券名称",
        path,
    )


def reset_missing_warning() -> None:
    """测试用: 清空「文件缺失」警告去重。"""
    _MISSING_WARNED.clear()


def canonical_symbol(symbol: str) -> str:
    """``sh.600000`` / ``600000`` / ``600000.SH`` → ``600000.SH``。"""
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    if text.startswith(("SH.", "SZ.", "BJ.")):
        market, code = text.split(".", 1)
        return f"{code}.{market}"
    if text.endswith((".SH", ".SZ", ".BJ")):
        return text
    if text.startswith(("6", "9")):
        return f"{text}.SH"
    if len(text) == 6 and text.startswith(("4", "8")):
        return f"{text}.BJ"
    if text.startswith(("0", "3")):
        return f"{text}.SZ"
    return text


def to_baostock_code(symbol: str) -> str | None:
    canon = canonical_symbol(symbol)
    if not canon.endswith((".SH", ".SZ", ".BJ")):
        return None
    code, market = canon.split(".")
    if not code.isdigit():
        return None
    return f"{market.lower()}.{code}"


def bare_symbol(symbol: str) -> str:
    canon = canonical_symbol(symbol)
    return canon.split(".", 1)[0]


def intervals_from_flags(
    symbol: str,
    rows: Sequence[FlagRow],
    *,
    st_type: str = _ST_TYPE,
    source: str = _SOURCE_BAOSTOCK,
) -> list[dict]:
    """把逐日 isST 收成 ``[start, end)``。

    只有明确的 isST=0 结束区间; 序列里的停牌缺口不切断。
    序列结束时仍为 ST, ``end_date`` 为空。
    """
    ordered = sorted((day, bool(flag)) for day, flag in rows)
    intervals: list[dict] = []
    start: date | None = None
    active = False
    for day, flag in ordered:
        if flag and not active:
            start = day
            active = True
        elif not flag and active and start is not None:
            intervals.append({
                "symbol": canonical_symbol(symbol),
                "start_date": start,
                "end_date": day,
                "st_type": st_type,
                "source": source,
            })
            start = None
            active = False
    if active and start is not None:
        intervals.append({
            "symbol": canonical_symbol(symbol),
            "start_date": start,
            "end_date": None,
            "st_type": st_type,
            "source": source,
        })
    return intervals


def intervals_frame(rows: Sequence[dict]) -> pl.DataFrame:
    if not rows:
        return empty_st_history()
    return pl.DataFrame(list(rows), schema=ST_HISTORY_SCHEMA).sort(["symbol", "start_date"])


def build_st_matrix(
    trading_dates: Sequence[date],
    symbols: Sequence[str],
    intervals: pl.DataFrame,
) -> np.ndarray:
    """``(交易日, 标的)`` 布尔矩阵。``start <= d < end``; 空 end 一直覆盖到序列末尾。"""
    out = np.zeros((len(trading_dates), len(symbols)), dtype=bool)
    if intervals.is_empty() or not trading_dates or not symbols:
        return out
    index: dict[str, int] = {}
    for asset_id, symbol in enumerate(symbols):
        text = str(symbol).strip().upper()
        index.setdefault(text, asset_id)
        index.setdefault(bare_symbol(text), asset_id)
        index.setdefault(canonical_symbol(text), asset_id)
    date_axis = np.array([np.datetime64(day, "D") for day in trading_dates])
    frame = intervals
    if frame["start_date"].dtype != pl.Date:
        frame = frame.with_columns(pl.col("start_date").cast(pl.Date, strict=False))
    if frame["end_date"].dtype != pl.Date:
        frame = frame.with_columns(pl.col("end_date").cast(pl.Date, strict=False))
    for row in frame.select("symbol", "start_date", "end_date").iter_rows(named=True):
        asset_id = index.get(canonical_symbol(str(row["symbol"])))
        if asset_id is None:
            asset_id = index.get(bare_symbol(str(row["symbol"])))
        if asset_id is None:
            continue
        start = row["start_date"]
        if start is None:
            continue
        left = int(np.searchsorted(date_axis, np.datetime64(start, "D"), side="left"))
        end = row["end_date"]
        right = (
            len(trading_dates)
            if end is None
            else int(np.searchsorted(date_axis, np.datetime64(end, "D"), side="left"))
        )
        if left < right:
            out[left:right, asset_id] = True
    return out


def listing_dates_for_symbols(
    symbols: Sequence[str],
    instruments: pl.DataFrame | None,
) -> tuple[date | None, ...] | None:
    """instruments.listing_date (stock_basic.list_date 落库名)。没有该列则返回 None。"""
    if instruments is None or instruments.is_empty() or "symbol" not in instruments.columns:
        return None
    column = next(
        (name for name in ("listing_date", "list_date") if name in instruments.columns),
        None,
    )
    if column is None:
        return None
    by_symbol: dict[str, date | None] = {}
    for row in instruments.select(["symbol", column]).iter_rows(named=True):
        parsed = parse_listing_date(row[column])
        for key in (
            str(row["symbol"]).strip().upper(),
            canonical_symbol(str(row["symbol"])),
            bare_symbol(str(row["symbol"])),
        ):
            if key and key not in by_symbol:
                by_symbol[key] = parsed
    found = tuple(
        by_symbol.get(str(symbol).strip().upper())
        or by_symbol.get(canonical_symbol(str(symbol)))
        or by_symbol.get(bare_symbol(str(symbol)))
        for symbol in symbols
    )
    if all(value is None for value in found):
        return None
    return found


def trading_day_counts(
    axis: Sequence[date],
    listing: Sequence[date | None],
    calendar: Sequence[date],
) -> np.ndarray:
    """每个轴上日期自上市日起的交易日序号 (上市当日 = 1)。上市前为 0。

    ``calendar`` 应覆盖本地已有交易日。上市日早于日历第一天时, 用
    ``busday_count`` 补上这段工作日作为下界 (不含 A 股节假日, 会略多算几天)。
    """
    if len(axis) == 0:
        return np.zeros((0, len(listing)), dtype=np.int32)
    cal_dates = sorted({day for day in calendar} or set(axis))
    cal = np.array([np.datetime64(day, "D") for day in cal_dates])
    axis_np = np.array([np.datetime64(day, "D") for day in axis])
    listed = np.array(
        [
            np.datetime64(day, "D") if day is not None else np.datetime64("NaT")
            for day in listing
        ]
    )
    starts = np.searchsorted(cal, listed, side="left")
    axis_right = np.searchsorted(cal, axis_np, side="right")
    counts = (axis_right[:, None] - starts[None, :]).astype(np.int32, copy=False)
    if len(cal):
        before = (~np.isnat(listed)) & (listed < cal[0])
        if before.any():
            prior = np.busday_count(listed[before], np.datetime64(cal_dates[0], "D"))
            counts[:, before] += prior.astype(np.int32, copy=False)
    before_listing = (~np.isnat(listed)) & (axis_np[:, None] < listed[None, :])
    counts[before_listing] = 0
    return counts


def attach_is_st_column(
    df: pl.DataFrame,
    instruments: pl.DataFrame,
    st_history: pl.DataFrame | None,
) -> pl.DataFrame:
    """给行情帧加上 ``_is_st``。

    ``st_history is None``: 用当前名称 (含 ST)。有区间表时按
    ``start <= date < end`` 覆盖, 不再看当前名称。
    """
    if df.is_empty():
        return df
    if "_is_st" in df.columns:
        df = df.drop("_is_st")
    if st_history is None:
        return _attach_name_st(df, instruments)
    if "date" not in df.columns or "symbol" not in df.columns:
        return _attach_name_st(df, instruments)
    history = _normalize_history(st_history)
    if history.is_empty():
        return df.with_columns(pl.lit(False).alias("_is_st"))
    dated = df.with_columns(pl.col("date").cast(pl.Date, strict=False).alias("_st_date"))
    keys = dated.select(
        pl.col("symbol"),
        pl.col("_st_date"),
        _bare_expr("symbol").alias("_bare"),
    ).unique()
    covered = (
        keys.join(history, on="_bare", how="inner")
        .filter(
            pl.col("start_date").is_not_null()
            & (pl.col("_st_date") >= pl.col("start_date"))
            & (pl.col("end_date").is_null() | (pl.col("_st_date") < pl.col("end_date")))
        )
        .select(["symbol", "_st_date"])
        .unique()
        .with_columns(pl.lit(True).alias("_is_st"))
    )
    return (
        dated.join(covered, on=["symbol", "_st_date"], how="left")
        .with_columns(pl.col("_is_st").fill_null(False))
        .drop("_st_date")
    )


def _bare_expr(column: str) -> pl.Expr:
    return (
        pl.col(column)
        .cast(pl.Utf8)
        .fill_null("")
        .str.to_uppercase()
        .str.replace(r"^(SH|SZ|BJ)\.", "")
        .str.replace(r"\.(SH|SZ|BJ)$", "")
    )


def _attach_name_st(df: pl.DataFrame, instruments: pl.DataFrame) -> pl.DataFrame:
    if instruments.is_empty() or "symbol" not in instruments.columns or "name" not in instruments.columns:
        return df.with_columns(pl.lit(False).alias("_is_st"))
    flags = (
        instruments.select(
            "symbol",
            polars_is_risk_warning_name(pl.col("name")).alias("_is_st"),
        )
        .unique(subset=["symbol"])
    )
    return df.join(flags, on="symbol", how="left").with_columns(
        pl.col("_is_st").fill_null(False)
    )


def _normalize_history(st_history: pl.DataFrame) -> pl.DataFrame:
    frame = st_history
    if "start_date" in frame.columns and frame["start_date"].dtype != pl.Date:
        frame = frame.with_columns(pl.col("start_date").cast(pl.Date, strict=False))
    if "end_date" in frame.columns and frame["end_date"].dtype != pl.Date:
        frame = frame.with_columns(pl.col("end_date").cast(pl.Date, strict=False))
    return frame.with_columns(_bare_expr("symbol").alias("_bare")).select(
        ["_bare", "start_date", "end_date"]
    )


def _parse_flag_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _fetch_baostock_flags(code: str, start: date, end: date) -> list[FlagRow]:
    import baostock as bs

    rs = bs.query_history_k_data_plus(
        code,
        "date,isST",
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        frequency="d",
        adjustflag="3",
    )
    if rs.error_code != "0":
        raise RuntimeError(f"baostock {code}: {rs.error_msg}")
    rows: list[FlagRow] = []
    while rs.next():
        day_text, flag = rs.get_row_data()
        day = _parse_flag_date(day_text)
        if day is None:
            continue
        rows.append((day, str(flag).strip() == "1"))
    return rows


def _login_baostock():
    import baostock as bs

    result = bs.login()
    if result.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {result.error_msg}")
    return bs


def _instrument_rows(data_dir: Path) -> pl.DataFrame:
    root = Path(data_dir) / "instruments"
    frames: list[pl.DataFrame] = []
    if root.exists():
        for path in sorted(root.rglob("*.parquet")):
            frames.append(pl.read_parquet(path))
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def _symbols_from_instruments(
    data_dir: Path,
    explicit: Sequence[str] | None,
) -> list[tuple[str, date]]:
    """返回 (canonical_symbol, 拉取起点)。起点优先用上市日。"""
    if explicit:
        return [(canonical_symbol(symbol), _DEFAULT_START) for symbol in explicit if canonical_symbol(symbol)]
    frame = _instrument_rows(data_dir)
    if frame.is_empty() or "symbol" not in frame.columns:
        return []
    column = next(
        (name for name in ("listing_date", "list_date") if name in frame.columns),
        None,
    )
    seen: set[str] = set()
    symbols: list[tuple[str, date]] = []
    for row in frame.iter_rows(named=True):
        symbol = canonical_symbol(str(row.get("symbol") or ""))
        if not symbol or symbol in seen or to_baostock_code(symbol) is None:
            continue
        # 指数代码 (000001.SH 等) 不在股票 ST 范围内: 6 位且以 6/0/3/4/8 开头的股票。
        code = symbol.split(".", 1)[0]
        if symbol.endswith(".SH") and not code.startswith(("6",)):
            continue
        seen.add(symbol)
        listed = parse_listing_date(row.get(column)) if column else None
        symbols.append((symbol, listed or _DEFAULT_START))
    return symbols


def _read_progress(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(item) for item in payload.get("completed", [])}


def _write_progress(path: Path, completed: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"completed": sorted(completed)}, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_intervals(data_dir: Path, rows: list[dict]) -> None:
    from app.services.fs_utils import atomic_write_parquet

    path = st_history_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(intervals_frame(rows), path)


def sync_st_history(
    data_dir: Path,
    *,
    symbols: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    fresh: bool = False,
    spot_check: int = 0,
    fetch_flags: FetchFlags | None = None,
    fetch_announcements: FetchAnnouncements | None = None,
    sleep_s: float = 0.0,
) -> int:
    """拉取 isST 并写区间表。返回区间行数。

    ``fetch_flags`` 仅测试注入。默认登录一次 baostock, 逐只拉取。
    中断后重跑会跳过 ``progress.json`` 里已完成的代码。
    """
    data_dir = Path(data_dir)
    end_day = end or date.today()
    targets = _symbols_from_instruments(data_dir, symbols)
    if not targets:
        raise ValueError(f"没有可同步的股票代码, 请先同步标的维表: {data_dir / 'instruments'}")

    progress_path = st_history_dir(data_dir) / "progress.json"
    completed = set() if fresh else _read_progress(progress_path)
    existing: list[dict] = []
    if not fresh and st_history_path(data_dir).exists():
        loaded = load_st_history(data_dir, warn=False)
        if loaded is not None and not loaded.is_empty():
            existing = loaded.to_dicts()
    keep_symbols = {canonical_symbol(symbol) for symbol, _listed in targets}
    pending = [(symbol, listed) for symbol, listed in targets if symbol not in completed]
    pending_symbols = {symbol for symbol, _listed in pending}
    # 即将重拉的标的丢掉旧区间; 已完成的和其他标的保留。
    rows = [
        row for row in existing
        if canonical_symbol(str(row["symbol"])) not in pending_symbols
    ]

    own_session = fetch_flags is None
    bs = _login_baostock() if own_session else None
    fetcher = fetch_flags or _fetch_baostock_flags
    failures: list[str] = []
    try:
        for index, (symbol, listed) in enumerate(pending, start=1):
            code = to_baostock_code(symbol)
            if code is None:
                continue
            fetch_start = start or listed
            if fetch_start < _DEFAULT_START:
                fetch_start = _DEFAULT_START
            try:
                flags = fetcher(code, fetch_start, end_day)
            except Exception as exc:  # noqa: BLE001 — 单只失败不让整批回补报废
                logger.warning("ST 区间拉取失败 %s: %s", symbol, exc)
                failures.append(symbol)
                continue
            rows.extend(intervals_from_flags(symbol, flags))
            completed.add(symbol)
            if index % _FLUSH_EVERY == 0 or index == len(pending):
                _write_intervals(data_dir, rows)
                _write_progress(progress_path, completed)
                logger.info(
                    "ST 区间进度 %d/%d, 区间 %d 行, 失败 %d",
                    index,
                    len(pending),
                    len(rows),
                    len(failures),
                )
            if sleep_s > 0:
                time.sleep(sleep_s)
        if not pending:
            _write_intervals(data_dir, rows)
    finally:
        if bs is not None:
            bs.logout()

    if spot_check > 0:
        issues = spot_check_cninfo(
            intervals_frame(rows),
            limit=spot_check,
            end=end_day,
            fetch_announcements=fetch_announcements,
        )
        for issue in issues:
            logger.warning("ST 抽查: %s", issue)
        if not issues:
            logger.info("ST 抽查 %d 只, 未发现与巨潮风险警示公告明显不一致的区间", spot_check)

    if failures:
        logger.warning("ST 区间有 %d 只拉取失败, 重跑会跳过已完成代码", len(failures))
    # 全部目标都尝试过之后清掉进度, 下次默认为增量重拉; 有失败则保留进度以便续跑。
    if not failures and progress_path.exists() and keep_symbols <= completed:
        progress_path.unlink()
    logger.info("ST 区间写入 %s, %d 行", st_history_path(data_dir), len(rows))
    return len(rows)


def spot_check_cninfo(
    intervals: pl.DataFrame,
    *,
    limit: int,
    end: date | None = None,
    fetch_announcements: FetchAnnouncements | None = None,
) -> list[str]:
    """抽查区间边界是否靠近巨潮「风险警示」公告。返回差异说明, 不修改区间。"""
    if limit <= 0 or intervals.is_empty():
        return []
    end_day = end or date.today()
    symbols = intervals["symbol"].unique().sort().to_list()[:limit]
    fetcher = fetch_announcements or _fetch_cninfo_announcements
    issues: list[str] = []
    for symbol in symbols:
        own = intervals.filter(pl.col("symbol") == symbol)
        starts = [day for day in own["start_date"].to_list() if day is not None]
        if not starts:
            continue
        window_start = min(starts)
        try:
            announcements = fetcher(str(symbol), window_start, end_day)
        except Exception as exc:  # noqa: BLE001 — 抽查失败只记录
            issues.append(f"{symbol} 公告查询失败: {exc}")
            continue
        if not announcements:
            issues.append(f"{symbol} 在 {window_start} 之后没有检索到风险警示公告")
            continue
        for ann_day, title in announcements:
            if _near_interval_boundary(own, ann_day):
                continue
            issues.append(f"{symbol} 公告 {ann_day} {title} 不靠近任何 ST 区间边界")
    return issues


def _near_interval_boundary(own: pl.DataFrame, ann_day: date, *, slack_days: int = 15) -> bool:
    for row in own.iter_rows(named=True):
        start = row["start_date"]
        end = row["end_date"]
        if start is not None and abs((ann_day - start).days) <= slack_days:
            return True
        if end is not None and abs((ann_day - end).days) <= slack_days:
            return True
        if start is not None and ann_day >= start and (end is None or ann_day < end):
            return True
    return False


def _fetch_cninfo_announcements(symbol: str, start: date, end: date) -> list[tuple[date, str]]:
    import akshare as ak

    code = bare_symbol(symbol)
    frame = ak.stock_zh_a_disclosure_report_cninfo(
        symbol=code,
        market="沪深京",
        keyword="风险警示",
        category="",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
    )
    if frame is None or len(frame) == 0:
        return []
    columns = list(frame.columns)
    title_col = next((name for name in columns if "标题" in str(name)), None)
    time_col = next((name for name in columns if "时间" in str(name) or "日期" in str(name)), None)
    if title_col is None or time_col is None:
        return []
    found: list[tuple[date, str]] = []
    for record in frame.to_dict("records"):
        day = _parse_flag_date(record.get(time_col))
        title = str(record.get(title_col) or "").strip()
        if day is not None and title:
            found.append((day, title))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回补 A 股 ST 区间到 data/st_history/intervals.parquet")
    parser.add_argument("--data-dir", default="", help="数据目录, 默认 settings.data_dir / DATA_DIR")
    parser.add_argument("--symbols", default="", help="逗号分隔代码, 默认维表全部 A 股")
    parser.add_argument("--start", default="", help="拉取起点 YYYY-MM-DD, 默认各股上市日或 1990-12-19")
    parser.add_argument("--end", default="", help="拉取终点 YYYY-MM-DD, 默认今天")
    parser.add_argument("--fresh", action="store_true", help="忽略已完成进度, 全量重拉")
    parser.add_argument(
        "--spot-check",
        type=int,
        default=0,
        help="抽查 N 只与巨潮风险警示公告对照, 0 表示不抽查",
    )
    parser.add_argument("--sleep", type=float, default=0.0, help="每只股票之间的间隔秒数")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        from app.config import settings

        data_dir = Path(settings.data_dir)
    explicit = [item.strip() for item in args.symbols.split(",") if item.strip()] or None
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    written = sync_st_history(
        data_dir,
        symbols=explicit,
        start=start,
        end=end,
        fresh=args.fresh,
        spot_check=args.spot_check,
        sleep_s=args.sleep,
    )
    print(f"wrote {written} intervals -> {st_history_path(data_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
