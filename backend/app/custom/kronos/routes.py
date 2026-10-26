"""Kronos 只读路由：状态、schema、门禁自述。

没有推理接口、没有上游转发、没有写接口。主进程只做三件事：读配置、探活、读
门禁记录。要跑推理只有离线批（``scripts/kronos_import_s3.py``）或独立 sidecar。
"""
from __future__ import annotations

from fastapi import APIRouter, Response

from app.custom.kronos import config, gates
from app.custom.kronos.schema import schema_payload


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/kronos", tags=["kronos"])

    def _research_headers(response: Response) -> None:
        response.headers["X-Research-Only"] = "true"

    @router.get("/status")
    def status(response: Response) -> dict:
        _research_headers(response)
        return config.status()

    @router.get("/schema")
    def schema(response: Response) -> dict:
        _research_headers(response)
        return schema_payload()

    @router.get("/gates")
    def gate_state(response: Response) -> dict:
        _research_headers(response)
        return gates.gate_payload()

    return router
