"""个股买卖点 HTTP 壳。计算在 services.trade_marks。"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from app.services.trade_marks import build_payload

router = APIRouter(tags=["trade-signals"])

_SYMBOL = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _parse_day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} 需要 YYYY-MM-DD") from exc


@router.get("/api/signals/{symbol}")
def get_trade_signals(
    symbol: str,
    request: Request,
    strategy: str = Query(""),
    start: str = Query(...),
    end: str = Query(...),
    intraday: str = Query(""),
):
    """策略买卖点、统计、DSA 价位、模拟盘成交和做T标记。结果按标的与区间缓存。"""
    if not _SYMBOL.match(symbol):
        raise HTTPException(status_code=400, detail="标的代码无效")
    start_day = _parse_day(start, "start")
    end_day = _parse_day(end, "end")
    if start_day > end_day:
        raise HTTPException(status_code=400, detail="start 不能晚于 end")
    intraday_day = _parse_day(intraday, "intraday") if intraday else None
    engine = getattr(request.app.state, "strategy_engine", None)
    strategy_id = strategy.strip() or None
    if strategy_id and engine is None:
        raise HTTPException(status_code=404, detail=f"unknown strategy: {strategy_id}")
    if strategy_id and engine is not None and not engine.has(strategy_id):
        raise HTTPException(status_code=404, detail=f"unknown strategy: {strategy_id}")
    try:
        return build_payload(
            repo=request.app.state.repo,
            engine=engine,
            data_dir=_data_dir(request),
            symbol=symbol,
            strategy_id=strategy_id,
            start=start_day,
            end=end_day,
            intraday=intraday_day,
        )
    except ValueError as exc:
        if str(exc).startswith("unknown strategy"):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise
