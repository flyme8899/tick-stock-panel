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
    normalize_upstream_path,
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


# ---------------------------------------------------------------------------
# 浏览器上游只读闸（P-sec）
#
# 仅供研究，不构成投资建议；执行须人工确认。
#
# ## 为什么只收窄 system / portfolio
#
# 这两个前缀底下是**配置、调度、持仓、交易相关**的写面。浏览器侧经
# `/api/dsa/upstream/*` 打过去，等于把一个能改上游配置、能动持仓的口子
# 暴露在页面上。诊断和展示只需要读，所以只收窄这两类，其它前缀
# （analysis / history / screening / backtest / intelligence …）本拍**不动**
# —— 收窄面越大越容易误伤，本拍只切这一刀。
#
# ## 为什么闸在 routes，而不是 proxy.forward()
#
# `schedule.save_schedule` 的服务端路径内部就要 `forward("PUT", "system/config")`。
# 在 `forward()` 里全局拒写，会把定时任务保存一起打断 —— 那是在修安全的名义下
# 干掉一个正常功能。所以闸只挂在**浏览器入口** `upstream()` 上：
# 浏览器来的非 GET 拒掉，服务端自己调 `forward` 照旧。
#
# ## 为什么是 405 不是 403
#
# 403 是「我知道你是谁，但你不能做这件事」；405 是「这个资源不支持这个方法」。
# 这里拒绝的是**方法**本身（资源仍然可读），405 语义更准，也让调用方一眼看出
# 「换成 GET 就行」。
_READONLY_UPSTREAM_PREFIXES = frozenset({"system", "portfolio"})
_READONLY_UPSTREAM_ALLOWED_METHODS = frozenset({"GET", "HEAD"})


def _reject_non_get_system_portfolio(path: str, method: str) -> None:
    """浏览器 upstream：system/portfolio 仅允许 GET，其余方法 405 且不转发。

    只被 `upstream()` 调用。**不要**把它挪进 `proxy.forward()` —— 会打断
    `schedule.save_schedule` 的服务端 `PUT system/config`。

    非法路径不在这里拦：交给既有 `forward()` 走 400，保持原来的错误口径，
    也避免这里先抛一个跟下面不一致的状态码。
    """
    try:
        normalized = normalize_upstream_path(path)
    except InvalidUpstreamPathError:
        return
    prefix = normalized.split("/", 1)[0]
    if prefix in _READONLY_UPSTREAM_PREFIXES and method.upper() not in _READONLY_UPSTREAM_ALLOWED_METHODS:
        raise HTTPException(
            status_code=405,
            detail=f"{prefix} 上游代理仅允许 GET（浏览器侧只读）；写操作请走服务端入口。",
            headers={"Allow": "GET, HEAD"},
        )


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
        # 闸在这里，不在 forward()。见 _reject_non_get_system_portfolio 上面那段说明。
        _reject_non_get_system_portfolio(path, request.method)
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
