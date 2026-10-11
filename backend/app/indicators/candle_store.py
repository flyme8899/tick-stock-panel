"""日 K 蜡烛形态的增量旁路。

全市场只重算最近 ``KEEP_DAYS + WARMUP`` 个交易日的 OHLC，写出最近
``KEEP_DAYS`` 天。更早的分区删掉。选股「近 N 日」只扫这些分区，
不在每次打开选股页时重算多年历史。

戳文件是 ``引擎|目录版本|最新 enriched 日期``。日期没变时
``ensure_candle_patterns`` 直接返回；盘后管道改过价格后走
``refresh_candle_patterns``，不看戳。
"""
from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path

import polars as pl

from app.indicators.candles import (
    active_engine,
    available_patterns,
    compute_candle_patterns,
    get_pattern,
    pattern_available,
    side_value,
)
from app.services.fs_utils import atomic_write_parquet, atomic_write_text

logger = logging.getLogger(__name__)

STORE_NAME = "kline_candle_patterns"
WARMUP = 20
KEEP_DAYS = 40
MAX_WITHIN = 30
CATALOG_VERSION = "1"
_OHLC = ["symbol", "date", "open", "high", "low", "close"]
_LOCK = threading.RLock()


def _store_dir(data_dir: Path) -> Path:
    return Path(data_dir) / STORE_NAME


def _stamp_path(data_dir: Path) -> Path:
    return _store_dir(data_dir) / ".stamp"


def _enriched_dates(data_dir: Path) -> list[str]:
    root = Path(data_dir) / "kline_daily_enriched"
    if not root.is_dir():
        return []
    dates = []
    for child in root.glob("date=*"):
        if child.is_dir() and (child / "part.parquet").is_file():
            dates.append(child.name[5:])
    return sorted(dates)


def _pattern_dates(data_dir: Path) -> list[str]:
    root = _store_dir(data_dir)
    if not root.is_dir():
        return []
    dates = []
    for child in root.glob("date=*"):
        if child.is_dir() and (child / "part.parquet").is_file():
            dates.append(child.name[5:])
    return sorted(dates)


def _stamp_text(engine: str, latest: str) -> str:
    return f"{engine}|{CATALOG_VERSION}|{latest}"


def refresh_candle_patterns(data_dir: Path, *, engine: str | None = None) -> dict:
    """重写最近一个窗口。没有 enriched 日 K 时写 0 天。"""
    with _LOCK:
        dates = _enriched_dates(data_dir)
        eng = engine or active_engine()
        if not dates:
            return {"written_days": 0, "engine": eng, "latest": None, "skipped": False}
        window = dates[-(KEEP_DAYS + WARMUP):]
        frames = []
        for day in window:
            path = Path(data_dir) / "kline_daily_enriched" / f"date={day}" / "part.parquet"
            frame = pl.read_parquet(path, columns=_OHLC)
            frames.append(frame)
        raw = pl.concat(frames, how="vertical_relaxed")
        computed = compute_candle_patterns(raw, engine=eng)
        keep = dates[-KEEP_DAYS:]
        columns = ["symbol", "date"]
        for pat in available_patterns(eng):
            if pat.column in computed.columns:
                columns.append(pat.column)
        day_text = pl.col("date").cast(pl.Utf8).str.slice(0, 10)
        out = (
            computed.with_columns(day_text.alias("_day"))
            .filter(pl.col("_day").is_in(keep))
            .select([*columns, "_day"])
        )
        store = _store_dir(data_dir)
        store.mkdir(parents=True, exist_ok=True)
        written = 0
        for keys, part in out.group_by("_day"):
            day = keys[0] if isinstance(keys, tuple) else keys
            dest = store / f"date={day}" / "part.parquet"
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_parquet(part.drop("_day"), dest)
            written += 1
        for child in store.glob("date=*"):
            if child.name[5:] not in set(keep):
                shutil.rmtree(child, ignore_errors=True)
        atomic_write_text(_stamp_path(data_dir), _stamp_text(eng, dates[-1]))
        logger.info("candle patterns refreshed: %d days, engine=%s", written, eng)
        return {"written_days": written, "engine": eng, "latest": dates[-1], "skipped": False}


def ensure_candle_patterns(data_dir: Path) -> dict:
    """戳和最新 enriched 日期一致时不重算。"""
    with _LOCK:
        dates = _enriched_dates(data_dir)
        eng = active_engine()
        if not dates:
            return {"written_days": 0, "engine": eng, "latest": None, "skipped": True}
        expected = _stamp_text(eng, dates[-1])
        stamp = _stamp_path(data_dir)
        if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == expected:
            return {"written_days": 0, "engine": eng, "latest": dates[-1], "skipped": True}
        return refresh_candle_patterns(data_dir, engine=eng)


def match_symbols(data_dir: Path, pattern_id: str, side: str, within: int) -> dict:
    """最近 ``within`` 个已落盘交易日里，形态方向命中的股票代码。"""
    pat = get_pattern(pattern_id)
    if pat is None:
        raise ValueError(f"未知蜡烛形态: {pattern_id}")
    if side not in pat.sides:
        raise ValueError(f"形态 {pat.name} 不支持方向 {side}")
    if within < 1 or within > MAX_WITHIN:
        raise ValueError(f"within 必须在 1 到 {MAX_WITHIN} 之间")
    eng = active_engine()
    if not pattern_available(pat, eng):
        raise ValueError(f"形态 {pat.name} 需要 TA-Lib，当前引擎是 {eng}")
    ensure_candle_patterns(data_dir)
    dates = _pattern_dates(data_dir)
    if not dates:
        return {"symbols": [], "as_of": None, "engine": eng, "count": 0, "pattern": pat.id, "side": side}
    chosen = dates[-within:]
    value = side_value(side)
    symbols: set[str] = set()
    for day in chosen:
        path = _store_dir(data_dir) / f"date={day}" / "part.parquet"
        frame = pl.read_parquet(path, columns=["symbol", pat.column])
        hits = frame.filter(pl.col(pat.column) == value)
        if not hits.is_empty():
            symbols.update(hits["symbol"].cast(pl.Utf8).to_list())
    ordered = sorted(symbols)
    return {
        "symbols": ordered,
        "as_of": chosen[-1],
        "engine": eng,
        "count": len(ordered),
        "pattern": pat.id,
        "side": side,
        "within": within,
    }
