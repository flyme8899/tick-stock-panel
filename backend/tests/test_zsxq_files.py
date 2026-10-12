"""知识星球 PDF 附件：只下 pdf、去重、超限、失败不挡文本，并写入研报库。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.market_time import CN_TZ
from app.news.collectors import parse_zsxq_payload
from app.news.config import zsxq_files_enabled
from app.news.extract import Lexicon
from app.news.host_collector import _run_zsxq, read_cursor, write_cursor
from app.news.zsxq_files import _signed_url, extract_pdf_text, files_dir, sync_zsxq_files
from app.research_reports.ima_store import for_code


@pytest.fixture(autouse=True)
def _files_enabled(monkeypatch):
    monkeypatch.setenv("NEWS_ZSXQ_FILES_ENABLED", "true")


SIGNED = "https://files.zsxq.com/Ft/signature-token?e=1"
PDF_TEXT = "Guizhou Moutai research note"
LONG_TEXT = ("A" * 180) + "ENDMARKER"
MOMENT = datetime(2026, 10, 9, 12, 0, tzinfo=CN_TZ)


class Proc:
    def __init__(self, code: int, stdout: str, stderr: str = ""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def _pdf_bytes(text: str) -> bytes:
    stream = b"BT /F1 18 Tf 36 120 Td (" + text.encode("ascii") + b") Tj ET"
    parts = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        ),
        (
            f"4 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream\nendobj\n"
        ),
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    header = b"%PDF-1.4\n"
    offset = len(header)
    offsets: list[int] = []
    body = b""
    for part in parts:
        offsets.append(offset)
        body += part
        offset += len(part)
    xref = [b"xref\n0 6\n", b"0000000000 65535 f \n"]
    xref.extend(f"{item:010d} 00000 n \n".encode("ascii") for item in offsets)
    trailer = f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{offset}\n%%EOF\n".encode("ascii")
    return header + body + b"".join(xref) + trailer


def _file(file_id: str, name: str, *, size: int = 1200, digest: str = "hash-a") -> dict:
    return {
        "file_id": file_id,
        "name": name,
        "size": size,
        "hash": digest,
        "download_url": SIGNED,
    }


def _topic(files: list[dict], *, topic_id: str = "topic-1", content: str = "帖子正文") -> dict:
    return {
        "topic_id": topic_id,
        "title": "晨会纪要",
        "content": content,
        "create_time": "2026-10-09T10:00:00+08:00",
        "owner": {"name": "作者"},
        "group_id": "2002",
        "group_name": "每日投研",
        "files": files,
    }


def _assert_no_secret(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix == ".pdf":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "signature-token" not in text
        assert "files.zsxq.com" not in text
        assert SIGNED not in text


def _rows(data_dir: Path) -> list[sqlite3.Row]:
    path = data_dir / "research" / "ima_reports.sqlite"
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM reports ORDER BY media_id"))
    finally:
        conn.close()


def _patch_host(monkeypatch) -> None:
    monkeypatch.setattr("app.news.host_collector.cn_now", lambda: MOMENT)
    monkeypatch.setattr("app.news.zsxq_files.cn_now", lambda: MOMENT)
    monkeypatch.setattr(
        "app.news.host_collector.which",
        lambda name: "/bin/zsxq-cli" if name == "zsxq-cli" else None,
    )
    monkeypatch.setattr(
        "app.news.host_collector.zsxq_groups",
        lambda: [("2002", "每日投研")],
    )
    monkeypatch.setenv("NEWS_ZSXQ_FILES_GAP_MS", "0")
    monkeypatch.setenv("NEWS_ZSXQ_FILES_ENABLED", "true")
    lexicon = Lexicon([("600519.SH", "贵州茅台", "600519")], [])
    monkeypatch.setattr("app.news.zsxq_files._load_lexicon", lambda: lexicon)


def test_parse_keeps_file_meta_without_signed_url():
    items, _page = parse_zsxq_payload([_topic([
        _file("pdf1", "贵州茅台.pdf", digest="abc"),
        _file("audio1", "电话会.mp3"),
        {"file_id": "../x", "name": "坏.pdf", "download_url": SIGNED},
    ])])
    files = items[0].raw["files"]
    assert files == [
        {"file_id": "pdf1", "name": "贵州茅台.pdf", "size": 1200, "hash": "abc"},
        {"file_id": "audio1", "name": "电话会.mp3", "size": 1200, "hash": "hash-a"},
    ]
    assert SIGNED not in json.dumps(items[0].raw, ensure_ascii=False)


def test_extract_pdf_text_reads_minimal_pdf():
    assert PDF_TEXT in extract_pdf_text(_pdf_bytes(PDF_TEXT))


def test_only_pdf_is_downloaded_and_enters_research_library(tmp_path, monkeypatch):
    _patch_host(monkeypatch)
    calls: list[list[str]] = []
    pdf = _pdf_bytes(PDF_TEXT + LONG_TEXT)
    monkeypatch.setattr("app.news.zsxq_files._read_limited", lambda _url, _opener, _max: pdf)

    def run_ok(argv):
        calls.append(list(argv))
        if argv[1:3] == ["auth", "status"]:
            return Proc(0, "logged in")
        if argv[1:3] == ["group", "+topics"]:
            return Proc(0, json.dumps([_topic([
                _file("pdf1", "贵州茅台.pdf", digest="hash-pdf"),
                _file("mp3-1", "电话会.mp3", digest="hash-mp3"),
                _file("sheet1", "估值.xlsx", digest="hash-xlsx"),
            ])]))
        assert argv[1:6] == ["api", "raw", "--method", "GET", "--path"]
        return Proc(0, json.dumps({"succeeded": True, "resp_data": {"download_url": SIGNED}}))

    assert _run_zsxq(tmp_path, run_ok, "", "", "") == "ok"
    download_paths = [argv[-1] for argv in calls if argv[1:3] == ["api", "raw"]]
    assert download_paths == ["/v2/files/pdf1/download_url"]
    inbox = json.loads(next((tmp_path / "news" / "inbox").glob("zsxq-*.json")).read_text(encoding="utf-8"))
    topic = inbox["payload"][0]
    assert topic["content"] == "帖子正文"
    assert [item["name"] for item in topic["files"]] == ["贵州茅台.pdf", "电话会.mp3", "估值.xlsx"]
    assert PDF_TEXT not in json.dumps(topic, ensure_ascii=False)
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["media_id"] == "zsxq:pdf1"
    assert rows[0]["broker"] == "知识星球·每日投研"
    assert rows[0]["title"] == "贵州茅台"
    assert PDF_TEXT in rows[0]["body"]
    assert "ENDMARKER" in rows[0]["body"]
    assert "ENDMARKER" not in rows[0]["summary"]
    assert "ENDMARKER" not in json.dumps(topic, ensure_ascii=False)
    assert rows[0]["publish_date"] == "2026-10-09"
    assert "topic_id=topic-1" in rows[0]["source_note"]
    assert "2026-10-09T10:00:00+08:00" in rows[0]["source_note"]
    assert "http" not in rows[0]["source_note"]
    assert len(rows[0]["summary"]) <= 160
    found = for_code(tmp_path, "600519")
    assert found and found[0]["title"] == "贵州茅台"
    index = json.loads((files_dir(tmp_path) / "index.json").read_text(encoding="utf-8"))
    assert set(index["files"]) == {"pdf1"}
    assert index["files"]["pdf1"]["status"] == "ok"
    assert (files_dir(tmp_path) / "pdf1.pdf").is_file()
    _assert_no_secret(tmp_path)


def test_hash_and_file_id_are_not_downloaded_twice(tmp_path):
    pdf = _pdf_bytes(PDF_TEXT)
    calls: list[str] = []

    def run(argv):
        calls.append(argv[-1])
        return Proc(0, json.dumps({"download_url": SIGNED}))

    topics = [_topic([
        _file("pdf1", "贵州茅台.pdf", digest="same"),
        _file("pdf2", "贵州茅台副本.pdf", digest="same"),
    ])]
    lexicon = Lexicon([("600519.SH", "贵州茅台", "600519")], [])
    first = sync_zsxq_files(
        tmp_path, topics, run, "/bin/zsxq-cli",
        opener=lambda _url: pdf, sleep=lambda _delay: None, lexicon=lexicon, moment=MOMENT,
    )
    assert first["downloaded"] == 1
    assert first["inserted"] == 1
    assert calls == ["/v2/files/pdf1/download_url"]
    second = sync_zsxq_files(
        tmp_path, topics, run, "/bin/zsxq-cli",
        opener=lambda _url: pdf, sleep=lambda _delay: None, lexicon=lexicon, moment=MOMENT,
    )
    assert second["downloaded"] == 0
    assert second["inserted"] == 0
    assert calls == ["/v2/files/pdf1/download_url"]
    assert len(_rows(tmp_path)) == 1
    index = json.loads((files_dir(tmp_path) / "index.json").read_text(encoding="utf-8"))
    assert index["files"]["pdf2"]["status"] == "ok"
    assert index["files"]["pdf2"]["reason"] == "duplicate"


def test_oversize_pdf_is_skipped_without_download(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_ZSXQ_FILES_MAX_MB", "30")
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return Proc(0, json.dumps({"download_url": SIGNED}))

    summary = sync_zsxq_files(
        tmp_path,
        [_topic([_file("big1", "超大.pdf", size=31 * 1024 * 1024)])],
        run,
        "/bin/zsxq-cli",
        opener=lambda _url: _pdf_bytes(PDF_TEXT),
        sleep=lambda _delay: None,
        moment=MOMENT,
    )
    assert summary["downloaded"] == 0
    assert summary["skipped"] == 1
    assert calls == []
    assert _rows(tmp_path) == []
    index = json.loads((files_dir(tmp_path) / "index.json").read_text(encoding="utf-8"))
    assert index["files"]["big1"]["status"] == "skipped"
    assert index["files"]["big1"]["reason"] == "oversize"


def test_download_failure_does_not_block_text_and_retries(tmp_path, monkeypatch, caplog):
    _patch_host(monkeypatch)
    state = {"fail": True}
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv[1:3] == ["auth", "status"]:
            return Proc(0, "logged in")
        if argv[1:3] == ["group", "+topics"]:
            return Proc(0, json.dumps([_topic([_file("pdf1", "贵州茅台.pdf")])]))
        if state["fail"]:
            state["fail"] = False
            return Proc(1, SIGNED, "temporary")
        return Proc(0, json.dumps({"resp_data": {"download_url": SIGNED}}))

    monkeypatch.setattr(
        "app.news.zsxq_files._read_limited",
        lambda _url, _opener, _max: _pdf_bytes(PDF_TEXT),
    )
    with caplog.at_level(logging.WARNING):
        assert _run_zsxq(tmp_path, run, "", "", "") == "ok"
    inbox = next((tmp_path / "news" / "inbox").glob("zsxq-*.json"))
    payload = json.loads(inbox.read_text(encoding="utf-8"))
    assert payload["payload"][0]["content"] == "帖子正文"
    assert payload["payload"][0]["files"][0]["file_id"] == "pdf1"
    assert read_cursor(tmp_path, "zsxq:2002", "") == MOMENT.isoformat()
    assert _rows(tmp_path) == []
    index = json.loads((files_dir(tmp_path) / "index.json").read_text(encoding="utf-8"))
    assert index["files"]["pdf1"]["status"] == "failed"
    assert "signature-token" not in caplog.text
    assert "files.zsxq.com" not in caplog.text
    _assert_no_secret(tmp_path)

    assert _run_zsxq(tmp_path, run, "", "", "") == "ok"
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["broker"] == "知识星球·每日投研"
    assert PDF_TEXT in rows[0]["body"]
    assert read_cursor(tmp_path, "zsxq:2002", "") == MOMENT.isoformat()
    _assert_no_secret(tmp_path)


def test_opener_error_does_not_persist_signed_url(tmp_path, caplog):
    def run(argv):
        return Proc(0, json.dumps({"resp_data": {"download_url": SIGNED}}))

    def opener(url):
        raise OSError(f"get failed {url}")

    with caplog.at_level(logging.WARNING):
        summary = sync_zsxq_files(
            tmp_path,
            [_topic([_file("pdf1", "贵州茅台.pdf")])],
            run,
            "/bin/zsxq-cli",
            opener=opener,
            sleep=lambda _delay: None,
            lexicon=Lexicon([], []),
            moment=MOMENT,
        )
    assert summary["failed"] == 1
    assert summary["inserted"] == 0
    assert "signature-token" not in caplog.text
    _assert_no_secret(tmp_path)


def test_round_cap_and_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_ZSXQ_FILES_PER_ROUND", "2")
    monkeypatch.setenv("NEWS_ZSXQ_FILES_GAP_MS", "1000")
    clock = {"now": 0.0}
    slept: list[float] = []

    def run(argv):
        return Proc(0, json.dumps({"download_url": SIGNED}))

    topics = [_topic([
        _file("a", "一.pdf", digest="a"),
        _file("b", "二.pdf", digest="b"),
        _file("c", "三.pdf", digest="c"),
    ])]
    pdf = _pdf_bytes(PDF_TEXT)
    first = sync_zsxq_files(
        tmp_path, topics, run, "/bin/zsxq-cli",
        opener=lambda _url: pdf,
        sleep=lambda delay: slept.append(delay) or clock.__setitem__("now", clock["now"] + delay),
        clock=lambda: clock["now"],
        lexicon=Lexicon([], []),
        moment=MOMENT,
    )
    assert first["downloaded"] == 2
    assert slept == [1.0]
    index = json.loads((files_dir(tmp_path) / "index.json").read_text(encoding="utf-8"))
    assert index["files"]["c"]["status"] == "pending"
    second = sync_zsxq_files(
        tmp_path, [], run, "/bin/zsxq-cli",
        opener=lambda _url: pdf,
        sleep=lambda delay: slept.append(delay) or clock.__setitem__("now", clock["now"] + delay),
        clock=lambda: clock["now"],
        lexicon=Lexicon([], []),
        moment=MOMENT,
    )
    assert second["downloaded"] == 1
    assert slept == [1.0]
    assert len(_rows(tmp_path)) == 3


def test_disabled_switch_skips_download_but_keeps_inbox(tmp_path, monkeypatch):
    _patch_host(monkeypatch)
    monkeypatch.setenv("NEWS_ZSXQ_FILES_ENABLED", "false")
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv[1:3] == ["auth", "status"]:
            return Proc(0, "logged in")
        return Proc(0, json.dumps([_topic([_file("pdf1", "贵州茅台.pdf")])]))

    assert _run_zsxq(tmp_path, run, "", "", "") == "ok"
    assert [argv[1:3] for argv in calls] == [["auth", "status"], ["group", "+topics"]]
    inbox = json.loads(next((tmp_path / "news" / "inbox").glob("zsxq-*.json")).read_text(encoding="utf-8"))
    assert inbox["payload"][0]["files"][0]["name"] == "贵州茅台.pdf"
    assert not (files_dir(tmp_path) / "index.json").exists()
    assert _rows(tmp_path) == []


def test_files_switch_defaults_on(monkeypatch):
    monkeypatch.delenv("NEWS_ZSXQ_FILES_ENABLED", raising=False)
    from app.config import settings
    monkeypatch.setattr(settings, "news_zsxq_files_enabled", "")
    assert zsxq_files_enabled() is True


def test_delete_after_extract_and_age_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWS_ZSXQ_FILES_DELETE_AFTER", "true")
    pdf = _pdf_bytes(PDF_TEXT)

    def run(argv):
        return Proc(0, json.dumps({"download_url": SIGNED}))

    sync_zsxq_files(
        tmp_path,
        [_topic([_file("pdf1", "贵州茅台.pdf")])],
        run,
        "/bin/zsxq-cli",
        opener=lambda _url: pdf,
        sleep=lambda _delay: None,
        lexicon=Lexicon([("600519.SH", "贵州茅台", "600519")], []),
        moment=MOMENT,
    )
    assert not (files_dir(tmp_path) / "pdf1.pdf").exists()
    assert _rows(tmp_path)[0]["title"] == "贵州茅台"

    old = files_dir(tmp_path)
    old.mkdir(parents=True, exist_ok=True)
    kept = old / "old1.pdf"
    kept.write_bytes(pdf)
    index = {
        "files": {
            "pdf1": {"status": "ok", "name": "贵州茅台.pdf"},
            "old1": {
                "status": "ok",
                "name": "旧稿.pdf",
                "hash": "old",
                "saved_at": (MOMENT - timedelta(days=90)).isoformat(),
            },
        }
    }
    (old / "index.json").write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setenv("NEWS_ZSXQ_FILES_DELETE_AFTER", "false")
    monkeypatch.setenv("NEWS_ZSXQ_FILES_KEEP_DAYS", "60")
    sync_zsxq_files(
        tmp_path, [], run, "/bin/zsxq-cli",
        opener=lambda _url: pdf, sleep=lambda _delay: None, moment=MOMENT,
    )
    assert not kept.exists()


def test_total_size_cap_drops_oldest_extracted_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr("app.news.zsxq_files.zsxq_files_max_total_bytes", lambda: 50)
    monkeypatch.setenv("NEWS_ZSXQ_FILES_KEEP_DAYS", "0")
    root = files_dir(tmp_path)
    root.mkdir(parents=True)
    older = root / "old.pdf"
    newer = root / "new.pdf"
    older.write_bytes(b"%PDF" + b"a" * 40)
    newer.write_bytes(b"%PDF" + b"b" * 40)
    now = MOMENT.timestamp()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))
    index = {
        "files": {
            "old": {"status": "ok", "name": "旧.pdf", "hash": "1", "saved_at": MOMENT.isoformat()},
            "new": {"status": "ok", "name": "新.pdf", "hash": "2", "saved_at": MOMENT.isoformat()},
        }
    }
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    sync_zsxq_files(
        tmp_path, [], lambda _argv: Proc(1, ""), "/bin/zsxq-cli",
        sleep=lambda _delay: None, moment=MOMENT,
    )
    assert not older.exists()
    assert newer.exists()


def test_signed_url_reads_real_cli_body():
    """zsxq-cli 把签名链接放在 body.resp_data.download_url，外层还有 status_code。"""
    payload = {
        "status_code": 200,
        "body": {"resp_data": {"download_url": SIGNED}},
        "trace_url": "https://evil.example/not-this",
    }
    assert _signed_url(json.dumps(payload)) == SIGNED
    assert _signed_url(json.dumps({"body": [{"resp_data": {"url": SIGNED}}]})) == SIGNED
    assert _signed_url(json.dumps({"body": {"resp_data": {"file": {"link": SIGNED}}}})) == SIGNED
    with pytest.raises(RuntimeError) as caught:
        _signed_url(json.dumps({
            "status_code": 200,
            "body": {"resp_data": {"download_url": "https://evil.example/secret"}},
        }))
    assert "没有拿到下载地址" in str(caught.value)
    assert "evil.example" not in str(caught.value)
    assert "files.zsxq.com" not in str(caught.value)


def test_file_backfill_queues_old_posts_without_moving_text(tmp_path, monkeypatch):
    _patch_host(monkeypatch)
    monkeypatch.setenv("NEWS_ZSXQ_FILES_PER_ROUND", "1")
    monkeypatch.setattr(
        "app.news.zsxq_files._read_limited",
        lambda _url, _opener, _max: _pdf_bytes(PDF_TEXT),
    )
    write_cursor(tmp_path, "zsxq:2002", MOMENT.isoformat())
    calls: list[list[str]] = []
    topics = [_topic([
        _file("pdf1", "贵州茅台.pdf", digest="hash-1"),
        _file("pdf2", "宁德时代.pdf", digest="hash-2"),
    ])]
    cli_body = json.dumps({
        "status_code": 200,
        "body": {"resp_data": {"download_url": SIGNED}},
    })

    def run(argv):
        calls.append(list(argv))
        if argv[1:3] == ["auth", "status"]:
            return Proc(0, "logged in")
        if argv[1:3] == ["group", "+topics"]:
            return Proc(0, json.dumps(topics))
        return Proc(0, cli_body)

    assert _run_zsxq(tmp_path, run, "", "", "", backfill_files_since="2026-10-09") == "ok"
    assert not list((tmp_path / "news" / "inbox").glob("*.json"))
    assert read_cursor(tmp_path, "zsxq:2002", "") == MOMENT.isoformat()
    downloads = [argv[-1] for argv in calls if argv[1:3] == ["api", "raw"]]
    assert downloads == ["/v2/files/pdf1/download_url"]
    index = json.loads((files_dir(tmp_path) / "index.json").read_text(encoding="utf-8"))
    assert index["files"]["pdf1"]["status"] == "ok"
    assert index["files"]["pdf2"]["status"] == "pending"
    assert len(_rows(tmp_path)) == 1
    _assert_no_secret(tmp_path)

    calls.clear()
    assert _run_zsxq(tmp_path, run, "", "", "") == "ok"
    downloads = [argv[-1] for argv in calls if argv[1:3] == ["api", "raw"]]
    assert downloads == ["/v2/files/pdf2/download_url"]
    assert not any(argv[1:3] == ["group", "+topics"] and "--end-time" in argv for argv in calls)
    topic_calls = [argv for argv in calls if argv[1:3] == ["group", "+topics"]]
    assert len(topic_calls) == 1
    assert len(_rows(tmp_path)) == 2
    assert read_cursor(tmp_path, "zsxq:2002", "") == MOMENT.isoformat()
    _assert_no_secret(tmp_path)


def test_invalid_file_backfill_date_does_not_scan_history(tmp_path, monkeypatch):
    _patch_host(monkeypatch)
    write_cursor(tmp_path, "zsxq:2002", MOMENT.isoformat())
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv[1:3] == ["auth", "status"]:
            return Proc(0, "logged in")
        return Proc(0, json.dumps([_topic([_file("pdf1", "贵州茅台.pdf")])]))

    assert _run_zsxq(tmp_path, run, "", "", "", backfill_files_since="不是日期") == "ok"
    assert [argv[1:3] for argv in calls] == [["auth", "status"], ["group", "+topics"]]
    assert _rows(tmp_path) == []


def test_collector_help_lists_file_backfill():
    script = Path(__file__).resolve().parents[2] / "scripts" / "news_host_collector.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "--backfill-files-since" in proc.stdout
