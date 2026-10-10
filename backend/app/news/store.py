"""资讯 SQLite。单文件、短保留期，避免在 8G 机器上堆全文。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from app.market_time import CN_TZ

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    author TEXT,
    title TEXT,
    clean_text TEXT NOT NULL,
    raw_json TEXT,
    content_hash TEXT NOT NULL,
    url TEXT,
    level TEXT,
    media_ids TEXT,
    extra_json TEXT,
    ingested_at TEXT NOT NULL,
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_news_hash ON news_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_news_time ON news_items(published_at);
CREATE TABLE IF NOT EXISTS news_mentions (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    code TEXT,
    origin TEXT NOT NULL,
    UNIQUE(item_id, kind, key)
);
CREATE INDEX IF NOT EXISTS idx_mentions_key ON news_mentions(kind, key);
CREATE TABLE IF NOT EXISTS collector_state (
    source TEXT PRIMARY KEY,
    last_ok_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    auth_state TEXT,
    items_ingested INTEGER NOT NULL DEFAULT 0
);
"""

_RAW_LIMIT = 24_000
_RETENTION_DAYS = 45


class NewsStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def insert_item(
        self,
        *,
        source: str,
        source_id: str,
        published_at: datetime,
        author: str,
        title: str,
        clean_text: str,
        raw: dict | None,
        content_hash: str,
        url: str,
        level: str,
        media_ids: list[str],
        extra: dict | None,
        mentions: list[tuple[str, str, str, str, str]],
    ) -> str:
        """返回 inserted 或 duplicate。同一来源的 source_id 只保留第一份。"""
        now = datetime.now(CN_TZ).isoformat(timespec="seconds")
        raw_text = json.dumps(raw or {}, ensure_ascii=False)[:_RAW_LIMIT]
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO news_items (
                    source, source_id, published_at, author, title, clean_text,
                    raw_json, content_hash, url, level, media_ids, extra_json, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    source_id,
                    published_at.astimezone(CN_TZ).isoformat(timespec="seconds"),
                    author[:80],
                    title[:180],
                    clean_text,
                    raw_text,
                    content_hash,
                    url[:500],
                    level[:16],
                    json.dumps(media_ids, ensure_ascii=False),
                    json.dumps(extra or {}, ensure_ascii=False)[:_RAW_LIMIT],
                    now,
                ),
            )
            if cur.rowcount == 0:
                self._conn.commit()
                return "duplicate"
            item_id = cur.lastrowid
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO news_mentions (item_id, kind, key, name, code, origin)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (item_id, kind, key, name[:80], code[:16], origin[:16])
                    for kind, key, name, code, origin in mentions
                ],
            )
            self._conn.execute(
                """
                INSERT INTO collector_state (source, items_ingested)
                VALUES (?, 1)
                ON CONFLICT(source) DO UPDATE SET
                    items_ingested = items_ingested + 1
                """,
                (source,),
            )
            self._conn.commit()
            return "inserted"

    def existing_ids(self, source: str, source_ids: list[str]) -> set[str]:
        """已经入库的 source_id。调用方据此跳过抽取，避免重复消耗额度。"""
        wanted = [item for item in source_ids if item]
        found: set[str] = set()
        if not wanted:
            return found
        with self._lock:
            for offset in range(0, len(wanted), 400):
                chunk = wanted[offset:offset + 400]
                marks = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT source_id FROM news_items WHERE source = ? AND source_id IN ({marks})",
                    (source, *chunk),
                )
                found.update(str(row["source_id"]) for row in rows)
        return found

    def mark_health(self, source: str, *, ok: bool, error: str = "", auth_state: str = "") -> None:
        now = datetime.now(CN_TZ).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO collector_state (source, last_ok_at, last_error, last_error_at, auth_state, items_ingested)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(source) DO UPDATE SET
                    last_ok_at = COALESCE(excluded.last_ok_at, collector_state.last_ok_at),
                    last_error = excluded.last_error,
                    last_error_at = excluded.last_error_at,
                    auth_state = CASE WHEN excluded.auth_state != '' THEN excluded.auth_state
                                      ELSE collector_state.auth_state END
                """,
                (
                    source,
                    now if ok else None,
                    "" if ok else error[:300],
                    None if ok else now,
                    auth_state,
                ),
            )
            self._conn.commit()

    def health_rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM collector_state"))

    def apply_retention(self, days: int = _RETENTION_DAYS) -> int:
        cutoff = (datetime.now(CN_TZ) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute("DELETE FROM news_items WHERE published_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def mention_events_since(self, start: datetime) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                """
                SELECT m.kind, m.key, m.name, m.origin, i.source, i.content_hash, i.published_at
                FROM news_mentions m
                JOIN news_items i ON i.id = m.item_id
                WHERE i.published_at >= ?
                """,
                (start.astimezone(CN_TZ).isoformat(timespec="seconds"),),
            ))

    def messages_for(
        self,
        *,
        kind: str,
        key: str,
        start: datetime,
        limit: int,
    ) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                """
                SELECT i.source, i.source_id, i.published_at, i.author, i.title,
                       i.clean_text, i.url, i.level, i.content_hash
                FROM news_mentions m
                JOIN news_items i ON i.id = m.item_id
                WHERE m.kind = ? AND m.key = ? AND i.published_at >= ?
                ORDER BY i.published_at DESC
                LIMIT ?
                """,
                (kind, key, start.astimezone(CN_TZ).isoformat(timespec="seconds"), limit),
            ))

    def items_for_symbol(self, symbols: list[str], start: datetime, limit: int) -> list[sqlite3.Row]:
        if not symbols:
            return []
        marks = ",".join("?" for _ in symbols)
        with self._lock:
            return list(self._conn.execute(
                f"""
                SELECT DISTINCT i.source, i.published_at, i.author, i.title,
                       i.clean_text, i.url, i.level
                FROM news_mentions m
                JOIN news_items i ON i.id = m.item_id
                WHERE m.kind = 'stock' AND m.key IN ({marks}) AND i.published_at >= ?
                ORDER BY i.published_at DESC
                LIMIT ?
                """,
                (*symbols, start.astimezone(CN_TZ).isoformat(timespec="seconds"), limit),
            ))

    def recent_for_feed(self, source: str, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            rows = list(self._conn.execute(
                """
                SELECT id, source_id, published_at, title, clean_text, url
                FROM news_items
                WHERE source = ?
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (source, limit),
            ))
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            marks = ",".join("?" for _ in ids)
            mentions = list(self._conn.execute(
                f"SELECT item_id, kind, key FROM news_mentions WHERE item_id IN ({marks}) ORDER BY id",
                ids,
            ))
        grouped: dict[int, list[sqlite3.Row]] = {}
        for mention in mentions:
            grouped.setdefault(mention["item_id"], []).append(mention)
        enriched = []
        for row in rows:
            item = dict(row)
            item["mentions"] = grouped.get(row["id"], [])
            enriched.append(item)
        return enriched

    def items_missing_mentions(self, start: datetime, limit: int) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                """
                SELECT i.id, i.title, i.clean_text, i.extra_json
                FROM news_items i
                LEFT JOIN news_mentions m ON m.item_id = i.id
                WHERE i.published_at >= ? AND m.id IS NULL AND length(i.clean_text) >= 12
                ORDER BY i.published_at DESC
                LIMIT ?
                """,
                (start.astimezone(CN_TZ).isoformat(timespec="seconds"), limit),
            ))

    def replace_mentions(self, item_id: int, mentions: list[tuple[str, str, str, str, str]]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM news_mentions WHERE item_id = ?", (item_id,))
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO news_mentions (item_id, kind, key, name, code, origin)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(item_id, k, key, name[:80], code[:16], origin[:16]) for k, key, name, code, origin in mentions],
            )
            self._conn.commit()
