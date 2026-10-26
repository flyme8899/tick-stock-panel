"""OpenBB 研究面扩展；删除本包即可移除独立查询路由。"""
from __future__ import annotations

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "openbb.research"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from app.custom.openbb.routes import build_router

    registrar.include_router(build_router())
