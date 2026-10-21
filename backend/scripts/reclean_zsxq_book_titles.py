#!/usr/bin/env python
"""一次性：把知识星球正文里被洗空的书名号补回来。

入库时 ``<e style="book" />``（以及带 title 的 web 标签）曾被整段删掉，
``clean_text`` 里只剩「《》」。本脚本只处理 ``source='zsxq'`` 且正文含「《》」
的行。原文标签若还在 ``raw_json`` 的 content/text，或还在收件箱 JSON 里，
就按现在的清洗规则重写 ``clean_text`` / ``title`` / ``content_hash``。
标签已经不在库里的行不会改写。

不改表结构，不重抽提及。写盘前先做 sqlite 备份。默认会写盘；只看计划时加
``--dry-run``。脚本不会被调度调用，需要时手动执行。

用法（在 backend/ 目录）::

    uv run python -m scripts.reclean_zsxq_book_titles --dry-run
    uv run python -m scripts.reclean_zsxq_book_titles --db /path/to/news.sqlite
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.news.cleaning import clean_text, content_hash
from app.news.collectors import load_inbox_payload, parse_zsxq_payload

logger = logging.getLogger(__name__)

_TITLE_LIMIT = 180


@dataclass
class RecleanChange:
    item_id: int
    source_id: str
    old_clean: str
    new_clean: str
    old_title: str
    new_title: str
    content_hash: str


@dataclass
class RecleanResult:
    scanned: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_no_original: int = 0
    dry_run: bool = False
    backup: str = ""
    changes: list[RecleanChange] = field(default_factory=list)


def default_db_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "news" / "news.sqlite"


def _tagged(text: str) -> bool:
    return "<e" in (text or "").lower()


def _raw_texts(raw_json: str | None) -> tuple[str, str]:
    """raw_json 里若还留着带 ``<e>`` 的正文或标题，就拿来重洗。"""
    try:
        raw = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(raw, dict):
        return "", ""
    body = ""
    for key in ("content", "text"):
        value = raw.get(key)
        if isinstance(value, str) and _tagged(value):
            body = value
            break
    title = raw.get("title")
    title_text = title if isinstance(title, str) and _tagged(title) else ""
    return body, title_text


def index_inbox(inbox: Path | None) -> dict[str, tuple[str, str]]:
    """收件箱 topic_id -> (原文, 原标题)。只保留还带 ``<e>`` 的帖子。"""
    found: dict[str, tuple[str, str]] = {}
    if inbox is None or not inbox.is_dir():
        return found
    paths = [path for path in inbox.glob("*.json") if path.is_file()]
    failed = inbox / "failed"
    if failed.is_dir():
        paths.extend(path for path in failed.glob("*.json") if path.is_file())
    for path in paths:
        try:
            source, payload = load_inbox_payload(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            logger.debug("跳过无法读取的收件箱文件 %s", path.name)
            continue
        if source != "zsxq":
            continue
        try:
            items, _page = parse_zsxq_payload(payload)
        except (TypeError, ValueError):
            continue
        for item in items:
            if not _tagged(item.text) and not _tagged(item.title):
                continue
            found[item.source_id] = (item.text or "", item.title or "")
    return found


def plan_change(
    row: sqlite3.Row,
    inbox: dict[str, tuple[str, str]],
) -> RecleanChange | None:
    """有原文标签才产生变更。返回 None 表示无法还原或重洗后没有变化。"""
    clean = str(row["clean_text"] or "")
    title = str(row["title"] or "")
    raw_body, raw_title = _raw_texts(row["raw_json"])
    inbox_body, inbox_title = inbox.get(str(row["source_id"] or ""), ("", ""))
    body = next((item for item in (raw_body, inbox_body, clean) if _tagged(item)), "")
    title_src = next((item for item in (raw_title, inbox_title, title) if _tagged(item)), "")
    if not body and not title_src:
        return None
    new_clean = clean_text(body) if body else clean
    new_title = (clean_text(title_src) if title_src else title)[:_TITLE_LIMIT]
    if new_clean == clean and new_title == title:
        return None
    try:
        media = json.loads(row["media_ids"] or "[]")
    except json.JSONDecodeError:
        media = []
    if not isinstance(media, list):
        media = []
    media_ids = [str(item) for item in media if item]
    return RecleanChange(
        item_id=int(row["id"]),
        source_id=str(row["source_id"] or ""),
        old_clean=clean,
        new_clean=new_clean,
        old_title=title,
        new_title=new_title,
        content_hash=content_hash(new_clean, media_ids),
    )


def backup_sqlite(src: Path, dest_dir: Path) -> Path:
    """用 sqlite backup API 复制一份一致快照。失败时调用方不得写库。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    dest = dest_dir / f"{src.name}.{stamp}.bak"
    suffix = 1
    while dest.exists():
        dest = dest_dir / f"{src.name}.{stamp}.{suffix}.bak"
        suffix += 1
    src_conn = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    dst_conn = sqlite3.connect(dest)
    try:
        src_conn.backup(dst_conn)
    except sqlite3.Error:
        dest.unlink(missing_ok=True)
        raise
    finally:
        dst_conn.close()
        src_conn.close()
    return dest


