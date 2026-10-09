"""分层脚本：单调因子得到单调分组，费用和换手按手算。"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from app.backtest.factor import FactorBacktestService, FactorConfig


def _script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "eval_factor_layers.py"
    spec = importlib.util.spec_from_file_location("eval_factor_layers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _panel(days: list[date], *, factor_of, forward_of) -> pl.DataFrame:
    rows = []
    for day in days:
        for index in range(5):
            rows.append({
                "symbol": f"S{index}",
                "date": day,
                "factor": float(factor_of(index)),
                "fwd": float(forward_of(index)),
                "entry_up": False,
                "entry_down": False,
            })
    return pl.DataFrame(rows)


def test_cost_and_turnover_math() -> None:
    module = _script()
    previous = {"A": 0.5, "B": 0.5}
    current = {"B": 0.5, "C": 0.5}
    assert module.leg_turnover(previous, current) == pytest.approx(0.5)
    assert module.leg_cost({}, {"A": 0.5, "B": 0.5}, 15) == pytest.approx(0.0015)
    assert module.leg_cost(previous, {"C": 0.5, "D": 0.5}, 15) == pytest.approx(0.0030)
    assert module.leg_cost(previous, current, 0) == 0.0

    days = [date(2026, 5, 6), date(2026, 5, 7)]
    rows = []
    for day, names in ((days[0], ("A", "B")), (days[1], ("B", "C"))):
        for name in names:
            rows.append({
                "symbol": name,
                "date": day,
                "_group": "Q5",
                "_next_return": 0.01,
            })
    service_turnover = FactorBacktestService._calc_turnover(
        pl.DataFrame(rows),
        FactorConfig(factor_name="factor", symbols=None, start=days[0], end=days[1], weight="equal"),
    )
    assert service_turnover == pytest.approx(module.leg_turnover(previous, current))


def test_summarize_matches_daily_factor_sharpe() -> None:
    module = _script()
    stats = module.summarize_returns([0.01, 0.03], hold=1)
    assert stats["sharpe"] == pytest.approx(2.0 * np.sqrt(252.0))
    assert stats["max_drawdown"] == pytest.approx(0.0)
    drawdown = module.summarize_returns([0.10, -0.50], hold=1)
    assert drawdown["max_drawdown"] == pytest.approx(-0.5)


def test_monotone_factor_gives_monotone_groups_and_cost() -> None:
    module = _script()
    days = [date(2026, 5, 6), date(2026, 5, 7)]
    frame = _panel(days, factor_of=lambda index: index, forward_of=lambda index: 0.01 * (index + 1))
    result = module.evaluate_layers(
        frame, factor="factor", n_groups=5, hold=1, direction="ic", cost_bps=[0, 15],
    )
    assert result["error"] is None
    assert result["ic"] == pytest.approx(1.0)
    assert result["direction"] == "高"
    assert result["group_means"] == pytest.approx([0.01, 0.02, 0.03, 0.04, 0.05])
    assert result["monotonicity"] == pytest.approx(1.0)
    assert result["turnover"] == pytest.approx(0.0)
    assert result["n_periods"] == 2
    assert result["costs"][0]["long"]["total"] == pytest.approx(1.05 * 1.05 - 1.0)
    assert result["costs"][15]["long"]["total"] == pytest.approx(1.0485 * 1.05 - 1.0)
    assert result["costs"][0]["long_short"]["total"] == pytest.approx(1.02 * 1.02 - 1.0)
    assert result["costs"][15]["long_short"]["total"] == pytest.approx(1.0185 * 1.02 - 1.0)
    assert result["costs"][0]["excess"]["total"] == pytest.approx(1.02 * 1.02 - 1.0)


def test_negative_ic_longs_the_low_factor_group() -> None:
    module = _script()
    days = [date(2026, 5, 6), date(2026, 5, 7)]
    frame = _panel(
        days,
        factor_of=lambda index: -(0.01 * (index + 1)),
        forward_of=lambda index: 0.01 * (index + 1),
    )
    result = module.evaluate_layers(
        frame, factor="factor", n_groups=5, hold=1, direction="ic", cost_bps=[0],
    )
    assert result["direction"] == "低"
    assert result["ic"] == pytest.approx(-1.0)
    assert result["monotonicity"] == pytest.approx(1.0)
    assert result["group_means"][-1] == pytest.approx(0.05)
    assert result["costs"][0]["long"]["total"] == pytest.approx(1.05 * 1.05 - 1.0)


def test_hold_steps_rebalance_dates() -> None:
    module = _script()
    days = [date(2026, 5, 6) + timedelta(days=offset) for offset in range(4)]
    assert module.rebalance_dates(days, 2) == [days[0], days[2]]
    assert module.parse_holds("1,3,5") == [1, 3, 5]
    frame = _panel(days, factor_of=lambda index: index, forward_of=lambda index: 0.01 * (index + 1))
    result = module.evaluate_layers(
        frame, factor="factor", n_groups=5, hold=2, direction="high", cost_bps=[0],
    )
    assert result["n_periods"] == 2
    assert result["direction"] == "高"
