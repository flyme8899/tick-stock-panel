"""内置蜡烛形态：用构造好的 OHLC 锁定主要形态，并确认标的之间不串。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.indicators.candles import CATALOG, compute_candle_patterns


def _bar(day: date, open_: float, high: float, low: float, close: float, symbol: str = "A") -> dict:
    return {
        "symbol": symbol,
        "date": day,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
    }


def _days(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _small_bodies(n: int = 12, symbol: str = "A") -> list[dict]:
    rows = []
    for day in _days(n):
        rows.append(_bar(day, 10.0, 10.3, 9.9, 10.2, symbol))
    return rows


def _last(frame: pl.DataFrame, column: str, symbol: str = "A") -> int:
    value = frame.filter(pl.col("symbol") == symbol)[column][-1]
    return int(value)


def test_builtin_catalog_columns_exist():
    rows = _small_bodies(20)
    frame = compute_candle_patterns(pl.DataFrame(rows), engine="builtin")
    missing = [pat.column for pat in CATALOG if pat.builtin and pat.column not in frame.columns]
    assert missing == []
    hidden = [pat.id for pat in CATALOG if not pat.builtin and pat.column in frame.columns]
    assert hidden == []


def test_flat_series_does_not_fire_hammer():
    rows = [_bar(day, 10.0, 10.0, 10.0, 10.0) for day in _days(30)]
    frame = compute_candle_patterns(pl.DataFrame(rows), engine="builtin")
    assert frame["cdl_hammer"].to_list() == [0] * 30


def test_engulfing_hammer_and_doji_family():
    days = _days(16)
    rows = _small_bodies(12)
    # 看跌吞没的前一根是小阳，当根大阴包住。
    rows.append(_bar(days[12], 10.0, 11.2, 9.9, 11.0))
    rows.append(_bar(days[13], 11.4, 11.5, 9.6, 9.7))
    # 锤头：小实体、长下影、短上影，实体靠近前低。
    rows.append(_bar(days[14], 10.0, 12.2, 9.8, 12.0))
    rows.append(_bar(days[15], 9.9, 9.92, 7.5, 9.85))
    frame = compute_candle_patterns(pl.DataFrame(rows), engine="builtin")
    assert int(frame["cdl_engulfing"][-3]) == -100
    assert int(frame["cdl_hammer"][-1]) == 100

    doji_rows = _small_bodies(12)
    doji_rows.append(_bar(days[12], 10.0, 12.0, 8.0, 10.0))
    doji = compute_candle_patterns(pl.DataFrame(doji_rows), engine="builtin")
    assert _last(doji, "cdl_doji") == 100

    dragon = _small_bodies(12)
    dragon.append(_bar(days[12], 10.0, 10.02, 8.0, 10.0))
    dragon_frame = compute_candle_patterns(pl.DataFrame(dragon), engine="builtin")
    assert _last(dragon_frame, "cdl_dragonfly_doji") == 100

    grave = _small_bodies(12)
    grave.append(_bar(days[12], 12.0, 14.0, 11.98, 12.0))
    grave_frame = compute_candle_patterns(pl.DataFrame(grave), engine="builtin")
    assert _last(grave_frame, "cdl_gravestone_doji") == -100


def test_hanging_man_stars_soldiers_crows_harami_piercing_dark_cloud():
    days = _days(20)
    base = _small_bodies(12)

    hang = [*base, _bar(days[12], 10.0, 12.2, 9.8, 12.0), _bar(days[13], 12.15, 12.18, 10.0, 12.1)]
    assert _last(compute_candle_patterns(pl.DataFrame(hang), engine="builtin"), "cdl_hanging_man") == -100

    morning = [*base, 
        _bar(days[12], 12.0, 12.2, 8.8, 9.0),
        _bar(days[13], 8.5, 8.6, 8.2, 8.4),
        _bar(days[14], 9.2, 11.2, 9.0, 11.0),
    ]
    assert _last(compute_candle_patterns(pl.DataFrame(morning), engine="builtin"), "cdl_morning_star") == 100

    evening = [*base, 
        _bar(days[12], 9.0, 12.2, 8.8, 12.0),
        _bar(days[13], 12.6, 12.8, 12.4, 12.5),
        _bar(days[14], 12.2, 12.3, 9.4, 9.6),
    ]
    assert _last(compute_candle_patterns(pl.DataFrame(evening), engine="builtin"), "cdl_evening_star") == -100

    soldiers = [*base, 
        _bar(days[12], 10.0, 12.05, 9.9, 12.0),
        _bar(days[13], 11.0, 13.05, 10.8, 13.0),
        _bar(days[14], 12.0, 14.05, 11.8, 14.0),
    ]
    assert _last(compute_candle_patterns(pl.DataFrame(soldiers), engine="builtin"), "cdl_three_white_soldiers") == 100

    crows = [*base, 
        _bar(days[12], 14.0, 14.1, 11.9, 12.0),
        _bar(days[13], 13.0, 13.1, 10.9, 11.0),
        _bar(days[14], 12.0, 12.1, 9.9, 10.0),
    ]
    assert _last(compute_candle_patterns(pl.DataFrame(crows), engine="builtin"), "cdl_three_black_crows") == -100

    harami = [*base, 
        _bar(days[12], 12.0, 12.2, 8.8, 9.0),
        _bar(days[13], 9.5, 10.6, 9.4, 10.5),
    ]
    assert _last(compute_candle_patterns(pl.DataFrame(harami), engine="builtin"), "cdl_harami") == 100

    piercing = [*base, 
        _bar(days[12], 12.0, 12.2, 8.8, 9.0),
        _bar(days[13], 8.5, 11.2, 8.4, 11.0),
    ]
    assert _last(compute_candle_patterns(pl.DataFrame(piercing), engine="builtin"), "cdl_piercing") == 100

    cloud = [*base, 
        _bar(days[12], 9.0, 12.2, 8.8, 12.0),
        _bar(days[13], 12.5, 12.6, 9.8, 10.0),
    ]
    assert _last(compute_candle_patterns(pl.DataFrame(cloud), engine="builtin"), "cdl_dark_cloud_cover") == -100


def test_two_symbols_do_not_share_pattern_state():
    days = _days(14)
    rows = []
    for day in days[:12]:
        rows.append(_bar(day, 10.0, 10.3, 9.9, 10.2, "A"))
        rows.append(_bar(day, 20.0, 20.0, 20.0, 20.0, "B"))
    rows.append(_bar(days[12], 10.0, 11.2, 9.9, 11.0, "A"))
    rows.append(_bar(days[12], 20.0, 20.0, 20.0, 20.0, "B"))
    rows.append(_bar(days[13], 11.4, 11.5, 9.6, 9.7, "A"))
    rows.append(_bar(days[13], 20.0, 20.0, 20.0, 20.0, "B"))
    frame = compute_candle_patterns(pl.DataFrame(rows), engine="builtin")
    assert _last(frame, "cdl_engulfing", "A") == -100
    assert _last(frame, "cdl_engulfing", "B") == 0
    assert _last(frame, "cdl_hammer", "B") == 0
