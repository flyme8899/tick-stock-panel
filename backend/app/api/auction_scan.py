"""全市场竞价扫描 API。

GET /api/auction-scan?date=  读取已落盘的当日列表。不在请求路径上抓快照。
当日文件不存在时 state=no_data。
"""

from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import auction_scan

router = APIRouter(prefix="/api/auction-scan", tags=["auction-scan"])


@router.get("")
def get_auction_scan(
    request: Request,
    date: str | None = Query(default=None, description="交易日 YYYY-MM-DD，缺省为北京时间今天"),
):
    """当日竞价扫描命中列表，以及已经落盘的日期。"""
    target = None
    if date:
        try:
            target = date_cls.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"date 格式应为 YYYY-MM-DD, 收到: {date}") from None
    return auction_scan.get_scan(request.app.state.repo.store.data_dir, target)
