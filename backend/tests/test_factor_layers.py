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


def _warmup_panel(days: list[date]) -> pl.DataFrame:
    """第一天的因子只有 S0 算得出来，模拟 60 日窗口的预热日。"""
    rows = []
    for offset, day in enumerate(days):
        for index in range(5):
            if offset == 0 and index > 0:
                continue
            rows.append({
                "symbol": f"S{index}",
                "date": day,
                "factor": float(index),
                "fwd": 1.0 if offset == 0 else 0.01 * (index + 1),
                "entry_up": False,
                "entry_down": False,
            })
    return pl.DataFrame(rows)


def test_warmup_dates_are_dropped_before_group_means() -> None:
    """预热日的分位是残缺的，组均和多空必须用同一批日期。"""
    module = _script()
    days = [date(2026, 5, 6), date(2026, 5, 7), date(2026, 5, 8)]
    result = module.evaluate_layers(
        _warmup_panel(days), factor="factor", n_groups=5, hold=1, direction="ic", cost_bps=[0],
    )
    assert result["error"] is None
    assert result["dropped_incomplete"] == 1
    assert result["n_periods"] == 2
    # 预热日那只 fwd=1.0 的股票不能进组均，否则 Q1 会被拉到 0.25 以上
    assert result["group_means"] == pytest.approx([0.01, 0.02, 0.03, 0.04, 0.05])


def test_min_symbols_per_rebalance_date() -> None:
    module = _script()
    days = [date(2026, 5, 6), date(2026, 5, 7)]
    frame = _panel(days, factor_of=lambda index: index, forward_of=lambda index: 0.01 * (index + 1))
    result = module.evaluate_layers(
        frame, factor="factor", n_groups=5, hold=1, direction="ic", cost_bps=[0], min_symbols=10,
    )
    assert result["dropped_thin"] == 2
    assert result["error"] == "没有形成持仓"


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


def _printed_row(*, n_periods: int, annual: float | None = 0.1) -> dict:
    perf = {
        "annual": annual,
        "sharpe": 1.0,
        "max_drawdown": -0.05,
        "n_periods": n_periods,
        "total": 0.01,
    }
    return {
        "factor": "a158_vsumd_60",
        "hold": 5,
        "ic": -0.1,
        "direction": "低",
        "monotonicity": 0.8,
        "turnover": 0.4,
        "n_periods": n_periods,
        "group_means": [0.01, 0.02, 0.03, 0.04, 0.05],
        "costs": {0.0: {"long": perf, "excess": perf, "long_short": perf}},
        "error": None,
    }


def test_default_factors_keep_one_volume_signal() -> None:
    """vsump_60 与 vsumn_60 是 vsumd_60 的仿射变换，默认只留后者。"""
    module = _script()
    assert module.DEFAULT_FACTORS == (
        "a158_vsumd_60",
        "a158_vstd_20",
        "a158_vma_20",
        "a158_std_20",
    )
    help_text = module._build_parser().format_help()
    assert "vsump+vsumn=1" in help_text
    assert "vsumd=2*vsump-1" in help_text


def test_table_prints_periods_and_warns_when_annualization_is_thin() -> None:
    module = _script()
    short = module.format_layer_table([_printed_row(n_periods=6)], [0.0])
    assert "0.1000（6期）" in short
    assert "样本不足 30 期，不可靠" in short
    assert "外推" in short
    enough = module.format_layer_table([_printed_row(n_periods=30)], [0.0])
    assert "0.1000（30期）" in enough
    assert "样本不足 30 期" not in enough
    missing = module.format_layer_table([_printed_row(n_periods=0, annual=None)], [0.0])
    assert "样本不足 30 期" not in missing


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