def reclean_zsxq_book_titles(
    db: Path,
    *,
    dry_run: bool = False,
    inbox: Path | None = None,
    backup_dir: Path | None = None,
) -> RecleanResult:
    """重洗含「《》」的星球帖。``dry_run`` 只统计，不备份、不写盘。"""
    if not db.is_file():
        raise FileNotFoundError(db)
    inbox_index = index_inbox(inbox if inbox is not None else db.parent / "inbox")
    result = RecleanResult(dry_run=dry_run)
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(conn.execute(
            """
            SELECT id, source_id, title, clean_text, raw_json, media_ids
            FROM news_items
            WHERE source = 'zsxq' AND instr(ifnull(clean_text, ''), '《》') > 0
            ORDER BY id
            """,
        ))
    finally:
        conn.close()

    result.scanned = len(rows)
    planned: list[RecleanChange] = []
    for row in rows:
        change = plan_change(row, inbox_index)
        if change is None:
            raw_body, raw_title = _raw_texts(row["raw_json"])
            inbox_body, inbox_title = inbox_index.get(str(row["source_id"] or ""), ("", ""))
            if not any(_tagged(item) for item in (
                raw_body, raw_title, inbox_body, inbox_title, str(row["clean_text"] or ""),
                str(row["title"] or ""),
            )):
                result.skipped_no_original += 1
            else:
                result.unchanged += 1
            continue
        planned.append(change)
    result.changes = planned
    result.updated = len(planned)
    if dry_run or not planned:
        return result

    dest_dir = backup_dir or (db.parent / "backup")
    backup = backup_sqlite(db, dest_dir)
    result.backup = str(backup)
    writer = sqlite3.connect(db, timeout=30)
    try:
        writer.execute("BEGIN")
        written = 0
        for change in planned:
            cur = writer.execute(
                """
                UPDATE news_items
                SET clean_text = ?, title = ?, content_hash = ?
                WHERE id = ? AND source = 'zsxq'
                  AND instr(ifnull(clean_text, ''), '《》') > 0
                """,
                (
                    change.new_clean,
                    change.new_title,
                    change.content_hash,
                    change.item_id,
                ),
            )
            written += cur.rowcount
        writer.commit()
    except sqlite3.Error:
        writer.rollback()
        raise
    finally:
        writer.close()
    result.updated = written
    return result


def _brief(text: str, limit: int = 72) -> str:
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)] + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重洗知识星球正文中的空书名号")
    parser.add_argument("--db", type=Path, default=None, help="news.sqlite 路径")
    parser.add_argument("--inbox", type=Path, default=None, help="收件箱目录，默认同库旁的 inbox")
    parser.add_argument("--backup-dir", type=Path, default=None, help="备份目录，默认 <db>/../backup")
    parser.add_argument("--dry-run", action="store_true", help="只扫描，不备份、不写盘")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = args.db or default_db_path()
    try:
        result = reclean_zsxq_book_titles(
            db,
            dry_run=args.dry_run,
            inbox=args.inbox,
            backup_dir=args.backup_dir,
        )
    except FileNotFoundError:
        logger.error("找不到数据库: %s", db)
        return 1
    mode = "dry-run" if result.dry_run else "write"
    logger.info(
        "[%s] 扫描 %s 行，可更新 %s，无原文跳过 %s，重洗后无变化 %s",
        mode,
        result.scanned,
        result.updated,
        result.skipped_no_original,
        result.unchanged,
    )
    if result.backup:
        logger.info("已备份到 %s", result.backup)
    for change in result.changes:
        logger.info(
            "id=%s source_id=%s 《》 -> %s",
            change.item_id,
            change.source_id,
            _brief(change.new_clean),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
