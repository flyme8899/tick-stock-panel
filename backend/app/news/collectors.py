"""财联社、华尔街见闻、ima 与宿主机收件箱的解析。

网络调用由 service 注入，这里只负责请求参数和响应归一，方便离线测试。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.market_time import CN_TZ
from app.news.cleaning import media_ids_from_text
from app.news.cls_sign import cls_query
from app.news.extract import StructuredStock

CLS_URL = "https://www.cls.cn/v1/roll/get_roll_list"
WSCN_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
IMA_BASE = "https://ima.qq.com/openapi/wiki/v1"
_DATE_FOLDER = re.compile(
    r"(20\d{2})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})"
)
_DATE_COMPACT = re.compile(r"^(20\d{2})(\d{2})(\d{2})$")


@dataclass
class Item:
    source: str
    source_id: str
    published_at: datetime
    author: str = ""
    title: str = ""
    text: str = ""
    url: str = ""
    level: str = ""
    media_ids: list[str] = field(default_factory=list)
    stocks: list[StructuredStock] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def parse_time(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, CN_TZ)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-5] + text[-5:-2] + ":" + text[-2:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def cls_params() -> dict[str, str]:
    return cls_query({"rn": "20", "refresh_type": "1"})


def parse_cls(payload: dict) -> list[Item]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = []
    if isinstance(data, dict):
        rows = data.get("roll_data") or data.get("items") or []
    items: list[Item] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("is_ad"):
            continue
        published = parse_time(row.get("ctime") or row.get("time"))
        if published is None:
            continue
        stocks = []
        for stock in row.get("stock_list") or []:
            if not isinstance(stock, dict):
                continue
            stocks.append(StructuredStock(
                name=str(stock.get("name") or ""),
                code=str(stock.get("StockID") or stock.get("stock_id") or stock.get("code") or ""),
            ))
        sectors = _names(row.get("subjects"), "subject_name")
        sectors.extend(_names(row.get("plate_list"), "name"))
        text = str(row.get("content") or row.get("brief") or "")
        title = str(row.get("title") or "")
        items.append(Item(
            source="cls",
            source_id=str(row.get("id") or ""),
            published_at=published,
            title=title,
            text=text or title,
            url=str(row.get("shareurl") or row.get("share_url") or ""),
            level=str(row.get("level") or ""),
            stocks=stocks,
            sectors=_uniq(sectors),
            raw={"id": row.get("id"), "level": row.get("level")},
        ))
    return [item for item in items if item.source_id]


def wscn_params(channel: str) -> dict[str, str]:
    return {"channel": channel, "client": "pc", "limit": "20"}


def parse_wscn(payload: dict, channel: str) -> list[Item]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = []
    if isinstance(data, dict):
        rows = data.get("items") or data.get("list") or []
    elif isinstance(data, list):
        rows = data
    items: list[Item] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        published = parse_time(row.get("display_time") or row.get("created_at") or row.get("ctime"))
        if published is None:
            continue
        stocks = []
        for stock in row.get("symbols") or []:
            if isinstance(stock, str):
                stocks.append(StructuredStock(code=stock))
            elif isinstance(stock, dict):
                stocks.append(StructuredStock(
                    name=str(stock.get("name") or ""),
                    code=str(stock.get("symbol") or stock.get("code") or ""),
                ))
        sectors = _names(row.get("related_themes") or row.get("themes"), "name")
        text = str(row.get("content_text") or row.get("content") or row.get("title") or "")
        items.append(Item(
            source="wscn",
            source_id=str(row.get("id") or ""),
            published_at=published,
            author=str(row.get("author") or ""),
            title=str(row.get("title") or ""),
            text=text,
            url=str(row.get("uri") or row.get("url") or ""),
            level=str(row.get("score") or ""),
            stocks=stocks,
            sectors=_uniq(sectors),
            raw={"id": row.get("id"), "channel": channel, "score": row.get("score")},
        ))
    return [item for item in items if item.source_id]


def parse_dws_payload(payload) -> list[Item]:
    messages = payload
    if isinstance(payload, dict):
        messages = payload.get("messages") or payload.get("items") or []
    if not isinstance(messages, list):
        return []
    items: list[Item] = []
    for row in messages:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("content") or "")
        media = media_ids_from_text(text)
        for ref in row.get("resourceRefs") or []:
            if isinstance(ref, dict) and ref.get("resourceId"):
                media.append(str(ref["resourceId"]))
        published = parse_time(row.get("createTime") or row.get("create_time"))
        if published is None:
            continue
        items.append(Item(
            source="dws",
            source_id=str(row.get("messageId") or row.get("message_id") or ""),
            published_at=published,
            author=str(row.get("sender") or ""),
            text=text,
            media_ids=_uniq(media),
            raw={"messageId": row.get("messageId")},
        ))
    return [item for item in items if item.source_id]


def parse_zsxq_payload(payload) -> tuple[list[Item], dict]:
    """返回条目和分页信息（has_more / next_end_time）。"""
    topics = payload
    page = {"has_more": False, "next_end_time": ""}
    if isinstance(payload, dict):
        topics = payload.get("topics") or payload.get("items") or payload.get("data") or []
        page["has_more"] = bool(payload.get("has_more"))
        page["next_end_time"] = str(payload.get("next_end_time") or "")
    if not isinstance(topics, list):
        return [], page
    items: list[Item] = []
    for row in topics:
        if not isinstance(row, dict):
            continue
        text = str(row.get("content") or "")
        title = str(row.get("title") or "")
        owner = row.get("owner") if isinstance(row.get("owner"), dict) else {}
        images = []
        for image in row.get("images") or []:
            if isinstance(image, dict) and image.get("image_id"):
                images.append(str(image["image_id"]))
        published = parse_time(row.get("create_time"))
        if published is None:
            continue
        items.append(Item(
            source="zsxq",
            source_id=str(row.get("topic_id") or ""),
            published_at=published,
            author=str(owner.get("name") or ""),
            title=title,
            text=text or title,
            media_ids=images,
            url="",
            raw={"topic_id": row.get("topic_id")},
        ))
    return [item for item in items if item.source_id], page


def ima_headers(client_id: str, api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "ima-openapi-clientid": client_id,
        "ima-openapi-apikey": api_key,
    }


def ima_retcode(payload: dict) -> int:
    try:
        return int(payload.get("retcode", payload.get("code", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def pick_knowledge_base(payload: dict, name: str, kb_id: str = "") -> str:
    if kb_id:
        return kb_id
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = (
        data.get("info_list")
        or data.get("infos")
        or data.get("knowledge_base_list")
        or data.get("list")
        or []
    )
    if isinstance(rows, dict):
        rows = [{"id": key, **(value if isinstance(value, dict) else {})} for key, value in rows.items()]
    hint = name or "爱分享"
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("kb_name") or row.get("name") or "")
        if title == hint or hint in title or "爱分享" in title:
            return str(row.get("kb_id") or row.get("id") or "")
    return ""


def ima_next_cursor(payload: dict) -> str:
    """is_end 为真或没有 next_cursor 时停止翻页。"""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if not isinstance(data, dict) or data.get("is_end") is True:
        return ""
    return str(data.get("next_cursor") or "").strip()


def split_ima_list(payload: dict) -> tuple[list[dict], list[dict]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    folders: list[dict] = []
    files: list[dict] = []
    for key in ("folder_list", "folders"):
        for row in data.get(key) or []:
            if isinstance(row, dict):
                folders.append(row)
    for row in data.get("knowledge_list") or []:
        if not isinstance(row, dict):
            continue
        media_id = str(row.get("media_id") or "")
        folder_id = str(row.get("folder_id") or "")
        title = str(row.get("title") or "")
        name = str(row.get("name") or title)
        if media_id.startswith("folder_") or (folder_id.startswith("folder_") and not media_id):
            folders.append({"folder_id": media_id or folder_id, "name": name})
            continue
        if folder_id and not media_id and not title:
            folders.append({"folder_id": folder_id, "name": name})
            continue
        if media_id or title:
            files.append(row)
            continue
        if folder_id and name:
            folders.append({"folder_id": folder_id, "name": name})
    return folders, files


def folder_date(name: str) -> datetime | None:
    text = (name or "").strip()
    match = _DATE_FOLDER.search(text) or _DATE_COMPACT.search(text)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=CN_TZ)
    except ValueError:
        return None


def latest_date_folders(folders: list[dict], keep: int = 2) -> list[dict]:
    dated = []
    for row in folders:
        name = str(row.get("name") or "")
        when = folder_date(name)
        if when is None:
            continue
        dated.append((when, row))
    dated.sort(key=lambda item: item[0], reverse=True)
    return [row for _when, row in dated[:keep]]


def parse_ima_titles(files: list[dict], folder_name: str) -> list[Item]:
    when = folder_date(folder_name) or datetime.now(CN_TZ)
    items: list[Item] = []
    for row in files:
        title = str(row.get("title") or row.get("name") or "").strip()
        media_id = str(row.get("media_id") or "").strip()
        if not title or not media_id:
            continue
        items.append(Item(
            source="ima",
            source_id=media_id,
            published_at=when,
            title=title,
            text=title,
            raw={"media_id": media_id, "folder": folder_name},
        ))
    return items


def load_inbox_payload(text: str) -> tuple[str, object]:
    payload = json.loads(text)
    if not isinstance(payload, dict) or "source" not in payload:
        raise ValueError("收件箱文件缺少 source")
    source = str(payload["source"])
    if source not in {"dws", "zsxq"}:
        raise ValueError(f"收件箱来源不支持: {source}")
    return source, payload.get("payload")


def _names(rows, field: str) -> list[str]:
    found = []
    for row in rows or []:
        if isinstance(row, str) and row.strip():
            found.append(row.strip())
        elif isinstance(row, dict):
            name = str(row.get(field) or row.get("name") or "").strip()
            if name:
                found.append(name)
    return found


def _uniq(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
