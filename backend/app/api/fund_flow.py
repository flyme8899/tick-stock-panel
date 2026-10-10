"""资金进出只读接口。拉取在后台线程，这些路径不触发出网。"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.fund_flow import query

router = APIRouter(prefix="/api/fund-flow", tags=["fund-flow"])


@router.get("/health")
def health() -> dict:
    return query.health()


@router.get("/board")
def board() -> dict:
    return query.board()


@router.get("/stock/{symbol}")
def stock(symbol: str, limit: int = Query(120, ge=1, le=500)) -> dict:
    return query.stock_series(symbol, limit=limit)


@router.get("/sectors")
def sectors(
    kind: str = Query(..., pattern="industry|concept"),
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict:
    return query.sectors(kind, trade_date=date)


@router.get("/margin")
def margin(
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    detail_limit: int = Query(100, ge=0, le=500),
) -> dict:
    return query.margin(trade_date=date, detail_limit=detail_limit)


@router.get("/etf-shares")
def etf_shares(date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$")) -> dict:
    return query.etf_shares(trade_date=date)


@router.get("/dsa-context")
def dsa_context() -> dict:
    return query.dsa_context()
