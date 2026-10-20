"""策略体检 r2：展示和推送标注。信号计算与导出不读验证状态。"""
from __future__ import annotations

import ast
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.trade_marks import strategy_signal_marks
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.strategy.monitor import MonitorRuleEngine
from app.strategy.validation_status import (
    CLASSIFICATION,
    FACTOR_RANK_NOTE,
    REFERENCE_NOTE,
    REVERSE_NOTE,
    SOURCE,
    annotate_strategy_push,
    attach_validation,
    present_trade_markers,
    pushes,
    resolve,
)

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
REVERSE = {
    "consecutive_limit_ups",
    "limit_up_momentum",
    "broken_board_recovery",
    "near_limit_up",
    "strong_open",
}
RETIRED = {
    "breakout_new_high_60d",
    "boll_breakout",
    "active_limit_gene",
    "ma_golden_cross",
    "platform_consolidation_breakout",
    "trend_breakout",
    "macd_golden",
    "ma_convergence_breakout",
    "volume_price_surge",
    "high_turnover_surge",
}
UNVERIFIED = {
    "fundamental_m1",
    "fundamental_m2",
    "fundamental_m3",
    "factor_rank_research",
}


def _builtin_metas() -> dict[str, dict]:
    engine = StrategyEngine(strategy_dirs=[BUILTIN_DIR])
    metas = {
        item["id"]: item
        for item in engine.list_strategies(include_research=True)
        if item.get("source") == "builtin"
    }
    assert not engine.load_errors(), engine.load_errors()
    return metas


def test_classification_covers_every_builtin_strategy():
    assert SOURCE == "策略体检 r2，2026-10-20"
    metas = _builtin_metas()
    assert set(metas) == set(CLASSIFICATION)
    counts = Counter(item["validation_status"] for item in metas.values())
    assert counts == Counter(reverse=5, retired=10, unverified=4, reference_only=10)
    assert set(REVERSE) == {sid for sid, item in metas.items() if item["validation_status"] == "reverse"}
    assert set(RETIRED) == {sid for sid, item in metas.items() if item["validation_status"] == "retired"}
    assert set(UNVERIFIED) == {sid for sid, item in metas.items() if item["validation_status"] == "unverified"}
    assert "rsi_midline_pullback" in metas
    assert metas["rsi_midline_pullback"]["validation_status"] == "reference_only"
    assert metas["rsi_midline_pullback"]["validation_note"] == REFERENCE_NOTE
    for sid, (status, note) in CLASSIFICATION.items():
        assert metas[sid]["validation_status"] == status
        assert metas[sid]["validation_note"] == note
    assert metas["factor_rank_research"]["validation_note"] == FACTOR_RANK_NOTE
    assert all(item["validation_note"] == REVERSE_NOTE for sid in REVERSE for item in [metas[sid]])


def test_missing_validation_status_defaults_to_reference_only():
    meta = {"id": "brand_new_strategy", "name": "新策略"}
    attach_validation(meta)
    assert meta["validation_status"] == "reference_only"
    assert meta["validation_note"] == REFERENCE_NOTE
    assert resolve({"validation_status": "retired"})[1]
    assert resolve({}) == ("reference_only", REFERENCE_NOTE)


def test_retired_markers_leave_buy_list_and_reverse_carries_warning():
    buy = {"id": "buy:1", "side": "buy", "style": "triangle", "rule": "原买点", "date": "2024-01-02"}
    sell = {"id": "sell:1", "side": "sell", "style": "triangle", "rule": "卖出", "date": "2024-01-03"}
    retired, retired_note = present_trade_markers(
        {"id": "ma_golden_cross", "validation_status": "retired"},
        [buy, sell],
    )
    assert retired_note
    assert [row["side"] for row in retired] == ["sell"]

    reverse, reverse_note = present_trade_markers(
        {"id": "consecutive_limit_ups", "validation_status": "reverse", "validation_note": REVERSE_NOTE},
        [buy, sell],
    )
    assert reverse_note == REVERSE_NOTE
    assert reverse[0]["side"] == "risk"
    assert reverse[0]["rule"] == REVERSE_NOTE
    assert reverse[1]["side"] == "sell"

    reference, note = present_trade_markers({"id": "custom_x"}, [dict(buy)])
    assert note == REFERENCE_NOTE
    assert reference[0]["side"] == "buy"
    assert REFERENCE_NOTE in reference[0]["rule"]


