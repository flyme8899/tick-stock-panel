"""把知识星球帖子里的 PDF 研报写入现有研报库。

只下载 .pdf。mp3、xlsx 留在收件箱元数据里。签名链接用完即弃，不进索引、库和日志。
文本采集的游标在此之前已经推进，这里失败只记索引，下轮再试。
热门事件不读这张表，也不把 PDF 正文写进资讯库。

正文先用 pypdf。它抛错或抽出空文本时，再试 poppler 的 ``pdftotext -layout``，
然后是 pdfminer.six。仍然没有文字层的，在交易时段之外用 OCR 读前 3 页。
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
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
    zsxq_ocr_max_per_round,
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
_OCR_PAGE_CAP = 3
_FILES_HOST = "files.zsxq.com"
# 本地文件还能用，不必重新下载。
_LOCAL_REUSE_REASONS = {"extract", "empty", "unreadable", "ocr-deferred"}
# 只把正文为空或无法读取的再排队。下载失败、超限、不是 PDF 保持原样。
_REQUEUE_REASONS = {"empty", "unreadable"}
_OCR_OPEN = 9 * 60 + 15
_OCR_CLOSE = 15 * 60 + 5
_RAPID_SNIPPET = (
    "import sys\n"
    "from rapidocr_onnxruntime import RapidOCR\n"
    "engine = RapidOCR()\n"
    "result, _elapse = engine(sys.argv[1])\n"
    "lines = []\n"
    "for item in result or []:\n"
    "    if item and len(item) > 1 and item[1]:\n"
    "        lines.append(str(item[1]))\n"
    "sys.stdout.write('\\n'.join(lines))\n"
)


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


def _ordered(index: dict, *, skip_ocr_deferred: bool = False) -> list[str]:
    rows = []
    for file_id, row in index["files"].items():
        if not isinstance(row, dict) or not _FILE_ID.fullmatch(str(file_id)):
            continue
        if row.get("status") in {"ok", "skipped"} or not _is_pdf(str(row.get("name") or "")):
            continue
        if skip_ocr_deferred and row.get("reason") == "ocr-deferred":
            continue
        attempts = int(row.get("attempts") or 0)
        if row.get("reason") == "ocr-deferred":
            tier = 2
        elif attempts == 0:
            tier = 0
        else:
            tier = 1
        rows.append((tier, str(file_id)))
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


def _accept_download_url(value: str) -> str:
    text = value.strip().strip('"')
    if not text.startswith("https://"):
        return ""
    try:
        return _require_files_host(text.split()[0])
    except RuntimeError:
        return ""


def _find_download_url(payload, depth: int = 0) -> str:
    """在任意嵌套里找 files.zsxq.com 的 https 链接。zsxq-cli 放在 body.resp_data 下。"""
    if depth > 8:
        return ""
    if isinstance(payload, str):
        return _accept_download_url(payload)
    if isinstance(payload, dict):
        preferred = ("download_url", "url", "body", "resp_data", "data", "result")
        for key in preferred:
            if key in payload:
                found = _find_download_url(payload.get(key), depth + 1)
                if found:
                    return found
        for key, value in payload.items():
            if key in preferred:
                continue
            found = _find_download_url(value, depth + 1)
            if found:
                return found
        return ""
    if isinstance(payload, list):
        for item in payload:
            found = _find_download_url(item, depth + 1)
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


def _stdout(proc) -> str:
    raw = getattr(proc, "stdout", "") or ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _with_nice(argv: list[str]) -> list[str]:
    """OCR 和后备解析让出 CPU。没有 nice 时直接跑，Windows 采集机不装它。"""
    if shutil.which("nice"):
        return ["nice", "-n", "19", *argv]
    return list(argv)


def _run_nice(argv: list[str], *, input_bytes: bytes | None = None, timeout: float = 60):
    return subprocess.run(
        _with_nice(argv),
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _pypdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("缺少 pypdf，暂不能抽取正文") from None
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise RuntimeError("附件无法读取") from exc
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


def _pdftotext_text(data: bytes) -> str:
    if shutil.which("pdftotext") is None:
        raise FileNotFoundError("pdftotext")
    proc = _run_nice(
        ["pdftotext", "-layout", "-enc", "UTF-8", "-f", "1", "-l", str(_PAGE_CAP), "-", "-"],
        input_bytes=data,
        timeout=60,
    )
    if int(getattr(proc, "returncode", 1) or 0) != 0:
        raise RuntimeError("pdftotext")
    return _stdout(proc)


def _pdfminer_text(data: bytes) -> str:
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        raise RuntimeError("缺少 pdfminer.six") from None
    return extract_text(BytesIO(data), maxpages=_PAGE_CAP) or ""


def extract_pdf_text(data: bytes) -> str:
    """pypdf 抛错或空白时，依次试 pdftotext -layout 和 pdfminer.six。

    三个解析器都没有文字时返回空字符串，调用方再决定要不要 OCR。
    解析器缺失或 PdfReadError 不直接失败，避免扫描件停在第一次异常上。
    """
    for name, reader in (
        ("pypdf", _pypdf_text),
        ("pdftotext", _pdftotext_text),
        ("pdfminer", _pdfminer_text),
    ):
        try:
            text = (reader(data) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.info("PDF 解析器 %s 未得到正文: %s", name, type(exc).__name__)
            continue
        if text:
            if len(text) > _BODY_LIMIT:
                return text[:_BODY_LIMIT]
            return text
    return ""


def _ocr_hours_closed(moment) -> bool:
    """北京时间 09:15–15:05 不做 OCR，避免和盘中抢 CPU。含两端整分钟。"""
    local = moment.astimezone(CN_TZ) if moment.tzinfo is not None else moment.replace(tzinfo=CN_TZ)
    minutes = local.hour * 60 + local.minute
    return _OCR_OPEN <= minutes <= _OCR_CLOSE


def _tesseract_images(images: list[Path]) -> str:
    parts: list[str] = []
    for image in images:
        proc = _run_nice(
            ["tesseract", str(image), "stdout", "-l", "chi_sim+eng"],
            timeout=60,
        )
        if int(getattr(proc, "returncode", 1) or 0) != 0:
            logger.info("tesseract 退出码 %s", getattr(proc, "returncode", 1))
            continue
        chunk = _stdout(proc).strip()
        if chunk:
            parts.append(chunk)
    return "\n".join(parts).strip()


def _rapidocr_images(images: list[Path]) -> str:
    parts: list[str] = []
    for image in images:
        proc = _run_nice(
            [sys.executable, "-c", _RAPID_SNIPPET, str(image)],
            timeout=90,
        )
        if int(getattr(proc, "returncode", 1) or 0) != 0:
            continue
        chunk = _stdout(proc).strip()
        if chunk:
            parts.append(chunk)
    return "\n".join(parts).strip()


def ocr_pdf_text(data: bytes) -> str:
    """前 3 页栅格化后 OCR。优先 tesseract chi_sim+eng，没有则试 rapidocr。"""
    if not data or shutil.which("pdftoppm") is None:
        return ""
    try:
        with tempfile.TemporaryDirectory(prefix="zsxq-ocr-") as tmp:
            pdf_path = Path(tmp) / "in.pdf"
            pdf_path.write_bytes(data)
            prefix = str(Path(tmp) / "page")
            proc = _run_nice(
                ["pdftoppm", "-f", "1", "-l", str(_OCR_PAGE_CAP), "-png", str(pdf_path), prefix],
                timeout=90,
            )
            if int(getattr(proc, "returncode", 1) or 0) != 0:
                return ""
            images = sorted(Path(tmp).glob("page*.png"))[:_OCR_PAGE_CAP]
            if not images:
                return ""
            if shutil.which("tesseract"):
                text = _tesseract_images(images)
            else:
                text = _rapidocr_images(images)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if len(text) > _BODY_LIMIT:
        return text[:_BODY_LIMIT]
    return text.strip()


def _ocr_decision(moment, used: int) -> str:
    """run 本轮可以 OCR；defer 先搁下；skip 表示 OCR 被关掉，按空正文失败。"""
    budget = zsxq_ocr_max_per_round()
    if budget <= 0:
        return "skip"
    if _ocr_hours_closed(moment) or used >= budget:
        return "defer"
    return "run"


def _defer_ocr(row: dict) -> None:
    """不增加放弃次数。交易时段或本轮名额用完后，下一轮还能再试。"""
    row["status"] = "failed"
    row["reason"] = "ocr-deferred"


def requeue_failed_files(data_dir: Path) -> int:
    """把此前正文为空或无法读取而放弃的 PDF 重新标成待处理。

    下载失败、超限、不是 PDF、已成功的不动。保留失败原因，这样本地 PDF 可以接着抽，不必重下。
    """
    data_dir = Path(data_dir)
    index = _load_index(data_dir)
    changed = 0
    for row in index["files"].values():
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        reason = str(row.get("reason") or "")
        if status in {"failed", "skipped"} and reason in _REQUEUE_REASONS:
            row["status"] = "pending"
            row["attempts"] = 0
            changed += 1
    if changed:
        _save_index(data_dir, index)
    return changed


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
    ocr_used = 0
    # 交易时段不再反复碰到已经决定延后的文件，把名额留给新下载。
    skip_deferred = _ocr_hours_closed(when) and zsxq_ocr_max_per_round() > 0
    candidates = _ordered(index, skip_ocr_deferred=skip_deferred)
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
        reuse = pdf_path.is_file() and row.get("reason") in _LOCAL_REUSE_REASONS
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
                decision = _ocr_decision(when, ocr_used)
                if decision == "defer":
                    _defer_ocr(row)
                    summary["failed"] += 1
                    changed = True
                    logger.info("知识星球 PDF %s 暂缓 OCR", file_id)
                    # 延后 OCR 不占本轮下载名额，后面的文字层 PDF 还能下。
                    continue
                if decision == "run":
                    ocr_used += 1
                    started = time.perf_counter()
                    try:
                        text = (ocr_pdf_text(data) or "").strip()
                    except Exception:  # noqa: BLE001
                        text = ""
                    logger.info(
                        "知识星球 PDF OCR %s 耗时 %.2f 秒",
                        file_id,
                        time.perf_counter() - started,
                    )
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
