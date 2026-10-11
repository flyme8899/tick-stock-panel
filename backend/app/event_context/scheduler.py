"""第二批东财数据的后台线程。

不进重任务槽。日 K 管道占着写盘时这一轮直接返回。
测试进程里循环空转，``run_once`` 仍可供单测直接调用。
同一时刻只跑一个子任务。上一个线程还活着时不再开新线程。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from app.event_context import config, jobs, store
from app.event_context.timeouts import task_timeout
from app.market_time import CN_TZ, cn_now

logger = logging.getLogger(__name__)

_CLOSE_AT = dt_time(15, 10)
_OPEN_AT = dt_time(9, 35)
_SESSION_END = dt_time(15, 0)
_LHB_AT = dt_time(17, 40)
_MARGIN_AT = dt_time(20, 10)
_CHANGES_GAP_S = 10 * 60
_NOTICE_GAP_S = 30 * 60
_RETRY_GAP_S = 30 * 60
_QA_GAP_S = 10 * 60
_LABELS = {
    "lhb": "龙虎榜明细",
    "margin": "两融明细",
    "qa": "互动问答",
    "notice": "公告",
    "changes": "盘口异动",
}

_worker: EventWorker | None = None
_worker_lock = threading.Lock()


def context_paused() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def previous_weekday(day: date) -> date:
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def session_day(now: datetime, *, trading: bool) -> str:
    local = now.astimezone(CN_TZ)
    clock = local.timetz().replace(tzinfo=None)
    if trading and clock >= _CLOSE_AT:
        return local.date().isoformat()
    return previous_weekday(local.date()).isoformat()


def due_tasks(now: datetime, *, trading: bool, schedule: dict, flags: dict[str, bool]) -> list[str]:
    """这一轮最多该跑的任务。调用方只取第一个。"""
    if not flags.get("enabled"):
        return []
    local = now.astimezone(CN_TZ)
    clock = local.timetz().replace(tzinfo=None)
    weekday = local.weekday() < 5
    morning = weekday and clock < _OPEN_AT
    session = weekday and trading and _OPEN_AT <= clock <= _SESSION_END
    day = session_day(local, trading=trading)
    due: list[str] = []
    if session and flags.get("changes") and not _recent(schedule, "changes_at", local, _CHANGES_GAP_S):
        due.append("changes")
    if session and flags.get("notice") and not _recent(schedule, "notice_at", local, _NOTICE_GAP_S):
        due.append("notice")
    catchup = (not session) and ((not trading) or morning or clock >= _CLOSE_AT)
    if not catchup:
        return due
    lhb_due = flags.get("lhb") and schedule.get("lhb_date") != day and (not trading or morning or clock >= _LHB_AT)
    if lhb_due and not _recent(schedule, "lhb_at", local, _RETRY_GAP_S):
        due.append("lhb")
    margin_due = flags.get("margin") and schedule.get("margin_date") != day and (
        not trading or morning or clock >= _MARGIN_AT
    )
    if margin_due and not _recent(schedule, "margin_at", local, _RETRY_GAP_S):
        due.append("margin")
    if flags.get("notice") and not _recent(schedule, "notice_at", local, _NOTICE_GAP_S):
        due.append("notice")
    if flags.get("qa") and schedule.get("qa_date") != day and not _recent(schedule, "qa_at", local, _QA_GAP_S):
        due.append("qa")
    return due


class EventWorker:
    def __init__(self, data_dir: Path | None = None, get=None) -> None:
        self._data_dir = data_dir
        self._get = get
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._inflight: threading.Thread | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="event-context", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not context_paused():
                    self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("事件上下文调度失败: %s", exc)
            self._stop.wait(30)

    def run_once(self, now: datetime | None = None) -> str | None:
        from app.fund_flow.scheduler import tickflow_sync_busy

        if tickflow_sync_busy():
            return None
        now = (now or cn_now()).astimezone(CN_TZ)
        trading = _trading(now)
        data_dir = self._dir()
        state = store.load_state(data_dir)
        schedule = dict(state.get("schedule") or {})
        flags = _flags()
        tasks = due_tasks(now, trading=trading, schedule=schedule, flags=flags)
        if not tasks:
            return None
        name = tasks[0]
        if _still_running(self):
            schedule["last_error"] = "上一个事件上下文任务仍在运行"
            _save_schedule(data_dir, schedule)
            return None
        schedule[f"{name}_at"] = now.isoformat(timespec="seconds")
        day = session_day(now, trading=trading)
        work_day = now.date().isoformat() if name == "changes" else day
        outcome = _run_bounded(self, name, lambda: _execute(name, self, data_dir, work_day))
        if outcome.get("error"):
            schedule["last_error"] = str(outcome["error"])[:300]
        elif outcome.get("ok"):
            schedule["last_error"] = ""
            if name in {"lhb", "margin"}:
                schedule[f"{name}_date"] = day
            if name == "qa" and outcome.get("complete"):
                schedule["qa_date"] = day
        _save_schedule(data_dir, schedule)
        return name

    def _dir(self) -> Path:
        if self._data_dir is not None:
            return Path(self._data_dir)
        from app.config import settings

        return Path(settings.data_dir)


def _flags() -> dict[str, bool]:
    return {
        "enabled": config.enabled(),
        "lhb": config.lhb_enabled(),
        "margin": config.margin_enabled(),
        "qa": config.qa_enabled(),
        "notice": config.notice_enabled(),
        "changes": config.changes_enabled(),
    }


def _execute(name: str, worker: EventWorker, data_dir: Path, day: str) -> dict:
    def stop() -> bool:
        return worker._stop.is_set() or worker._cancel.is_set()

    getter = worker._get
    if name == "lhb":
        return jobs.sync_lhb(data_dir, day, get=getter, stop=stop)
    if name == "margin":
        return jobs.sync_margin(data_dir, day, get=getter, stop=stop)
    if name == "qa":
        return jobs.sync_qa(data_dir, get=getter, stop=stop)
    if name == "notice":
        return jobs.sync_notice(data_dir, get=getter, stop=stop)
    if name == "changes":
        return jobs.sync_changes(data_dir, day, get=getter, stop=stop)
    return {"ok": False, "error": f"未知任务 {name}"}


def _run_bounded(worker: EventWorker, name: str, fn) -> dict:
    if _still_running(worker):
        return {"ok": False, "busy": True, "error": "上一个事件上下文任务仍在运行"}
    worker._cancel.clear()
    timeout_s = task_timeout(name)
    holder: dict = {}

    def target() -> None:
        try:
            holder["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            holder["value"] = {"ok": False, "error": str(exc)[:300]}

    thread = threading.Thread(target=target, name=f"event-{name}", daemon=True)
    worker._inflight = thread
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        worker._cancel.set()
        label = _LABELS.get(name, name)
        logger.warning("事件上下文%s超时（%.0f 秒）", label, timeout_s)
        return {"ok": False, "error": f"{label}任务超时", "timed_out": True}
    if worker._inflight is thread:
        worker._inflight = None
    worker._cancel.clear()
    value = holder.get("value")
    if isinstance(value, dict):
        return value
    return {"ok": False, "error": "任务没有结果"}


def _still_running(worker: EventWorker) -> bool:
    thread = worker._inflight
    return thread is not None and thread.is_alive()


def _recent(state: dict, key: str, now: datetime, gap_s: float) -> bool:
    raw = state.get(key)
    if not raw:
        return False
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=CN_TZ)
    return (now - stamp).total_seconds() < gap_s


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


def _save_schedule(data_dir: Path, schedule: dict) -> None:
    state = store.load_state(data_dir)
    state["schedule"] = schedule
    store.save_state(data_dir, state)


def get_worker() -> EventWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = EventWorker()
        return _worker
