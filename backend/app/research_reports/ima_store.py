"""ima 券商研报全文。``data/research/ima_reports.sqlite``。

和东财研报 parquet 分开：那边只有标题和 PDF 地址，个股列表会整段读进内存。
长正文放在这里，按 media_id 去重，按代码查提及个股。热门事件不读这张表。
没有识别出股票代码的全文也留在这张表里，标签是「行业/综合」，不写 mentions。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

from app.news.extract import code6_of
from app.services.fs_utils import atomic_write_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    media_id TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    publish_date TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_note TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    tag TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ima_reports_date ON reports(publish_date);
CREATE TABLE IF NOT EXISTS mentions (
    media_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    code TEXT NOT NULL,
    PRIMARY KEY (media_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_ima_mentions_code ON mentions(code);
"""

_lock = threading.Lock()
_BODY_API_LIMIT = 8000
RETENTION_DAYS = 60
# 没有个股映射的研报用这个标签，搜索能命中，个股页不会把它算成某只股票。
INDUSTRY_TAG = "行业/综合"


def db_path(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "ima_reports.sqlite"


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "ima_state.json"


def _connect(data_dir: Path) -> sqlite3.Connection:
    path = db_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _ensure_tag(conn)
    return conn


def _ensure_tag(conn: sqlite3.Connection) -> None:
    """旧库没有 tag 列。补上之后，把没有 mentions 的历史全文标成行业/综合。"""
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(reports)")}
    if "tag" in cols:
        return
    try:
        conn.execute("ALTER TABLE reports ADD COLUMN tag TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(reports)")}
        if "tag" in cols:
            return
        raise
    conn.execute(
        """
        UPDATE reports
        SET tag = ?
        WHERE media_id NOT IN (SELECT media_id FROM mentions)
        """,
        (INDUSTRY_TAG,),
    )
    conn.commit()


def known_ids(data_dir: Path, media_ids: list[str]) -> set[str]:
    wanted = [item for item in media_ids if item]
    found: set[str] = set()
    if not wanted or not db_path(data_dir).exists():
        return found
    with _lock:
        conn = _connect(data_dir)
        try:
            for offset in range(0, len(wanted), 400):
                chunk = wanted[offset:offset + 400]
                marks = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT media_id FROM reports WHERE media_id IN ({marks})",
                    chunk,
                )
                found.update(str(row["media_id"]) for row in rows)
        finally:
            conn.close()
    return found


