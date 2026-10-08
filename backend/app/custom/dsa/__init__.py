"""daily_stock_analysis sidecar bridge.

Deleting this package removes the decision workspace routes. TSP's own market
data, screener, monitor and backtest do not import it.
"""
from __future__ import annotations

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "dsa.workspace"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from app.custom.dsa.routes import build_router

    registrar.include_router(build_router())
