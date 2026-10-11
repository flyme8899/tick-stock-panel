"""本次补上的选股字段：均线排列、KDJ/MA 交叉、资金窗口、可选财务列。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.backtest.fundamentals import attach_fundamental_factors, load_fundamental_snapshot
from app.fund_flow import factors, store
from app.indicators.pipeline import compute_indicators, compute_signals


def test_ma_alignment_and_kdj_cross():
    rows = []
    for i in range(80):
        close = 10.0 + i * 0.2
        rows.append({
            "symbol": "600000.SH",
            "date": date(2024, 1, 1) + timedelta(days=i),
            "open": close - 0.05,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": 1000.0,
            "amount": close * 1000.0,
        })
    indicators = compute_indicators(pl.DataFrame(rows))
    assert indicators["ma_bull_align"][-1] == 1
    assert indicators["ma_bear_align"][-1] == 0

    crossed = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "date": [date(2024, 1, 2), date(2024, 1, 3)],
        "close": [10.0, 11.0],
        "ma30": [10.5, 10.5],
        "kdj_k": [40.0, 60.0],
        "kdj_d": [50.0, 50.0],
    })
    signals = compute_signals(crossed, needed={"signal_kdj_golden", "signal_ma30_breakout"})
    assert signals["signal_kdj_golden"].to_list()[-1] is True
    assert signals["signal_ma30_breakout"].to_list()[-1] is True


def test_fund_flow_windows_stay_null_until_complete(tmp_path):
    for offset, value in enumerate((1, 2, 3, 4, 5, 6, 7, 8, 9, 10), start=1):
        day = date(2026, 1, offset).isoformat()
        store.write_rows(tmp_path, "stock", day, [{
            "symbol": "600519.SH", "code": "600519", "trade_date": day,
            "main_net": float(value), "large_net": float(value) * 10, "super_net": None,
            "source": "test",
        }])
    frame = factors.main_net_frame(tmp_path).sort("trade_date")
    first = frame.row(0, named=True)
    third = frame.row(2, named=True)
    last = frame.row(-1, named=True)
    assert first["ff_main_net"] == 1
    assert first["ff_main_net_3d"] is None
    assert first["ff_main_net_10d"] is None
    assert third["ff_main_net_3d"] == 6
    assert third["ff_main_net_5d"] is None
    assert last["ff_main_net_10d"] == 55
    assert last["ff_large_net"] == 100
    assert last["ff_super_net"] is None
    assert last["ff_main_net_5d"] == 40


def test_optional_pe_does_not_drop_snapshot(tmp_path):
    out = tmp_path / "financials" / "metrics"
    out.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "announce_date": ["2026-04-01"],
        "roe": [12.0],
        "gross_margin": [30.0],
        "net_margin": [8.0],
        "revenue_yoy": [5.0],
        "net_income_yoy": [4.0],
        "debt_to_asset_ratio": [40.0],
        "bps": [5.0],
    }).write_parquet(out / "part.parquet")
    snapshot = load_fundamental_snapshot(tmp_path)
    assert snapshot is not None
    panel = pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date(2026, 4, 3)],
        "close": [10.0],
    })
    attached = attach_fundamental_factors(
        panel, snapshot, ["roe_latest", "pe_latest", "bps_latest"],
    )
    assert attached["roe_latest"][0] == 12.0
    assert attached["bps_latest"][0] == 5.0
    assert attached["pe_latest"][0] is None
