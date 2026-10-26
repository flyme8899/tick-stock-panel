"""TSP-facing routes. The browser never talks to the DSA process directly."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.custom.dsa.catalog import catalog_payload
from app.custom.dsa.commands import CommandError, execute
from app.custom.dsa.jobs import run_etf_rotation
from app.custom.dsa.ledger_audit import ledger_audit_payload
from app.custom.dsa.proxy import (
    InvalidUpstreamPathError,
    UpstreamError,
    base_url,
    enabled,
    forward,
    health,
)
from app.custom.dsa.quant_evidence import evidence_summary
from app.custom.dsa.schedule import ScheduleSettingsError, load_schedule, save_schedule
from app.custom.dsa.stock_context import load_context_cached


class BotCommandRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SniperLineRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    name: str = ""
    analysis_id: str = ""
    analyzed_at: str = ""
    ideal_buy: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None


class ScheduleRequest(BaseModel):
    enabled: bool = False
    time: str = "18:00"
    trading_days_only: bool = True
    region: str = "cn"
    watchlist: str = ""


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/dsa", tags=["dsa"])

    @router.get("/status")
    def status() -> dict:
        reachable, detail = health()
        return {
            "enabled": enabled(),
            "base_url": base_url(),
            "reachable": reachable,
            "detail": detail,
        }

    @router.get("/catalog")
    def catalog() -> dict:
        return catalog_payload()

    @router.get("/quant-evidence")
    def quant_evidence() -> dict:
        return evidence_summary()

    @router.get("/ledger-audit")
    def ledger_audit(
        code: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict:
        """上游 ``backtest_results`` 的只读审计视图。

        读的是上游 ``GET /backtest/results``（不是 ``/run``），所以没有写库
        副作用。响应恒带 ``audit_status=unaudited``，任何展示面都要带
        「未经 TSP 审计」；取不到上游时返回空行 + 原因，不抛 500，也不
        拿空账本冒充「没有亏损记录」。
        """
        return ledger_audit_payload(code=code, page=page, limit=limit)

    @router.get("/stock-context")
    def stock_context(symbol: str, request: Request) -> dict:
        repo = getattr(request.app.state, "repo", None)
        return load_context_cached(symbol, repo=repo)

    @router.post("/sniper-lines")
    def sniper_lines(body: SniperLineRequest) -> dict:
        from app.custom.dsa.sniper_alerts import register

        try:
            return register(body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/bot/command")
    def bot_command(body: BotCommandRequest) -> dict:
        try:
            return execute(body.text)
        except CommandError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/jobs/etf-rotation")
    def etf_rotation() -> dict:
        return run_etf_rotation()

    @router.get("/schedule")
    def get_schedule() -> dict:
        try:
            return load_schedule()
        except ScheduleSettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except UpstreamError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.put("/schedule")
    def put_schedule(body: ScheduleRequest) -> dict:
        try:
            return save_schedule(
                enabled=body.enabled,
                time=body.time,
                trading_days_only=body.trading_days_only,
                region=body.region,
                watchlist=body.watchlist,
            )
        except ScheduleSettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except UpstreamError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @router.api_route(
        "/upstream/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def upstream(path: str, request: Request) -> Response:
        body = await request.body()
        content_type = request.headers.get("content-type")
        params = list(request.query_params.multi_items())
        try:
            status_code, payload, media, extra = forward(
                request.method,
                path,
                params=params,
                body=body,
                content_type=content_type,
            )
        except InvalidUpstreamPathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except UpstreamError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return Response(content=payload, status_code=status_code, media_type=media, headers=extra)

    return router
