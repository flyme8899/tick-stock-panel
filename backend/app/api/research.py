"""研报只读接口。拉取在后台线程，这些路径不下载 PDF。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.research_reports import query

router = APIRouter(prefix="/api/research", tags=["research"])


def _dir() -> Path:
    return Path(settings.data_dir)


@router.get("/health")
def health() -> dict:
    return query.health(_dir())


@router.get("/search")
def search(q: str = Query("", max_length=80), limit: int = Query(20, ge=1, le=50)) -> dict:
    """按标题、摘要、正文和标签搜全文库，包括没有个股映射的行业/综合研报。"""
    return {"query": q, "reports": query.ima_search(_dir(), q, limit=limit)}


@router.get("/stock/{symbol}")
def stock(symbol: str, limit: int = Query(20, ge=1, le=50)) -> dict:
    try:
        rows = query.for_symbol(_dir(), symbol, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"symbol": symbol, "reports": rows, "ima_reports": query.ima_for_symbol(_dir(), symbol, limit=limit)}


@router.get("/industry")
def industry(limit: int = Query(30, ge=1, le=100)) -> dict:
    return {"reports": query.industry(_dir(), limit=limit)}
