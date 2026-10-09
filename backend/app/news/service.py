"""采集编排：清洗、抽取、入库、热门候选。不在请求线程里打外部网络。"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta
from pathlib import Path

import httpx

from app.config import settings
from app.market_time import cn_now
from app.news.cleaning import clean_text, content_hash, excerpt
from app.news.collectors import (
    CLS_URL,
    IMA_BASE,
    WSCN_URL,
    Item,
    cls_params,
    ima_headers,
    ima_retcode,
    latest_date_folders,
    load_inbox_payload,
    parse_cls,
    parse_dws_payload,
    parse_ima_titles,
    parse_time,
    parse_wscn,
    parse_zsxq_payload,
    pick_knowledge_base,
    split_ima_list,
    wscn_params,
)
from app.news.config import (
    SOURCE_LABELS,
    SOURCE_ORDER,
    llm_extract_enabled,
    source_configured,
    source_enabled,
    source_locked,
)
from app.news.extract import Lexicon, Mention, StructuredStock, parse_llm_payload
from app.news.scoring import MentionEvent, score_candidates
from app.news.store import NewsStore

logger = logging.getLogger(__name__)

_STORE: NewsStore | None = None
_STORE_LOCK = threading.Lock()
_LEXICON: tuple[float, Lexicon] | None = None
_LLM_TIMES: list[float] = []
_LLM_LOCK = threading.Lock()
_LLM_PER_HOUR = 10


def get_store() -> NewsStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = NewsStore(settings.data_dir / "news" / "news.sqlite")
        return _STORE


def reset_store_for_tests(path: Path | None = None) -> NewsStore:
    global _STORE, _LEXICON
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = NewsStore(path or (settings.data_dir / "news" / "news.sqlite"))
        _LEXICON = None
        return _STORE


def lexicon_from_repo(repo) -> Lexicon:
    stocks: list[tuple[str, str, str]] = []
    if repo is not None:
        for getter in ("get_instruments", "get_etf_instruments"):
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
    lexicon = lexicon or get_lexicon()
    inserted = 0
    duplicate = 0
    for item in items:
        if not item.source_id:
            continue
        clean = clean_text(item.text)
        title = clean_text(item.title) or _title_from(clean)
        digest = content_hash(clean, item.media_ids)
        mentions = lexicon.extract(
            f"{title}\n{clean}",
            item.stocks,
            item.sectors,
        )
        if not mentions and llm_extract_enabled() and len(clean) >= 40:
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
            extra={"stocks": [s.__dict__ for s in item.stocks], "sectors": item.sectors},
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


def hot_candidates(*, kind: str = "all", window_hours: int = 24, baseline_days: int = 4, limit: int = 20):
    now = cn_now()
    start = now - timedelta(hours=window_hours, days=baseline_days)
    events = []
    for row in get_store().mention_events_since(start):
        published = parse_time(row["published_at"])
        if published is None:
            continue
        events.append(MentionEvent(
            kind=row["kind"],
            key=row["key"],
            name=row["name"],
            source=row["source"],
            content_hash=row["content_hash"],
            published_at=published,
        ))
    ranked = score_candidates(events, now=now, window_hours=window_hours, baseline_days=baseline_days)
    if kind in {"stock", "sector"}:
        ranked = [item for item in ranked if item.kind == kind]
    return ranked[: max(1, min(limit, 50))]


def message_view(row, *, limit: int = 240) -> dict:
    return {
        "source": row["source"],
        "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
        "published_at": row["published_at"],
        "author": row["author"] or "",
        "title": row["title"] or "",
        "excerpt": excerpt(row["clean_text"] or "", limit),
        "url": row["url"] or "",
        "level": row["level"] or "",
    }


def hot_messages(kind: str, key: str, *, window_hours: int = 24, limit: int = 30) -> list[dict]:
    start = cn_now() - timedelta(hours=window_hours)
    rows = get_store().messages_for(kind=kind, key=key, start=start, limit=limit)
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
        return {"source": "hot", "name": SOURCE_LABELS["hot"], "items": _hot_feed_items()}
    if source not in SOURCE_ORDER:
        raise ValueError(f"未知来源 {source}")
    items = []
    for row in get_store().recent_for_feed(source, limit):
        symbols = []
        sectors = []
        for mention in row["mentions"]:
            if mention["kind"] == "stock":
                symbols.append(mention["key"])
            elif mention["kind"] == "sector":
                sectors.append(mention["key"])
        items.append({
            "source_id": row["source_id"],
            "title": row["title"] or excerpt(row["clean_text"], 40),
            "summary": excerpt(row["clean_text"], 400),
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
        response = client.get(
            CLS_URL,
            params=cls_params(),
            headers={"Referer": "https://www.cls.cn/telegraph", "User-Agent": "tsp-news/1.0"},
        )
        response.raise_for_status()
        items = parse_cls(response.json())
        result = ingest_items(items)
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
            response = client.get(
                WSCN_URL,
                params=wscn_params(channel),
                headers={"User-Agent": "tsp-news/1.0"},
            )
            response.raise_for_status()
            items.extend(parse_wscn(response.json(), channel))
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
        listing = _ima_post(client, headers, "get_knowledge_list", {
            "knowledge_base_id": kb_id,
            "cursor": "",
            "limit": 50,
        })
        folders, _files = split_ima_list(listing)
        picked = latest_date_folders(folders, 2)
        items: list[Item] = []
        if not picked:
            _folders, files = split_ima_list(listing)
            items.extend(parse_ima_titles(files, ""))
        for folder in picked:
            folder_id = str(folder.get("folder_id") or "")
            body = {
                "knowledge_base_id": kb_id,
                "cursor": "",
                "limit": 50,
            }
            if folder_id:
                body["folder_id"] = folder_id
            page = _ima_post(client, headers, "get_knowledge_list", body)
            _sub, files = split_ima_list(page)
            items.extend(parse_ima_titles(files, str(folder.get("name") or "")))
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
    inserted = 0
    duplicate = 0
    errors = 0
    for path in sorted(inbox.glob("*.json"))[:20]:
        try:
            source, payload = load_inbox_payload(path.read_text(encoding="utf-8"))
            if source == "dws":
                items = parse_dws_payload(payload)
            else:
                items, _page = parse_zsxq_payload(payload)
            if not source_enabled(source):
                continue
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
    _read_host_auth(inbox.parent / "health")
    return {"inserted": inserted, "duplicate": duplicate, "errors": errors}


def run_due(source: str) -> dict:
    if source == "cls":
        return collect_cls()
    if source == "wscn":
        return collect_wscn()
    if source == "ima":
        return collect_ima()
    if source in {"dws", "zsxq"}:
        return collect_inbox()
    return {}


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


def _hot_feed_items() -> list[dict]:
    today = cn_now().date().isoformat()
    items = []
    for kind, label in (("sector", "热门板块"), ("stock", "热门个股")):
        for candidate in hot_candidates(kind=kind, limit=8):
            symbols = [candidate.key] if kind == "stock" else []
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
    return items


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


def _llm_mentions(title: str, clean: str, lexicon: Lexicon) -> list[Mention]:
    now = time.monotonic()
    with _LLM_LOCK:
        while _LLM_TIMES and now - _LLM_TIMES[0] > 3600:
            _LLM_TIMES.pop(0)
        if len(_LLM_TIMES) >= _LLM_PER_HOUR:
            return []
        _LLM_TIMES.append(now)
    prompt = (
        "从下面的财经消息里抽出 A 股股票和板块。"
        "只输出 JSON：{\"stocks\":[{\"name\":\"\",\"code\":\"\"}],\"sectors\":[]}。"
        "没有把握就留空，不要编造代码。\n\n"
        f"标题：{title}\n正文：{excerpt(clean, 500)}"
    )
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
        return []
    mentions = parse_llm_payload(raw, lexicon)
    return [
        Mention(item.kind, item.key, item.name, item.code, "llm")
        for item in mentions
    ]


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