def test_retired_is_not_pushed_and_reverse_push_uses_warning():
    assert pushes({"validation_status": "retired"}) is False
    assert annotate_strategy_push({"validation_status": "retired"}, "buy_signal", "策略「均线」买入信号") is None
    assert annotate_strategy_push(
        {"validation_status": "retired"}, "pool_entry", "策略「均线」进入选股结果 茅台",
    ) is None

    text = annotate_strategy_push(
        {"validation_status": "reverse", "validation_note": REVERSE_NOTE},
        "buy_signal",
        "策略「连板股」买入信号 茅台 +1.2%",
    )
    assert text is not None
    assert REVERSE_NOTE in text
    assert "买入信号" not in text
    assert "风险提示" in text

    pool = annotate_strategy_push(
        {"validation_status": "reverse", "validation_note": REVERSE_NOTE},
        "pool_entry",
        "策略「连板股」进入选股结果 茅台",
    )
    assert pool is not None and REVERSE_NOTE in pool and "进入选股结果" not in pool

    unverified = annotate_strategy_push(
        {"id": "factor_rank_research", "validation_status": "unverified", "validation_note": FACTOR_RANK_NOTE},
        "buy_signal",
        "策略「因子排名研究」买入信号 茅台",
    )
    assert unverified is not None
    assert "未经验证：导出失败" in unverified

    reference = annotate_strategy_push({"id": "new_one"}, "buy_signal", "策略「新」买入信号 茅台")
    assert reference is not None and REFERENCE_NOTE in reference


def _filter_defn(status: str | None):
    def filter_history(frame, params):
        del params
        return frame.filter(pl.col("close") > 10)

    meta = {
        "id": "close_gt_10",
        "name": "收盘大于10",
        "tags": [],
        "description": "",
    }
    if status is not None:
        meta["validation_status"] = status
        meta["validation_note"] = "已下线，不作为买点展示，也不推送" if status == "retired" else REVERSE_NOTE
    return SimpleNamespace(
        meta=meta,
        execution_backend="polars_expr",
        filter_history_fn=filter_history,
        filter_fn=None,
        entry_signals=[],
        exit_signals=[],
        basic_filter={},
        lookback_days=1,
        composite=None,
    )


def _engine(defn):
    class Engine:
        def get(self, strategy_id):
            if strategy_id != defn.meta["id"]:
                raise ValueError(f"unknown strategy: {strategy_id}")
            return defn

        def resolve_params(self, strategy, params=None, overrides=None):
            del strategy, overrides
            return dict(params or {})

    return Engine()


def _frame():
    return pl.DataFrame({
        "symbol": ["600519.SH"] * 4,
        "date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)],
        "close": [9.0, 11.0, 12.0, 8.0],
    })


def test_signal_export_still_includes_retired_and_reverse_buys():
    """展示过滤之前，信号标记照常导出。回测模块也不读取验证状态。"""
    panel = _frame()
    for status in ("retired", "reverse"):
        marks, note = strategy_signal_marks(
            _engine(_filter_defn(status)),
            "close_gt_10",
            panel,
            symbol="600519.SH",
            start="2024-01-02",
            end="2024-01-05",
        )
        assert note is None
        assert any(row["side"] == "buy" for row in marks)
        shown, _shown_note = present_trade_markers(_filter_defn(status).meta, marks)
        if status == "retired":
            assert all(row["side"] != "buy" for row in shown)
        else:
            assert any(row["side"] == "risk" and row["rule"] == REVERSE_NOTE for row in shown)

    backend = Path(__file__).resolve().parents[1] / "app"
    for relative in ("backtest/engine.py", "backtest/strategy.py", "backtest/matrix.py"):
        source = (backend / relative).read_text(encoding="utf-8")
        assert "validation_status" not in source


def test_engine_still_runs_retired_strategy(tmp_path):
    code = '''import polars as pl
META = {"id": "retired_run", "name": "下线仍计算", "asset_types": ["stock"], "timeframes": ["1d"], "validation_status": "retired", "validation_note": "已下线"}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.col("close") > 0
'''
    (tmp_path / "retired_run.py").write_text(code, encoding="utf-8")
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    result = engine.run(
        "retired_run",
        StrategyDataContext(
            asset_type="stock",
            timeframe="1d",
            as_of=date(2026, 10, 20),
            current=pl.DataFrame({
                "symbol": ["600519.SH"],
                "name": ["茅台"],
                "close": [10.0],
                "amount": [1.0e8],
                "date": [date(2026, 10, 20)],
            }),
        ),
    )
    assert result.rows
    assert engine.get("retired_run").meta["validation_status"] == "retired"


