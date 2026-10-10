"""ETF rotation modes: bucket blend, equal weight, legacy, and the card payload."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "daily_stock_analysis"
sys.path.insert(0, str(VENDOR))

from src.core.etf_rotation import (  # noqa: E402
    CASH,
    DEFAULT_BUCKET_SPEC,
    DISCLAIMER,
    Bucket,
    RotationParams,
    _risk_off_weights,
    compose_snapshot,
    parse_buckets,
    parse_lookbacks,
    parse_mode,
    parse_weighting,
    run_backtest,
    signal_weights,
)
from src.services.etf_rotation_service import etf_daily_fetchers  # noqa: E402

from app.custom.dsa.etf_job import split_rotation_output  # noqa: E402


def _params(**overrides) -> RotationParams:
    base = dict(
        mode="blended_bucket",
        lookbacks=(2, 3, 4),
        rebalance="monthly",
        top_n=3,
        weighting="inv_vol",
        cost_bps=10,
        buckets=parse_buckets("A股:510300|510500;512890;513100"),
    )
    base.update(overrides)
    return RotationParams(**base)


def _frame(index: pd.DatetimeIndex, **columns: list[float]) -> pd.DataFrame:
    return pd.DataFrame(columns, index=index)


def test_bucket_spec_keeps_only_the_best_member_of_a_named_bucket() -> None:
    buckets = parse_buckets(DEFAULT_BUCKET_SPEC)
    assert buckets[0] == Bucket("A股", ("510300", "510500", "159915"))
    assert [bucket.members for bucket in buckets[1:]] == [("512890",), ("513100",), ("518880",)]
    assert parse_mode(None) == "blended_bucket"
    assert parse_mode("legacy") == "legacy"
    assert parse_mode("nope") == "blended_bucket"
    assert parse_lookbacks(None) == (20, 60, 120)
    assert parse_weighting(None, "blended_bucket") == "inv_vol"
    assert parse_weighting("inv_vol", "equal_weight") == "equal"
    assert parse_weighting("inv_vol", "legacy") == "equal"


def test_named_bucket_uses_best_rank_and_safe_asset_fills_the_remainder() -> None:
    day = pd.Timestamp("2024-06-28")
    index = pd.DatetimeIndex([day])
    closes = _frame(
        index,
        **{"510300": [10.0], "510500": [10.0], "512890": [10.0], "513100": [10.0], "511880": [10.0]},
    )
    avg_rank = _frame(index, **{"510300": [1.0], "510500": [2.0], "512890": [1.5], "513100": [3.0]})
    momentum = _frame(index, **{"510300": [0.02], "510500": [0.20], "512890": [-0.05], "513100": [0.04]})
    vol = _frame(index, **{"510300": [0.02], "510500": [0.01], "512890": [0.03], "513100": [0.01]})

    weights, scores, buckets, kind, risk_off = signal_weights(
        closes,
        closes,
        day,
        _params(),
        ["510300", "510500", "512890", "513100"],
        "511880",
        (),
        1.0,
        1.0,
        avg_rank,
        momentum,
        vol,
    )

    assert kind == "avg_rank"
    assert risk_off is False
    assert buckets["510300"] == "A股"
    assert "510500" not in weights
    assert "512890" not in weights
    assert scores["510300"] == pytest.approx(1.0)
    assert weights["510300"] == pytest.approx(2 / 9)
    assert weights["513100"] == pytest.approx(4 / 9)
    assert weights["511880"] == pytest.approx(1 / 3)


def test_negative_best_member_drops_the_whole_bucket() -> None:
    day = pd.Timestamp("2024-06-28")
    index = pd.DatetimeIndex([day])
    closes = _frame(index, **{"510300": [10.0], "510500": [10.0], "511880": [10.0]})
    avg_rank = _frame(index, **{"510300": [1.0], "510500": [2.0]})
    momentum = _frame(index, **{"510300": [-0.01], "510500": [0.2]})
    weights, _scores, _buckets, _kind, _risk_off = signal_weights(
        closes,
        closes,
        day,
        _params(buckets=parse_buckets("A股:510300|510500"), top_n=1),
        ["510300", "510500"],
        "511880",
        (),
        1.0,
        1.0,
        avg_rank,
        momentum,
        None,
    )
    assert weights == {"511880": 1.0}


def test_signal_ignores_prices_after_the_signal_close() -> None:
    index = pd.bdate_range("2021-01-04", periods=40)
    signal_day = index[20]
    base = {code: [100.0 + step for step in range(len(index))] for code in ("510300", "512890", "513100")}
    calm = _frame(index, **base, **{"511880": [1.0] * len(index)})
    jumped = calm.copy()
    jumped.loc[index[21]:, "513100"] *= 5
    params = _params(lookbacks=(2, 3, 4), weighting="equal")
    risk = ["510300", "512890", "513100"]

    def decide(frame: pd.DataFrame) -> dict[str, float]:
        weights, *_rest = signal_weights(frame, frame, signal_day, params, risk, "511880", (), 1.0, 1.0)
        return weights

    assert decide(calm.loc[:signal_day]) == decide(jumped)


def test_trades_fill_on_the_next_close() -> None:
    index = pd.bdate_range("2021-01-04", periods=80)
    trend = [100 * (1.003 ** step) for step in range(len(index))]
    flat = [100.0] * len(index)
    down = [100 * (0.997 ** step) for step in range(len(index))]
    closes = _frame(
        index,
        **{"510300": trend, "510500": flat, "512890": down, "513100": flat, "511880": flat},
    )
    result = run_backtest(
        closes,
        ["510300", "510500", "512890", "513100"],
        "511880",
        _params(),
    )
    assert result.trades
    for trade in result.trades:
        assert trade.exec_date > trade.signal_date
        assert index.get_loc(trade.exec_date) == index.get_loc(trade.signal_date) + 1


def test_equal_weight_restores_the_pool_monthly() -> None:
    index = pd.bdate_range("2022-01-03", periods=40)
    closes = _frame(
        index,
        **{code: [100.0 + offset + step for step in range(len(index))] for offset, code in enumerate(
            ("510300", "510500", "159915", "512890", "513100", "518880")
        )},
    )
    result = run_backtest(
        closes,
        list(closes.columns),
        "511880",
        RotationParams(mode="equal_weight", rebalance="monthly", top_n=3, weighting="equal", cost_bps=10),
    )
    assert result.trades
    weights = result.trades[-1].to_weights
    assert set(weights) == set(closes.columns)
    assert all(weight == pytest.approx(1 / 6) for weight in weights.values())


def test_legacy_stays_on_the_weekly_top_two_path() -> None:
    index = pd.bdate_range("2023-01-02", periods=30)
    closes = _frame(
        index,
        **{
            "510300": [100 * (1.01 ** step) for step in range(len(index))],
            "510500": [100 * (1.002 ** step) for step in range(len(index))],
            "159915": [100 * (0.99 ** step) for step in range(len(index))],
            "511880": [1.0] * len(index),
        },
    )
    params = RotationParams(
        mode="legacy",
        lookback_days=5,
        rebalance="weekly",
        top_n=2,
        weighting="equal",
        cost_bps=10,
    )
    result = run_backtest(closes, ["510300", "510500", "159915"], "511880", params)
    assert result.trades
    held = {code for code, weight in result.trades[-1].to_weights.items() if code != CASH and weight > 0}
    assert held <= {"510300", "510500", "511880"}
    assert len([code for code in held if code != "511880"]) <= 2
    off = run_backtest(
        closes,
        ["510300", "510500", "159915"],
        "511880",
        RotationParams(
            mode="legacy",
            lookback_days=5,
            rebalance="weekly",
            top_n=2,
            weighting="equal",
            cost_bps=10,
            drawdown_risk_off=False,
        ),
    )
    pd.testing.assert_series_equal(result.equity, off.equity)


def test_drawdown_hook_is_a_noop_until_enabled() -> None:
    row = pd.Series({"511880": 1.0})
    weights = {"510300": 1.0}
    params = _params(drawdown_risk_off=False, drawdown_limit=0.15)
    unchanged, triggered = _risk_off_weights(weights, params, equity=0.8, peak=1.0, safe_asset="511880", quoted_row=row)
    assert triggered is False
    assert unchanged == weights
    moved, triggered = _risk_off_weights(
        weights,
        _params(drawdown_risk_off=True, drawdown_limit=0.15),
        equity=0.8,
        peak=1.0,
        safe_asset="511880",
        quoted_row=row,
    )
    assert triggered is True
    assert moved == {"511880": 1.0}


def test_snapshot_carries_the_card_fields_and_disclaimer() -> None:
    index = pd.bdate_range("2021-01-04", periods=80)
    trend = [100 * (1.002 ** step) for step in range(len(index))]
    flat = [50.0] * len(index)
    closes = _frame(
        index,
        **{"510300": trend, "510500": flat, "512890": flat, "513100": trend, "511880": flat},
    )
    _result, snapshot = compose_snapshot(
        closes,
        ["510300", "510500", "512890", "513100"],
        "511880",
        _params(),
        names={"510300": "沪深300"},
        next_session=lambda day: (pd.Timestamp(day) + pd.offsets.BDay(1)).date(),
    )
    assert snapshot["mode"] == "blended_bucket"
    assert snapshot["mode_label"] == "分桶混合动量"
    assert snapshot["signal_date"] == f"{index[-1]:%Y-%m-%d}"
    assert snapshot["disclaimer"] == DISCLAIMER
    assert snapshot["score_kind"] == "avg_rank"
    assert isinstance(snapshot["holdings"], list) and snapshot["holdings"]
    assert snapshot["next_rebalance"]


class _Source:
    def __init__(self, name: str, priority: int, adjust: str = "forward") -> None:
        self.name = name
        self.priority = priority
        self.kline_adjust = adjust
        self.api_key = "test-key"
        self.timeout = 1.0
        self.batch_daily_enabled = False
        self.batch_size = 10


def test_tickflow_keeps_its_priority_and_requests_forward_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    def clone(fetcher):
        copied = _Source("TickFlowFetcher", fetcher.priority, adjust="forward")
        copied.api_key = fetcher.api_key
        return copied

    monkeypatch.setattr("src.services.etf_rotation_service._clone_tickflow_forward", clone)
    original = _Source("TickFlowFetcher", 0, adjust="none")
    ordered = etf_daily_fetchers([
        _Source("BaostockFetcher", 3),
        original,
        _Source("EfinanceFetcher", 4),
        _Source("TushareFetcher", 1),
    ])
    assert [item.name for item in ordered] == ["TickFlowFetcher", "BaostockFetcher", "EfinanceFetcher"]
    assert ordered[0].priority == 0
    assert ordered[0].kline_adjust == "forward"
    assert original.kline_adjust == "none"
    assert ordered[0] is not original

    later = etf_daily_fetchers([
        _Source("BaostockFetcher", 3),
        _Source("TickFlowFetcher", 5, adjust="none"),
    ])
    assert [item.name for item in later] == ["BaostockFetcher", "TickFlowFetcher"]
    already = _Source("TickFlowFetcher", 1, adjust="forward")
    assert etf_daily_fetchers([already]) == [already]


def test_job_output_splits_the_card_from_the_log() -> None:
    detail, result = split_rotation_output(
        'INFO start\nTSP_ETF_RESULT {"signal_date":"2026-09-30","mode":"blended_bucket"}\nINFO end\n'
    )
    assert "TSP_ETF_RESULT" not in detail
    assert result == {"signal_date": "2026-09-30", "mode": "blended_bucket"}


def test_acceptance_numbers_stay_in_the_guide() -> None:
    guide = Path(__file__).resolve().parents[2] / "docs" / "dsa-integration.md"
    vendor = VENDOR / "docs" / "etf-rotation.md"
    for path in (guide, vendor):
        text = path.read_text(encoding="utf-8")
        assert "2021-01" in text
        assert "2026-10-09" in text
        assert "12.6%" in text
        assert "-20%" in text
        assert "1.00" in text
        assert "9.5%" in text
        assert "-17.7%" in text
        assert "-4.8%" in text
        assert "-46%" in text
        assert "10 bp" in text or "10bp" in text
