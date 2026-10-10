"""基本面 M2。筛选规则在 fundamental_screens。"""

from app.strategy.fundamental_screens import (
    BASIC_FILTER,
    LOOKBACK_DAYS,
    filter_history_m2,
    meta_m2,
)

META = meta_m2()
EXECUTION_BACKEND = "python_history_legacy"
LOOKBACK_DAYS = LOOKBACK_DAYS
BASIC_FILTER = BASIC_FILTER
ENTRY_SIGNALS: list[str] = []
EXIT_SIGNALS: list[str] = []


def filter_history(df, params):
    return filter_history_m2(df, params)
