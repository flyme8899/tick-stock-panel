"""选股补充字段的后台线程。

不进重任务槽。日 K 管道占着写盘时这一轮直接返回，下一轮再续。
测试进程里循环空转，``run_once`` 仍可供单测直接调用。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from app.market_time import CN_TZ, cn_now
from app.selection_fields import config, jobs
from app.selection_fields.store import load_checkpoint
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

RETRY_GAP_S = 30 * 60
_CLOSE_AT = dt_time(15, 10)
_OPEN_AT = dt_time(9, 35)
_TASKS = (
    ("northbound", dt_time(16, 20), "northbound"),
    ("holders", dt_time(16, 30), "holders"),
    ("index", dt_time(16, 40), "index"),
    ("unlock", dt_time(16, 50), "unlock"),
    ("pe", dt_time(17, 10), "pe"),
)

_worker: SelectionWorker | None = None
_worker_lock = threading.Lock()
_TASK_LABELS = {
    "northbound": "北向持股",
    "holders": "股东户数",
    "index": "指数成分",
    "unlock": "解禁时间",
    "pe": "PE(TTM)",
}


def selection_paused() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def previous_weekday(day: date) -> date:
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def session_day(now: datetime, *, trading: bool) -> str:
    """交易日 15:10 之后用当天，其余时间用上一工作日。"""
    local = now.astimezone(CN_TZ)
    clock = local.timetz().replace(tzinfo=None)
    if trading and clock >= _CLOSE_AT:
        return local.date().isoformat()
    return previous_weekday(local.date()).isoformat()


def due_tasks(now: datetime, *, trading: bool, state: dict, flags: dict[str, bool]) -> list[str]:
    """这一轮最多该跑的任务。调用方只取第一个。盘中不拉。"""
    if not flags.get("enabled"):
        return []
    local = now.astimezone(CN_TZ)
    clock = local.timetz().replace(tzinfo=None)
    morning = local.weekday() < 5 and clock < _OPEN_AT
    if trading and not morning and clock < _CLOSE_AT:
        return []
    day = session_day(local, trading=trading)
    catchup = (not trading) or morning
    due: list[str] = []
    for name, at, flag in _TASKS:
        if not flags.get(flag):
            continue
        if str(state.get(f"{name}_date") or "") == day:
            continue
        if name != "pe" and _recent(state, f"{name}_at", local, RETRY_GAP_S):
            continue
        if catchup or clock >= at:
            due.append(name)
    return due


class SelectionWorker:
    def __init__(self, data_dir: Path | None = None, ak=None) -> None:
        self._data_dir = data_dir
        self._ak = ak
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._inflight: threading.Thread | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="selection-fields", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not selection_paused():
                    self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("选股补充字段调度失败: %s", exc)
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
            "enabled": config.enabled(),
            "northbound": config.northbound_enabled(),
            "holders": config.holders_enabled(),
            "index": config.index_members_enabled(),
            "unlock": config.unlock_enabled(),
            "pe": config.pe_ttm_enabled(),
        }
        tasks = due_tasks(now, trading=trading, state=state, flags=flags)
        if not tasks:
            return None
        day = session_day(now, trading=trading)
        started: list[str] = []
        for name in tasks:
            if _still_running(self):
                state["last_error"] = "上一个补充字段任务仍在运行"
                break
            if name != "pe":
                state[f"{name}_at"] = now.isoformat(timespec="seconds")
            outcome = _run_bounded(
                self,
                name,
                lambda name=name: _execute(name, self, data_dir, now.date(), day),
            )
            started.append(name)
            if outcome.get("error"):
                state["last_error"] = str(outcome["error"])[:300]
            if outcome.get("timed_out") or outcome.get("busy") or outcome.get("stopped"):
                break
            if name == "pe":
                if outcome.get("complete"):
                    state["pe_date"] = day
                elif outcome.get("note"):
                    state["pe_note"] = outcome["note"]
                break
            if outcome.get("ok"):
                state[f"{name}_date"] = day
            break
        _save_state(data_dir, state)
        return started[0] if started else None

    def _call(self, name: str, **kwargs):
        if self._ak is not None:
            return self._ak(name, **kwargs)
        return jobs.live_ak(name, **kwargs)

    def _dir(self) -> Path:
        if self._data_dir is not None:
            return Path(self._data_dir)
        from app.config import settings

        return Path(settings.data_dir)


def _stopped(worker: SelectionWorker) -> bool:
    return worker._stop.is_set() or worker._cancel.is_set()


def _still_running(worker: SelectionWorker) -> bool:
    thread = worker._inflight
    return thread is not None and thread.is_alive()


def _advance(worker: SelectionWorker, data_dir: Path, day: str) -> dict:
    from app.fund_flow.scheduler import tickflow_sync_busy

    return jobs.advance_pe(
        data_dir,
        worker._call,
        day,
        flush_every=config.pe_flush_every(),
        busy=tickflow_sync_busy,
        stop=lambda: _stopped(worker),
    )


def _execute(name: str, worker: SelectionWorker, data_dir: Path, today: date, day: str) -> dict:
    def stop() -> bool:
        return _stopped(worker)

    if name == "pe":
        return _advance(worker, data_dir, day)
    if name == "index":
        return jobs.sync_indexes(worker._call, data_dir, day=day, stop=stop)
    return _run_named(name, worker._call, data_dir, today, stop)


def _run_bounded(worker: SelectionWorker, name: str, fn) -> dict:
    """到点还没返回就通知线程停下。线程还活着时调用方不再开新线程。"""
    from app.selection_fields.timeouts import task_timeout

    if _still_running(worker):
        return {"ok": False, "busy": True, "error": "上一个补充字段任务仍在运行"}
    worker._cancel.clear()
    timeout_s = task_timeout(name)
    holder: dict = {}

    def target() -> None:
        try:
            holder["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            holder["value"] = {"ok": False, "error": str(exc)[:300]}

    thread = threading.Thread(target=target, name=f"selection-{name}", daemon=True)
    worker._inflight = thread
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        worker._cancel.set()
        label = _TASK_LABELS.get(name, name)
        logger.warning("选股补充字段%s超时（%.0f 秒）", label, timeout_s)
        return {"ok": False, "error": f"{label}任务超时", "timed_out": True}
    if worker._inflight is thread:
        worker._inflight = None
    worker._cancel.clear()
    value = holder.get("value")
    if isinstance(value, dict):
        return value
    return {"ok": False, "error": "任务没有结果"}


def _run_named(name: str, ak, data_dir: Path, today: date, stop) -> dict:
    if name == "northbound":
        return jobs.sync_northbound(ak, data_dir, stop=stop)
    if name == "holders":
        return jobs.sync_holders(ak, data_dir)
    if name == "index":
        return jobs.sync_indexes(ak, data_dir, stop=stop)
    if name == "unlock":
        return jobs.sync_unlock(ak, data_dir, today)
    return {"ok": False, "error": f"未知任务 {name}"}


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


def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "selection_fields" / "state.json"


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


def pe_finished(data_dir: Path, day: str) -> bool:
    checkpoint = load_checkpoint(data_dir)
    if str(checkpoint.get("trade_date") or "") != day:
        return False
    total = int(checkpoint.get("total") or 0)
    if total <= 0:
        return False
    done = len(checkpoint.get("done") or [])
    skipped = len(checkpoint.get("skipped") or [])
    return done + skipped >= total


def get_worker() -> SelectionWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = SelectionWorker()
        return _worker
