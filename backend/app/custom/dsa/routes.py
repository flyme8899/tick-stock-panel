"""TSP-facing routes. The browser never talks to the DSA process directly."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.custom.dsa.catalog import catalog_payload
from app.custom.dsa.commands import CommandError, execute
from app.custom.dsa.jobs import run_etf_rotation
from app.custom.dsa.proxy import (
    InvalidUpstreamPathError,
    UpstreamError,
    base_url,
    enabled,
    forward,
    health,
)
from app.custom.dsa.schedule import ScheduleSettingsError, load_schedule, save_schedule


class BotCommandRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


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
