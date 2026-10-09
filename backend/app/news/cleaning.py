"""资讯正文清洗与内容指纹。

钉钉播报会把敏感词拆开（投zi、谷份）。替换只覆盖这些固定写法，
避免把「中谷物流」「谷歌」「谷物」改坏。
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote

# 长词在前，避免「投ziji金」被先换成「投资ji金」。
_PHRASES: tuple[tuple[str, str], ...] = (
    ("投ziji金", "投资基金"),
    ("投zi", "投资"),
    ("ji金", "基金"),
    ("谷票", "股票"),
    ("谷价", "股价"),
    ("谷市", "股市"),
    ("谷东", "股东"),
    ("谷份", "股份"),
    ("谷息", "股息"),
    ("谷权", "股权"),
    ("谷民", "股民"),
    ("谷本", "股本"),
    ("美谷", "美股"),
    ("港谷", "港股"),
    ("A谷", "A股"),
    ("个谷", "个股"),
    ("万谷", "万股"),
)

_HASHTAG = re.compile(
    r"""<e\s+[^>]*type\s*=\s*["']hashtag["'][^>]*/>""",
    re.IGNORECASE,
)
_E_TAG = re.compile(r"<e\s+[^>]*/>", re.IGNORECASE)
_IMG_PLACEHOLDER = re.compile(r"\[图片消息\]\([^)]*\)")
_MEDIA_IN_TEXT = re.compile(r"mediaId=([^)\s]+)")
_JI_BETWEEN_HAN = re.compile(r"(?<=[\u4e00-\u9fff])ji(?=[\u4e00-\u9fff])")


def strip_hashtags(text: str) -> str:
    """去掉知识星球 `<e type="hashtag" .../>`，不把标签标题拼回正文。"""
    return _E_TAG.sub("", _HASHTAG.sub("", text or ""))


def clean_text(text: str) -> str:
    """清洗一条消息。空串表示没有可分析正文（纯图片）。"""
    raw = strip_hashtags(text or "")
    raw = _IMG_PLACEHOLDER.sub("", raw)
    raw = raw.replace("\u200b", "")
    for src, dst in _PHRASES:
        raw = raw.replace(src, dst)
    raw = _JI_BETWEEN_HAN.sub("基", raw)
    raw = re.sub(r"[ \t]+\n", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = re.sub(r"[ \t]{2,}", " ", raw)
    return raw.strip()


def excerpt(text: str, limit: int = 240) -> str:
    """对外只给摘录，避免把采集到的全文再发出去。"""
    body = re.sub(r"\s+", " ", text or "").strip()
    if len(body) <= limit:
        return body
    return body[: max(0, limit - 1)].rstrip() + "…"


def content_hash(clean: str, media_ids: list[str] | None = None) -> str:
    """跨源去重指纹。短文本不参与正文合并，避免「图片」互相撞车。"""
    compact = re.sub(r"\s+", "", clean or "")
    if len(compact) < 12:
        media = ",".join(sorted({m.strip() for m in (media_ids or []) if m and m.strip()}))
        compact = f"media:{media}" if media else compact
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def media_ids_from_text(text: str) -> list[str]:
    found = []
    seen: set[str] = set()
    for match in _MEDIA_IN_TEXT.finditer(text or ""):
        media_id = unquote(match.group(1)).strip()
        if media_id and media_id not in seen:
            seen.add(media_id)
            found.append(media_id)
    return found


def decode_hashtag_title(tag: str) -> str:
    """测试辅助：标签 title 是百分号编码，业务清洗会整段丢掉。"""
    match = re.search(r"""title\s*=\s*["']([^"']+)["']""", tag)
    if not match:
        return ""
    return unquote(match.group(1))
