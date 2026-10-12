"""券商研报 Markdown 的标题、来源注释和短摘要。

知识库条目标题写成「券商｜标题」。文件头的 HTML 注释或「来源：」行只说明出处，
热门事件用的短摘要不带这段，避免把转存注释当成事件正文。
"""
from __future__ import annotations

import re

from app.news.cleaning import excerpt

_PIPE = re.compile(r"\s*[|｜]\s*")
_MD_SUFFIX = re.compile(r"\.(?:md|markdown|txt)$", re.IGNORECASE)
_BOOKENDS = re.compile(r"^[「『]|[」』]$")
_ONE_COMMENT = re.compile(r"[ \t]*<!--(.*?)-->[ \t]*(?:\n|\Z)", re.DOTALL)
_LEADING_SOURCE = re.compile(r"\A(?:[ \t]*(?:>[ \t]*)?来源[:：][^\n]*(?:\n|\Z))+")
_SKIP_EXT = (
    ".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".xlsx", ".xls", ".ppt", ".pptx", ".zip", ".rar", ".mp4", ".mp3",
)


def is_markdown_name(title: str) -> bool:
    """日期文件夹里只下载 Markdown。二进制扩展名直接跳过，不再请求正文。"""
    name = (title or "").strip()
    lower = name.lower()
    if any(lower.endswith(ext) for ext in _SKIP_EXT):
        return False
    if lower.endswith((".md", ".markdown", ".txt")):
        return True
    return "｜" in name or "|" in name


def split_report_title(title: str) -> tuple[str, str]:
    """「招商证券｜光伏跟踪.md」→ (招商证券, 光伏跟踪)。没有分隔符时券商为空。"""
    text = _BOOKENDS.sub("", (title or "").strip()).strip()
    text = _MD_SUFFIX.sub("", text).strip()
    text = _BOOKENDS.sub("", text).strip()
    parts = _PIPE.split(text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return "", text.strip()


def split_source_header(body: str) -> tuple[str, str]:
    """拆开文件头的来源注释。返回 (注释, 剩余正文)。"""
    raw = body or ""
    notes: list[str] = []
    while True:
        match = _ONE_COMMENT.match(raw)
        if match is None:
            break
        note = (match.group(1) or "").strip()
        if note:
            notes.append(note)
        raw = raw[match.end():]
    source = _LEADING_SOURCE.match(raw)
    if source:
        notes.append(source.group(0).strip())
        raw = raw[source.end():]
    return "\n".join(notes).strip(), raw.lstrip()


def short_summary(prose: str, title: str, *, limit: int = 160) -> str:
    """给热门事件和情报桥的短摘要。标题行和来源注释不重复放进来。"""
    chunks: list[str] = []
    title_key = re.sub(r"\s+", "", title or "")
    for line in (prose or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        plain = stripped.lstrip(">").strip()
        compact = re.sub(r"\s+", "", plain)
        if title_key and compact in {title_key, ""}:
            continue
        chunks.append(plain)
        if sum(len(part) for part in chunks) >= limit:
            break
    return excerpt(" ".join(chunks), limit)
