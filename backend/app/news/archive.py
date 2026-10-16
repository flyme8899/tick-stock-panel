"""历史资讯归档库。

和 ``news.sqlite`` 分开：不做 45 天清理，只存快讯本身，不存提及。
只给查询和回测用，推送、热点、提醒都不读这里。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

from app.market_time import CN_TZ

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_archive (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT '',
    stock_codes TEXT NOT NULL DEFAULT '[]',
    url TEXT NOT NULL DEFAULT '',
    ingested_at TEXT NOT NULL,
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_archive_time ON news_archive(published_at);
CREATE INDEX IF NOT EXISTS idx_archive_source ON news_archive(source, published_at);
CREATE INDEX IF NOT EXISTS idx_archive_level ON news_archive(level);
"""

# 实时采集里会顺手抄一份进归档的来源。东财 7×24 目前只有历史导入。
LIVE_ARCHIVE_SOURCES = frozenset({"cls", "eastmoney_724"})
MAX_LIMIT = 500


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=CN_TZ)
    return value.astimezone(CN_TZ).isoformat(timespec="seconds")


class NewsArchive:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def insert_many(self, rows: Iterable[dict]) -> dict[str, int]:
        """INSERT OR IGNORE。同一来源的 source_id 只留第一份，不覆盖。"""
        now = datetime.now(CN_TZ).isoformat(timespec="seconds")
        batch = []
        for row in rows:
            sid = str(row.get("source_id") or "").strip()
            source = str(row.get("source") or "").strip()
            published = row.get("published_at")
            if not sid or not source or published is None:
                continue
            codes = row.get("stock_codes") or []
            batch.append((
                source[:32],
                sid[:64],
                _iso(published) if isinstance(published, datetime) else str(published),
                str(row.get("title") or "")[:300],
                str(row.get("content") or ""),
                str(row.get("level") or "")[:16],
                json.dumps([str(c) for c in codes], ensure_ascii=False),
                str(row.get("url") or "")[:500],
                now,
            ))
        if not batch:
            return {"inserted": 0, "duplicate": 0}
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO news_archive (
                    source, source_id, published_at, title, content, level,
                    stock_codes, url, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            self._conn.commit()
            inserted = self._conn.total_changes - before
        return {"inserted": inserted, "duplicate": len(batch) - inserted}

    def query(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        source: str | None = None,
        levels: list[str] | None = None,
        keyword: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        where, args = [], []
        if start is not None:
            where.append("published_at >= ?")
            args.append(_iso(start))
        if end is not None:
            where.append("published_at < ?")
            args.append(_iso(end))
        if source:
            where.append("source = ?")
            args.append(source)
        if levels:
            where.append(f"level IN ({','.join('?' * len(levels))})")
            args.extend(levels)
        if keyword:
            # LIKE 的通配符按字面量处理。
            esc = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')")
            args.extend([f"%{esc}%", f"%{esc}%"])
        sql = (
            "SELECT source, source_id, published_at, title, content, level, stock_codes, url "
            "FROM news_archive"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        args.extend([max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))])
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["stock_codes"] = json.loads(item["stock_codes"] or "[]")
            except ValueError:
                item["stock_codes"] = []
            out.append(item)
        return out

    def stats(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) AS n, MIN(published_at) AS first, MAX(published_at) AS last "
                "FROM news_archive GROUP BY source ORDER BY source"
            ).fetchall()
        return [dict(r) for r in rows]


_ARCHIVE: NewsArchive | None = None
_ARCHIVE_LOCK = threading.Lock()


def default_path() -> Path:
    from app.config import settings

    return Path(settings.data_dir) / "news_archive.sqlite"


def get_archive() -> NewsArchive:
    global _ARCHIVE
    with _ARCHIVE_LOCK:
        if _ARCHIVE is None:
            _ARCHIVE = NewsArchive(default_path())
        return _ARCHIVE


def reset_archive_for_tests(path: Path | None = None) -> NewsArchive:
    global _ARCHIVE
    with _ARCHIVE_LOCK:
        if _ARCHIVE is not None:
            _ARCHIVE.close()
        _ARCHIVE = NewsArchive(path or default_path())
        return _ARCHIVE


def archive_live_items(rows: list[dict]) -> None:
    """实时采集入库后抄一份。归档出错只记日志，不影响主流程。"""
    rows = [r for r in rows if r.get("source") in LIVE_ARCHIVE_SOURCES]
    if not rows:
        return
    try:
        get_archive().insert_many(rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("资讯归档写入失败: %s", exc)
