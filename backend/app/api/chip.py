"""个股筹码分布。汇总和价格档都在本地用前复权日 K 估算，请求不打外部行情。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.chip.query import distribution
from app.chip.store import parse_day

router = APIRouter(prefix="/api/chip", tags=["chip"])


def _data_dir(request: Request):
    repo = getattr(request.app.state, "repo", None)
    if repo is not None:
        return repo.store.data_dir
    from app.config import settings

    return settings.data_dir


@router.get("/{symbol}")
def get_chip(
    symbol: str,
    request: Request,
    date: str | None = Query(default=None, description="交易日 YYYY-MM-DD，缺省为最近已落盘日"),
) -> dict:
    if date is not None and parse_day(date) is None:
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")
    payload = distribution(_data_dir(request), symbol, parse_day(date))
    if payload is None:
        raise HTTPException(status_code=404, detail="没有该日的日K，无法估算筹码分布")
    return payload
