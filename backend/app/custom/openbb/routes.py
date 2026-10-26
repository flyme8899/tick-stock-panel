"""固定 GET 研究端点；没有任意上游路径或方法转发。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Response

from app.plugins.openbb.client import OpenBBClient, OpenBBError, status as openbb_status


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/openbb", tags=["openbb"])

    @router.get("/status")
    def status() -> dict:
        return openbb_status()

    def query(dataset: str, symbol: str, start_date: date | None, end_date: date | None) -> dict:
        try:
            return OpenBBClient().query(dataset, symbol, start_date, end_date)
        except OpenBBError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.get("/hk_daily", response_model=None)
    def hk_daily(response: Response, symbol: str = "0700.HK", start_date: date | None = None, end_date: date | None = None) -> dict:
        response.headers["X-Research-Only"] = "true"
        return query("hk_daily", symbol, start_date, end_date)

    @router.get("/us_daily", response_model=None)
    def us_daily(response: Response, symbol: str = "AAPL", start_date: date | None = None, end_date: date | None = None) -> dict:
        response.headers["X-Research-Only"] = "true"
        return query("us_daily", symbol, start_date, end_date)

    @router.get("/macro", response_model=None)
    def macro(response: Response, symbol: str = "DGS10", start_date: date | None = None, end_date: date | None = None) -> dict:
        response.headers["X-Research-Only"] = "true"
        return query("macro", symbol, start_date, end_date)

    return router
