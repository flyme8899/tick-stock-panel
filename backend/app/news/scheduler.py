"""资讯轮询线程。交易时段密、盘后疏，单线程，不占 API 事件循环。"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from datetime import time as dt_time

from app.market_time import cn_now
from app.news.collectors import FOREIGN_RSS_SOURCES
from app.news.config import SOURCE_ORDER, source_enabled
from app.news.etf_flow import etf_wait_seconds
from app.news.service import backfill_mentions, collect_inbox, get_lexicon, run_due

logger = logging.getLogger(__name__)


def interval_seconds(source: str, now: datetime | None = None) -> int:
    now = now or cn_now()
    clock = now.timetz().replace(tzinfo=None)
    weekday = now.weekday() < 5
    trading = weekday and dt_time(9, 15) <= clock <= dt_time(15, 5)
    daytime = dt_time(8, 0) <= clock <= dt_time(22, 0)
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
    if source in FOREIGN_RSS_SOURCES:
        return 300
    if source == "sec":
        return 180
    return 600


class NewsScheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next: dict[str, float] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="news-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = cn_now()
            stamp = now.timestamp()
            for source in SOURCE_ORDER:
                if not source_enabled(source):
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
                        # 申赎稿在交易日次日早晨发布，周五的在周六。pending 才在窗口里继续等。
                        result = run_due(source) or {}
                        self._next[source] = stamp + etf_wait_seconds(
                            now, pending=bool(result.get("pending")),
                        )
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
