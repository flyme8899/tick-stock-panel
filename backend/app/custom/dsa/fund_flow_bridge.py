"""把本地资金进出摘要接进 DSA 大盘复盘。

不改 vendor。令牌和主机沿用资讯桥。TSP 没开、没有落盘或网络失败时，
复盘仍用原来的新闻，不插入空数字。
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger("tsp.dsa.fund_flow_bridge")

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal", "app", "tsp"}


def feed_base_url() -> str:
    raw = (os.environ.get("TSP_NEWS_BASE_URL") or "http://127.0.0.1:3018").strip().rstrip("/")
    return raw


def feed_token() -> str:
    return (os.environ.get("NEWS_DSA_FEED_TOKEN") or "").strip()


def context_url() -> str:
    return f"{feed_base_url()}/api/fund-flow/dsa-context"


def is_allowed_context_url(url: str) -> bool:
    parsed = urlparse(url)
    if (parsed.path or "").rstrip("/") != "/api/fund-flow/dsa-context":
        return False
    host = (parsed.hostname or "").lower()
    allowed = set(_ALLOWED_HOSTS)
    extra = urlparse(feed_base_url()).hostname
    if extra:
        allowed.add(extra.lower())
    return host in allowed


def context_rows(payload: dict) -> list[dict]:
    rows = []
    for item in (payload.get("items") or [])[:2]:
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not title or not summary:
            continue
        rows.append({
            "title": title[:300],
            "snippet": summary[:2000],
            "source": "TSP资金进出",
            "published_date": str(item.get("published_at") or ""),
            "url": "",
        })
    return rows


def fetch_context(*, getter=None) -> dict:
    if getter is not None:
        return getter()
    import requests

    token = feed_token()
    url = context_url()
    if not token or not is_allowed_context_url(url):
        return {"items": []}
    response = requests.get(
        url,
        headers={"X-News-Feed-Token": token, "User-Agent": "tsp-dsa-fund-flow/1.0"},
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {"items": []}


def install() -> None:
    token = feed_token()
    if not token:
        logger.info("未配置 NEWS_DSA_FEED_TOKEN，DSA 不读取资金进出")
        return
    try:
        import src.market_analyzer as market
    except ImportError:
        logger.info("当前不是 DSA 进程，跳过资金进出桥")
        return
    original = market.MarketAnalyzer._merge_persisted_market_intelligence
    if getattr(original, "_tsp_fund_flow", False):
        return

    def _merge(self, news):
        merged = original(self, news)
        try:
            extra = context_rows(fetch_context())
        except Exception:  # noqa: BLE001
            logger.warning("读取 TSP 资金进出失败，复盘不插入这段", exc_info=True)
            extra = []
        return extra + list(merged or [])

    _merge._tsp_fund_flow = True  # type: ignore[attr-defined]
    market.MarketAnalyzer._merge_persisted_market_intelligence = _merge
    logger.info("已安装资金进出桥：%s", context_url())
