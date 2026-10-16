"""做T池盘中五档快照采集 — 研究用旁路线, 只写 data/depth5_intraday。

用途: 量化做T信号触发时的真实买卖价差。与 depth_service(连板封单 sealed,
data/depth5) 完全独立, 不读写其数据, 只复用其 provider 路由与分片限速。

- 标的: 做T池 = 自选 (+ 持仓, 跟随 news_push_t_include_positions), 上限 T_CAP
- 时段: 交易日 09:30-11:30 / 13:00-14:57 (北京时间), 不采集合竞价
- 间隔: TSP_DEPTH_INTRADAY_INTERVAL_S, 默认 3s, 最小 3s; 失败/空返回指数退避到 60s
- 落盘: 内存缓冲, 每 TSP_DEPTH_INTRADAY_FLUSH_S(默认 60s) 及收盘/停止时写
  data/depth5_intraday/date=YYYY-MM-DD/part-HHMMSS-<seq>.parquet (原子写)
- 开关: TSP_DEPTH_INTRADAY_ENABLED, 默认开; 任何异常只记日志与 last_error
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path

import polars as pl

from app.market_time import cn_now

logger = logging.getLogger(__name__)

LEVELS = 5
MIN_INTERVAL_S = 3.0
MAX_BACKOFF_S = 60.0
SESSIONS = ((dt_time(9, 30), dt_time(11, 30)), (dt_time(13, 0), dt_time(14, 57)))
SUBDIR = "depth5_intraday"

SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "source_ts_ms": pl.Int64,
    "fetched_at": pl.Float64,
    "last_price": pl.Float64,
    **{f"bid{i}_px": pl.Float64 for i in range(1, LEVELS + 1)},
    **{f"bid{i}_vol": pl.Int64 for i in range(1, LEVELS + 1)},
    **{f"ask{i}_px": pl.Float64 for i in range(1, LEVELS + 1)},
    **{f"ask{i}_vol": pl.Int64 for i in range(1, LEVELS + 1)},
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def in_collect_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    t = now.time()
    return any(start <= t <= end for start, end in SESSIONS)


def _num(value, cast):
    if value is None:
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def depth_to_row(symbol: str, data: dict, fetched_at: float, last_price: float | None) -> dict:
    """把 provider 的 depth dict 摊平成一行; 缺档补 None, 不臆造。"""
    row: dict = {
        "symbol": symbol,
        "source_ts_ms": _num(data.get("timestamp"), int),
        "fetched_at": fetched_at,
        "last_price": last_price,
    }
    for side in ("bid", "ask"):
        prices = data.get(f"{side}_prices") or []
        vols = data.get(f"{side}_volumes") or []
        for i in range(LEVELS):
            px = _num(prices[i], float) if i < len(prices) else None
            row[f"{side}{i + 1}_px"] = round(px, 3) if px is not None else None
            row[f"{side}{i + 1}_vol"] = _num(vols[i], int) if i < len(vols) else None
    return row


def next_interval(base: float, failures: int) -> float:
    if failures <= 0:
        return base
    return min(MAX_BACKOFF_S, base * (2 ** min(failures, 6)))


class DepthIntradayCollector:
    def __init__(self, data_dir: Path, depth_service, *, symbols_fn=None,
                 last_price_fn=None, trading_day_fn=None, now_fn=cn_now) -> None:
        self.data_dir = Path(data_dir)
        self.depth_service = depth_service
        self.symbols_fn = symbols_fn or _t_pool_symbols
        self.last_price_fn = last_price_fn or _last_prices
        self.trading_day_fn = trading_day_fn or _is_trading_day
        self.now_fn = now_fn
        self.enabled = _env_bool("TSP_DEPTH_INTRADAY_ENABLED", True)
        self.interval = _env_float("TSP_DEPTH_INTRADAY_INTERVAL_S", MIN_INTERVAL_S, MIN_INTERVAL_S)
        self.flush_s = _env_float("TSP_DEPTH_INTRADAY_FLUSH_S", 60.0, 5.0)
        self._buf: list[dict] = []
        self._buf_day: str | None = None
        self._seq = 0
        self._failures = 0
        self._last_ts: dict[str, int] = {}
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status: dict = {
            "enabled": self.enabled, "interval_s": self.interval, "running": False,
            "day": None, "rows_today": 0, "polls_today": 0, "symbols": 0,
            "last_poll_at": None, "last_flush_at": None, "last_error": None,
            "last_error_at": None, "consecutive_failures": 0,
        }

    # ---------------- lifecycle ----------------
    def start(self) -> None:
        if not self.enabled:
            logger.info("depth intraday: 开关关闭 (TSP_DEPTH_INTRADAY_ENABLED)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="depth-intraday", daemon=True)
        self._thread.start()
        self.status["running"] = True
        logger.info("depth intraday 已启动, 间隔 %.1fs", self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self.status["running"] = False
        self._safe_flush()

    def get_status(self) -> dict:
        return dict(self.status)

    # ---------------- loop ----------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            wait = self.interval
            try:
                wait = self.tick()
            except Exception as e:  # noqa: BLE001 — 永不让线程死掉
                self._record_error(e)
                wait = next_interval(self.interval, self._failures)
            self._stop.wait(wait)

    def tick(self) -> float:
        """一拍; 返回下一拍等待秒数。"""
        now = self.now_fn()
        if not in_collect_window(now):
            self._safe_flush()
            return 15.0
        if self.trading_day_fn(now) is False:
            return 300.0
        self._roll_day(now.date().isoformat())
        self.poll_once()
        if time.monotonic() - self._last_flush >= self.flush_s:
            self._safe_flush()
        return next_interval(self.interval, self._failures)

    def poll_once(self) -> int:
        symbols = self.symbols_fn()
        self.status["symbols"] = len(symbols)
        if not symbols:
            return 0
        try:
            data = self.depth_service._call_depth_batch(symbols)
        except Exception as e:  # noqa: BLE001
            self._record_error(e)
            return 0
        if not data:
            self._record_error(RuntimeError("depth batch 返回空"))
            return 0
        fetched = time.time()
        try:
            last = self.last_price_fn(symbols) or {}
        except Exception:  # noqa: BLE001
            last = {}
        rows = []
        dup = 0
        for s, d in data.items():
            if not isinstance(d, dict):
                continue
            row = depth_to_row(s, d, fetched, last.get(s))
            ts = row["source_ts_ms"]
            # 源时间戳没变 = 盘口没更新, 不重复落盘
            if ts is not None and self._last_ts.get(s) == ts:
                dup += 1
                continue
            if ts is not None:
                self._last_ts[s] = ts
            rows.append(row)
        self.status["dup_skipped_today"] = self.status.get("dup_skipped_today", 0) + dup
        with self._lock:
            self._buf.extend(rows)
        self._failures = 0
        self.status.update(consecutive_failures=0, last_poll_at=fetched,
                           polls_today=self.status["polls_today"] + 1,
                           rows_today=self.status["rows_today"] + len(rows))
        return len(rows)

    # ---------------- persistence ----------------
    def _roll_day(self, day: str) -> None:
        if self._buf_day == day:
            return
        self._safe_flush()
        self._buf_day = day
        self._seq = 0
        self._last_ts = {}
        self.status.update(day=day, rows_today=0, polls_today=0, dup_skipped_today=0)

    def _safe_flush(self) -> None:
        try:
            self.flush()
        except Exception as e:  # noqa: BLE001
            self._record_error(e)

    def flush(self) -> Path | None:
        with self._lock:
            rows, self._buf = self._buf, []
        self._last_flush = time.monotonic()
        if not rows or not self._buf_day:
            return None
        self._seq += 1
        out_dir = self.data_dir / SUBDIR / f"date={self._buf_day}"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.now_fn().strftime("%H%M%S")
        out = out_dir / f"part-{stamp}-{self._seq:04d}.parquet"
        tmp = out.with_name(out.name + ".tmp")
        try:
            pl.DataFrame(rows, schema=SCHEMA).write_parquet(tmp)
            os.replace(tmp, out)
        except Exception:
            with self._lock:  # 写失败放回缓冲, 下次重试
                self._buf = rows + self._buf
            raise
        self.status["last_flush_at"] = time.time()
        return out

    def _record_error(self, e: Exception) -> None:
        self._failures += 1
        self.status.update(last_error=f"{type(e).__name__}: {e}"[:300],
                           last_error_at=time.time(), consecutive_failures=self._failures)
        logger.warning("depth intraday 失败(连续%d): %s", self._failures, e)


def _t_pool_symbols() -> list[str]:
    from app.news.push import T_CAP, _flag_on, _position_symbols, t_universe
    from app.services.watchlist import list_symbols

    watchlist = [str(row.get("symbol") or "") for row in list_symbols()]
    positions = _position_symbols() if _flag_on("news_push_t_include_positions") else []
    return t_universe(watchlist, positions, include_positions=bool(positions), cap=T_CAP)


def _last_prices(symbols: list[str]) -> dict[str, float]:
    from app.news.push import _cached_live_rows

    rows = _cached_live_rows(symbols) or []
    out: dict[str, float] = {}
    for row in rows:
        price = row.get("raw_close") or row.get("close")
        if row.get("symbol") and price:
            out[str(row["symbol"])] = float(price)
    return out


def _is_trading_day(now: datetime) -> bool | None:
    from app.services.trading_day import is_trading_day
    return is_trading_day(now)
