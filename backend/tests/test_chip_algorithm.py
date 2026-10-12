"""筹码算法对齐东财网页 CYQCalculator（akshare stock_cyq_em 内嵌 JS）。

东财 push2his 从当前网络连不上，夹具是同一段 JS 在本地 node 上的输出。
价格字段是 toFixed(2)，比率用绝对误差 1e-9。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.chip.algorithm import PRICE_BINS, compute_window

# (open, close, high, low, turnover percent). null 换手在数组里用 nan。
ASYMMETRIC = [
    (10.2, 10.8, 11.0, 10.0, 8.5),
    (10.7, 11.4, 11.6, 10.5, 6.2),
    (11.3, 11.0, 11.8, 10.9, 4.1),
    (11.0, 11.9, 12.2, 10.8, 9.4),
    (11.8, 12.4, 12.6, 11.5, 5.5),
    (12.3, 12.0, 12.5, 11.7, 3.2),
    (12.0, 12.8, 13.0, 11.9, 7.7),
    (12.7, 13.2, 13.4, 12.4, 11.0),
]
ONE_WORD = [
    (8.0, 8.4, 8.6, 7.8, 5),
    (8.4, 8.4, 8.4, 8.4, 12),
    (8.4, 8.9, 9.1, 8.3, 4.5),
    (8.8, 8.6, 9.0, 8.5, 3),
]
SHORT_LISTING = [
    (20, 22, 23, 19.5, 45),
    (22, 21, 22.5, 20.5, 30),
    (21, 23.5, 24, 20.8, 25),
]
GAP_TURNOVER = [
    (15, 15.4, 15.6, 14.8, 6),
    (15.3, 15.2, 15.5, 15.0, None),
    (15.2, 15.8, 16.0, 15.1, 0),
    (15.7, 16.2, 16.4, 15.5, 8),
]

EXPECTED = {
    "asymmetric": {
        "benefit_ratio": 0.9802520188228676,
        "avg_cost": 12.03,
        "cost_90_low": 10.46,
        "cost_90_high": 13.08,
        "concentration_90": 0.11149130310806964,
        "cost_70_low": 10.94,
        "cost_70_high": 12.88,
        "concentration_70": 0.08145893229607089,
    },
    "one_word": {
        "benefit_ratio": 0.710119554633923,
        "avg_cost": 8.40,
        "cost_90_low": 8.08,
        "cost_90_high": 8.88,
        "concentration_90": 0.04716981132075465,
        "cost_70_low": 8.30,
        "cost_70_high": 8.75,
        "concentration_70": 0.0263929618768328,
    },
    "short_listing": {
        "benefit_ratio": 0.9837297959731678,
        "avg_cost": 21.67,
        "cost_90_low": 20.44,
        "cost_90_high": 23.12,
        "concentration_90": 0.061705569678761334,
        "cost_70_low": 20.92,
        "cost_70_high": 22.55,
        "concentration_70": 0.037517369152385384,
    },
    "gap_turnover": {
        "benefit_ratio": 0.9423816151864275,
        "avg_cost": 15.76,
        "cost_90_low": 14.99,
        "cost_90_high": 16.22,
        "concentration_90": 0.03922243247892652,
        "cost_70_low": 15.14,
        "cost_70_high": 16.08,
        "concentration_70": 0.029922613929492723,
    },
}


def _arrays(rows: list[tuple]):
    open_ = np.array([row[0] for row in rows], dtype=np.float64)
    close = np.array([row[1] for row in rows], dtype=np.float64)
    high = np.array([row[2] for row in rows], dtype=np.float64)
    low = np.array([row[3] for row in rows], dtype=np.float64)
    turnover = np.array([np.nan if row[4] is None else row[4] for row in rows], dtype=np.float64)
    return open_, high, low, close, turnover


def _run(rows: list[tuple]):
    open_, high, low, close, turnover = _arrays(rows)
    return compute_window(open_, high, low, close, turnover, with_bins=True)


@pytest.mark.parametrize("name,rows", [
    ("asymmetric", ASYMMETRIC),
    ("one_word", ONE_WORD),
    ("short_listing", SHORT_LISTING),
    ("gap_turnover", GAP_TURNOVER),
])
def test_matches_eastmoney_js(name: str, rows: list[tuple]):
    result = _run(rows)
    expected = EXPECTED[name]
    assert result.computable
    assert result.benefit_ratio == pytest.approx(expected["benefit_ratio"], abs=1e-9)
    assert result.avg_cost == expected["avg_cost"]
    assert result.cost_90_low == expected["cost_90_low"]
    assert result.cost_90_high == expected["cost_90_high"]
    assert result.concentration_90 == pytest.approx(expected["concentration_90"], abs=1e-9)
    assert result.cost_70_low == expected["cost_70_low"]
    assert result.cost_70_high == expected["cost_70_high"]
    assert result.concentration_70 == pytest.approx(expected["concentration_70"], abs=1e-9)
    assert result.bins
    assert abs(sum(item["ratio"] for item in result.bins) - 1) < 1e-9
    assert len(result.bins) <= PRICE_BINS


def test_missing_turnover_is_not_a_fake_zero():
    rows = [(10, 10.2, 10.4, 9.8, None), (10.2, 10.5, 10.6, 10.0, None)]
    result = _run(rows)
    assert result.computable is False
    assert result.benefit_ratio is None
    assert result.avg_cost is None


def test_empty_window_and_invalid_last_bar():
    empty = compute_window(
        np.array([]), np.array([]), np.array([]), np.array([]), np.array([]),
    )
    assert empty.computable is False
    rows = [(10, 10.2, 10.4, 9.8, 5), (10.2, 0, 10.6, 10.0, 4)]
    result = _run(rows)
    assert result.computable is False


def test_zero_turnover_bar_inside_the_range_does_not_change_the_result():
    base = [
        (10.2, 10.8, 11.0, 10.0, 8.5),
        (12.7, 13.2, 13.4, 12.4, 11.0),
    ]
    with_flat = [
        base[0],
        (11.0, 11.2, 11.4, 10.8, 0),
        base[1],
    ]
    left = _run(base)
    right = _run(with_flat)
    assert right.benefit_ratio == pytest.approx(left.benefit_ratio, abs=1e-12)
    assert right.avg_cost == left.avg_cost
    assert right.concentration_90 == pytest.approx(left.concentration_90, abs=1e-12)


def test_short_history_still_computes():
    result = _run(SHORT_LISTING)
    assert result.computable
    assert result.close == 23.5
    assert math.isfinite(result.benefit_ratio or 0)