def insert_report(data_dir: Path, row: dict) -> bool:
    """新 media_id 返回 True。已有的不覆盖，调用方因此不必再下载。"""
    media_id = str(row.get("media_id") or "").strip()
    if not media_id:
        return False
    symbols = []
    seen: set[str] = set()
    for item in row.get("symbols") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append((
            media_id,
            symbol[:32],
            str(item.get("name") or "")[:80],
            str(item.get("code") or "")[:16],
        ))
    tag = str(row.get("tag") or "").strip()[:40]
    if not symbols and not tag:
        tag = INDUSTRY_TAG
    with _lock:
        conn = _connect(data_dir)
        try:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO reports (
                    media_id, broker, publish_date, title, body, summary,
                    source_note, ingested_at, tag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    media_id,
                    str(row.get("broker") or "")[:80],
                    str(row.get("publish_date") or ""),
                    str(row.get("title") or "")[:180],
                    str(row.get("body") or ""),
                    str(row.get("summary") or "")[:200],
                    str(row.get("source_note") or "")[:500],
                    str(row.get("ingested_at") or ""),
                    tag,
                ),
            )
            inserted = cur.rowcount == 1
            if inserted and symbols:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO mentions (media_id, symbol, name, code)
                    VALUES (?, ?, ?, ?)
                    """,
                    symbols,
                )
            conn.commit()
            return inserted
        finally:
            conn.close()


def apply_retention(data_dir: Path, today: date, *, days: int = RETENTION_DAYS) -> int:
    """发布日早于保留窗口的全文删掉。窗口按自然日，默认 60 天。"""
    if not db_path(data_dir).exists():
        return 0
    cutoff = (today - timedelta(days=days)).isoformat()
    with _lock:
        conn = _connect(data_dir)
        try:
            conn.execute(
                """
                DELETE FROM mentions WHERE media_id IN (
                    SELECT media_id FROM reports WHERE publish_date < ?
                )
                """,
                (cutoff,),
            )
            cur = conn.execute("DELETE FROM reports WHERE publish_date < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()


def for_code(data_dir: Path, code: str, *, limit: int = 20) -> list[dict]:
    """一只股票最近的研报。没有库文件时返回空列表，不创建文件。"""
    wanted = code6_of(code)
    if not wanted or not db_path(data_dir).exists():
        return []
    with _lock:
        conn = _connect(data_dir)
        try:
            rows = conn.execute(
                """
                SELECT r.media_id, r.broker, r.publish_date, r.title, r.body,
                       r.summary, r.source_note, r.tag
                FROM reports r
                JOIN mentions m ON m.media_id = r.media_id
                WHERE m.code = ?
                GROUP BY r.media_id
                ORDER BY r.publish_date DESC, r.ingested_at DESC
                LIMIT ?
                """,
                (wanted, int(limit)),
            ).fetchall()
            found = []
            for row in rows:
                symbols = conn.execute(
                    """
                    SELECT symbol, name, code FROM mentions
                    WHERE media_id = ? ORDER BY symbol
                    """,
                    (row["media_id"],),
                ).fetchall()
                found.append(_public(row, symbols))
            return found
        finally:
            conn.close()


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search(data_dir: Path, query: str, *, limit: int = 20) -> list[dict]:
    """按标题、摘要、正文、券商和标签检索。没有个股映射的行业/综合研报也能命中。"""
    text = str(query or "").strip()[:80]
    if not text or not db_path(data_dir).exists():
        return []
    capped = min(50, max(1, int(limit)))
    pattern = _like_pattern(text)
    with _lock:
        conn = _connect(data_dir)
        try:
            rows = conn.execute(
                """
                SELECT media_id, broker, publish_date, title, body, summary,
                       source_note, tag
                FROM reports
                WHERE title LIKE ? ESCAPE '\\'
                   OR summary LIKE ? ESCAPE '\\'
                   OR body LIKE ? ESCAPE '\\'
                   OR broker LIKE ? ESCAPE '\\'
                   OR tag LIKE ? ESCAPE '\\'
                ORDER BY publish_date DESC, ingested_at DESC
                LIMIT ?
                """,
                (pattern, pattern, pattern, pattern, pattern, capped),
            ).fetchall()
            found = []
            for row in rows:
                symbols = conn.execute(
                    """
                    SELECT symbol, name, code FROM mentions
                    WHERE media_id = ? ORDER BY symbol
                    """,
                    (row["media_id"],),
                ).fetchall()
                found.append(_public(row, symbols))
            return found
        finally:
            conn.close()


def context_lines(data_dir: Path, symbol: str, *, limit: int = 3) -> list[str]:
    lines = []
    for row in for_code(data_dir, symbol, limit=limit):
        text = f"{row['publish_date']} {row['broker']} {row['title']}"
        summary = str(row.get("summary") or "").strip()
        if summary:
            text += f"：{summary}"
        lines.append(text)
    return lines


def load_run_state(data_dir: Path) -> dict:
    path = state_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def patch_run_state(data_dir: Path, **fields) -> dict:
    state = load_run_state(data_dir)
    state.update(fields)
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2))
    return state


def health_extra(data_dir: Path) -> dict | None:
    state = load_run_state(data_dir)
    if "last_inserted" not in state:
        return None
    try:
        count = int(state["last_inserted"])
    except (TypeError, ValueError):
        return None
    return {"last_inserted": count}


def _public(row: sqlite3.Row, symbols: list[sqlite3.Row]) -> dict:
    body = str(row["body"] or "")
    if len(body) > _BODY_API_LIMIT:
        body = body[:_BODY_API_LIMIT - 1].rstrip() + "…"
    return {
        "media_id": row["media_id"],
        "broker": row["broker"] or "",
        "publish_date": row["publish_date"] or "",
        "title": row["title"] or "",
        "summary": row["summary"] or "",
        "body": body,
        "source_note": row["source_note"] or "",
        "tag": str(row["tag"] or ""),
        "symbols": [
            {"symbol": item["symbol"], "name": item["name"] or "", "code": item["code"] or ""}
            for item in symbols
        ],
    }
