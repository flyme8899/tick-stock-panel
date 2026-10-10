"""个股买卖点：信号上升沿、前复权远期收益、DSA 价位、做T冷却、API 契约。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.trade_signals import router
from app.services.trade_marks import (
    apply_forward_returns,
    clear_trade_mark_cache,
    forward_return,
    paper_fills,
    parse_dsa_levels,
    replay_t_marks,
    strategy_signal_marks,
    summarize_entries,
)


def _frame(closes: list[float], *, symbol: str = "600519.SH", amount: float = 1e9) -> pl.DataFrame:
    start = date(2024, 1, 2)
    return pl.DataFrame({
        "symbol": [symbol] * len(closes),
        "name": ["贵州茅台"] * len(closes),
        "date": [start + timedelta(days=offset) for offset in range(len(closes))],
        "open": closes,
        "high": [value + 0.2 for value in closes],
        "low": [value - 0.2 for value in closes],
        "close": closes,
        "volume": [1_000_000.0] * len(closes),
        "amount": [amount] * len(closes),
        "total_shares": [2e9] * len(closes),
    })


def _engine(defn):
    class Engine:
        def get(self, strategy_id):
            if strategy_id != defn.meta["id"]:
                raise ValueError(f"unknown strategy: {strategy_id}")
            return defn

        def resolve_params(self, strategy, params=None, overrides=None):
            del strategy, overrides
            return dict(params or {})

        def list_strategies(self, include_research=False):
            del include_research
            return [{
                **defn.meta,
                "source": "builtin",
                "timeframes": ["1d"],
                "asset_types": ["stock"],
            }]

        def has(self, strategy_id):
            return strategy_id == defn.meta["id"]

        def required_history_bars(self, strategy_ids, **kwargs):
            del strategy_ids, kwargs
            return 5

    return Engine()


def _filter_defn():
    def filter_history(frame, params):
        del params
        return frame.filter(pl.col("close") > 10)

    return SimpleNamespace(
        meta={
            "id": "close_gt_10",
            "name": "收盘大于10",
            "description": "收盘价高于 10 元",
            "tags": [],
        },
        execution_backend="python_history_legacy",
        filter_history_fn=filter_history,
        filter_fn=None,
        entry_signals=[],
        exit_signals=[],
        basic_filter={"enabled": False},
        matrix_strategy=None,
        composite=None,
    )


def test_forward_return_uses_later_closes_and_stops_at_the_end():
    closes = [10.0, 11.0, 12.0, 9.0, 8.0, 13.0]
    assert forward_return(closes, 0, 5) == pytest.approx(0.3)
    assert forward_return(closes, 2, 5) is None
    assert forward_return([10.0, None], 0, 1) is None


def test_win_rate_counts_positive_20d_only():
    stats = summarize_entries([
        {"side": "buy", "fwd20": 0.02},
        {"side": "buy", "fwd20": -0.01},
        {"side": "buy", "fwd20": None},
        {"side": "sell", "fwd20": 0.5},
    ])
    assert stats["signal_count"] == 3
    assert stats["win_sample"] == 2
    assert stats["win_rate"] == pytest.approx(0.5)
    assert stats["avg_fwd20"] == pytest.approx(0.005)


def test_python_filter_edges_do_not_use_future_bars():
    closes = [9, 9, 11, 12, 9, 13, 14]
    full = _frame([float(value) for value in closes])
    engine = _engine(_filter_defn())
    start = "2024-01-02"
    end = "2024-01-20"
    full_marks, note = strategy_signal_marks(
        engine, "close_gt_10", full, symbol="600519.SH", start=start, end=end,
    )
    assert note is None
    prefix = _frame([float(value) for value in closes[:5]])
    prefix_marks, _note = strategy_signal_marks(
        engine, "close_gt_10", prefix, symbol="600519.SH", start=start, end=end,
    )
    prefix_dates = {row["date"] for row in prefix_marks}
    assert {row["date"] for row in full_marks if row["date"] <= "2024-01-06"} == prefix_dates
    buys = [row for row in full_marks if row["side"] == "buy"]
    sells = [row for row in full_marks if row["side"] == "sell"]
    assert [row["date"] for row in buys] == ["2024-01-04", "2024-01-07"]
    assert [row["date"] for row in sells] == ["2024-01-06"]
    priced = apply_forward_returns(full_marks, [
        (f"2024-01-0{index + 2}", float(value)) for index, value in enumerate(closes)
    ])
    first = next(row for row in priced if row["date"] == "2024-01-04")
    assert first["price"] == pytest.approx(11.0)
    assert first["fwd5"] is None
    assert first["rule"].startswith("收盘大于10")


def test_breakout_strategy_uses_distinct_style():
    from app.strategy.engine import StrategyEngine

    engine = StrategyEngine(strategy_dirs=[Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"])
    closes = [10.0 + index * 0.05 for index in range(90)]
    frame = _frame(closes)
    marks, note = strategy_signal_marks(
        engine,
        "trend_breakout",
        frame,
        symbol="600519.SH",
        start="2024-03-01",
        end="2024-04-30",
        params={"use_volume_filter": False, "require_n_day_high": True, "require_above_ma60": True},
    )
    assert note is None
    buys = [row for row in marks if row["side"] == "buy"]
    if buys:
        assert all(row["style"] == "breakout" for row in buys)
        assert "趋势突破" in buys[0]["rule"]


def test_macd_marks_stay_stable_when_future_bars_are_appended():
    from app.strategy.engine import StrategyEngine

    engine = StrategyEngine(strategy_dirs=[Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"])
    decline = [50 - index * 0.4 for index in range(40)]
    rise = [decline[-1] + index * 0.8 for index in range(1, 50)]
    closes = decline + rise
    params = {"use_volume_filter": False, "require_macd_golden": True}
    full, note = strategy_signal_marks(
        engine, "macd_golden", _frame(closes), symbol="600519.SH",
        start="2024-01-02", end="2024-06-01", params=params,
    )
    assert note is None
    assert any(row["side"] == "buy" for row in full)
    cut = closes[:-12]
    cut_last = (date(2024, 1, 2) + timedelta(days=len(cut) - 1)).isoformat()
    prefix, _note = strategy_signal_marks(
        engine, "macd_golden", _frame(cut), symbol="600519.SH",
        start="2024-01-02", end="2024-06-01", params=params,
    )
    assert [row["date"] for row in prefix] == [
        row["date"] for row in full if row["date"] <= cut_last
    ]
    assert all(row["style"] == "triangle" for row in full if row["side"] == "buy")


def test_fundamental_without_universe_does_not_invent_marks():
    defn = SimpleNamespace(
        meta={"id": "fundamental_m1", "name": "成长质量精选", "description": "", "tags": ["基本面"]},
        execution_backend="python_history_legacy",
        filter_history_fn=lambda frame, params: frame,
        filter_fn=None,
        entry_signals=[],
        exit_signals=[],
        basic_filter={"enabled": False},
        matrix_strategy=None,
        composite=None,
    )
    marks, note = strategy_signal_marks(
        _engine(defn), "fundamental_m1", _frame([10, 11, 12]),
        symbol="600519.SH", start="2024-01-02", end="2024-02-01",
    )
    assert marks == []
    assert "全市场" in (note or "")


def test_parse_dsa_levels_and_hide_when_absent():
    report = {
        "strategy": {"stop_loss": "1700.00", "ideal_buy": "1800"},
        "details": {
            "raw_result": {
                "dashboard": {"price": {"support_level": 1680, "resistance_level": "1900.5"}},
            },
        },
    }
    levels = {row["kind"]: row for row in parse_dsa_levels(report)}
    assert levels["support"]["price"] == pytest.approx(1680)
    assert levels["resistance"]["label"] == "压力"
    assert levels["stop"]["price"] == pytest.approx(1700)
    assert parse_dsa_levels({"summary": "没有价位"}) == []
    text_only = {"body": "支撑位：12.5，压力位：14，止损价：11.2"}
    parsed = {row["kind"]: row["price"] for row in parse_dsa_levels(text_only)}
    assert parsed == {"support": 12.5, "resistance": 14.0, "stop": 11.2}


def test_t_replay_skips_first_bar_and_respects_60_minute_cooldown():
    def bar(minute: int, close: float) -> dict:
        hour, mins = divmod(9 * 60 + 35 + minute, 60)
        return {
            "datetime": f"2024-06-03 {hour:02d}:{mins:02d}:00",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1,
            "amount": 10 * 100,
        }

    bars = [bar(0, 10)]
    bars[0]["volume"] = 1000
    bars[0]["amount"] = 10 * 1000 * 100
    bars.append(bar(1, 10.4))
    bars.extend(bar(offset, 10.4) for offset in range(2, 10))
    bars.append(bar(10, 10.0))
    bars.append(bar(11, 10.4))
    bars.extend(bar(offset, 10.0) for offset in range(12, 80))
    bars.append(bar(80, 10.4))
    marks = replay_t_marks(bars, symbol="600519.SH", prev_close=10, cooldown_min=60)
    above = [row for row in marks if row["rule"] == "高于分时均价"]
    assert len(above) == 2
    assert above[0]["side"] == "sell"
    assert above[0]["time"] == "09:36"
    assert above[1]["time"] != "09:46"


def test_paper_fills_keep_this_symbol_inside_the_window(tmp_path: Path):
    account = tmp_path / "paper" / "accounts" / "default"
    account.mkdir(parents=True)
    (account / "account.json").write_text(json.dumps({
        "id": "default", "name": "默认", "created_at": "2024-01-01T00:00:00",
        "initial_cash": 1, "cash": 1, "status": "active",
    }), encoding="utf-8")
    rows = [
        {"kind": "fill", "symbol": "600519.SH", "date": "2024-03-01", "side": "buy", "price": 10.5, "qty": 100},
        {"kind": "fill", "symbol": "000001.SZ", "date": "2024-03-01", "side": "buy", "price": 8, "qty": 100},
        {"kind": "fill", "symbol": "600519.SH", "date": "2024-05-01", "side": "sell", "price": 12, "qty": 100},
        {"kind": "corp_action", "symbol": "600519.SH", "date": "2024-03-02", "side": "buy", "price": 1, "qty": 1},
    ]
    (account / "fills.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    found = paper_fills(tmp_path, "600519.SH", "2024-01-01", "2024-04-01")
    assert found == [{
        "date": "2024-03-01", "side": "buy", "price": 10.5, "qty": 100, "account": "default",
    }]


def test_api_unknown_strategy_empty_window_and_cache(tmp_path: Path):
    clear_trade_mark_cache()
    calls = {"daily": 0}

    def get_daily(symbol, start, end):
        del start, end
        calls["daily"] += 1
        return _frame([9, 11, 12, 8], symbol=symbol)

    defn = _filter_defn()
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_daily=get_daily)
    app.state.strategy_engine = _engine(defn)
    client = TestClient(app)

    missing = client.get("/api/signals/600519.SH?strategy=nope&start=2024-01-01&end=2024-02-01")
    assert missing.status_code == 404

    bad = client.get("/api/signals/600519.SH?start=2024-02-01&end=2024-01-01")
    assert bad.status_code == 400

    ok = client.get(
        "/api/signals/600519.SH?strategy=close_gt_10&start=2024-01-02&end=2024-01-10",
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["markers"][0]["side"] == "buy"
    assert body["markers"][0]["date"] == "2024-01-03"
    assert body["stats"]["signal_count"] == 1
    assert body["stats"]["win_rate"] is None
    assert body["levels"] == []
    assert body["fills"] == []
    assert any(row["id"] == "close_gt_10" for row in body["strategies"])

    again = client.get(
        "/api/signals/600519.SH?strategy=close_gt_10&start=2024-01-02&end=2024-01-10",
    )
    assert again.status_code == 200
    assert calls["daily"] == 1

    empty = client.get("/api/signals/../x?start=2024-01-01&end=2024-01-02")
    assert empty.status_code == 404 or empty.status_code == 400
