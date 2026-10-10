"""HTTP：热门候选、单股资讯、来源开关、给 DSA 拉的 feed。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.news.config import (
    SOURCE_ORDER,
    feed_matches,
    push_status,
    set_push_prefs,
    set_source_enabled,
)
from app.news.push import send_test
from app.news.service import (
    event_detail,
    feed_for_source,
    health_payload,
    hot_candidates,
    hot_event_listing,
    hot_messages,
    news_for_symbol,
)

router = APIRouter(prefix="/api/news", tags=["news"])

_FEED_SOURCE_PATTERN = "^(?:" + "|".join((*SOURCE_ORDER, "hot")) + ")$"


class SourceToggle(BaseModel):
    enabled: bool


class SourceUpdate(BaseModel):
    sources: dict[str, SourceToggle] = Field(default_factory=dict)


class PushUpdate(BaseModel):
    enabled: bool | None = None
    types: dict[str, bool] = Field(default_factory=dict)


class PushTest(BaseModel):
    confirm: bool = False


@router.get("/hot")
def hot(
    kind: str = Query("all", pattern="all|stock|sector|etf|event"),
    window_hours: int = Query(24, ge=1, le=168),
    baseline_days: int = Query(4, ge=1, le=14),
    limit: int = Query(20, ge=1, le=50),
):
    """具体事件是主列表。板块、个股和 ETF 仍是近 24 小时相对基线的升温榜。"""
    payload = {
        "kind": kind,
        "window_hours": window_hours,
        "baseline_days": baseline_days,
        "candidates": [],
    }
    if kind in {"all", "event"}:
        listing = hot_event_listing(limit=limit)
        payload["events"] = listing["events"]
        payload["as_of"] = listing["as_of"]
        payload["trading_day"] = listing["trading_day"]
        payload["fallback"] = listing["fallback"]
        payload["hint"] = listing["hint"]
        payload["updated_at"] = listing["updated_at"]
    if kind != "event":
        rows = hot_candidates(
            kind=kind,
            window_hours=window_hours,
            baseline_days=baseline_days,
            limit=limit,
        )
        candidates = [
            {
                "kind": item.kind,
                "key": item.key,
                "name": item.name,
                "score": item.score,
                "story_count": item.story_count,
                "effective_mentions": item.effective_mentions,
                "sources": list(item.sources),
                "growth": item.growth,
                "baseline_effective": item.baseline_effective,
            }
            for item in rows
        ]
        payload["candidates"] = _attach_fund_flow(candidates)
    return payload


def _attach_fund_flow(candidates: list[dict]) -> list[dict]:
    """热门候选带上资金字段。读失败时保持原列表，页面仍能打开。"""
    try:
        from app.config import settings
        from app.fund_flow.factors import annotate_hot

        return annotate_hot(candidates, settings.data_dir)
    except Exception:  # noqa: BLE001
        return candidates


@router.get("/messages")
def messages(
    kind: str = Query(..., pattern="stock|sector|etf|event"),
    key: str = Query(..., min_length=1, max_length=64),
    window_hours: int = Query(24, ge=1, le=168),
    limit: int = Query(30, ge=1, le=50),
):
    if kind == "event":
        if "/" in key or ".." in key:
            raise HTTPException(status_code=422, detail="事件 id 无效")
        return event_detail(key, limit=limit)
    return {
        "kind": kind,
        "key": key,
        "items": hot_messages(kind, key, window_hours=window_hours, limit=limit),
    }


@router.get("/stocks/{symbol}")
def stock_news(
    symbol: str,
    hours: int = Query(72, ge=1, le=24 * 30),
    limit: int = Query(30, ge=1, le=50),
):
    return news_for_symbol(symbol, hours=hours, limit=limit)


@router.get("/health")
def health():
    return health_payload()


@router.put("/sources")
def update_sources(body: SourceUpdate):
    applied = {}
    for source, toggle in body.sources.items():
        if source not in SOURCE_ORDER:
            raise HTTPException(status_code=400, detail=f"未知来源 {source}")
        applied[source] = set_source_enabled(source, toggle.enabled)
    return {"applied": applied, **health_payload()}


@router.get("/push")
def push_state():
    return push_status()


@router.put("/push")
def update_push(body: PushUpdate):
    try:
        return set_push_prefs(enabled=body.enabled, types=body.types)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/push/test")
def push_test(body: PushTest):
    """只有请求体明确 confirm=true 才发。不会在打开页面时自动发送。"""
    try:
        return send_test(confirm=body.confirm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@router.get("/dsa-feed")
def dsa_feed(
    source: str = Query(..., pattern=_FEED_SOURCE_PATTERN),
    limit: int = Query(50, ge=1, le=50),
    header_token: str = Header("", alias="X-News-Feed-Token"),
):
    if not feed_matches(header_token):
        raise HTTPException(status_code=404, detail="未启用")
    try:
        return feed_for_source(source, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
