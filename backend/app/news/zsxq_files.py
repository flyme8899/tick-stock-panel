"""把知识星球帖子里的 PDF 研报写入现有研报库。

只下载 .pdf。mp3、xlsx 留在收件箱元数据里。签名链接用完即弃，不进索引、库和日志。
文本采集的游标在此之前已经推进，这里失败只记索引，下轮再试。
热门事件不读这张表，也不把 PDF 正文写进资讯库。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from app.market_time import CN_TZ, cn_now
from app.news.collectors import parse_time
from app.news.config import (
    zsxq_files_delete_after,
    zsxq_files_enabled,
    zsxq_files_gap_s,
    zsxq_files_keep_days,
    zsxq_files_max_bytes,
    zsxq_files_max_total_bytes,
    zsxq_files_per_round,
    zsxq_source_label,
)
from app.news.host_collector import zsxq_file_argv
from app.research_reports.ima_store import insert_report, known_ids
from app.research_reports.ima_text import short_summary

logger = logging.getLogger(__name__)

_FILE_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
_URL = re.compile(r"https?://\S+")
_BODY_LIMIT = 200_000
_SUMMARY_LIMIT = 160
_EMPTY_GIVE_UP = 3
_DOWNLOAD_GIVE_UP = 8
_PAGE_CAP = 80
_FILES_HOST = "files.zsxq.com"


def files_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "research" / "zsxq_files"


def _index_path(data_dir: Path) -> Path:
    return files_dir(data_dir) / "index.json"


def _media_id(file_id: str) -> str:
    return f"zsxq:{file_id}"


def _is_pdf(name: str) -> bool:
    return Path(str(name or "")).suffix.lower() == ".pdf"


def _scrub(value: str, limit: int) -> str:
    text = _URL.sub("", str(value or "")).replace(_FILES_HOST, "")
    return text.strip()[:limit]


def _load_index(data_dir: Path) -> dict:
    path = _index_path(data_dir)
    if not path.is_file():
        return {"files": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"files": {}}
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, dict):
        return {"files": {}}
    return {"files": files}


def _save_index(data_dir: Path, index: dict) -> None:
    path = _index_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(index, ensure_ascii=False, indent=2)
    if "://" in text or _FILES_HOST in text:
        text = _URL.sub("", text).replace(_FILES_HOST, "")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _enqueue(index: dict, topics: list) -> bool:
    changed = False
    for topic in topics or []:
        if not isinstance(topic, dict):
            continue
        published = _scrub(topic.get("create_time") or topic.get("published_at") or "", 40)
        group_name = _scrub(topic.get("group_name") or "", 80)
        topic_id = _scrub(topic.get("topic_id") or "", 64)
        for meta in topic.get("files") or []:
            if not isinstance(meta, dict) or not _is_pdf(str(meta.get("name") or "")):
                continue
            file_id = str(meta.get("file_id") or "").strip()
            if not _FILE_ID.fullmatch(file_id):
                continue
            name = _scrub(meta.get("name") or "", 120)
            digest = _scrub(meta.get("hash") or "", 64)
            try:
                size = int(meta.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if size < 0:
                size = 0
            current = index["files"].get(file_id)
            if isinstance(current, dict):
                if current.get("status") in {"ok", "skipped"}:
                    continue
                current["name"] = name or current.get("name") or ""
                current["hash"] = digest or current.get("hash") or ""
                current["size"] = size or int(current.get("size") or 0)
                current["topic_id"] = topic_id or current.get("topic_id") or ""
                current["published_at"] = published or current.get("published_at") or ""
                current["group_name"] = group_name or current.get("group_name") or ""
                changed = True
                continue
            index["files"][file_id] = {
                "hash": digest,
                "name": name,
                "size": size,
                "status": "pending",
                "attempts": 0,
                "reason": "",
                "topic_id": topic_id,
                "published_at": published,
                "group_name": group_name,
                "saved_at": "",
            }
            changed = True
    return changed


def _hash_done(index: dict, digest: str, file_id: str) -> bool:
    if not digest:
        return False
    for other_id, row in index["files"].items():
        if other_id == file_id or not isinstance(row, dict):
            continue
        if row.get("hash") == digest and row.get("status") == "ok":
            return True
    return False


def _ordered(index: dict) -> list[str]:
    rows = []
    for file_id, row in index["files"].items():
        if not isinstance(row, dict) or not _FILE_ID.fullmatch(str(file_id)):
            continue
        if row.get("status") in {"ok", "skipped"} or not _is_pdf(str(row.get("name") or "")):
            continue
        attempts = int(row.get("attempts") or 0)
        rows.append((0 if attempts == 0 else 1, str(file_id)))
    rows.sort()
    return [file_id for _tier, file_id in rows]


class _Pacer:
    def __init__(self, gap: float, sleep, now):
        self.gap = gap
        self.sleep = sleep
        self.now = now
        self._next: float | None = None

    def wait(self) -> None:
        current = self.now()
        if self._next is not None and self.gap > 0:
            delay = self._next - current
            if delay > 0:
                self.sleep(delay)
                current = self.now()
        self._next = current + self.gap


def _signed_url(stdout: str) -> str:
    text = (stdout or "").strip()
    if text.startswith("https://"):
        url = text.split()[0].strip().strip('"')
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError("没有拿到下载地址") from None
        url = _find_download_url(payload)
    if not url:
        raise RuntimeError("没有拿到下载地址")
    return _require_files_host(url)


def _find_download_url(payload, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(payload, str):
        text = payload.strip()
        return text if text.startswith("https://") else ""
    if not isinstance(payload, dict):
        return ""
    for key in ("download_url", "url"):
        found = _find_download_url(payload.get(key), depth + 1)
        if found:
            return found
    for key in ("resp_data", "data", "result"):
        found = _find_download_url(payload.get(key), depth + 1)
        if found:
            return found
    return ""


def _require_files_host(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host != _FILES_HOST
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("下载地址不可用")
    return url.strip()


def _read_limited(url: str, opener, max_bytes: int) -> bytes:
    safe = _require_files_host(url)
    try:
        blob = bytes(opener(safe)) if opener is not None else _urllib_read(safe, max_bytes)
    except RuntimeError as exc:
        if str(exc) == "文件超过大小上限":
            raise
        raise RuntimeError("附件下载失败") from None
    except Exception:  # noqa: BLE001
        raise RuntimeError("附件下载失败") from None
    if len(blob) > max_bytes:
        raise RuntimeError("文件超过大小上限")
    return blob


def _urllib_read(url: str, max_bytes: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "tsp-zsxq-files"})
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.URLError:
        raise RuntimeError("附件下载失败") from None
    with response:
        length = response.headers.get("Content-Length") if response.headers else None
        if length and str(length).isdigit() and int(length) > max_bytes:
            raise RuntimeError("文件超过大小上限")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = response.read(65_536)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise RuntimeError("文件超过大小上限")
            chunks.append(block)
    return b"".join(chunks)


def extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("缺少 pypdf，暂不能抽取正文") from None
    try:
        reader = PdfReader(BytesIO(data))
    except Exception:  # noqa: BLE001
        raise RuntimeError("附件无法读取") from None
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise RuntimeError("附件无法读取") from None
    parts: list[str] = []
    total = 0
    for page in list(reader.pages)[:_PAGE_CAP]:
        try:
            chunk = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            chunk = ""
        parts.append(chunk)
        total += len(chunk)
        if total >= _BODY_LIMIT:
            break
    return "\n".join(parts).strip()


def _title_from_name(name: str, file_id: str) -> str:
    stem = Path(str(name or "")).stem.strip()
    if not stem or stem.lower() == ".pdf":
        stem = file_id
    return stem[:180]


def _load_lexicon():
    try:
        from app.news.service import get_lexicon

        return get_lexicon()
    except Exception:  # noqa: BLE001
        logger.warning("知识星球研报未抽取个股")
        return None


def _symbols(title: str, body: str, lexicon) -> list[dict]:
    if lexicon is None:
        lexicon = _load_lexicon()
    if lexicon is None:
        return []
    try:
        mentions = lexicon.extract(f"{title}\n{body[:8000]}")
    except Exception:  # noqa: BLE001
        return []
    found: list[dict] = []
    seen: set[str] = set()
    for mention in mentions:
        if getattr(mention, "kind", "") != "stock":
            continue
        key = str(getattr(mention, "key", "") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        found.append({
            "symbol": key,
            "name": str(getattr(mention, "name", "") or ""),
            "code": str(getattr(mention, "code", "") or ""),
        })
    return found


def _record(row: dict, body: str, lexicon, moment) -> dict:
    published = parse_time(row.get("published_at"))
    if published is not None:
        day = published.date().isoformat()
    else:
        day = moment.astimezone(CN_TZ).date().isoformat()
    file_id = str(row.get("file_id") or "")
    title = _title_from_name(str(row.get("name") or ""), file_id)
    if len(body) > _BODY_LIMIT:
        body = body[:_BODY_LIMIT] + "\n\n（正文已截断）"
    topic_id = _scrub(row.get("topic_id") or "", 64)
    published_at = _scrub(row.get("published_at") or "", 40)
    return {
        "media_id": _media_id(file_id),
        "broker": zsxq_source_label(str(row.get("group_name") or "")),
        "publish_date": day,
        "title": title,
        "body": body,
        "summary": short_summary(body, title, limit=_SUMMARY_LIMIT),
        "source_note": _scrub(f"topic_id={topic_id} published_at={published_at}", 500),
        "symbols": _symbols(title, body, lexicon),
        "ingested_at": moment.astimezone(CN_TZ).isoformat(timespec="seconds"),
    }


def _fail(row: dict, reason: str) -> None:
    attempts = int(row.get("attempts") or 0) + 1
    row["attempts"] = attempts
    give_up = reason == "oversize"
    if reason in {"empty", "not-pdf", "unreadable"} and attempts >= _EMPTY_GIVE_UP:
        give_up = True
    if reason == "download" and attempts >= _DOWNLOAD_GIVE_UP:
        give_up = True
    row["status"] = "skipped" if give_up else "failed"
    row["reason"] = reason


def _download(run, binary: str, file_id: str, opener, max_bytes: int) -> bytes:
    proc = run(zsxq_file_argv(binary, file_id))
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError("附件下载失败")
    url = _signed_url(getattr(proc, "stdout", "") or "")
    data = _read_limited(url, opener, max_bytes)
    if not data.startswith(b"%PDF"):
        raise RuntimeError("附件不是 PDF")
    return data


def _store_pdf(data_dir: Path, file_id: str, data: bytes) -> Path:
    path = files_dir(data_dir) / f"{file_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pdf.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def _enforce_disk(data_dir: Path, index: dict, *, keep_days: int, max_total: int, moment) -> None:
    root = files_dir(data_dir)
    if not root.is_dir():
        return
    cutoff = moment.astimezone(CN_TZ) - timedelta(days=keep_days) if keep_days > 0 else None
    for path in root.glob("*.pdf"):
        row = index["files"].get(path.stem)
        if not isinstance(row, dict) or row.get("status") != "ok":
            continue
        if cutoff is None:
            continue
        saved = parse_time(row.get("saved_at"))
        if saved is None:
            saved = cn_now()
        if saved < cutoff:
            path.unlink(missing_ok=True)
    if max_total <= 0 or not root.is_dir():
        return
    existing = [path for path in root.glob("*.pdf") if path.is_file()]
    existing.sort(key=lambda path: path.stat().st_mtime)
    total = sum(path.stat().st_size for path in existing)
    for path in existing:
        if total <= max_total:
            break
        row = index["files"].get(path.stem)
        if not isinstance(row, dict) or row.get("status") != "ok":
            continue
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size


def _reason_of(exc: Exception) -> str:
    text = str(exc)
    if "超过大小上限" in text:
        return "oversize"
    if "不是 PDF" in text:
        return "not-pdf"
    if "没有可抽取" in text:
        return "empty"
    if "无法读取" in text:
        return "unreadable"
    if "pypdf" in text:
        return "extract"
    if "://" in text or _FILES_HOST in text:
        return "download"
    if "下载" in text or "下载地址" in text:
        return "download"
    return "download"


def sync_zsxq_files(
    data_dir: Path,
    topics: list,
    run,
    binary: str,
    *,
    opener=None,
    sleep=None,
    clock=None,
    lexicon=None,
    moment=None,
) -> dict:
    """下载本轮新 PDF，并重试索引里尚未成功的。返回本轮计数。"""
    summary = {"downloaded": 0, "skipped": 0, "failed": 0, "inserted": 0}
    if not zsxq_files_enabled():
        return summary
    data_dir = Path(data_dir)
    index = _load_index(data_dir)
    changed = _enqueue(index, topics)
    per_round = zsxq_files_per_round()
    max_bytes = zsxq_files_max_bytes()
    pacer = _Pacer(zsxq_files_gap_s(), sleep or time.sleep, clock or time.monotonic)
    when = moment or cn_now()
    delete_after = zsxq_files_delete_after()
    used = 0
    candidates = _ordered(index)
    known: set[str] = set()
    if candidates:
        known = known_ids(data_dir, [_media_id(file_id) for file_id in candidates])
    for file_id in candidates:
        if used >= per_round:
            break
        row = index["files"][file_id]
        size = int(row.get("size") or 0)
        if size > max_bytes:
            row["status"] = "skipped"
            row["reason"] = "oversize"
            summary["skipped"] += 1
            changed = True
            continue
        digest = str(row.get("hash") or "")
        if _hash_done(index, digest, file_id) or _media_id(file_id) in known:
            row["status"] = "ok"
            row["reason"] = "duplicate" if _media_id(file_id) not in known else ""
            row["saved_at"] = row.get("saved_at") or when.astimezone(CN_TZ).isoformat(timespec="seconds")
            summary["skipped"] += 1
            changed = True
            continue
        pdf_path = files_dir(data_dir) / f"{file_id}.pdf"
        reuse = pdf_path.is_file() and row.get("reason") in {"extract", "empty"}
        try:
            if reuse:
                data = pdf_path.read_bytes()
                if len(data) > max_bytes:
                    raise RuntimeError("文件超过大小上限")
            else:
                pacer.wait()
                data = _download(run, binary, file_id, opener, max_bytes)
                pdf_path = _store_pdf(data_dir, file_id, data)
                summary["downloaded"] += 1
            text = extract_pdf_text(data)
            if not text:
                raise RuntimeError("附件没有可抽取的正文")
            record = _record({**row, "file_id": file_id}, text, lexicon, when)
            if insert_report(data_dir, record):
                summary["inserted"] += 1
                known.add(record["media_id"])
            row["status"] = "ok"
            row["reason"] = ""
            row["saved_at"] = when.astimezone(CN_TZ).isoformat(timespec="seconds")
            changed = True
            if delete_after:
                pdf_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            reason = _reason_of(exc)
            _fail(row, reason)
            changed = True
            if row["status"] == "skipped":
                summary["skipped"] += 1
                pdf_path.unlink(missing_ok=True)
            else:
                summary["failed"] += 1
            logger.warning("知识星球附件失败 %s: %s", file_id, reason)
        used += 1
    _enforce_disk(
        data_dir,
        index,
        keep_days=zsxq_files_keep_days(),
        max_total=zsxq_files_max_total_bytes(),
        moment=when,
    )
    if changed:
        _save_index(data_dir, index)
    return summary
