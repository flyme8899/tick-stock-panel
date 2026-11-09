"""XTick 历史竞价补充 API。

只服务手动查询。不抓主行情，不改竞价扫描落盘。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.services import xtick_supplement

router = APIRouter(prefix="/api/xtick-supplement", tags=["xtick-supplement"])


@router.get("/status")
def supplement_status():
    """开关、凭据是否已配置、是否允许全市场。不返回 token，不访问上游。"""
    return xtick_supplement.status_payload()


@router.get("/bidhistory")
def bidhistory(
    request: Request,
    code: str = Query(description="6 位股票代码。all 仅在运行时显式放开后可用"),
    trade_date: str = Query(description="交易日 YYYY-MM-DD，开始日与结束日相同"),
    seq: str | None = Query(default=None, description="留空不传给上游；0 或 1 原样传递"),
):
    """一个交易日的历史竞价终态。失败时仍返回补充状态，调用方不要把它写入行情。"""
    return xtick_supplement.query_bidhistory(
        request.app.state.repo.store.data_dir,
        code=code,
        trade_date=trade_date,
        seq=seq,
    )
