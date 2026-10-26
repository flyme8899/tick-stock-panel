"""OpenBB builtin plugin：可用性门控与研究面查询，不实现主行情同步协议。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.plugins.openbb.client import ALLOWED_SYMBOLS, OpenBBClient


def availability() -> tuple[bool, str]:
    """未配置或 health 失败均不注册为可用 provider，原因进入矩阵 pending。"""
    return OpenBBClient().health()


@dataclass
class _OpenBBConfig:
    name: str = "openbb"
    display_name: str = "OpenBB 研究源"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(ALLOWED_SYMBOLS))
    path: None = None
    builtin: bool = True


class OpenBBProvider:
    name = "openbb"
    builtin = True

    def __init__(self) -> None:
        self.config = _OpenBBConfig()

    def close(self) -> None:
        pass

    def query(
        self, dataset: str, symbol: str, start_date: date | None = None, end_date: date | None = None
    ) -> dict:
        """仅供 /api/openbb/* 调用；刻意不提供 get_daily/get_financials。"""
        return OpenBBClient().query(dataset, symbol, start_date, end_date)
