"""ima 券商研报的后台线程。

用户每天 0:30 开始把前一天的研报转进知识库，所以这里不在凌晨拉取。
默认北京时间 06:30 和 08:30 各跑一轮，开关和时刻由环境变量决定。
测试进程里线程起来也不出网。
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from app.market_time import CN_TZ, cn_now
from app.research_reports import ima_store

logger = logging.getLogger(__name__)

FAILURE_COOLDOWN_S = 10 * 60
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
_worker: ImaResearchWorker | None = None
_worker_lock = threading.Lock()


def paused() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def parse_clocks(raw: str) -> tuple[str, ...]:
    found: set[str] = set()
    for part in re.split(r"[,，;\s]+", raw or ""):
        text = part.strip()
        match = _CLOCK.fullmatch(text)
        if match is None:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            continue
        found.add(f"{hour:02d}:{minute:02d}")
    return tuple(sorted(found))


def due_slot_keys(now: datetime, times: tuple[str, ...], done: list[str]) -> list[str]:
    """已经到点、并且今天这一档还没跑完的时刻。"""
    local = now.astimezone(CN_TZ) if now.tzinfo else now.replace(tzinfo=CN_TZ)
    clock = local.timetz().replace(tzinfo=None)
    day = local.date().isoformat()
    finished = {str(item) for item in done}
    due = []
    for hhmm in times:
        hour, minute = (int(part) for part in hhmm.split(":"))
        if clock >= dt_time(hour, minute):
            key = f"{day} {hhmm}"
            if key not in finished:
                due.append(key)
    return due


def _recent_failure(state: dict, now: datetime) -> bool:
    if not str(state.get("last_error") or "").strip():
        return False
    raw = state.get("last_attempt_at")
    if not raw:
        return False
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=CN_TZ)
    return (now.astimezone(CN_TZ) - stamp.astimezone(CN_TZ)).total_seconds() < FAILURE_COOLDOWN_S


def _trim_slots(slots: list[str], today) -> list[str]:
    cutoff = (today - timedelta(days=14)).isoformat()
    return [item for item in slots if str(item)[:10] >= cutoff]


class ImaResearchWorker:
    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ima-research", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not paused():
                    self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ima 券商研报调度失败: %s", exc)
            self._stop.wait(30)

    def run_once(self, now: datetime | None = None, *, runner=None) -> str | None:
        if runner is None and paused():
            return None
        from app.news.config import ima_research_times, source_enabled

        if not source_enabled("ima_research"):
            return None
        moment = (now or cn_now()).astimezone(CN_TZ)
        data_dir = self._dir()
        state = ima_store.load_run_state(data_dir)
        if _recent_failure(state, moment):
            return None
        times = ima_research_times()
        due = due_slot_keys(moment, times, list(state.get("done_slots") or []))
        if not due:
            return None
        call = runner or _sync
        try:
            result = call(data_dir, stop=self._stop.is_set)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ima 券商研报采集失败: %s", exc)
            ima_store.patch_run_state(
                data_dir,
                last_error=str(exc)[:300],
                last_attempt_at=moment.isoformat(timespec="seconds"),
            )
            return "error"
        if not isinstance(result, dict):
            return "error"
        if result.get("stopped"):
            return "stopped"
        if result.get("ok") or result.get("listed"):
            slots = [str(item) for item in (ima_store.load_run_state(data_dir).get("done_slots") or [])]
            for key in due:
                if key not in slots:
                    slots.append(key)
            ima_store.patch_run_state(data_dir, done_slots=_trim_slots(slots, moment.date()))
            return due[-1]
        ima_store.patch_run_state(data_dir, last_attempt_at=moment.isoformat(timespec="seconds"))
        return "error"

    def _dir(self) -> Path:
        if self._data_dir is not None:
            return Path(self._data_dir)
        from app.config import settings

        return Path(settings.data_dir)


def _sync(data_dir: Path, stop) -> dict:
    import httpx

    from app.config import settings
    from app.news.collectors import ima_headers
    from app.news.config import ima_research_kb_id
    from app.research_reports.ima_jobs import sync_ima_research

    kb_id = ima_research_kb_id()
    if not kb_id:
        raise RuntimeError("未配置知识库")
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        return sync_ima_research(
            data_dir,
            client=client,
            kb_id=kb_id,
            headers=ima_headers(settings.ima_client_id.strip(), settings.ima_api_key.strip()),
            stop=stop,
        )


def get_worker() -> ImaResearchWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = ImaResearchWorker()
        return _worker
