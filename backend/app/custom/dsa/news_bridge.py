"""把 TSP 资讯接进 DSA 情报库。

DSA 进程启动时由 dsa_bootstrap 调用 install()。不改 vendor 文件：
运行时放行 source_type=tsp、允许访问配置好的 TSP 地址，并把每条资讯按
股票 / 板块展开后写入 intelligence_items。TSP 没开、令牌没配或网络失败
都只记日志，不挡住 DSA 启动和分析。

个股分析读 scope_type=symbol，大盘复盘读 market。热门候选另有一个来源，
并在大盘复盘合并新闻时插到前面。
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("tsp.dsa.news_bridge")

CN = timezone(timedelta(hours=8))
_SOURCES = (
    ("dws", "钉钉作文实时", "钉钉群「作文实时」，由宿主机只读采集后交给 TSP。"),
    ("zsxq", "知识星球纳指星球调研", "知识星球「纳指星球调研」，由宿主机只读采集后交给 TSP。"),
    ("ima", "ima爱分享", "ima 知识库「【爱分享】的财经资讯」，只同步标题。"),
    ("cls", "财联社", "财联社电报，保留级别、个股和板块标签。"),
    ("wscn", "华尔街见闻", "华尔街见闻快讯，保留标的、主题和热度。"),
    ("etf_flow", "ETF领航者", "公众号 ETF领航者的每日 ETF 申购赎回。优先读网易号，表格图片由视觉模型抽取。"),
    ("cnbc", "CNBC", "CNBC 头条与市场 RSS，只存标题和摘要。"),
    ("marketwatch", "MarketWatch", "MarketWatch 头条 RSS，只存标题和摘要。"),
    ("wsj", "华尔街日报市场", "华尔街日报市场 RSS，只存标题和摘要，不抓付费正文。"),
    ("bloomberg", "彭博", "彭博市场与科技 RSS，只存标题和摘要，不抓付费正文。"),
    ("scmp", "南华早报", "南华早报商业与中国经济 RSS，只存标题、摘要和链接，不抓付费正文。"),
    ("reuters", "路透", "路透商业、市场与国际 sitemap，只存标题和链接。主源失败时改用 Google News。"),
    (
        "reddit",
        "Reddit",
        "Reddit 的 wallstreetbets、stocks、investing 新帖 Atom，只存标题、摘要、链接、作者和时间。每次只请求一个子版。",
    ),
    ("sec", "SEC 8-K", "SEC 最新 8-K Atom，只存标题、摘要和申报链接。"),
    ("hot", "TSP热门候选", "TSP 按当天资讯聚类的具体事件；没有事件时仍给热门板块和个股，供大盘复盘引用。"),
)
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal", "app", "tsp"}
_COOLDOWN_S = 180
_lock = threading.Lock()
_last_sync = 0.0
_started = False


def feed_base_url() -> str:
    raw = (os.environ.get("TSP_NEWS_BASE_URL") or "http://127.0.0.1:3018").strip().rstrip("/")
    return raw


def feed_token() -> str:
    return (os.environ.get("NEWS_DSA_FEED_TOKEN") or "").strip()


def is_tsp_feed_url(url: str) -> bool:
    parsed = urlparse(url)
    if (parsed.path or "").rstrip("/") != "/api/news/dsa-feed":
        return False
    host = (parsed.hostname or "").lower()
    allowed = set(_ALLOWED_HOSTS)
    extra = urlparse(feed_base_url()).hostname
    if extra:
        allowed.add(extra.lower())
    return host in allowed


def item_url(item: dict) -> str:
    url = str(item.get("url") or "").strip()
    if url.startswith(("http://", "https://")) and len(url) <= 1000:
        return url
    digest = hashlib.sha256(
        f"{item.get('source_id')}|{item.get('title')}".encode()
    ).hexdigest()[:24]
    return f"no-url:intel:{digest}"


def parse_published(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(CN).replace(tzinfo=None)
    return parsed


def expand_item(item: dict, *, source_id: int, source_name: str, now: datetime) -> list[dict]:
    """一条资讯写成市场行，再按股票、板块各写一行。标签因此能被个股分析命中。"""
    title = str(item.get("title") or "未命名资讯").strip()[:300]
    summary = str(item.get("summary") or "").strip()[:2000]
    if not title:
        return []
    base = {
        "source_id": source_id,
        "source_name": source_name[:100],
        "source_type": "tsp",
        "title": title,
        "summary": summary,
        "url": item_url(item),
        "source": source_name[:100],
        "published_at": parse_published(item.get("published_at")) or now,
        "fetched_at": now,
        "market": "cn",
        "raw_payload": "",
    }
    scopes: list[tuple[str, str | None]] = [("market", None)]
    for symbol in list(item.get("symbols") or [])[:8]:
        text = str(symbol).strip()
        if text:
            scopes.append(("symbol", text[:64]))
    for sector in list(item.get("sectors") or [])[:6]:
        text = str(sector).strip()
        if text:
            scopes.append(("sector", text[:64]))
    rows = []
    seen: set[tuple[str, str | None]] = set()
    for scope_type, scope_value in scopes:
        if (scope_type, scope_value) in seen:
            continue
        seen.add((scope_type, scope_value))
        rows.append({**base, "scope_type": scope_type, "scope_value": scope_value})
    return rows


def hot_news_rows(items: list[dict]) -> list[dict]:
    rows = []
    for item in items[:4]:
        rows.append({
            "title": item.get("title") or "热门候选",
            "snippet": item.get("summary") or "",
            "source": "TSP热门候选",
            "published_date": str(item.get("published_at") or ""),
            "url": "",
        })
    return rows


def _fetch_json(source_key: str) -> dict:
    import requests

    token = feed_token()
    if not token:
        raise RuntimeError("未配置 NEWS_DSA_FEED_TOKEN")
    url = f"{feed_base_url()}/api/news/dsa-feed"
    if not is_tsp_feed_url(f"{url}?source={source_key}"):
        raise RuntimeError("TSP 资讯地址不在允许的主机名单里")
    response = requests.get(
        url,
        params={"source": source_key, "limit": 50},
        headers={"X-News-Feed-Token": token, "User-Agent": "tsp-dsa-news-bridge/1.0"},
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("TSP feed 不是 JSON 对象")
    return payload


def sync_tsp_sources(service, *, fetch: bool = True) -> dict:
    """建好各资讯源和热门候选源，并按冷却时间拉取。DSA 不可用时由调用方吞掉异常。"""
    global _last_sync
    if not feed_token():
        return {"skipped": True, "reason": "token"}
    _ensure_sources(service)
    now = time.monotonic()
    with _lock:
        if not fetch or now - _last_sync < _COOLDOWN_S:
            return {"skipped": True, "reason": "cooldown" if fetch else "ensure-only"}
        _last_sync = now
    saved = 0
    errors = []
    for _key, name, _desc in _SOURCES:
        source = _find_source(service, name)
        if source is None or not source.enabled:
            continue
        try:
            result = service.fetch_source(source.id)
            saved += int(result.get("saved_count") or 0)
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": name, "error": str(exc)[:160]})
            logger.warning("同步 TSP 资讯源失败 %s: %s", name, exc)
    return {"saved": saved, "errors": errors}


def _ensure_sources(service) -> None:
    base = feed_base_url()
    for key, name, description in _SOURCES:
        if _find_source(service, name) is not None:
            continue
        service.create_source({
            "name": name,
            "source_type": "tsp",
            "url": f"{base}/api/news/dsa-feed?source={key}",
            "enabled": True,
            "scope_type": "market",
            "market": "cn",
            "description": description,
        })


def _find_source(service, name: str):
    rows, _total = service.repo.list_sources(page=1, page_size=100)
    for row in rows:
        if row.name == name:
            return row
    return None


def _retention_days(service) -> int:
    config = getattr(service, "config", None)
    raw = getattr(config, "news_intel_retention_days", 30) if config is not None else 30
    try:
        days = int(raw or 30)
    except (TypeError, ValueError):
        days = 30
    return days if days > 0 else 30


def _disabled_error(source_id: int) -> Exception:
    message = f"Intelligence source is disabled: {source_id}"
    try:
        from src.services.intelligence_service import IntelligenceServiceError
    except ImportError:
        return RuntimeError(message)
    return IntelligenceServiceError(message)


def _feed_samples(items: list[dict], source_name: str) -> list[dict]:
    samples = []
    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        published = item.get("published_at")
        if isinstance(published, datetime):
            published = published.isoformat(timespec="seconds")
        samples.append({
            "title": title[:300],
            "summary": str(item.get("summary") or "")[:2000],
            "url": str(item.get("url") or ""),
            "source": source_name,
            "published_at": str(published or ""),
        })
        if len(samples) >= 5:
            break
    return samples


def _fetch_tsp_source(service, source, *, dry_run: bool) -> dict:
    if not getattr(source, "enabled", False):
        raise _disabled_error(source.id)
    now = datetime.now()
    try:
        key = _source_key(source.url)
        payload = _fetch_json(key)
        items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
        rows = []
        for item in items:
            rows.extend(expand_item(item, source_id=source.id, source_name=source.name, now=now))
        saved = 0 if dry_run else service.repo.upsert_items(rows)
        deleted = 0
        if not dry_run:
            deleted = int(service.repo.apply_retention(_retention_days(service)) or 0)
            service.repo.update_source_status(source.id, status="success", error=None, fetched_at=now)
        return {
            "ok": True,
            "source_id": source.id,
            "fetched_count": len(items),
            "saved_count": saved,
            "retention_deleted": deleted,
            "dry_run": dry_run,
            "sample_items": _feed_samples(items, source.name),
        }
    except Exception as exc:
        if not dry_run:
            service.repo.update_source_status(source.id, status="failed", error=str(exc)[:300])
        raise


def _source_key(url: str) -> str:
    parsed = urlparse(url)
    for part in parsed.query.split("&"):
        if part.startswith("source="):
            return part.split("=", 1)[1]
    return ""


def install() -> None:
    """在 DSA 进程里打补丁。不在 DSA 里导入时直接返回。"""
    global _started
    token = feed_token()
    if not token:
        logger.info("未配置 NEWS_DSA_FEED_TOKEN，DSA 不拉取 TSP 资讯")
        return
    try:
        import src.services.intelligence_service as intel
    except ImportError:
        logger.info("当前不是 DSA 进程，跳过资讯桥")
        return
    intel._ALLOWED_SOURCE_TYPES.add("tsp")
    _patch_validate(intel)
    _patch_fetch(intel)
    _patch_refresh(intel)
    _patch_market_review()
    if _started:
        return
    _started = True
    thread = threading.Thread(target=_background, name="tsp-news-bridge", daemon=True)
    thread.start()
    logger.info("已安装 TSP 资讯桥：%s", feed_base_url())


def _patch_validate(intel) -> None:
    original = intel.IntelligenceService._validate_url

    def _validate_url(self, raw_url: str, *, allow_no_url: bool = False) -> None:
        if is_tsp_feed_url(raw_url):
            return
        return original(self, raw_url, allow_no_url=allow_no_url)

    if getattr(original, "_tsp_news", False):
        return
    _validate_url._tsp_news = True  # type: ignore[attr-defined]
    intel.IntelligenceService._validate_url = _validate_url


def _patch_fetch(intel) -> None:
    original = intel.IntelligenceService.fetch_source
    if getattr(original, "_tsp_news", False):
        return

    def fetch_source(self, source_id: int, *, dry_run: bool = False) -> dict:
        source = self.repo.get_source(source_id)
        if source is not None and source.source_type == "tsp":
            return _fetch_tsp_source(self, source, dry_run=dry_run)
        return original(self, source_id, dry_run=dry_run)

    fetch_source._tsp_news = True  # type: ignore[attr-defined]
    intel.IntelligenceService.fetch_source = fetch_source


def _patch_refresh(intel) -> None:
    original = intel.IntelligenceService.refresh_auto_sources
    if getattr(original, "_tsp_news", False):
        return

    def refresh_auto_sources(self, *, force: bool = False) -> dict:
        try:
            sync_tsp_sources(self, fetch=True)
        except Exception:  # noqa: BLE001
            logger.warning("分析前同步 TSP 资讯失败，沿用本地情报库", exc_info=True)
        return original(self, force=force)

    refresh_auto_sources._tsp_news = True  # type: ignore[attr-defined]
    intel.IntelligenceService.refresh_auto_sources = refresh_auto_sources


def _patch_market_review() -> None:
    try:
        import src.market_analyzer as market
    except ImportError:
        return
    original = market.MarketAnalyzer._merge_persisted_market_intelligence
    if getattr(original, "_tsp_news", False):
        return

    def _merge(self, news):
        merged = original(self, news)
        try:
            payload = _fetch_json("hot")
            extra = hot_news_rows(payload.get("items") or [])
        except Exception:  # noqa: BLE001
            extra = []
        return extra + list(merged or [])

    _merge._tsp_news = True  # type: ignore[attr-defined]
    market.MarketAnalyzer._merge_persisted_market_intelligence = _merge


def _background() -> None:
    time.sleep(20)
    while True:
        try:
            from src.services.intelligence_service import IntelligenceService

            sync_tsp_sources(IntelligenceService(), fetch=True)
        except Exception:  # noqa: BLE001
            logger.warning("TSP 资讯后台同步失败，下一轮再试", exc_info=True)
        time.sleep(300)
