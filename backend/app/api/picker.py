"""选股页 API。重计算走 heavy-job 槽，不占用页面读数用的 interactive collect 位。"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.services.heavy_job_limiter import HeavyJobLimitTimeoutError
from app.services.picker import (
    FilterSpec,
    SourceSpec,
    deps_from_app,
    run_picker,
    safe_source_id,
    sync_dsa_watchlist,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/picker", tags=["picker"])


class SourceIn(BaseModel):
    type: Literal["fundamental", "hot_events", "technical", "factor", "dsa"]
    id: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not safe_source_id(value):
            raise ValueError("来源 id 无效")
        return value

    @field_validator("params")
    @classmethod
    def _params(cls, value: dict[str, Any]) -> dict[str, Any]:
        # 旧客户端仍会带上时间窗和最少来源数。热门事件已改按交易日热度选取，运行时不再读取这两项。
        allowed = {"window", "min_sources"}
        extra = set(value) - allowed
        if extra:
            raise ValueError("不支持的来源参数")
        if "window" in value and value["window"] not in {"24h", "3d"}:
            raise ValueError("时间窗只能是 24h 或 3d")
        return value


class FiltersIn(BaseModel):
    industries: list[str] = Field(default_factory=list, max_length=40)
    market_cap_min: float | None = Field(default=None, ge=0, le=100000)
    market_cap_max: float | None = Field(default=None, ge=0, le=100000)
    pe_min: float | None = Field(default=None, ge=0, le=10000)
    pe_max: float | None = Field(default=None, ge=0, le=10000)
    exclude_st: bool = False
    exclude_financial: bool = False
    max_per_industry: int | None = Field(default=None, ge=1, le=50)

    @field_validator("industries")
    @classmethod
    def _industries(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = str(item).strip()
            if not text or len(text) > 40:
                raise ValueError("行业名称无效")
            cleaned.append(text)
        return cleaned


class RunIn(BaseModel):
    sources: list[SourceIn] = Field(min_length=1, max_length=12)
    combine: Literal["and", "or"] = "or"
    filters: FiltersIn = Field(default_factory=FiltersIn)


class DsaSyncIn(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=50)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = str(item).strip()
            if not text or len(text) > 32 or any(ch in text for ch in "/\\"):
                raise ValueError("股票代码无效")
            cleaned.append(text)
        return cleaned


def _filters(body: FiltersIn) -> FilterSpec:
    if (
        body.market_cap_min is not None
        and body.market_cap_max is not None
        and body.market_cap_min > body.market_cap_max
    ):
        raise HTTPException(status_code=400, detail="市值区间下限不能大于上限")
    if body.pe_min is not None and body.pe_max is not None and body.pe_min > body.pe_max:
        raise HTTPException(status_code=400, detail="市盈率区间下限不能大于上限")
    return FilterSpec(
        industries=list(body.industries),
        market_cap_min=body.market_cap_min,
        market_cap_max=body.market_cap_max,
        pe_min=body.pe_min,
        pe_max=body.pe_max,
        exclude_st=body.exclude_st,
        exclude_financial=body.exclude_financial,
        max_per_industry=body.max_per_industry,
    )


@router.get("/sources")
def sources(request: Request):
    try:
        deps = deps_from_app(request)
        from app.services.picker import build_catalog

        return build_catalog(deps)
    except Exception as exc:
        logger.exception("picker sources failed")
        raise HTTPException(status_code=500, detail="选股来源暂时不可用") from exc


@router.post("/run")
def run(body: RunIn, request: Request):
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    filters = _filters(body.filters)
    deps = deps_from_app(request, limiter=shared_heavy_job_limiter)
    try:
        return run_picker(
            [SourceSpec(type=item.type, id=item.id, params=dict(item.params)) for item in body.sources],
            body.combine,
            filters,
            deps,
        )
    except HeavyJobLimitTimeoutError as exc:
        raise HTTPException(status_code=503, detail="有其他重计算正在占用资源，请稍后再运行选股") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("picker run failed")
        raise HTTPException(status_code=500, detail="选股运行失败") from exc


@router.post("/dsa-sync")
def dsa_sync(body: DsaSyncIn, request: Request):
    """把已加入 TSP 自选的代码再写入 DSA STOCK_LIST。TSP 自选仍走原有接口。"""
    del request
    try:
        return sync_dsa_watchlist(body.symbols, dsa_forward)
    except Exception as exc:
        logger.exception("picker dsa sync failed")
        raise HTTPException(status_code=500, detail="同步 DSA 自选失败") from exc


def dsa_forward(method: str, path: str, body: dict | None) -> tuple[int, dict]:
    from app.services.picker import dsa_forward_request

    return dsa_forward_request(method, path, body)
