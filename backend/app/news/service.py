"""采集编排：清洗、抽取、入库、热门候选。不在请求线程里打外部网络。"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.config import settings
from app.market_time import cn_now
from app.news.cleaning import clean_text, content_hash, excerpt
from app.news.collectors import (
    CLS_URL,
    FOREIGN_FEEDS,
    FOREIGN_SOURCES,
    IMA_BASE,
    REDDIT_MIN_GAP,
    REDDIT_USER_AGENT,
    REUTERS_GOOGLE_NEWS,
    REUTERS_SITEMAP_INDEX,
    WSCN_URL,
    Feed,
    Item,
    cls_next_last_time,
    cls_params,
    ima_headers,
    ima_next_cursor,
    ima_retcode,
    latest_date_folders,
    latest_reuters_news_sitemaps,
    load_inbox_payload,
    paginate_until_seen,
    parse_cls,
    parse_dws_payload,
    parse_feed_xml,
    parse_ima_titles,
    parse_reddit_atom,
    parse_reuters_google_news,
    parse_reuters_news_sitemap,
    parse_time,
    parse_wscn,
    parse_zsxq_payload,
    pick_knowledge_base,
    reddit_backoff_seconds,
    reddit_feed_url,
    reddit_retry_after_seconds,
    split_ima_list,
    wscn_next_cursor,
    wscn_params,
)
from app.news.config import (
    SOURCE_LABELS,
    SOURCE_ORDER,
    llm_extract_enabled,
    reddit_subreddits,
    sec_configured,
    sec_user_agent,
    source_configured,
    source_enabled,
    source_locked,
)
from app.news.etf_flow import collect_etf_flow
from app.news.extract import (
    Lexicon,
    Mention,
    StructuredStock,
    _is_fund_name,
    _usable_sector_name,
    parse_llm_payload,
    parse_llm_summary,
)
from app.news.scoring import Candidate, MentionEvent, mention_weight, score_candidates
from app.news.store import NewsStore

logger = logging.getLogger(__name__)

_STORE: NewsStore | None = None
_STORE_LOCK = threading.Lock()
_LEXICON: tuple[float, Lexicon] | None = None
_LLM_TIMES: list[float] = []
_LLM_LOCK = threading.Lock()
_LLM_PER_HOUR = 10
# 一次外文轮询可能带上几十条新稿。模型只处理前几条，避免单轮把额度打光。
_FOREIGN_LLM_PER_POLL = 10
# 本地维表里的 ETF / 股票集合。请求线程不扫盘，大约 10 分钟复用一次。
_ASSET_TYPES: tuple[float, dict[str, str]] | None = None
_ASSET_TTL = 600
# 50/51/56/58 是沪市基金，15/16 是深市 ETF 和 LOF。维表里有的代码以维表为准。
_FUND_CODE = re.compile(r"^(?:15|16|50|51|56|58)\d{4}$")


def get_store() -> NewsStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = NewsStore(settings.data_dir / "news" / "news.sqlite")
        return _STORE


def reset_store_for_tests(path: Path | None = None) -> NewsStore:
    global _STORE, _LEXICON, _ASSET_TYPES
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = NewsStore(path or (settings.data_dir / "news" / "news.sqlite"))
        _LEXICON = None
        _ASSET_TYPES = None
    from app.news.hot_events import clear_hot_event_cache

    clear_hot_event_cache()
    return _STORE


def lexicon_from_repo(repo) -> Lexicon:
    stocks: list[tuple[str, str, str]] = []
    if repo is not None:
        # ETF 不进个股候选。名称里的指数片段（中证1000ETF南方）会把指数讨论算到基金上。
        for getter in ("get_instruments",):
            try:
                frame = getattr(repo, getter)()
            except Exception as exc:  # noqa: BLE001
                logger.debug("读取标的维表失败 %s: %s", getter, exc)
                continue
            if frame is None or frame.is_empty() or "symbol" not in frame.columns:
                continue
            name_col = "name" if "name" in frame.columns else None
            for row in frame.iter_rows(named=True):
                symbol = str(row.get("symbol") or "")
                name = str(row.get(name_col) or "") if name_col else ""
                stocks.append((symbol, name, symbol))
    sectors = _sector_names(repo)
    return Lexicon(stocks, sectors)


def get_lexicon(repo=None) -> Lexicon:
    global _LEXICON
    now = time.monotonic()
    cached = _LEXICON
    if cached is not None and now - cached[0] < 600 and repo is None:
        return cached[1]
    built = lexicon_from_repo(repo if repo is not None else _repo())
    _LEXICON = (now, built)
    return built


def ingest_items(items: list[Item], lexicon: Lexicon | None = None) -> dict[str, int]:
    store = get_store()
    pending = [item for item in items if item.source_id]
    known: set[tuple[str, str]] = set()
    grouped: dict[str, list[str]] = {}
    for item in pending:
        grouped.setdefault(item.source, []).append(item.source_id)
    for source, ids in grouped.items():
        known.update((source, sid) for sid in store.existing_ids(source, ids))
    known_urls: set[tuple[str, str]] = set()
    urls_by_source: dict[str, list[str]] = {}
    for item in pending:
        if item.source in FOREIGN_SOURCES and item.url:
            urls_by_source.setdefault(item.source, []).append(item.url[:500])
    for source, urls in urls_by_source.items():
        known_urls.update((source, url) for url in store.existing_urls(source, urls))
    inserted = 0
    duplicate = 0
    foreign_llm_used = 0
    for item in pending:
        ident = (item.source, item.source_id)
        url_key = None
        if item.source in FOREIGN_SOURCES and item.url:
            url_key = (item.source, item.url[:500])
        if ident in known or (url_key and url_key in known_urls):
            duplicate += 1
            continue
        known.add(ident)
        if url_key:
            known_urls.add(url_key)
        if lexicon is None:
            lexicon = get_lexicon()
        clean = clean_text(item.text)
        title = clean_text(item.title) or _title_from(clean)
        digest = content_hash(clean, item.media_ids)
        mentions = lexicon.extract(
            f"{title}\n{clean}",
            item.stocks,
            item.sectors,
        )
        extra = {"stocks": [stock.__dict__ for stock in item.stocks], "sectors": item.sectors}
        if item.source in FOREIGN_SOURCES:
            # 超过本轮上限后不再落到通用抽取，否则限额形同虚设。
            if (
                llm_extract_enabled()
                and foreign_llm_used < _FOREIGN_LLM_PER_POLL
                and len(f"{title}\n{clean}".strip()) >= 8
            ):
                foreign_llm_used += 1
                summary_zh, llm_found = _llm_foreign_enrich(title, clean, lexicon)
                if summary_zh:
                    extra["summary_zh"] = summary_zh
                mentions = _merge_mentions(mentions, llm_found)
        elif not mentions and llm_extract_enabled() and len(clean) >= 40:
            mentions = _llm_mentions(title, clean, lexicon)
        status = store.insert_item(
            source=item.source,
            source_id=item.source_id,
            published_at=item.published_at,
            author=item.author,
            title=title,
            clean_text=clean,
            raw=item.raw,
            content_hash=digest,
            url=item.url,
            level=item.level,
            media_ids=item.media_ids,
            extra=extra,
            mentions=[(m.kind, m.key, m.name, m.code, m.origin) for m in mentions],
        )
        if status == "inserted":
            inserted += 1
        else:
            duplicate += 1
    if inserted:
        store.apply_retention()
    return {"inserted": inserted, "duplicate": duplicate}


def backfill_mentions(lexicon: Lexicon, *, limit: int = 200) -> int:
    """词典晚于入库时，给最近还没有提及的正文补一次抽取。"""
    store = get_store()
    rows = store.items_missing_mentions(cn_now() - timedelta(days=3), limit)
    updated = 0
    for row in rows:
        extra = {}
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except json.JSONDecodeError:
            extra = {}
        stocks = [
            StructuredStock(name=str(item.get("name") or ""), code=str(item.get("code") or ""))
            for item in (extra.get("stocks") or [])
            if isinstance(item, dict)
        ]
        mentions = lexicon.extract(
            f"{row['title'] or ''}\n{row['clean_text'] or ''}",
            stocks,
            [str(name) for name in (extra.get("sectors") or [])],
        )
        if not mentions:
            continue
        store.replace_mentions(
            row["id"],
            [(m.kind, m.key, m.name, m.code, m.origin) for m in mentions],
        )
        updated += 1
    return updated


def known_asset_types() -> dict[str, str]:
    """本地标的维表：symbol → etf 或 stock。ETF 优先。仓库还没挂上时返回空表。"""
    global _ASSET_TYPES
    now = time.monotonic()
    if _ASSET_TYPES is not None and now - _ASSET_TYPES[0] < _ASSET_TTL:
        return _ASSET_TYPES[1]
    repo = _repo()
    if repo is None:
        return {}
    mapping: dict[str, str] = {}
    try:
        etfs = repo.get_etf_symbol_set() or set()
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 ETF 维表失败: %s", exc)
        etfs = set()
    for symbol in etfs:
        text = str(symbol or "").strip()
        if text:
            mapping[text] = "etf"
    try:
        frame = repo.get_instruments()
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取股票维表失败: %s", exc)
        frame = None
    if frame is not None and not frame.is_empty() and "symbol" in frame.columns:
        for symbol in frame.get_column("symbol").to_list():
            text = str(symbol or "").strip()
            if text:
                mapping.setdefault(text, "stock")
    _ASSET_TYPES = (now, mapping)
    return mapping


def asset_kind_of(symbol: str, name: str = "") -> str:
    """个股还是 ETF/LOF/基金。维表里有的代码信维表，没有才看名称和代码前缀。"""
    text = (symbol or "").strip()
    known = known_asset_types()
    for key in _asset_lookup_keys(text):
        kind = known.get(key)
        if kind == "etf":
            return "etf"
        if kind == "stock":
            return "stock"
    if _is_fund_name(name):
        return "etf"
    code = text.split(".", 1)[0]
    if _FUND_CODE.fullmatch(code):
        return "etf"
    return "stock"


def _asset_lookup_keys(symbol: str) -> list[str]:
    if not symbol:
        return []
    keys = [symbol]
    code = symbol.split(".", 1)[0]
    if code and code not in keys:
        keys.append(code)
    if "." not in symbol and code:
        keys.extend(f"{code}{suffix}" for suffix in (".SH", ".SZ", ".BJ"))
    return keys


def _retag_fund(item: Candidate) -> Candidate:
    if item.kind == "stock" and asset_kind_of(item.key, item.name) == "etf":
        return replace(item, kind="etf")
    return item


def hot_candidates(*, kind: str = "all", window_hours: int = 24, baseline_days: int = 4, limit: int = 20):
    now = cn_now()
    start = now - timedelta(hours=window_hours, days=baseline_days)
    events = []
    for row in get_store().mention_events_since(start):
        published = parse_time(row["published_at"])
        if published is None:
            continue
        if row["kind"] == "sector" and not _sector_name_ok(row["key"], row["name"]):
            continue
        events.append(MentionEvent(
            kind=row["kind"],
            key=row["key"],
            name=row["name"],
            source=row["source"],
            content_hash=row["content_hash"],
            published_at=published,
            weight=mention_weight(str(row["origin"] or "")),
        ))
    ranked = [_retag_fund(item) for item in score_candidates(
        events, now=now, window_hours=window_hours, baseline_days=baseline_days,
    )]
    if kind in {"stock", "sector", "etf"}:
        ranked = [item for item in ranked if item.kind == kind]
    return ranked[: max(1, min(limit, 50))]


def public_hot_event(event: dict) -> dict:
    """给页面和推送的事件字段。不带内部条目 id。个股和 ETF 分开。"""
    stocks = []
    etfs = []
    for stock in event.get("mentioned_stocks") or []:
        if not isinstance(stock, dict):
            continue
        key = str(stock.get("key") or "").strip()
        if not key:
            continue
        try:
            mentions = int(stock.get("mentions") or 0)
        except (TypeError, ValueError):
            mentions = 0
        row = {
            "key": key,
            "name": str(stock.get("name") or key),
            "mentions": mentions,
        }
        if asset_kind_of(key, row["name"]) == "etf":
            etfs.append(row)
        else:
            stocks.append(row)
    try:
        heat = float(event.get("heat") or 0)
    except (TypeError, ValueError):
        heat = 0.0
    return {
        "key": str(event.get("key") or ""),
        "name": str(event.get("name") or ""),
        "concepts": [str(item) for item in (event.get("concepts") or []) if str(item).strip()],
        "headline": str(event.get("headline") or ""),
        "mentions": int(event.get("mentions") or 0),
        "source_count": int(event.get("source_count") or 0),
        "first_seen": str(event.get("first_seen") or ""),
        "heat": round(heat, 4),
        "stocks": stocks,
        "etfs": etfs,
    }


def hot_event_listing(now: datetime | None = None, *, limit: int = 20) -> dict:
    """热门事件页的主列表。和选股用同一套聚类，按热度从高到低。"""
    snapshot = top_hot_events(now)
    events = [public_hot_event(item) for item in snapshot.get("events") or [] if isinstance(item, dict)]
    return {
        "events": events[: max(1, min(limit, 50))],
        "as_of": snapshot.get("as_of"),
        "trading_day": snapshot.get("trading_day"),
        "fallback": bool(snapshot.get("fallback")),
        "hint": snapshot.get("hint"),
        "updated_at": snapshot.get("updated_at"),
    }


def event_detail(key: str, *, limit: int = 30, now: datetime | None = None) -> dict:
    """点开一个具体事件：簇里的资讯摘录，以及正文里提到的个股。"""
    wanted = (key or "").strip()
    snapshot = top_hot_events(now)
    event = next(
        (item for item in snapshot.get("events") or [] if isinstance(item, dict) and item.get("key") == wanted),
        None,
    )
    if event is None:
        return {"kind": "event", "key": wanted, "event": None, "stocks": [], "etfs": [], "items": []}
    ids: list[int] = []
    for raw in event.get("item_ids") or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    rows = get_store().items_by_ids(ids)
    public = public_hot_event(event)
    return {
        "kind": "event",
        "key": wanted,
        "event": public,
        "stocks": public["stocks"],
        "etfs": public["etfs"],
        "items": [message_view(row) for row in rows[: max(1, min(limit, 50))]],
    }


def top_hot_events(now: datetime | None = None) -> dict:
    """当前交易日热度最高的具体事件。

    同一交易日的标题按主体、动作和细概念聚类，热度是资讯条数乘来源数，再按更新时间衰减。
    当天没有资讯时，改用不晚于今天、且落在回看窗口里的最近一天，并在 hint 里标明。
    结果缓存约 10 分钟。模型不可用时保留关键词标题。
    """
    from app.news.hot_events import build_top_hot_events

    return build_top_hot_events(now, get_store())


def message_view(row, *, limit: int = 240) -> dict:
    return {
        "source": row["source"],
        "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
        "published_at": row["published_at"],
        "author": row["author"] or "",
        "title": row["title"] or "",
        "excerpt": _row_summary(row, limit),
        "url": row["url"] or "",
        "level": row["level"] or "",
    }


def hot_messages(kind: str, key: str, *, window_hours: int = 24, limit: int = 30) -> list[dict]:
    if kind == "event":
        return event_detail(key, limit=limit)["items"]
    if kind == "sector" and not _usable_sector_name(key):
        return []
    # 基金代码入库时仍记成 stock。热门 ETF 页签按这个原样去取摘录。
    stored_kind = "stock" if kind == "etf" else kind
    start = cn_now() - timedelta(hours=window_hours)
    rows = get_store().messages_for(kind=stored_kind, key=key, start=start, limit=limit)
    seen: set[str] = set()
    out = []
    for row in rows:
        # 同一故事跨源都保留，界面要看到来源；同来源同指纹只留最新一条。
        ident = f"{row['source']}:{row['content_hash']}"
        if ident in seen:
            continue
        seen.add(ident)
        out.append(message_view(row))
    return out


def news_for_symbol(symbol: str, *, hours: int = 72, limit: int = 30) -> dict:
    keys = _symbol_keys(symbol)
    start = cn_now() - timedelta(hours=max(1, min(hours, 24 * 30)))
    rows = get_store().items_for_symbol(keys, start, max(1, min(limit, 50)))
    return {
        "symbol": keys[0] if keys else symbol,
        "hours": hours,
        "items": [message_view(row, limit=180) for row in rows],
    }


def feed_for_source(source: str, *, limit: int = 50) -> dict:
    limit = max(1, min(limit, 50))
    if source == "hot":
        return {"source": "hot", "name": SOURCE_LABELS["hot"], "items": _hot_feed_items(limit)}
    if source not in SOURCE_ORDER:
        raise ValueError(f"未知来源 {source}")
    items = []
    for row in get_store().recent_for_feed(source, limit):
        symbols = []
        sectors = []
        for mention in row["mentions"]:
            if mention["kind"] == "stock":
                symbols.append(mention["key"])
            elif mention["kind"] == "sector" and _usable_sector_name(mention["key"]):
                sectors.append(mention["key"])
        items.append({
            "source_id": row["source_id"],
            "title": row["title"] or excerpt(row["clean_text"], 40),
            "summary": _row_summary(row, 400),
            "url": row["url"] or "",
            "published_at": row["published_at"],
            "symbols": symbols[:8],
            "sectors": sectors[:6],
        })
    return {"source": source, "name": SOURCE_LABELS[source], "items": items}


def health_payload() -> dict:
    rows = {row["source"]: row for row in get_store().health_rows()}
    sources = []
    for source in (*SOURCE_ORDER,):
        row = rows.get(source)
        sources.append({
            "id": source,
            "label": SOURCE_LABELS[source],
            "enabled": source_enabled(source),
            "configured": source_configured(source),
            "locked": source_locked(source),
            "auth_state": (row["auth_state"] if row else "") or "unknown",
            "last_ok_at": row["last_ok_at"] if row else None,
            "last_error": (row["last_error"] if row else "") or "",
            "items_ingested": row["items_ingested"] if row else 0,
        })
    return {"sources": sources, "as_of": cn_now().isoformat(timespec="seconds")}


def collect_cls(client: httpx.Client | None = None) -> dict:
    own = client is None
    client = client or httpx.Client(timeout=12.0, follow_redirects=True)
    try:
        def fetch(token):
            response = client.get(
                CLS_URL,
                params=cls_params(token),
                headers={"Referer": "https://www.cls.cn/telegraph", "User-Agent": "tsp-news/1.0"},
            )
            response.raise_for_status()
            payload = response.json()
            page = parse_cls(payload)
            ids = [item.source_id for item in page]
            hit = bool(ids) and bool(get_store().existing_ids("cls", ids))
            return page, cls_next_last_time(payload), hit

        result = ingest_items(paginate_until_seen(fetch))
        get_store().mark_health("cls", ok=True, auth_state="n/a")
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("财联社采集失败: %s", exc)
        get_store().mark_health("cls", ok=False, error=str(exc), auth_state="n/a")
        return {"inserted": 0, "duplicate": 0, "error": str(exc)[:200]}
    finally:
        if own:
            client.close()


def collect_wscn(client: httpx.Client | None = None) -> dict:
    own = client is None
    client = client or httpx.Client(timeout=12.0, follow_redirects=True)
    try:
        items: list[Item] = []
        for channel in ("global-channel", "a-stock-channel"):
            def fetch(token, channel=channel):
                response = client.get(
                    WSCN_URL,
                    params=wscn_params(channel, token),
                    headers={"User-Agent": "tsp-news/1.0"},
                )
                response.raise_for_status()
                payload = response.json()
                page = parse_wscn(payload, channel)
                ids = [item.source_id for item in page]
                hit = bool(ids) and bool(get_store().existing_ids("wscn", ids))
                return page, wscn_next_cursor(payload), hit

            items.extend(paginate_until_seen(fetch))
        result = ingest_items(items)
        get_store().mark_health("wscn", ok=True, auth_state="n/a")
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("华尔街见闻采集失败: %s", exc)
        get_store().mark_health("wscn", ok=False, error=str(exc), auth_state="n/a")
        return {"inserted": 0, "duplicate": 0, "error": str(exc)[:200]}
    finally:
        if own:
            client.close()


def collect_ima(client: httpx.Client | None = None) -> dict:
    """只读标题。共享知识库正文接口会返回 220030，这里不调用。"""
    if not source_configured("ima"):
        return {"inserted": 0, "duplicate": 0, "skipped": True}
    own = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=True)
    headers = ima_headers(settings.ima_client_id, settings.ima_api_key)
    try:
        kb_id = settings.ima_kb_id.strip()
        if not kb_id:
            found = _ima_post(client, headers, "search_knowledge_base", {
                "query": settings.ima_kb_name,
                "cursor": "",
                "limit": 20,
            })
            kb_id = pick_knowledge_base(found, settings.ima_kb_name)
        if not kb_id:
            raise RuntimeError("没有找到知识库「爱分享」")
        folders, files = _ima_list_pages(client, headers, {"knowledge_base_id": kb_id})
        picked = latest_date_folders(folders, 2)
        items: list[Item] = []
        if not picked:
            items.extend(parse_ima_titles(files, ""))
        for folder in picked:
            folder_id = str(folder.get("folder_id") or "")
            body = {"knowledge_base_id": kb_id}
            if folder_id:
                body["folder_id"] = folder_id
            _sub, folder_files = _ima_list_pages(client, headers, body)
            items.extend(parse_ima_titles(folder_files, str(folder.get("name") or "")))
        result = ingest_items(items)
        get_store().mark_health("ima", ok=True, auth_state="ok")
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("ima 采集失败: %s", exc)
        auth = "expired" if "110030" in str(exc) else "unknown"
        get_store().mark_health("ima", ok=False, error=str(exc), auth_state=auth)
        return {"inserted": 0, "duplicate": 0, "error": str(exc)[:200]}
    finally:
        if own:
            client.close()


def collect_inbox(directory: Path | None = None) -> dict:
    inbox = directory or (settings.data_dir / "news" / "inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    failed_dir = inbox / "failed"
    inserted = 0
    duplicate = 0
    errors = 0
    files = [path for path in inbox.glob("*.json") if path.is_file()]
    files.sort(key=lambda path: (path.stat().st_mtime, path.name))
    for path in files[:20]:
        try:
            source, payload = load_inbox_payload(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("收件箱 %s 处理失败: %s", path.name, exc)
            hinted = _inbox_source_hint(path)
            if hinted:
                get_store().mark_health(hinted, ok=False, error=f"{path.name}: {exc}"[:300])
            _move_inbox_failed(path, failed_dir)
            continue
        if not source_enabled(source):
            path.unlink(missing_ok=True)
            continue
        try:
            if source == "dws":
                items = parse_dws_payload(payload)
            else:
                items, _page = parse_zsxq_payload(payload)
            result = ingest_items(items)
            inserted += result["inserted"]
            duplicate += result["duplicate"]
            get_store().mark_health(source, ok=True)
            path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("收件箱 %s 处理失败: %s", path.name, exc)
            hinted = _inbox_source_hint(path)
            if hinted:
                get_store().mark_health(hinted, ok=False, error=f"{path.name}: {exc}"[:300])
            _move_inbox_failed(path, failed_dir)
    _read_host_auth(inbox.parent / "health")
    return {"inserted": inserted, "duplicate": duplicate, "errors": errors}


def collect_foreign(source: str, client: httpx.Client | None = None) -> dict:
    """只拉 feed。304 沿用缓存的 ETag，不解析正文，也不打开条目链接。"""
    feeds = FOREIGN_FEEDS.get(source)
    if not feeds:
        return {"inserted": 0, "duplicate": 0, "skipped": True}
    if source == "sec" and not sec_configured():
        return {"inserted": 0, "duplicate": 0, "skipped": True}
    own = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=True)
    headers = _foreign_headers(source)
    items: list[Item] = []
    errors: list[str] = []
    ok_feeds = 0
    try:
        for feed in feeds:
            try:
                pulled, status = _pull_feed(client, feed, headers)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{feed.url}: {exc}"[:180])
                logger.warning("%s feed 失败: %s", source, exc)
                continue
            if status in {200, 304}:
                ok_feeds += 1
            items.extend(pulled)
        result = ingest_items(items) if items else {"inserted": 0, "duplicate": 0}
        label = SOURCE_LABELS.get(source, source)
        if ok_feeds == 0:
            message = "; ".join(errors) or "feed 请求失败"
            get_store().mark_health(source, ok=False, error=message, auth_state="n/a")
            return {**result, "error": message[:200]}
        get_store().mark_health(source, ok=True, auth_state="n/a")
        if errors:
            logger.warning("%s 部分 feed 失败: %s", label, errors[0])
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 采集失败: %s", SOURCE_LABELS.get(source, source), exc)
        get_store().mark_health(source, ok=False, error=str(exc), auth_state="n/a")
        return {"inserted": 0, "duplicate": 0, "error": str(exc)[:200]}
    finally:
        if own:
            client.close()


def run_due(source: str) -> dict:
    if source == "cls":
        return collect_cls()
    if source == "wscn":
        return collect_wscn()
    if source == "ima":
        return collect_ima()
    if source in {"dws", "zsxq"}:
        return collect_inbox()
    if source == "etf_flow":
        return collect_etf_flow()
    if source == "reddit":
        return collect_reddit()
    if source == "reuters":
        return collect_reuters()
    if source in FOREIGN_FEEDS:
        return collect_foreign(source)
    return {}


def _foreign_headers(source: str) -> dict[str, str]:
    if source == "sec":
        return {
            "User-Agent": sec_user_agent(),
            "Accept": "application/atom+xml, application/xml, text/xml",
        }
    return {
        "User-Agent": "tsp-news/1.0",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
    }


def _conditional_get(client: httpx.Client, url: str, headers: dict[str, str]):
    etag, modified = get_store().get_feed_cache(url)
    request_headers = dict(headers)
    if etag:
        request_headers["If-None-Match"] = etag
    if modified:
        request_headers["If-Modified-Since"] = modified
    return client.get(url, headers=request_headers)


def _remember_feed(url: str, response) -> None:
    get_store().save_feed_cache(
        url,
        _header(getattr(response, "headers", None), "etag"),
        _header(getattr(response, "headers", None), "last-modified"),
    )


def _pull_feed(client: httpx.Client, feed: Feed, headers: dict[str, str]) -> tuple[list[Item], int]:
    response = _conditional_get(client, feed.url, headers)
    status = int(getattr(response, "status_code", 200))
    if status == 304:
        return [], 304
    response.raise_for_status()
    items = parse_feed_xml(response.text, feed.source, feed_url=feed.url)
    # 解析失败时保留上一份校验值，下一轮仍拉完整响应，而不是把坏正文记成已同步。
    _remember_feed(feed.url, response)
    return items, status


def collect_reuters(client: httpx.Client | None = None) -> dict:
    """路透官网返回 401，不打开文章。先读 sitemap index 的最新 news sitemap。

    索引或这一页失败时改拉 Google News，只留标题和链接。
    索引返回 304 表示清单没变，不再解析，也不改走备用源。
    """
    own = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=True)
    headers = _foreign_headers("reuters")
    try:
        items, primary_error = _reuters_primary(client, headers)
        if primary_error is not None:
            logger.warning("路透 sitemap 失败，改用 Google News: %s", primary_error)
            items, fallback_error = _reuters_google(client, headers)
            if fallback_error is not None:
                message = f"{primary_error}; {fallback_error}"
                get_store().mark_health("reuters", ok=False, error=message, auth_state="n/a")
                return {"inserted": 0, "duplicate": 0, "error": message[:200]}
        result = ingest_items(items) if items else {"inserted": 0, "duplicate": 0}
        get_store().mark_health("reuters", ok=True, auth_state="n/a")
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("路透采集失败: %s", exc)
        get_store().mark_health("reuters", ok=False, error=str(exc), auth_state="n/a")
        return {"inserted": 0, "duplicate": 0, "error": str(exc)[:200]}
    finally:
        if own:
            client.close()


def _reuters_primary(client: httpx.Client, headers: dict[str, str]) -> tuple[list[Item], str | None]:
    """成功时错误是 None，包括 304 和过滤后没有条目。失败时不写入索引的校验值。"""
    index_url = REUTERS_SITEMAP_INDEX
    try:
        response = _conditional_get(client, index_url, headers)
        status = int(getattr(response, "status_code", 200))
        if status == 304:
            cached_etag, cached_modified = get_store().get_feed_cache(index_url)
            if cached_etag or cached_modified:
                return [], None
            return [], "sitemap index 返回 304"
        response.raise_for_status()
        pages = latest_reuters_news_sitemaps(response.text)
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)[:180]
    if not pages:
        return [], "sitemap index 没有 news sitemap"
    items: list[Item] = []
    errors: list[str] = []
    ok_pages = 0
    for page_url in pages:
        try:
            page = _conditional_get(client, page_url, headers)
            page_status = int(getattr(page, "status_code", 200))
            if page_status != 304:
                page.raise_for_status()
                parsed, _saw_title = parse_reuters_news_sitemap(page.text, feed_url=page_url)
                _remember_feed(page_url, page)
                items.extend(parsed)
            ok_pages += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{page_url}: {exc}"[:180])
    if ok_pages == 0:
        return [], "; ".join(errors) or "news sitemap 请求失败"
    _remember_feed(index_url, response)
    if errors:
        logger.warning("路透部分 news sitemap 失败: %s", errors[0])
    return items, None


_REDDIT_CURSOR = "reddit:cursor"
_REDDIT_NOT_BEFORE = "reddit:not-before"
_REDDIT_BACKOFF = "reddit:backoff"
_REDDIT_BACKOFF_CAP = 3600


def collect_reddit(client: httpx.Client | None = None, *, now: float | None = None) -> dict:
    """每次只请求一个子版的 /new/.rss。两次请求至少隔 75 秒，不在这条线程里 sleep。

    429 读 Retry-After，没有这个头就从 75 秒起倍增。成功或 304 才轮到下一个子版。
    不读取 REDDIT_CLIENT_ID / SECRET，也不带 Authorization。
    """
    moment = time.time() if now is None else float(now)
    subs = reddit_subreddits()
    if not subs:
        message = "Reddit 子版列表没有合法名称"
        get_store().mark_health("reddit", ok=False, error=message, auth_state="n/a")
        return {"inserted": 0, "duplicate": 0, "error": message}
    not_before = _reddit_stamp(_REDDIT_NOT_BEFORE)
    if moment < not_before:
        remaining = max(1, math.ceil(not_before - moment))
        return {
            "inserted": 0,
            "duplicate": 0,
            "skipped": True,
            "retry_after": min(remaining, _REDDIT_BACKOFF_CAP),
        }
    cursor = _reddit_stamp(_REDDIT_CURSOR)
    sub = subs[cursor % len(subs)]
    url = reddit_feed_url(sub)
    own = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=True)
    headers = {
        "User-Agent": REDDIT_USER_AGENT,
        "Accept": "application/atom+xml, application/xml, text/xml",
    }
    try:
        try:
            response = _conditional_get(client, url, headers)
        except Exception as exc:  # noqa: BLE001
            _reddit_arm(moment, REDDIT_MIN_GAP)
            message = str(exc)[:180]
            logger.warning("Reddit 采集失败: %s", exc)
            get_store().mark_health("reddit", ok=False, error=message, auth_state="n/a")
            return {
                "inserted": 0,
                "duplicate": 0,
                "error": message,
                "retry_after": REDDIT_MIN_GAP,
            }
        status = int(getattr(response, "status_code", 200))
        if status == 429:
            header = reddit_retry_after_seconds(
                _header(getattr(response, "headers", None), "retry-after"),
                moment,
            )
            wait = reddit_backoff_seconds(header, _reddit_stamp(_REDDIT_BACKOFF))
            _reddit_put(_REDDIT_BACKOFF, wait)
            _reddit_arm(moment, wait)
            message = f"HTTP 429，{wait}s 后再试"
            get_store().mark_health("reddit", ok=False, error=message, auth_state="n/a")
            return {"inserted": 0, "duplicate": 0, "error": message, "retry_after": wait}
        if status == 304:
            _reddit_success_gate(moment, cursor)
            get_store().mark_health("reddit", ok=True, auth_state="n/a")
            return {"inserted": 0, "duplicate": 0, "retry_after": REDDIT_MIN_GAP}
        if status >= 400:
            _reddit_arm(moment, REDDIT_MIN_GAP)
            message = f"HTTP {status}"
            get_store().mark_health("reddit", ok=False, error=message, auth_state="n/a")
            return {
                "inserted": 0,
                "duplicate": 0,
                "error": message,
                "retry_after": REDDIT_MIN_GAP,
            }
        try:
            items = parse_reddit_atom(response.text, feed_url=url, subreddit=sub)
        except Exception as exc:  # noqa: BLE001
            _reddit_arm(moment, REDDIT_MIN_GAP)
            message = str(exc)[:180]
            logger.warning("Reddit 解析失败: %s", exc)
            get_store().mark_health("reddit", ok=False, error=message, auth_state="n/a")
            return {
                "inserted": 0,
                "duplicate": 0,
                "error": message,
                "retry_after": REDDIT_MIN_GAP,
            }
        result = ingest_items(items) if items else {"inserted": 0, "duplicate": 0}
        # 入库成功后再记 ETag 和轮转。中途失败下一轮仍拉这一版的完整响应。
        _remember_feed(url, response)
        _reddit_success_gate(moment, cursor)
        get_store().mark_health("reddit", ok=True, auth_state="n/a")
        return {**result, "retry_after": REDDIT_MIN_GAP}
    finally:
        if own:
            client.close()


def _reddit_success_gate(moment: float, cursor: int) -> None:
    _reddit_put(_REDDIT_CURSOR, cursor + 1)
    _reddit_put(_REDDIT_BACKOFF, 0)
    _reddit_arm(moment, REDDIT_MIN_GAP)


def _reddit_arm(moment: float, wait: int) -> None:
    _reddit_put(_REDDIT_NOT_BEFORE, math.ceil(moment + wait))


def _reddit_stamp(key: str) -> int:
    raw, _modified = get_store().get_feed_cache(key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _reddit_put(key: str, value: int) -> None:
    get_store().save_feed_cache(key, str(int(value)), "")


def _reuters_google(client: httpx.Client, headers: dict[str, str]) -> tuple[list[Item], str | None]:
    url = REUTERS_GOOGLE_NEWS
    try:
        response = _conditional_get(client, url, headers)
        status = int(getattr(response, "status_code", 200))
        if status == 304:
            return [], None
        response.raise_for_status()
        items = parse_reuters_google_news(response.text, feed_url=url)
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)[:180]
    _remember_feed(url, response)
    return items, None


def _header(headers, name: str) -> str:
    if not headers:
        return ""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value or "").strip()
    return ""


_IMA_MAX_PAGES = 8


def _ima_list_pages(client: httpx.Client, headers: dict, body: dict) -> tuple[list[dict], list[dict]]:
    """按 next_cursor 翻页，最多 8 页，避免游标停在原地时打满额度。"""
    folders: list[dict] = []
    files: list[dict] = []
    cursor = ""
    seen: set[str] = set()
    for _page in range(_IMA_MAX_PAGES):
        page = _ima_post(client, headers, "get_knowledge_list", {**body, "cursor": cursor, "limit": 50})
        page_folders, page_files = split_ima_list(page)
        folders.extend(page_folders)
        files.extend(page_files)
        nxt = ima_next_cursor(page)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    return folders, files


def _move_inbox_failed(path: Path, failed_dir: Path) -> None:
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = failed_dir / path.name
    if dest.exists():
        dest = failed_dir / f"{path.stem}-{int(path.stat().st_mtime)}{path.suffix}"
    path.replace(dest)


def _ima_post(client: httpx.Client, headers: dict, method: str, body: dict) -> dict:
    response = client.post(f"{IMA_BASE}/{method}", headers=headers, json=body)
    response.raise_for_status()
    payload = response.json()
    code = ima_retcode(payload)
    if code == 110021:
        time.sleep(8)
        response = client.post(f"{IMA_BASE}/{method}", headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
        code = ima_retcode(payload)
    if code == 220030:
        logger.info("ima 返回 220030，该知识库正文不可读，只保留已拿到的标题")
        return {"retcode": 0, "data": {}}
    if code != 0:
        raise RuntimeError(f"ima {code}: {payload.get('errmsg') or payload.get('message') or ''}"[:200])
    time.sleep(0.4)
    return payload


def _inbox_source_hint(path: Path) -> str:
    name = path.name
    for source in ("dws", "zsxq"):
        if name.startswith(f"{source}-") or name.startswith(f"{source}."):
            return source
    return ""


def _read_host_auth(health_dir: Path) -> None:
    if not health_dir.is_dir():
        return
    for source in ("dws", "zsxq"):
        path = health_dir / f"{source}.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        state = str(payload.get("auth") or "")
        if state:
            get_store().mark_health(
                source,
                ok=state == "ok",
                error="" if state == "ok" else str(payload.get("detail") or "登录失效"),
                auth_state=state,
            )


def _hot_feed_items(limit: int = 50) -> list[dict]:
    """DSA 热门候选源。有具体事件时先给事件；没有时仍给板块和个股升温榜。"""
    today = cn_now().date().isoformat()
    items = []
    listing = hot_event_listing(limit=limit)
    for event in listing["events"]:
        items.append({
            "source_id": f"hot:event:{event['key']}:{today}",
            "title": event["name"],
            "summary": (
                f"具体事件。提及 {event['mentions']} 条，来源 {event['source_count']} 个，"
                f"首见 {event['first_seen'] or '未知'}。"
                f"代表标题：{event['headline'] or event['name']}。"
                "仅供内部研究。"
            ),
            "url": "",
            "published_at": cn_now().isoformat(timespec="seconds"),
            "symbols": [row["key"] for row in (*event["stocks"], *(event.get("etfs") or []))][:8],
            "sectors": list(event["concepts"])[:6],
        })
    if items:
        return items[:limit]
    for kind, label in (("sector", "热门板块"), ("stock", "热门个股"), ("etf", "热门ETF")):
        for candidate in hot_candidates(kind=kind, limit=8):
            symbols = [candidate.key] if kind in {"stock", "etf"} else []
            sectors = [candidate.key] if kind == "sector" else []
            items.append({
                "source_id": f"hot:{kind}:{candidate.key}:{today}",
                "title": f"{label}候选：{candidate.name}",
                "summary": (
                    f"近24小时 {candidate.story_count} 条故事，"
                    f"来源 {'/'.join(SOURCE_LABELS.get(s, s) for s in candidate.sources)}，"
                    f"相对基线 {candidate.growth} 倍。仅供内部研究。"
                ),
                "url": "",
                "published_at": cn_now().isoformat(timespec="seconds"),
                "symbols": symbols,
                "sectors": sectors,
            })
    return items[:limit]


def _symbol_keys(symbol: str) -> list[str]:
    text = (symbol or "").strip().upper()
    if not text:
        return []
    keys = [text]
    lexicon = get_lexicon()
    resolved, _name, code = lexicon.resolve(text)
    if resolved and resolved not in keys:
        keys.insert(0, resolved)
    if code:
        for suffix in (".SH", ".SZ", ".BJ"):
            formed = f"{code}{suffix}"
            if formed not in keys and (not lexicon.by_code or code in lexicon.by_code):
                known = lexicon.by_code.get(code)
                if known:
                    keys = [known[0]]
                    break
    return keys


def _title_from(clean: str) -> str:
    line = clean.split("\n", 1)[0].strip()
    return line[:40]


def _reserve_llm_call() -> bool:
    now = time.monotonic()
    with _LLM_LOCK:
        while _LLM_TIMES and now - _LLM_TIMES[0] > 3600:
            _LLM_TIMES.pop(0)
        if len(_LLM_TIMES) >= _LLM_PER_HOUR:
            return False
        _LLM_TIMES.append(now)
        return True


def _llm_text(prompt: str) -> str:
    try:
        import asyncio

        from app.services.ai_provider import generate_ai_text

        raw = asyncio.run(generate_ai_text(
            [{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=400,
            timeout=30,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.info("资讯 LLM 抽取失败: %s", exc)
        return ""
    return str(raw or "")


def _llm_mentions(title: str, clean: str, lexicon: Lexicon) -> list[Mention]:
    if not _reserve_llm_call():
        return []
    prompt = (
        "从下面的财经消息里抽出 A 股股票和板块。"
        "只输出 JSON：{\"stocks\":[{\"name\":\"\",\"code\":\"\"}],\"sectors\":[]}。"
        "没有把握就留空，不要编造代码。\n\n"
        f"标题：{title}\n正文：{excerpt(clean, 500)}"
    )
    raw = _llm_text(prompt)
    if not raw:
        return []
    return [
        Mention(item.kind, item.key, item.name, item.code, "llm")
        for item in parse_llm_payload(raw, lexicon)
    ]


def _llm_foreign_enrich(title: str, clean: str, lexicon: Lexicon) -> tuple[str, list[Mention]]:
    """只把标题和 feed 摘要交给模型，换一句中文摘要和词典内的股票、板块。"""
    if not _reserve_llm_call():
        return "", []
    prompt = (
        "下面是一条外文财经资讯的标题和来源给出的摘要，不是全文。"
        "只根据这些文字输出 JSON："
        "{\"summary_zh\":\"一句中文摘要\",\"stocks\":[{\"name\":\"\",\"code\":\"\"}],\"sectors\":[]}。"
        "不要编造未出现的事实，不要补写付费正文。没有把握的股票或板块留空。\n\n"
        f"标题：{title}\n摘要：{excerpt(clean, 500)}"
    )
    raw = _llm_text(prompt)
    if not raw:
        return "", []
    mentions = [
        Mention(item.kind, item.key, item.name, item.code, "llm")
        for item in parse_llm_payload(raw, lexicon)
    ]
    return parse_llm_summary(raw), mentions


def _merge_mentions(primary: list[Mention], extra: list[Mention]) -> list[Mention]:
    seen = {(item.kind, item.key) for item in primary}
    merged = list(primary)
    for item in extra:
        ident = (item.kind, item.key)
        if ident in seen:
            continue
        seen.add(ident)
        merged.append(item)
    return merged


def _row_summary(row, limit: int) -> str:
    zh = ""
    raw = ""
    if isinstance(row, dict):
        raw = row.get("extra_json") or ""
    else:
        keys = row.keys()
        if "extra_json" in keys:
            raw = row["extra_json"] or ""
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            zh = str(payload.get("summary_zh") or "").strip()
    if zh:
        return excerpt(zh, limit)
    return excerpt(row["clean_text"] or "", limit)


def _sector_name_ok(key: str, name: str) -> bool:
    return _usable_sector_name(key) and _usable_sector_name(name or key)


def _sector_names(repo) -> list[str]:
    if repo is None:
        return []
    try:
        import polars as pl

        from app.services.rps_rotation import _load_concept_map_df
    except Exception:  # noqa: BLE001
        return []
    names: list[str] = []
    wide = {"融资融券", "沪股通", "深股通", "沪深股通"}
    for kind in ("concept", "industry"):
        try:
            frame, _count = _load_concept_map_df(repo, kind)
        except Exception as exc:  # noqa: BLE001
            logger.debug("板块词典 %s 不可用: %s", kind, exc)
            continue
        if frame.is_empty() or kind not in frame.columns:
            continue
        counts = frame.group_by(kind).len()
        for row in counts.filter(pl.col("len") <= 800).iter_rows(named=True):
            name = str(row[kind] or "").strip()
            if name and name not in wide:
                names.append(name)
    return names


def _repo():
    try:
        from app.main import app
        repo = getattr(getattr(app, "state", None), "repo", None)
        return repo
    except Exception:  # noqa: BLE001
        return None
