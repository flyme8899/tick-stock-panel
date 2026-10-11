"""打板和研报的后台线程。

交易日 9:35–15:00 每 10 分钟轻量刷新涨停池。15:10 之后写一份收盘快照。
研报在 16:00 之后按小批量增量同步，每轮只做一件事，避免和收盘快照抢东财配额。
日 K 管道占着写盘时这一轮让路。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from app.market_feeds import config
from app.market_time import CN_TZ, cn_now
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

INTRADAY_GAP_S = 10 * 60
RETRY_GAP_S = 30 * 60
_CLOSE_AT = dt_time(15, 10)
_RESEARCH_AT = dt_time(16, 0)
_OPEN_AT = dt_time(9, 35)
_SESSION_END = dt_time(15, 0)

_worker: MarketFeedWorker | None = None
_worker_lock = threading.Lock()


def feeds_paused() -> bool:
    """测试进程不因默认打开而出网。"""
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def previous_weekday(day: date) -> date:
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def session_day(now: datetime, *, trading: bool) -> str:
    """收盘前用上一交易日，15:10 之后用当天。"""
    local = now.astimezone(CN_TZ)
    clock = local.timetz().replace(tzinfo=None)
    if trading and clock >= _CLOSE_AT:
        return local.date().isoformat()
    return previous_weekday(local.date()).isoformat()


def _parse_stamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed


def _recent(state: dict, key: str, now: datetime, gap_s: float) -> bool:
    stamp = _parse_stamp(state.get(key))
    if stamp is None:
        return False
    return (now - stamp).total_seconds() < gap_s


def research_pending(state: dict, day: str) -> bool:
    if str(state.get("industry_date") or "") != day:
        return True
    if str(state.get("research_date") or "") != day:
        return True
    done = {str(item) for item in (state.get("research_done") or [])}
    queue = [str(item) for item in (state.get("research_queue") or [])]
    return any(symbol not in done for symbol in queue)


def due_tasks(
    now: datetime,
    *,
    trading: bool,
    state: dict,
    flags: dict[str, bool],
) -> list[str]:
    """这一轮最多该跑的任务，按优先级排列。调用方只取第一个。"""
    if not flags.get("limit_board") and not flags.get("research"):
        return []
    local = now.astimezone(CN_TZ)
    clock = local.timetz().replace(tzinfo=None)
    due: list[str] = []
    close_day = session_day(local, trading=trading)
    morning = local.weekday() < 5 and clock < _OPEN_AT
    close_missing = str(state.get("limit_close_date") or "") != close_day
    close_cooled = not _recent(state, "limit_close_at", local, RETRY_GAP_S)
    if flags.get("limit_board") and close_missing and close_cooled and (
        (trading and clock >= _CLOSE_AT) or morning
    ):
        due.append("limit_close")
    if (
        flags.get("limit_board")
        and trading
        and _OPEN_AT <= clock <= _SESSION_END
        and not _recent(state, "limit_intraday_at", local, INTRADAY_GAP_S)
        and "limit_close" not in due
    ):
        due.append("limit_intraday")
    research_window = (trading and clock >= _RESEARCH_AT) or morning
    if (
        flags.get("research")
        and research_window
        and research_pending(state, close_day)
        and not _recent(state, "research_at", local, 60)
    ):
        due.append("research")
    return due


class MarketFeedWorker:
    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="market-feeds", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not feeds_paused():
                    self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("打板/研报调度失败: %s", exc)
            self._stop.wait(30)

    def run_once(self, now: datetime | None = None) -> str | None:
        from app.fund_flow.scheduler import tickflow_sync_busy

        if tickflow_sync_busy():
            return None
        now = (now or cn_now()).astimezone(CN_TZ)
        trading = _trading(now)
        data_dir = self._dir()
        state = _load_state(data_dir)
        flags = {
            "limit_board": config.limit_board_enabled(),
            "research": config.research_enabled(),
        }
        tasks = due_tasks(now, trading=trading, state=state, flags=flags)
        if not tasks:
            return None
        name = tasks[0]
        if name == "limit_close":
            self._limit(now, trading=trading, state=state, kind="limit_close")
        elif name == "limit_intraday":
            self._limit(now, trading=trading, state=state, kind="limit_intraday")
        else:
            self._research(now, trading=trading, state=state)
        _save_state(data_dir, state)
        return name

    def _limit(self, now: datetime, *, trading: bool, state: dict, kind: str) -> None:
        from app.limit_board.jobs import sync_day

        day = session_day(now, trading=trading) if kind == "limit_close" else now.date().isoformat()
        stamp = now.isoformat(timespec="seconds")
        if kind == "limit_intraday":
            state["limit_intraday_at"] = stamp
        else:
            state["limit_close_at"] = stamp
        try:
            result = sync_day(
                self._dir(),
                day,
                eastmoney=config.limit_board_eastmoney(),
                ths=config.limit_board_ths(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("打板同步失败: %s", exc)
            state["limit_error"] = str(exc)[:180]
            return
        state["limit_error"] = "; ".join(result.get("errors") or [])[:300]
        state["ths_status"] = result.get("ths")
        if kind == "limit_close" and not result.get("errors"):
            state["limit_close_date"] = day
        elif kind == "limit_close" and result.get("wrote"):
            # 部分池子已经落盘。有错误时不标完成，冷却后重试失败的池子。
            # 重试会整表替换已成功的池子，仍然正确。
            pass

    def _research(self, now: datetime, *, trading: bool, state: dict) -> None:
        from app.research_reports.jobs import sync_industry, sync_symbol, target_symbols

        day = session_day(now, trading=trading)
        state["research_at"] = now.isoformat(timespec="seconds")
        data_dir = self._dir()
        if str(state.get("research_date") or "") != day:
            state["research_date"] = day
            state["research_queue"] = (
                target_symbols(data_dir, limit=config.research_max_symbols())
                if config.research_stock()
                else []
            )
            state["research_done"] = []
        if not config.research_industry():
            state["industry_date"] = day
        if config.research_industry() and str(state.get("industry_date") or "") != day:
            try:
                sync_industry(data_dir, today=now.date())
                state["industry_date"] = day
                state["research_error"] = ""
            except Exception as exc:  # noqa: BLE001
                logger.warning("行业研报同步失败: %s", exc)
                state["research_error"] = str(exc)[:180]
            return
        if not config.research_stock():
            return
        done = {str(item) for item in (state.get("research_done") or [])}
        queue = [str(item) for item in (state.get("research_queue") or [])]
        batch = [symbol for symbol in queue if symbol not in done][: config.research_batch_size()]
        errors = []
        finished = list(state.get("research_done") or [])
        for symbol in batch:
            try:
                sync_symbol(data_dir, symbol, today=now.date())
                finished.append(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("个股研报 %s 失败: %s", symbol, exc)
                errors.append(f"{symbol}: {exc}"[:120])
                finished.append(symbol)
        state["research_done"] = finished
        state["research_error"] = "; ".join(errors)[:300]

    def _dir(self) -> Path:
        if self._data_dir is not None:
            return Path(self._data_dir)
        from app.config import settings

        return Path(settings.data_dir)


def _trading(now: datetime) -> bool:
    local = now.astimezone(CN_TZ)
    if local.weekday() >= 5:
        return False
    try:
        from app.services.trading_day import is_trading_day

        verdict = is_trading_day(local)
    except Exception:  # noqa: BLE001
        return True
    if verdict is None:
        return True
    return bool(verdict)


def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "market_feeds" / "state.json"


def _load_state(data_dir: Path) -> dict:
    path = _state_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(data_dir: Path, state: dict) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))


def get_worker() -> MarketFeedWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = MarketFeedWorker()
        return _worker
