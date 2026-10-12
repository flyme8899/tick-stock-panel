"""从个人 ima 知识库增量拉取券商研报全文。

根目录是券商文件夹，下面是日期文件夹，再下面是 Markdown。每个券商只看最近
两个日期文件夹。新的 media_id 才调用 get_media_info，拿到签名链接后立刻下载，
链接不落盘。列表每页 20 条，相邻请求至少间隔 0.6 秒，并带超时。

全文写入研报库的独立表。热门事件只收标题和短摘要，并且每轮有条数上限，
避免再把长正文灌进资讯库。
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlparse

from app.market_time import CN_TZ, cn_now
from app.news.collectors import (
    IMA_BASE,
    Item,
    folder_date,
    ima_next_cursor,
    ima_retcode,
    latest_date_folders,
    split_ima_list,
)
from app.news.extract import Lexicon, StructuredStock
from app.research_reports import ima_store
from app.research_reports.ima_text import (
    is_markdown_name,
    short_summary,
    split_report_title,
    split_source_header,
)

logger = logging.getLogger(__name__)

MIN_GAP_S = 0.6
LIST_LIMIT = 20
ROOT_PAGE_CAP = 30
FOLDER_PAGE_CAP = 8
DATE_FOLDER_KEEP = 2
HOT_CAP = 24
SUMMARY_LIMIT = 160
BODY_LIMIT = 200_000
DOWNLOAD_BYTES = 2_000_000
LIST_TIMEOUT_S = 20.0
MEDIA_TIMEOUT_S = 20.0
DOWNLOAD_TIMEOUT_S = 30.0
_URL_IN_TEXT = re.compile(r"https?://\S+")


class SyncStoppedError(Exception):
    """关机时停在两个请求之间，不把这一轮记成采集失败。"""


class Pacer:
    """保证相邻两次请求的起点至少隔 ``gap`` 秒。第一次不等待。"""

    def __init__(self, gap: float, sleep: Callable[[float], None], now: Callable[[], float]):
        self.gap = gap
        self.sleep = sleep
        self.now = now
        self._next: float | None = None

    def wait(self) -> None:
        current = self.now()
        if self._next is not None:
            delay = self._next - current
            if delay > 0:
                # 0.6 不能用二进制精确表示，误差不能把间隔缩到 gap 以下。
                if delay + 1e-6 >= self.gap:
                    delay = max(delay, self.gap)
                self.sleep(delay)
                current = self.now()
        self._next = current + self.gap


def sync_ima_research(
    data_dir,
    *,
    client,
    kb_id: str,
    headers: dict,
    lexicon: Lexicon | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    gap: float = MIN_GAP_S,
    stop: Callable[[], bool] | None = None,
) -> dict:
    moment = (now or cn_now()).astimezone(CN_TZ)
    pacer = Pacer(gap, sleep, clock)
    lex = lexicon if lexicon is not None else _lexicon()
    inserted = 0
    failed = 0
    fresh: list[dict] = []
    try:
        folders, _files = _list_pages(
            client, headers, kb_id, "", pacer, stop, page_cap=ROOT_PAGE_CAP,
        )
    except SyncStoppedError:
        return {"ok": False, "listed": False, "stopped": True, "inserted": inserted, "failed": failed}
    except Exception as exc:  # noqa: BLE001
        message = _public_error(exc)
        logger.warning("ima 券商研报列表失败: %s", message)
        _remember(data_dir, moment, inserted=0, ok=False, error=message)
        return {"ok": False, "listed": False, "inserted": 0, "failed": 0, "error": message}

    try:
        for folder in folders:
            _raise_if_stopped(stop)
            broker = str(folder.get("name") or "").strip()
            folder_id = str(folder.get("folder_id") or "").strip()
            if not folder_id or not broker or folder_date(broker) is not None:
                continue
            try:
                children, _loose = _list_pages(
                    client, headers, kb_id, folder_id, pacer, stop, page_cap=FOLDER_PAGE_CAP,
                )
            except SyncStoppedError:
                raise
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("ima 券商文件夹读取失败 %s: %s", broker, _public_error(exc))
                continue
            for dated in latest_date_folders(children, DATE_FOLDER_KEEP):
                _raise_if_stopped(stop)
                when = folder_date(str(dated.get("name") or ""))
                dated_id = str(dated.get("folder_id") or "").strip()
                if when is None or not dated_id:
                    continue
                day = when.date().isoformat()
                try:
                    _nested, files = _list_pages(
                        client, headers, kb_id, dated_id, pacer, stop, page_cap=FOLDER_PAGE_CAP,
                    )
                except SyncStoppedError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    logger.warning("ima 日期文件夹读取失败 %s %s: %s", broker, day, _public_error(exc))
                    continue
                added, missed = _download_new(
                    data_dir,
                    client=client,
                    headers=headers,
                    files=files,
                    broker=broker,
                    day=day,
                    lexicon=lex,
                    moment=moment,
                    pacer=pacer,
                    stop=stop,
                    fresh=fresh,
                )
                inserted += added
                failed += missed
    except SyncStoppedError:
        inserted = max(inserted, len(fresh))
        _ingest_hot(fresh, lexicon=lex, now=moment)
        _remember(data_dir, moment, inserted=inserted, ok=False, error="采集被中断")
        return {"ok": False, "listed": False, "stopped": True, "inserted": inserted, "failed": failed}

    hot = _ingest_hot(fresh, lexicon=lex, now=moment)
    ima_store.apply_retention(data_dir, moment.date())
    health_ok = failed == 0 or inserted > 0
    error = "" if health_ok else f"{failed} 篇正文没有下载下来"
    _remember(data_dir, moment, inserted=inserted, ok=health_ok, error=error)
    logger.info("ima 券商研报新入库 %s 篇，热门事件摘要 %s 条", inserted, hot)
    return {
        "ok": health_ok,
        "listed": True,
        "inserted": inserted,
        "failed": failed,
        "hot": hot,
    }


def _download_new(
    data_dir,
    *,
    client,
    headers: dict,
    files: list[dict],
    broker: str,
    day: str,
    lexicon: Lexicon,
    moment: datetime,
    pacer: Pacer,
    stop: Callable[[], bool] | None,
    fresh: list[dict],
) -> tuple[int, int]:
    pending = []
    for row in files:
        title = str(row.get("title") or row.get("name") or "").strip()
        media_id = str(row.get("media_id") or "").strip()
        if not media_id or not is_markdown_name(title):
            continue
        pending.append((media_id, title))
    known = ima_store.known_ids(data_dir, [media_id for media_id, _title in pending])
    inserted = 0
    failed = 0
    for media_id, title in pending:
        if media_id in known:
            continue
        _raise_if_stopped(stop)
        try:
            body = _download_markdown(client, headers, media_id, pacer)
            record = _record(media_id, broker, day, title, body, lexicon, moment)
        except SyncStoppedError:
            raise
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("ima 研报正文下载失败 %s: %s", media_id, type(exc).__name__)
            continue
        if record is None:
            failed += 1
            continue
        if ima_store.insert_report(data_dir, record):
            inserted += 1
            known.add(media_id)
            fresh.append(record)
    return inserted, failed


def _record(
    media_id: str,
    broker: str,
    day: str,
    file_title: str,
    body: str,
    lexicon: Lexicon,
    moment: datetime,
) -> dict | None:
    title_broker, title = split_report_title(file_title)
    broker_name = (broker or title_broker or "").strip()
    title = (title or file_title).strip()
    if not broker_name or not title or not body.strip():
        return None
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + "\n\n（正文已截断）"
    note, prose = split_source_header(body)
    summary = short_summary(prose, title, limit=SUMMARY_LIMIT)
    mentions = lexicon.extract(f"{title}\n{prose[:8000]}")
    symbols = []
    seen: set[str] = set()
    for mention in mentions:
        if mention.kind != "stock" or not mention.key or mention.key in seen:
            continue
        seen.add(mention.key)
        symbols.append({"symbol": mention.key, "name": mention.name, "code": mention.code})
    return {
        "media_id": media_id,
        "broker": broker_name,
        "publish_date": day,
        "title": title,
        "body": body,
        "summary": summary,
        "source_note": note,
        "symbols": symbols,
        "ingested_at": moment.isoformat(timespec="seconds"),
    }


def _download_markdown(client, headers: dict, media_id: str, pacer: Pacer) -> str:
    payload = _post(client, headers, "get_media_info", {"media_id": media_id}, MEDIA_TIMEOUT_S, pacer)
    url, extra = _signed_target(payload)
    pacer.wait()
    response = client.get(url, headers=extra, timeout=DOWNLOAD_TIMEOUT_S)
    _raise_status(response)
    content = getattr(response, "content", None)
    if content is None:
        text = str(getattr(response, "text", "") or "")
        content = text.encode("utf-8")
    if not isinstance(content, (bytes, bytearray)):
        content = str(content).encode("utf-8")
    if len(content) > DOWNLOAD_BYTES or b"\x00" in content[:1024]:
        raise RuntimeError("正文不是 Markdown")
    return bytes(content).decode("utf-8-sig", errors="replace").strip()


def _signed_target(payload: dict) -> tuple[str, dict]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    info = data.get("url_info") if isinstance(data.get("url_info"), dict) else {}
    url = str(info.get("url") or data.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("没有可用的签名链接")
    raw_headers = info.get("headers") if isinstance(info.get("headers"), dict) else {}
    extra = {str(key): str(value) for key, value in raw_headers.items() if value is not None}
    return url, extra


def _list_pages(client, headers, kb_id: str, folder_id: str, pacer: Pacer, stop, *, page_cap: int):
    folders: list[dict] = []
    files: list[dict] = []
    cursor = ""
    seen: set[str] = set()
    for _page in range(page_cap):
        body = {"knowledge_base_id": kb_id, "cursor": cursor, "limit": LIST_LIMIT}
        if folder_id:
            body["folder_id"] = folder_id
        page = _post(client, headers, "get_knowledge_list", body, LIST_TIMEOUT_S, pacer)
        page_folders, page_files = split_ima_list(page)
        folders.extend(page_folders)
        files.extend(page_files)
        nxt = ima_next_cursor(page)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
        _raise_if_stopped(stop)
    return _dedupe(folders, "folder_id"), _dedupe(files, "media_id")


def _post(client, headers: dict, method: str, body: dict, timeout: float, pacer: Pacer) -> dict:
    pacer.wait()
    response = client.post(f"{IMA_BASE}/{method}", headers=headers, json=body, timeout=timeout)
    payload = _json_payload(response)
    if ima_retcode(payload) == 110021:
        pacer.sleep(8)
        pacer.wait()
        response = client.post(f"{IMA_BASE}/{method}", headers=headers, json=body, timeout=timeout)
        payload = _json_payload(response)
    code = ima_retcode(payload)
    if code != 0:
        message = payload.get("errmsg") or payload.get("msg") or payload.get("message") or ""
        raise RuntimeError(f"ima {code}: {message}"[:200])
    return payload


def _json_payload(response) -> dict:
    _raise_status(response)
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("ima 响应不是对象")
    return payload


def _raise_status(response) -> None:
    status = int(getattr(response, "status_code", 200) or 200)
    if status >= 400:
        raise RuntimeError(f"HTTP {status}")


def _dedupe(rows: list[dict], key: str) -> list[dict]:
    seen: set[str] = set()
    found = []
    for row in rows:
        ident = str(row.get(key) or "")
        if ident and ident in seen:
            continue
        if ident:
            seen.add(ident)
        found.append(row)
    return found


def _ingest_hot(rows: list[dict], *, lexicon: Lexicon, now: datetime) -> int:
    if not rows:
        return 0
    from app.news.config import source_enabled

    if not source_enabled("ima_research"):
        return 0
    from app.news.service import ingest_items

    chosen = _hot_rows(rows)
    items = []
    for row in chosen:
        summary = str(row.get("summary") or "").strip() or str(row.get("title") or "")
        stocks = [
            StructuredStock(name=str(item.get("name") or ""), code=str(item.get("code") or item.get("symbol") or ""))
            for item in (row.get("symbols") or [])[:8]
            if isinstance(item, dict)
        ]
        items.append(Item(
            source="ima_research",
            source_id=str(row["media_id"]),
            published_at=_published(str(row.get("publish_date") or ""), now),
            author=str(row.get("broker") or "")[:80],
            title=str(row.get("title") or "")[:180],
            text=summary[:SUMMARY_LIMIT],
            stocks=stocks,
            raw={
                "media_id": row["media_id"],
                "broker": row.get("broker") or "",
                "publish_date": row.get("publish_date") or "",
            },
        ))
    result = ingest_items(items, lexicon)
    return int(result.get("inserted") or 0)


def _hot_rows(rows: list[dict]) -> list[dict]:
    def rank(row: dict) -> tuple:
        mentioned = 1 if row.get("symbols") else 0
        return (mentioned, str(row.get("publish_date") or ""), str(row.get("media_id") or ""))

    ordered = sorted(rows, key=rank, reverse=True)
    return ordered[:HOT_CAP]


def _published(day: str, now: datetime) -> datetime:
    try:
        year, month, date = (int(part) for part in day.split("-"))
        stamp = datetime(year, month, date, 20, 0, tzinfo=CN_TZ)
    except ValueError:
        return now
    local = now.astimezone(CN_TZ)
    if stamp > local:
        return local.replace(microsecond=0)
    return stamp


def _remember(data_dir, moment: datetime, *, inserted: int, ok: bool, error: str) -> None:
    fields = {
        "last_inserted": int(inserted),
        "last_attempt_at": moment.isoformat(timespec="seconds"),
    }
    if ok:
        fields["last_ok_at"] = moment.isoformat(timespec="seconds")
        fields["last_error"] = ""
    else:
        fields["last_error"] = error[:300]
    ima_store.patch_run_state(data_dir, **fields)
    try:
        from app.news.service import get_store

        get_store().mark_health(
            "ima_research",
            ok=ok,
            error="" if ok else error[:300],
            auth_state="ok" if ok else "unknown",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("ima 券商研报健康状态未写入: %s", exc)


def _public_error(exc: Exception) -> str:
    text = _URL_IN_TEXT.sub("签名链接", str(exc))
    return text[:200]


def _raise_if_stopped(stop: Callable[[], bool] | None) -> None:
    if stop is not None and stop():
        raise SyncStoppedError


def _lexicon() -> Lexicon:
    from app.news.service import get_lexicon

    return get_lexicon()
