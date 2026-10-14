"""资讯轮询线程。交易时段密、盘后疏，不占 API 事件循环。热门事件在另一条后台线程里算。"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from datetime import time as dt_time

from app.market_time import cn_now
from app.news.collectors import (
    FOREIGN_RSS_SOURCES,
    FOREIGN_SLOW_SOURCES,
    GLH_BACKOFF_CAP,
    GLH_POLL_SECONDS,
    REDDIT_MIN_GAP,
)
from app.news.config import BACKGROUND_SOURCES, SOURCE_ORDER, source_enabled
from app.news.etf_flow import etf_wait_seconds
from app.news.hot_snapshot import HOT_REFRESH_SECONDS
from app.news.service import backfill_mentions, collect_inbox, get_lexicon, run_due

logger = logging.getLogger(__name__)


def outbound_paused(source: str) -> bool:
    """格隆汇默认打开。测试进程不因此请求 gelonghui.com。"""
    return source == "glh" and bool(os.environ.get("PYTEST_CURRENT_TEST"))


def interval_seconds(source: str, now: datetime | None = None) -> int:
    now = now or cn_now()
    clock = now.timetz().replace(tzinfo=None)
    weekday = now.weekday() < 5
    trading = weekday and dt_time(9, 15) <= clock <= dt_time(15, 5)
    daytime = dt_time(8, 0) <= clock <= dt_time(22, 0)
    if source == "glh":
        return GLH_POLL_SECONDS
    if source in {"cls", "wscn"}:
        if trading:
            return 45
        return 300 if daytime else 1800
    if source in {"dws", "zsxq"}:
        if trading:
            return 300
        return 900 if daytime else 1800
    if source == "ima":
        return 6 * 3600
    if source == "reddit":
        return REDDIT_MIN_GAP
    if source in FOREIGN_SLOW_SOURCES:
        return 900
    if source in FOREIGN_RSS_SOURCES:
        return 300
    if source == "sec":
        return 180
    return 600


def next_delay(source: str, result: dict | None = None, now: datetime | None = None) -> int:
    """Reddit 和格隆汇用采集返回的 retry_after。其余来源仍走固定间隔。"""
    if source == "reddit":
        raw = (result or {}).get("retry_after")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
            return min(raw, 3600)
        return REDDIT_MIN_GAP
    if source == "glh":
        raw = (result or {}).get("retry_after")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
            return min(raw, GLH_BACKOFF_CAP)
        return GLH_POLL_SECONDS
    return interval_seconds(source, now)


class NewsScheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hot_thread: threading.Thread | None = None
        self._next: dict[str, float] = {}

    def start(self) -> None:
        collector_alive = self._thread is not None and self._thread.is_alive()
        hot_alive = self._hot_thread is not None and self._hot_thread.is_alive()
        if collector_alive and hot_alive:
            return
        self._stop.clear()
        if not hot_alive:
            from app.news.hot_snapshot import load_persisted_snapshot

            try:
                load_persisted_snapshot()
            except Exception as exc:  # noqa: BLE001
                logger.warning("热门事件快照读取失败: %s", exc)
            self._hot_thread = threading.Thread(
                target=self._hot_loop, name="news-hot-events", daemon=True,
            )
            self._hot_thread.start()
        if not collector_alive:
            self._thread = threading.Thread(target=self._loop, name="news-collector", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._hot_thread:
            self._hot_thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = cn_now()
            stamp = now.timestamp()
            for source in SOURCE_ORDER:
                if (
                    outbound_paused(source)
                    or source in BACKGROUND_SOURCES
                    or not source_enabled(source)
                ):
                    continue
                due = self._next.get(source, 0)
                if stamp < due:
                    continue
                try:
                    if source in {"dws", "zsxq"}:
                        collect_inbox()
                        self._next["dws"] = stamp + interval_seconds("dws", now)
                        self._next["zsxq"] = stamp + interval_seconds("zsxq", now)
                    elif source == "etf_flow":
                        # 申赎稿在交易日次日发布。pending 时 07:35–09:00 每 15 分钟，之后到 15:00 每 30 分钟。
                        result = run_due(source) or {}
                        self._next[source] = stamp + etf_wait_seconds(
                            now, pending=bool(result.get("pending")),
                        )
                    elif source in {"reddit", "glh"}:
                        result = run_due(source) or {}
                        self._next[source] = stamp + next_delay(source, result, now)
                    else:
                        run_due(source)
                        self._next[source] = stamp + interval_seconds(source, now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("资讯轮询 %s 失败: %s", source, exc)
                    self._next[source] = stamp + 120
            if int(stamp) % 3600 < 30:
                try:
                    backfill_mentions(get_lexicon())
                except Exception as exc:  # noqa: BLE001
                    logger.debug("补抽提及失败: %s", exc)
            try:
                from app.news.push import tick
                tick(now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("钉钉推送失败: %s", exc)
            self._stop.wait(15)

    def _hot_loop(self) -> None:
        from app.news.hot_snapshot import refresh_hot_events_sync

        while not self._stop.is_set():
            try:
                refresh_hot_events_sync()
            except Exception as exc:  # noqa: BLE001
                logger.warning("热门事件快照计算失败: %s", exc)
            self._stop.wait(HOT_REFRESH_SECONDS)