def test_monitor_does_not_push_retired_but_still_records_result():
    rows_seq = [
        [{"symbol": "600519.SH", "name": "茅台"}],
        [{"symbol": "600519.SH", "name": "茅台"}, {"symbol": "000001.SZ", "name": "平安"}],
    ]
    calls = {"n": 0}

    class Engine:
        def get(self, strategy_id):
            return SimpleNamespace(
                meta={
                    "id": strategy_id,
                    "name": "均线金叉",
                    "validation_status": "retired",
                    "validation_note": "已下线，不作为买点展示，也不推送",
                },
                execution_backend="polars_expr",
                filter_history_fn=None,
                filter_fn=None,
                entry_signals=[],
                exit_signals=[],
                basic_filter={},
                lookback_days=1,
            )

        def required_history_bars(self, strategy_ids, **kwargs):
            del strategy_ids, kwargs
            return 1

        def run(self, strategy_id, context, **kwargs):
            del strategy_id, context, kwargs
            rows = rows_seq[min(calls["n"], len(rows_seq) - 1)]
            calls["n"] += 1
            return SimpleNamespace(
                total=len(rows),
                rows=rows,
                scores={row["symbol"]: 1.0 for row in rows},
                as_of=date(2026, 10, 20),
                entry_signal_hits=[{"symbol": row["symbol"], "signals": ["signal_ma_golden_5_20"]} for row in rows],
                exit_signal_hits=[],
            )

    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(Engine())
    frame = pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"],
        "name": ["茅台", "平安"],
        "close": [10.0, 11.0],
        "change_pct": [0.01, 0.02],
    })
    rule = {
        "id": "rule-retired",
        "type": "strategy",
        "strategy_id": "ma_golden_cross",
        "asset_type": "stock",
        "scope": "all",
        "notify_events": ["buy_signal", "pool_entry", "sell_signal", "pool_exit"],
        "cooldown_seconds": 0,
    }
    first = monitor._evaluate_rule(frame, rule, 1_000.0)
    second = monitor._evaluate_rule(frame, rule, 2_000.0)
    assert first == []
    assert second == []
    assert calls["n"] == 2
    recorded = monitor._building_strategy_results["ma_golden_cross"]
    assert recorded["total"] == 2


def test_monitor_reverse_push_includes_warning():
    class Engine:
        def get(self, strategy_id):
            return SimpleNamespace(
                meta={
                    "id": strategy_id,
                    "name": "连板股",
                    "validation_status": "reverse",
                    "validation_note": REVERSE_NOTE,
                },
                execution_backend="polars_expr",
                filter_history_fn=None,
                filter_fn=None,
                entry_signals=[],
                exit_signals=[],
                basic_filter={},
                lookback_days=1,
            )

        def required_history_bars(self, strategy_ids, **kwargs):
            del strategy_ids, kwargs
            return 1

        def run(self, strategy_id, context, **kwargs):
            del strategy_id, context, kwargs
            rows = [{"symbol": "600519.SH", "name": "茅台"}]
            return SimpleNamespace(
                total=1,
                rows=rows,
                scores={"600519.SH": 1.0},
                as_of=date(2026, 10, 20),
                entry_signal_hits=[{"symbol": "600519.SH", "signals": ["signal_limit_up"]}],
                exit_signal_hits=[],
            )

    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(Engine())
    frame = pl.DataFrame({
        "symbol": ["600519.SH"],
        "name": ["茅台"],
        "close": [10.0],
        "change_pct": [0.01],
    })
    rule = {
        "id": "rule-reverse",
        "type": "strategy",
        "strategy_id": "consecutive_limit_ups",
        "asset_type": "stock",
        "scope": "all",
        "notify_events": ["pool_entry"],
        "cooldown_seconds": 0,
    }
    assert monitor._evaluate_rule(frame, rule, 1_000.0) == []
    monitor._strategy_pools[("rule-reverse", "consecutive_limit_ups", "stock")] = set()
    events = monitor._evaluate_rule(frame, rule, 2_000.0)
    assert events
    assert all(REVERSE_NOTE in event["message"] for event in events)
    assert all("进入选股结果" not in event["message"] for event in events)


def test_builtin_modules_do_not_parse_as_missing_status():
    """每个内置 META 都挂了验证字段，分类表是唯一名单。"""
    missing = []
    for path in sorted(BUILTIN_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        has_call = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "validation_fields"
            for node in ast.walk(tree)
        )
        if path.name in {"fundamental_m1.py", "fundamental_m2.py", "fundamental_m3.py"}:
            continue
        if not has_call:
            missing.append(path.name)
    assert missing == []
    assert pytest.importorskip("app.strategy.builtin.fundamental_m1").META["validation_status"] == "unverified"
