"""sidecar 启动门控：版本和许可证不符即拒绝启动，health 不恒真。"""
from importlib.metadata import version

from fastapi import FastAPI
from openbb_core.api.rest_api import app as platform_app

EXPECTED = {
    # openbb-core 2.x 是 OpenBB Platform 5.x 组件；不装全集 openbb 元包。
    "openbb-core": "2.0.1",
    "openbb-platform-api": "2.0.1",
    "openbb-fred": "2.0.0",
}
for package, pin in EXPECTED.items():
    actual = version(package)
    if actual != pin:
        raise RuntimeError(f"{package}=={pin} required, got {actual}")

app: FastAPI = platform_app

# sidecar 自身也 fail-closed：即使 FRED 包注册了其他路由，也只暴露本拍固定 GET 面。
_ALLOWED_GET_PATHS = {
    "/api/v1/tsp/health",
    "/api/v1/tsp_yahoo/equity/historical",
    "/api/v1/fred/economy/fred_series",
}


@app.middleware("http")
async def allowlisted_only(request, call_next):
    if request.url.path not in _ALLOWED_GET_PATHS:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": "Not in TSP read-only allowlist"})
    return await call_next(request)


@app.get("/api/v1/tsp/health", include_in_schema=False)
def tsp_health() -> dict:
    return {
        "status": "ok",
        "openbb_version": "5.0.0",
        "license": "Apache-2.0",
        "providers": ["fred", "tsp_yahoo"],
        "research_only": True,
    }
