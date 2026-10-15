"""热点事件榜的按日快照。

热门事件内存快照只保留最新一份，研究无法回测「当时的榜」。每个交易日在
固定窗口里把名次落成 parquet，按日期分区，同一天同一档只保留一份：

- preopen：交易日 09:15 前，分区日是当天，后来的覆盖先前的。
- postclose：交易日 15:05 到 20:00，分区日是刚收盘的当天，第一份留下。
- nextday：15:00 之后或休市日，分区日是下一开市日；09:15 前的当天也写这一档，
  所以 09:15 前最后一次就是最终的次日口径榜。后来的覆盖先前的，09:15 后不再写。

归档看计算开始时刻，不看算完的墙钟。15:05 前开工、15:05 后才写完的那一份
不算收盘榜。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_now

logger = logging.getLogger(__name__)

SLOT_PREOPEN = "preopen"
SLOT_POSTCLOSE = "postclose"
SLOT_NEXTDAY = "nextday"
_SLOTS = (SLOT_PREOPEN, SLOT_POSTCLOSE, SLOT_NEXTDAY)
_POLICY_FIRST = "first"
_POLICY_LATEST = "latest"

_SCHEMA: dict[str, pl.DataType] = {
    "session_date": pl.Date,
    "slot": pl.Utf8,
    "rank": pl.Int32,
    "event_key": pl.Utf8,
    "title": pl.Utf8,
    "category": pl.Utf8,
    "concepts": pl.List(pl.Utf8),
    "stocks": pl.List(pl.Utf8),
    "etfs": pl.List(pl.Utf8),
    "score": pl.Float64,
    "breakdown_importance": pl.Float64,
    "breakdown_confirmation": pl.Float64,
    "breakdown_mapping": pl.Float64,
    "breakdown_freshness": pl.Float64,
    "breakdown_heat": pl.Float64,
    "validation_state": pl.Utf8,
    "validation_label": pl.Utf8,
    "validation_score": pl.Float64,
    "direction": pl.Utf8,
    "importance": pl.Utf8,
    "computed_at": pl.Utf8,
    "trading_day": pl.Utf8,
    "as_of": pl.Utf8,
}


def rank_root(data_dir: Path | str) -> Path:
    return Path(data_dir) / "news" / "hot_event_rank"


def rank_path(data_dir: Path | str, session_date: date, slot: str) -> Path:
    return rank_root(data_dir) / f"date={session_date.isoformat()}" / f"{slot}.parquet"


def due_slots(now: datetime) -> list[tuple[str, date, str]]:
    """当前时刻该落哪些档。(slot, 分区日, first|latest)。窗口外为空。"""
    from app.news.market_confirm import _clock_day_closed, _next_open_day

    local = now.astimezone(CN_TZ)
    clock = local.time()
    day = local.date()
    closed = _clock_day_closed(local)
    slots: list[tuple[str, date, str]] = []
    if not closed and clock < dt_time(9, 15):
        slots.append((SLOT_PREOPEN, day, _POLICY_LATEST))
        slots.append((SLOT_NEXTDAY, day, _POLICY_LATEST))
    if not closed and dt_time(15, 5) <= clock < dt_time(20, 0):
        slots.append((SLOT_POSTCLOSE, day, _POLICY_FIRST))
    if closed or clock >= dt_time(15, 0):
        slots.append((SLOT_NEXTDAY, _next_open_day(day), _POLICY_LATEST))
    return slots


def empty_hot_event_rank() -> pl.DataFrame:
    return pl.DataFrame(schema=_SCHEMA)


def archive_hot_rank(
    snapshot: dict,
    *,
    now: datetime,
    data_dir: Path | str | None = None,
) -> list[Path]:
    """按 now 的窗口把榜写入 parquet。窗口外什么都不写。"""
    from app.config import settings
    from app.services.fs_utils import atomic_write_parquet

    root = Path(data_dir) if data_dir is not None else Path(settings.data_dir)
    written: list[Path] = []
    for slot, session_date, policy in due_slots(now):
        path = rank_path(root, session_date, slot)
        if policy == _POLICY_FIRST and path.exists():
            continue
        frame = _frame_from_snapshot(snapshot, slot=slot, session_date=session_date, now=now)
        if frame.is_empty():
            # 空榜不覆盖已有快照: ``latest`` 档每轮都写, 一次上游异常就会用空表
            # 抹掉当天的历史榜。第一档仍写空表 —— 那是 ``first`` 的语义
            # (先到先得, 用空榜占位挡住收盘后的晚到快照), 由 policy 区分。
            if policy == _POLICY_FIRST:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_parquet(frame, path)
                written.append(path)
                logger.warning(
                    "热点榜 %s 档为空榜, 按 first 语义占位落盘 %s", slot, session_date.isoformat(),
                )
                continue
            logger.warning(
                "热点榜 %s 档本轮无有效事件, 跳过落盘, 保留已有快照 %s", slot, session_date.isoformat(),
            )
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(frame, path)
        written.append(path)
    return written


def load_hot_event_rank(
    data_dir: Path | str,
    day: date,
    slot: str | None = None,
) -> pl.DataFrame:
    """读取某一天的榜。slot 为空时把当天已有的档拼在一起。没有文件时返回空表。"""
    if slot is not None and slot not in _SLOTS:
        raise ValueError(f"未知榜单档位: {slot}")
    names = (slot,) if slot else _SLOTS
    frames = []
    for name in names:
        path = rank_path(data_dir, day, name)
        if path.is_file():
            frames.append(pl.read_parquet(path))
    if not frames:
        return empty_hot_event_rank()
    return pl.concat(frames, how="diagonal_relaxed")


def capture_hot_rank_now(now: datetime | None = None) -> bool:
    """调度补拍。内存快照的计算时刻已在当前窗口内就直接归档，否则同步重算一次。

    重算锁被占用时等待那一份结束，不再紧接着算第二遍。那一份若开工于窗口外，
    这次补拍会错过，下一轮 5 分钟热榜循环会补上。
    """
    from app.news.hot_snapshot import _parse_stamp, current_snapshot, refresh_hot_events_sync

    moment = (now or cn_now()).astimezone(CN_TZ)
    if not due_slots(moment):
        return False
    current = current_snapshot()
    stamp = _parse_stamp((current or {}).get("computed_at"))
    if current is not None and stamp is not None and _slot_key(stamp) == _slot_key(moment):
        archive_hot_rank(current, now=stamp)
        return True
    refresh_hot_events_sync()
    return True


def _slot_key(moment: datetime) -> tuple[tuple[str, str], ...]:
    return tuple((slot, day.isoformat()) for slot, day, _policy in due_slots(moment))


def _frame_from_snapshot(snapshot: dict, *, slot: str, session_date: date, now: datetime) -> pl.DataFrame:
    from app.news.service import public_hot_event

    events = snapshot.get("events") if isinstance(snapshot, dict) else None
    if not isinstance(events, list):
        events = []
    computed_at = str(snapshot.get("computed_at") or now.astimezone(CN_TZ).isoformat(timespec="seconds"))
    trading_day = str(snapshot.get("trading_day") or "")
    as_of = str(snapshot.get("as_of") or "")
    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue
        public = public_hot_event(event)
        confirmation = public.get("confirmation") if isinstance(public.get("confirmation"), dict) else {}
        breakdown = public.get("breakdown") if isinstance(public.get("breakdown"), dict) else {}
        rows.append({
            "session_date": session_date,
            "slot": slot,
            # 序号在跳过非法项之后按有效行重新编号, 不能直接用 events 的下标 ——
            # 列表里混进非 dict 时, 下标会留下空洞 (1,2,4,5), 研究侧按 rank 取
            # 「第 N 名」就会错位。
            "rank": len(rows) + 1,
            "event_key": str(public.get("key") or ""),
            "title": str(public.get("name") or ""),
            "category": str(public.get("category") or ""),
            "concepts": [str(item) for item in (public.get("concepts") or []) if str(item).strip()],
            "stocks": [str(item.get("key") or "") for item in (public.get("stocks") or []) if isinstance(item, dict) and item.get("key")],
            "etfs": [str(item.get("key") or "") for item in (public.get("etfs") or []) if isinstance(item, dict) and item.get("key")],
            "score": _float_or_none(public.get("score")),
            "breakdown_importance": _float_or_none(breakdown.get("importance")),
            "breakdown_confirmation": _float_or_none(breakdown.get("confirmation")),
            "breakdown_mapping": _float_or_none(breakdown.get("mapping")),
            "breakdown_freshness": _float_or_none(breakdown.get("freshness")),
            "breakdown_heat": _float_or_none(breakdown.get("heat")),
            "validation_state": str(confirmation.get("state") or ""),
            "validation_label": str(confirmation.get("label") or ""),
            "validation_score": _float_or_none(confirmation.get("score")),
            "direction": str(public.get("direction") or ""),
            "importance": str(public.get("importance") or ""),
            "computed_at": computed_at,
            "trading_day": trading_day,
            "as_of": as_of,
        })
    if not rows:
        return empty_hot_event_rank()
    return pl.DataFrame(rows).cast(_SCHEMA)


def _float_or_none(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number
