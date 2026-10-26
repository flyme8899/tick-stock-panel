"""最小 V5 provider：只注册 EquityHistorical，不开放新闻、财务或期权。"""
from openbb_core.provider.abstract.provider import Provider
from tsp_yahoo.models import EquityHistoricalFetcher

provider = Provider(
    name="tsp_yahoo",
    website="https://finance.yahoo.com",
    description="TSP read-only daily historical samples via Yahoo Finance.",
    credentials=None,
    fetcher_dict={"EquityHistorical": EquityHistoricalFetcher},
)
