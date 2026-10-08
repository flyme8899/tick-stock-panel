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


class BotCommandRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


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
