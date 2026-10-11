"""打板只读接口。拉取在后台线程，这些路径不触发出网。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query

from app.config import settings
from app.limit_board import query, store

router = APIRouter(prefix="/api/limit-board", tags=["limit-board"])


def _dir() -> Path:
    return Path(settings.data_dir)


@router.get("/health")
def health() -> dict:
    return query.health(_dir())


@router.get("/sentiment")
def sentiment(
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict:
    day = date or store.latest_date(_dir(), "sentiment")
    if not day:
        return {"trade_date": None, "sentiment": None}
    return {"trade_date": day, "sentiment": query.sentiment_for(_dir(), day)}
