"""HTTP：热门候选、单股资讯、来源开关、给 DSA 拉的 feed。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.news.config import (
    SOURCE_ORDER,
    feed_matches,
    push_status,
    set_push_prefs,
    set_source_enabled,
)
from app.market_time import CN_TZ
from app.news.archive import MAX_LIMIT as _ARCHIVE_MAX, get_archive
from app.news.push import send_test
from app.news.service import (
    event_detail,
    feed_for_source,
    health_payload,
    hot_candidates,
    hot_event_listing,
    hot_messages,
    news_for_symbol,
    theme_board,
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


class MxHotUpdate(BaseModel):
    enabled: bool


class EtfFlowIngest(BaseModel):
    url: str = Field(..., min_length=8, max_length=800)


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
        payload["computed_at"] = listing.get("computed_at")
        payload["status"] = listing.get("status")
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


@router.get("/hot-rank")
def hot_rank(
    day: str = Query(..., alias="date", description="分区日 YYYY-MM-DD"),
    slot: str | None = Query(None, description="preopen、postclose 或 nextday；空则返回当天全部档"),
):
    """研究读取某一天的热点事件榜快照。没有这一天时 rows 为空。"""
    from app.config import settings
    from app.news.hot_rank import load_hot_event_rank

    try:
        session = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date 必须是 YYYY-MM-DD") from exc
    try:
        frame = load_hot_event_rank(settings.data_dir, session, slot)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "date": session.isoformat(),
        "slot": slot,
        "rows": frame.to_dicts(),
    }


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


@router.get("/archive")
def archive(
    start: date | None = Query(None, description="起始日（含），北京时间"),
    end: date | None = Query(None, description="结束日（含），北京时间"),
    source: str | None = Query(None, pattern="^[a-z0-9_]{1,32}$"),
    level: str | None = Query(None, max_length=32, description="逗号分隔，如 A,B"),
    q: str | None = Query(None, min_length=1, max_length=64),
    limit: int = Query(100, ge=1, le=_ARCHIVE_MAX),
    offset: int = Query(0, ge=0, le=100_000),
):
    """历史资讯归档，只读。不进推送和热点。"""
    levels = [x.strip() for x in (level or "").split(",") if x.strip()] or None
    items = get_archive().query(
        start=datetime.combine(start, time.min, CN_TZ) if start else None,
        end=datetime.combine(end + timedelta(days=1), time.min, CN_TZ) if end else None,
        source=source,
        levels=levels,
        keyword=q,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/archive/stats")
def archive_stats():
    return {"sources": get_archive().stats()}


@router.get("/stocks/{symbol}")
def stock_news(
    symbol: str,
    hours: int = Query(72, ge=1, le=24 * 30),
    limit: int = Query(30, ge=1, le=50),
):
    return news_for_symbol(symbol, hours=hours, limit=limit)


@router.get("/theme-board")
def theme_board_view(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(400, ge=1, le=2000),
):
    """盘后复盘板：星球复盘笔记里的主题表，**只读展示**。

    ``strength`` 为笔记作者手写的强弱判断，原样透传，不是买卖信号。
    """
    return theme_board(days=days, limit=limit)


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


@router.get("/mx-hot")
def mx_hot_state():
    from app.news.mx_hot import status

    return status()


@router.put("/mx-hot")
def update_mx_hot(body: MxHotUpdate):
    from app.news.mx_hot import set_enabled

    return set_enabled(body.enabled)


@router.post("/etf-flow/ingest")
def ingest_etf_flow_url(body: EtfFlowIngest):
    """用一篇 mp.weixin.qq.com 原文补采 ETF领航者。短链会先解析出 sn。

    只接受面板登录会话。不进开放 API Token 清单。
    """
    from app.news.etf_flow import ExtractFailedError, _public_error, ingest_wechat_url

    try:
        return ingest_wechat_url(body.url)
    except ExtractFailedError as exc:
        raise HTTPException(status_code=400, detail=_public_error(exc) or "采集失败") from exc


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
