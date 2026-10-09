"""HTTP：热门候选、单股资讯、来源开关、给 DSA 拉的 feed。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.news.config import SOURCE_ORDER, feed_token, set_source_enabled
from app.news.service import (
    feed_for_source,
    health_payload,
    hot_candidates,
    hot_messages,
    news_for_symbol,
)

router = APIRouter(prefix="/api/news", tags=["news"])


class SourceToggle(BaseModel):
    enabled: bool


class SourceUpdate(BaseModel):
    sources: dict[str, SourceToggle] = Field(default_factory=dict)


def feed_authorized(token: str) -> bool:
    expected = feed_token()
    return bool(expected) and token == expected


@router.get("/hot")
def hot(
    kind: str = Query("all", pattern="all|stock|sector"),
    window_hours: int = Query(24, ge=1, le=168),
    baseline_days: int = Query(4, ge=1, le=14),
    limit: int = Query(20, ge=1, le=50),
):
    rows = hot_candidates(
        kind=kind,
        window_hours=window_hours,
        baseline_days=baseline_days,
        limit=limit,
    )
    return {
        "kind": kind,
        "window_hours": window_hours,
        "baseline_days": baseline_days,
        "candidates": [
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
        ],
    }


@router.get("/messages")
def messages(
    kind: str = Query(..., pattern="stock|sector"),
    key: str = Query(..., min_length=1, max_length=64),
    window_hours: int = Query(24, ge=1, le=168),
    limit: int = Query(30, ge=1, le=50),
):
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


@router.get("/dsa-feed")
def dsa_feed(
    source: str = Query(..., pattern="dws|zsxq|ima|cls|wscn|hot"),
    limit: int = Query(50, ge=1, le=50),
    token: str = Query(""),
    header_token: str = Header("", alias="X-News-Feed-Token"),
):
    if not feed_authorized(token or header_token):
        raise HTTPException(status_code=404, detail="未启用")
    try:
        return feed_for_source(source, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
