"""热门事件快照。

后台每 5 分钟算一次，结果放进内存并落盘。请求只读这份快照，不再聚类、不再调模型。
过期时先返回上一份，再用单飞锁在后台重算。还没有快照时返回空列表和「正在计算」。
"""
from __future__ import annotations

import copy
import json
import logging
import threading
from datetime import datetime

from app.config import settings
from app.market_time import cn_now, current_trading_day

logger = logging.getLogger(__name__)

# 和调度间隔一致。超过这个时间的快照仍可返回，同时触发一次重算。
HOT_REFRESH_SECONDS = 300

_LOCK = threading.Lock()
_COMPUTE = threading.Lock()
_MEMORY: dict | None = None


def snapshot_path():
    return settings.data_dir / "news" / "hot_events_snapshot.json"


def clear_hot_snapshot_for_tests() -> None:
    global _MEMORY
    with _LOCK:
        _MEMORY = None


def publish_hot_snapshot(
    payload: dict,
    *,
    persist: bool = True,
    computed_at: str | None = None,
) -> dict:
    """原子替换内存快照。persist 为真时再写盘，写盘失败不影响已经换上的内存结果。"""
    global _MEMORY
    stored = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    stored.pop("status", None)
    stamp = computed_at if computed_at is not None else stored.get("computed_at")
    if not stamp:
        stamp = cn_now().isoformat(timespec="seconds")
    stored["computed_at"] = stamp
    if not isinstance(stored.get("events"), list):
        stored["events"] = []
    with _LOCK:
        _MEMORY = stored
    if persist:
        _write_disk(stored)
    return copy.deepcopy(stored)


def load_persisted_snapshot() -> bool:
    """启动时把磁盘快照装进内存。文件缺失或损坏时保持现状，不抛给启动流程。"""
    path = snapshot_path()
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("热门事件快照无法读取", exc_info=True)
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        logger.warning("热门事件快照格式无效")
        return False
    publish_hot_snapshot(payload, persist=False, computed_at=str(payload.get("computed_at") or ""))
    return True


def read_served_snapshot() -> dict:
    """请求路径。没有快照就返回正在计算；过期则返回旧结果并后台重算。"""
    with _LOCK:
        current = _MEMORY
    if current is None:
        refresh_hot_events_async()
        return _computing_payload()
    served = copy.deepcopy(current)
    if _is_stale(served):
        refresh_hot_events_async()
        served["status"] = "stale"
    else:
        served["status"] = "ready"
    return served


def refresh_hot_events_async() -> None:
    """单飞：已经有一份在算时直接返回，不再开第二份。"""
    if not _COMPUTE.acquire(blocking=False):
        return
    threading.Thread(
        target=_compute_held,
        name="hot-events-refresh",
        daemon=True,
    ).start()


def refresh_hot_events_sync() -> None:
    """调度线程用。别人正在算时等那一份结束，不紧接着再算一遍。"""
    if not _COMPUTE.acquire(blocking=False):
        with _COMPUTE:
            return
    try:
        _compute_once()
    finally:
        _COMPUTE.release()


def wait_hot_refresh(timeout: float = 2.0) -> bool:
    """测试里等后台那一份结束，避免线程在用例之间改内存。"""
    acquired = _COMPUTE.acquire(timeout=timeout)
    if not acquired:
        return False
    _COMPUTE.release()
    return True


def _compute_held() -> None:
    try:
        _compute_once()
    finally:
        _COMPUTE.release()


def _compute_once() -> None:
    from app.news.hot_events import build_top_hot_events
    from app.news.service import get_store

    snapshot = build_top_hot_events(cn_now(), get_store(), refresh=True)
    publish_hot_snapshot(snapshot, persist=True)


def _computing_payload() -> dict:
    now = cn_now()
    return {
        "as_of": None,
        "trading_day": current_trading_day(now).isoformat(),
        "fallback": False,
        "hint": "正在计算",
        "updated_at": None,
        "computed_at": None,
        "status": "computing",
        "events": [],
    }


def _is_stale(snapshot: dict) -> bool:
    parsed = _parse_stamp(snapshot.get("computed_at"))
    if parsed is None:
        return True
    age = (cn_now() - parsed).total_seconds()
    return age >= HOT_REFRESH_SECONDS


def _parse_stamp(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        from app.market_time import CN_TZ

        return parsed.replace(tzinfo=CN_TZ)
    return parsed


def _write_disk(payload: dict) -> None:
    path = snapshot_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        logger.warning("热门事件快照未能落盘", exc_info=True)
