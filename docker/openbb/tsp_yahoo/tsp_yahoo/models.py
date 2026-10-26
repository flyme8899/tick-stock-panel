"""Apache-2.0 yfinance 到 OpenBB V5 标准日K模型的最小适配。"""
from typing import Any

from openbb_core.provider.abstract.fetcher import Fetcher
from openbb_core.provider.standard_models.equity_historical import (
    EquityHistoricalData,
    EquityHistoricalQueryParams,
)
from openbb_core.provider.utils.errors import EmptyDataError


class EquityHistoricalFetcher(Fetcher[EquityHistoricalQueryParams, list[EquityHistoricalData]]):
    @staticmethod
    def transform_query(params: dict[str, Any]) -> EquityHistoricalQueryParams:
        return EquityHistoricalQueryParams(**params)

    @staticmethod
    def extract_data(query: EquityHistoricalQueryParams, credentials: dict | None, **kwargs: Any):
        import yfinance as yf

        symbols = query.symbol.split(",")
        frame = yf.download(
            symbols,
            start=query.start_date,
            end=query.end_date,
            interval="1d",
            auto_adjust=False,
            actions=False,
            progress=False,
            group_by="ticker",
            threads=False,
        )
        if frame.empty:
            raise EmptyDataError()
        return frame

    @staticmethod
    def transform_data(query: EquityHistoricalQueryParams, data: Any, **kwargs: Any):
        symbols = query.symbol.upper().split(",")
        records = []
        for symbol in symbols:
            frame = data[symbol] if len(symbols) > 1 else data
            for timestamp, row in frame.iterrows():
                records.append(EquityHistoricalData.model_validate({
                    "date": timestamp.date(),
                    "open": row.get("Open"),
                    "high": row.get("High"),
                    "low": row.get("Low"),
                    "close": row.get("Close"),
                    "volume": row.get("Volume"),
                    "symbol": symbol,
                }))
        if not records:
            raise EmptyDataError()
        return records
