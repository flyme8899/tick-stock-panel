"""基本面 M3。筛选规则在 fundamental_screens。"""

from app.strategy.fundamental_screens import (
    BASIC_FILTER,
    LOOKBACK_DAYS,
    filter_history_m3,
    meta_m3,
)

META = meta_m3()
EXECUTION_BACKEND = "python_history_legacy"
LOOKBACK_DAYS = LOOKBACK_DAYS
BASIC_FILTER = BASIC_FILTER
ENTRY_SIGNALS: list[str] = []
EXIT_SIGNALS: list[str] = []


def filter_history(df, params):
    return filter_history_m3(df, params)
