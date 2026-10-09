"""成交量参与率在矩阵/遗留撮合上的余量规则。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.backtest.engine import BacktestEngine, MatcherConfig


def _panel(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _bar(
    symbol: str,
    day: int,
    price: float = 10.0,
    *,
    volume: float = 100_000,
    limit_up: bool = False,
    limit_down: bool = False,
    high: float | None = None,
    low: float | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "date": date(2024, 1, 1) + timedelta(days=day),
        "open": price,
        "high": price if high is None else high,
        "low": price if low is None else low,
        "close": price,
        "volume": volume,
        "score": 1.0,
        "signal_limit_up": limit_up,
        "signal_limit_down": limit_down,
    }


def _masks(panel: pl.DataFrame, entries: set[tuple[str, int]], exits: set[tuple[str, int]]):
    base = date(2024, 1, 1)
    entry_values = []
    exit_values = []
    for row in panel.select(["symbol", "date"]).iter_rows(named=True):
        key = (row["symbol"], (row["date"] - base).days)
        entry_values.append(key in entries)
        exit_values.append(key in exits)
    return pl.Series(entry_values), pl.Series(exit_values)


def _run(panel: pl.DataFrame, entries, exits, **kwargs):
    config = MatcherConfig(
        matching="open_t+1",
        fees_pct=0,
        slippage_bps=0,
        max_positions=1,
        initial_capital=100_000,
        **kwargs,
    )
    engine = BacktestEngine(repo=None)
    matrix = engine.simulate_portfolio(panel, entries, exits, config)
    legacy = engine.simulate_portfolio_legacy(panel, entries, exits, config)
    return matrix, legacy


def test_volume_limit_off_matches_uncapped_fill() -> None:
    panel = _panel([_bar("A", i, volume=5) for i in range(4)])
    entries, exits = _masks(panel, {("A", 0)}, set())
    capped_off, legacy_off = _run(panel, entries, exits, volume_limit=None)
    also_off, _ = _run(panel, entries, exits, volume_limit=0)
    assert capped_off.trades[0].shares == also_off.trades[0].shares == 10_000
    assert capped_off.stats["execution"]["buy_volume_capped"] == 0
    assert legacy_off.trades[0].shares == 10_000


def test_partial_buy_drops_remainder() -> None:
    # 资金可买 100 手, 当根 20 手 * 50% = 10 手。余量不在下一日补买。
    rows = [_bar("A", i, volume=100_000) for i in range(4)]
    rows[1]["volume"] = 20
    panel = _panel(rows)
    entries, exits = _masks(panel, {("A", 0)}, set())
    matrix, legacy = _run(panel, entries, exits, volume_limit=0.5)
    assert len(matrix.trades) == 1
    assert matrix.trades[0].shares == 1_000
    assert matrix.stats["execution"]["buy_volume_capped"] == 1
    assert legacy.trades[0].shares == 1_000
    assert legacy.stats["execution"]["buy_volume_capped"] == 1


def test_buy_skipped_when_cap_below_one_lot() -> None:
    rows = [_bar("A", i) for i in range(3)]
    rows[1]["volume"] = 1
    panel = _panel(rows)
    entries, exits = _masks(panel, {("A", 0)}, set())
    matrix, legacy = _run(panel, entries, exits, volume_limit=0.5)
    assert matrix.trades == []
    assert matrix.stats["execution"]["buy_volume_limit"] == 1
    assert legacy.trades == []
    assert legacy.stats["execution"]["buy_volume_limit"] == 1


def test_partial_sell_carries_remainder_to_next_day() -> None:
    rows = [_bar("A", i, volume=100_000) for i in range(5)]
    rows[2]["volume"] = 40  # 40 手 * 50% = 20 手, 持仓 100 手只卖出一半
    panel = _panel(rows)
    entries, exits = _masks(panel, {("A", 0)}, {("A", 1)})
    matrix, legacy = _run(panel, entries, exits, volume_limit=0.5)
    assert [t.shares for t in matrix.trades] == [2_000, 8_000]
    assert matrix.trades[0].exit_date == "2024-01-03"
    assert matrix.trades[1].exit_date == "2024-01-04"
    assert matrix.stats["execution"]["sell_volume_capped"] == 1
    assert [t.shares for t in legacy.trades] == [2_000, 8_000]
    assert legacy.stats["execution"]["sell_volume_capped"] == 1


def test_last_bar_does_not_bypass_volume_cap() -> None:
    rows = [_bar("A", i, volume=100_000) for i in range(3)]
    rows[2]["volume"] = 1
    panel = _panel(rows)
    entries, exits = _masks(panel, {("A", 0)}, {("A", 1)})
    matrix, legacy = _run(panel, entries, exits, volume_limit=0.5)
    assert matrix.trades == []
    assert matrix.stats["execution"]["sell_volume_limit"] >= 1
    assert matrix.equity_curve[-1]["value"] > 0
    assert legacy.trades == []


def test_missing_volume_column_does_not_apply_cap() -> None:
    panel = _panel([_bar("A", i) for i in range(3)]).drop("volume")
    entries, exits = _masks(panel, {("A", 0)}, set())
    matrix, legacy = _run(panel, entries, exits, volume_limit=0.1)
    assert matrix.trades[0].shares == 10_000
    assert matrix.stats["execution"]["buy_volume_limit"] == 0
    assert legacy.trades[0].shares == 10_000


def test_independent_one_lot_skips_then_force_closes() -> None:
    rows = [_bar("A", i, volume=100) for i in range(3)]
    rows[0]["volume"] = 5  # 5 手 * 10% < 1 手, 买不进
    rows[2]["volume"] = 0
    panel = _panel(rows)
    entries, _exits = _masks(panel, {("A", 0)}, set())
    exits = pl.Series([False] * len(panel))
    blocked = BacktestEngine(repo=None).simulate_independent_candidates(
        panel,
        entries,
        exits,
        MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0, volume_limit=0.1, max_hold_days=1),
    )
    assert blocked.trades == []
    assert blocked.stats["execution"]["buy_volume_limit"] == 1

    rows[0]["volume"] = 20  # 20 * 10% = 2 手, 固定 1 手可买
    rows[1]["volume"] = 0.4  # 不足 1 手可卖, 但不是零成交停牌
    rows[2]["volume"] = 0.4
    panel = _panel(rows)
    entries, _ = _masks(panel, {("A", 0)}, set())
    closed = BacktestEngine(repo=None).simulate_independent_candidates(
        panel,
        entries,
        exits,
        MatcherConfig(matching="close_t", fees_pct=0, slippage_bps=0, volume_limit=0.1, max_hold_days=1),
    )
    assert len(closed.trades) == 1
    assert closed.trades[0].shares == 100
    assert closed.stats["execution"]["sell_volume_limit"] >= 1
