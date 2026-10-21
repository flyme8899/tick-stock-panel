"""一次性重洗脚本：只处理 source=zsxq 且正文含「《》」的帖子。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.market_time import CN_TZ
from app.news.cleaning import content_hash
from app.news.store import NewsStore
from scripts.reclean_zsxq_book_titles import reclean_zsxq_book_titles

_BOOK = '<e type="web" href="https://example.test/book" title="%E4%B8%89%E4%BD%93" style="book" />'


def _insert(path: Path, **kwargs) -> None:
    store = NewsStore(path)
    try:
        store.insert_item(
            published_at=datetime(2026, 10, 16, 15, 40, tzinfo=CN_TZ),
            author="星球",
            url="",
            level="",
            media_ids=kwargs.pop("media_ids", []),
            extra={},
            mentions=[],
            **kwargs,
        )
    finally:
        store.close()


def _text(path: Path, source_id: str) -> tuple[str, str, str]:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT title, clean_text, content_hash FROM news_items WHERE source_id = ?",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0], row[1], row[2]


def test_dry_run_does_not_write_or_backup(tmp_path: Path) -> None:
    db = tmp_path / "news.sqlite"
    original = f"以及《{_BOOK}》。；OpenAI发布新模型"
    _insert(
        db,
        source="zsxq",
        source_id="34951",
        title=f"复盘《{_BOOK}》",
        clean_text="以及《》。；OpenAI发布新模型",
        raw={"content": original, "title": f"复盘《{_BOOK}》"},
        content_hash="old-34951",
        media_ids=["m1"],
    )
    _insert(
        db,
        source="zsxq",
        source_id="34088",
        title="复盘笔记",
        clean_text="见《》",
        raw={"group_name": "纳指星球调研"},
        content_hash="old-34088",
    )
    _insert(
        db,
        source="cls",
        source_id="cls-1",
        title="快讯",
        clean_text="其它来源的《》不动",
        raw={},
        content_hash="old-cls",
    )
    backup_dir = tmp_path / "backup"
    preview = reclean_zsxq_book_titles(db, dry_run=True, backup_dir=backup_dir, inbox=tmp_path / "missing-inbox")
    assert preview.scanned == 2
    assert preview.updated == 1
    assert preview.skipped_no_original == 1
    assert preview.backup == ""
    assert not backup_dir.exists()
    assert _text(db, "34951")[1] == "以及《》。；OpenAI发布新模型"

    result = reclean_zsxq_book_titles(db, dry_run=False, backup_dir=backup_dir, inbox=tmp_path / "missing-inbox")
    assert result.updated == 1
    assert result.backup
    title, clean, digest = _text(db, "34951")
    assert title == "复盘《三体》"
    assert clean == "以及《三体》。；OpenAI发布新模型"
    assert "《》" not in clean
    assert digest == content_hash(clean, ["m1"])
    assert _text(db, "34088")[1] == "见《》"
    assert _text(db, "cls-1")[1] == "其它来源的《》不动"

    bak = sqlite3.connect(result.backup)
    try:
        old = bak.execute(
            "SELECT clean_text FROM news_items WHERE source_id = '34951'",
        ).fetchone()
    finally:
        bak.close()
    assert old[0] == "以及《》。；OpenAI发布新模型"

    again = reclean_zsxq_book_titles(db, dry_run=False, backup_dir=backup_dir)
    assert again.updated == 0
    assert again.skipped_no_original == 1
    assert again.backup == ""
    assert _text(db, "34951")[1] == clean


def test_inbox_original_is_used_when_raw_has_no_tag(tmp_path: Path) -> None:
    db = tmp_path / "news.sqlite"
    _insert(
        db,
        source="zsxq",
        source_id="34088",
        title="复盘笔记：端侧AI",
        clean_text="见《》还没还原",
        raw={"topic_id": "34088"},
        content_hash="old",
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    payload = {
        "source": "zsxq",
        "payload": {
            "topics": [{
                "topic_id": "34088",
                "title": "复盘笔记：端侧AI",
                "content": '见《<e type="web" title="%E6%B4%BB%E7%9D%80" style="book" />》还没还原',
                "create_time": "2026-10-16T18:00:00+08:00",
                "owner": {"name": "星球"},
            }],
        },
    }
    (inbox / "zsxq-34088.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = reclean_zsxq_book_titles(db, dry_run=False, inbox=inbox, backup_dir=tmp_path / "backup")
    assert result.updated == 1
    title, clean, _digest = _text(db, "34088")
    assert title == "复盘笔记：端侧AI"
    assert clean == "见《活着》还没还原"
    assert Path(result.backup).is_file()
