"""回测 ephemeral overlay (当次多选叠加, 不落盘) 测试。

覆盖批次 20261026gbtstrat 的 G-T7 / G-T8 / G-T9 后端面:
- 请求面字段钉死: overlay_children / merge_mode(默认 intersect) / min_confirm
- ≥2 子策略才启用; 1 个拒并提示改单选; 重复/超上限/权重非法拒
- 禁嵌套: source/execution_backend 为 composite 或 id 前缀 composite_ → 拒
- 不落盘: 跑完 data/strategies/composite/ 下不多出任何文件
- 不强制先 save: 无须存在任何 composite_* 策略也能跑通
- 合并语义复用 merge_signal_matrices: union / intersect 结果不同且符合定义
- 归因: 结果 strategy_info 带 composite_children + ephemeral_overlay 标记
- 回归: 落盘 composite 单选路径的 merge_mode 仍取 params (默认 union), 未被改口径
- 零碰: 选股 merge_results 未被改动

合成行情与 composite e2e 测试同法 (上涨趋势制造金叉), 不依赖生产数据。
"""
from __future__ import annotations

import inspect
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from fastapi import HTTPException

from app.backtest import overlay as overlay_mod
from app.backtest.engine import BacktestEngine
from app.backtest.matrix import build_market_data_matrix
from app.backtest.overlay import (
    EPHEMERAL_OVERLAY_STRATEGY_ID,
    MIN_OVERLAY_CHILDREN,
    OverlaySpecError,
    build_overlay_spec,
    parse_overlay_children,
)
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.composite import DEFAULT_MERGE_MODE, MERGE_MODES, normalize_merge_mode
from app.strategy.engine import StrategyEngine

_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "app" / "strategy" / "builtin"


# ── 夹具: 合成行情 + 服务 ──────────────────────────────────────────


def _synthetic_panel() -> pl.DataFrame:
    np.random.seed(42)
    n_days = 100
    symbols = ["000001.SZ", "000002.SZ", "600000.SH"]
    rows = []
    for sym_idx, sym in enumerate(symbols):
        base = 10.0 + sym_idx * 5
        for t in range(n_days):
            d = date(2025, 1, 2) + timedelta(days=int(t * 1.5))
            if t < 50:
                close = base + np.random.uniform(-0.3, 0.3)
            else:
                close = base * (1 + (t - 50) * 0.015) + np.random.uniform(-0.3, 0.3)
            open_p = close + np.random.uniform(-0.2, 0.2)
            high = max(open_p, close) + np.random.uniform(0.1, 0.5)
            low = min(open_p, close) - np.random.uniform(0.1, 0.5)
            vol = float(np.random.uniform(1e6, 5e6))
            rows.append({
                "symbol": sym, "date": d, "open": open_p, "high": high,
                "low": low, "close": close, "volume": vol,
                "raw_close": close, "raw_high": high, "raw_low": low,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


class _RepoStub:
    def get_index_daily(self, *args, **kwargs) -> pl.DataFrame:
        return pl.DataFrame()


def _make_service(panel: pl.DataFrame, strategy_dirs: list[Path]) -> StrategyBacktestService:
    bt_engine = BacktestEngine(_RepoStub())

    def _load_mdm(self, symbols, start, end, feature_plan, asset_type="stock", **kw):
        df = panel.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        if symbols:
            df = df.filter(pl.col("symbol").is_in(symbols))
        cols = [c for c in sorted(set(feature_plan.base_columns)) if c in df.columns]
        return build_market_data_matrix(df.select(cols), field_columns=feature_plan.matrix_columns)

    bt_engine.load_market_data_matrix_for_backtest = _load_mdm.__get__(bt_engine)
    return StrategyBacktestService(bt_engine, StrategyEngine(strategy_dirs=strategy_dirs))


@pytest.fixture
def svc(tmp_path):
    """内置策略目录 + 空 composite 目录 (验证不落盘/不强制先 save)。"""
    comp_dir = tmp_path / "strategies" / "composite"
    comp_dir.mkdir(parents=True)
    service = _make_service(_synthetic_panel(), [_BUILTIN_DIR, comp_dir])
    return service, comp_dir


def _overlay_children(*ids: str, weight: float = 1.0) -> list[dict]:
    return [{"strategy_id": sid, "weight": weight} for sid in ids]


# 合成面板上 intersect(默认, 全部子策略同一天命中) 能出交易的组合:
# boll_breakout 与 macd_golden 在上涨趋势段确实有同日交集。默认合并模式就是
# intersect, 所以主路径用例必须用一对真能交集的行, 否则测的是"没信号"而不是叠加。
_PAIR = ("boll_breakout", "macd_golden")


def _cfg(children: list[dict] | None, **kw) -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id=kw.pop("strategy_id", "ma_golden_cross"),
        symbols=None,
        start=date(2025, 2, 1),
        end=date(2025, 5, 1),
        mode="position",
        max_positions=5,
        holding_days=10,
        overrides={"basic_filter": {"enabled": False}},
        overlay_children=children,
        **kw,
    )

# ── G-T7 请求面与解析 ──────────────────────────────────────────────


def test_default_merge_mode_is_intersect() -> None:
    """默认写死 intersect, 且与 CompositeStrategyDialog 的口径同源。"""
    assert DEFAULT_MERGE_MODE == "intersect"
    assert MERGE_MODES == ("union", "intersect")
    spec = build_overlay_spec(overlay_children=_overlay_children("a", "b"))
    assert spec.merge_mode == "intersect"
    assert spec.min_confirm == 0


def test_normalize_merge_mode_rejects_unknown_values() -> None:
    assert normalize_merge_mode("UNION") == "union"
    assert normalize_merge_mode(" Intersect ") == "intersect"
    assert normalize_merge_mode(None) == "intersect"
    for bad in ("and", "or", "both", ""):
        if bad == "":
            continue
        with pytest.raises(ValueError):
            normalize_merge_mode(bad)


def test_single_child_is_rejected_with_hint() -> None:
    """1 个子策略没有合并语义: 必须拒, 且提示改单选 (不能静默降级)。"""
    with pytest.raises(OverlaySpecError) as exc:
        parse_overlay_children(_overlay_children("ma_golden_cross"))
    assert str(MIN_OVERLAY_CHILDREN) in str(exc.value)
    assert "单选" in str(exc.value)


def test_duplicate_and_oversized_children_are_rejected() -> None:
    with pytest.raises(OverlaySpecError, match="重复"):
        parse_overlay_children(_overlay_children("a", "a"))
    from app.strategy.engine import MAX_COMPOSITE_CHILDREN

    many = [{"strategy_id": f"s{i}"} for i in range(MAX_COMPOSITE_CHILDREN + 1)]
    with pytest.raises(OverlaySpecError, match="最多"):
        parse_overlay_children(many)


def test_negative_weight_and_missing_id_are_rejected() -> None:
    with pytest.raises(OverlaySpecError, match="不能为负"):
        parse_overlay_children([
            {"strategy_id": "a", "weight": -1},
            {"strategy_id": "b", "weight": 1},
        ])
    with pytest.raises(OverlaySpecError, match="缺少 strategy_id"):
        parse_overlay_children([{"weight": 1}, {"strategy_id": "b"}])


def test_min_confirm_out_of_range_is_rejected() -> None:
    with pytest.raises(OverlaySpecError, match="min_confirm"):
        build_overlay_spec(
            overlay_children=_overlay_children("a", "b"),
            merge_mode="intersect",
            min_confirm=3,
        )


def test_missing_weight_defaults_to_one() -> None:
    """权重缺省 1.0 且不在解析阶段归一 —— 归一是合并器的事。"""
    children = parse_overlay_children([{"strategy_id": "a"}, {"strategy_id": "b"}])
    assert [c.weight for c in children] == [1.0, 1.0]


# ── G-T9 禁嵌套 ────────────────────────────────────────────────────


def test_composite_child_is_rejected(svc) -> None:
    """子策略本身是 composite → 拒 (三处判据任一命中)。"""
    service, comp_dir = svc
    (comp_dir / "composite_mix.py").write_text(
        'META = {"id": "composite_mix", "name": "mix", "asset_types": ["stock"], '
        '"timeframes": ["1d"], "children": [{"strategy_id": "ma_golden_cross", "weight": 1}]}\n'
        'EXECUTION_BACKEND = "composite"\n',
        encoding="utf-8",
    )
    service.strategy_engine.reload()
    cfg = _cfg(_overlay_children("composite_mix", "macd_golden"))
    result = service.run(cfg)
    assert result.error is not None
    assert "叠加" in result.error and "composite_mix" in result.error


def test_nested_check_covers_all_three_signals() -> None:
    """禁嵌套不能只看 execution_backend: source / id 前缀命中同样要拒。"""
    from types import SimpleNamespace

    from app.backtest.overlay import OverlaySpec

    class _Engine:
        def __init__(self, defs):
            self._defs = defs

        def has(self, sid):
            return sid in self._defs

        def get(self, sid):
            return self._defs[sid]

    cases = {
        "source": SimpleNamespace(source="composite", execution_backend="matrix_native"),
        "backend": SimpleNamespace(source="custom", execution_backend="composite"),
        "prefix": SimpleNamespace(source="builtin", execution_backend="matrix_native"),
    }
    for label, bad in cases.items():
        bad_id = "composite_x" if label == "prefix" else "child_a"
        engine = _Engine({
            bad_id: bad,
            "child_b": SimpleNamespace(source="builtin", execution_backend="matrix_native"),
        })
        spec2 = OverlaySpec(
            children=parse_overlay_children(
                _overlay_children(bad_id, "child_b")
            )
        )
        with pytest.raises(OverlaySpecError, match="再叠叠加"):
            overlay_mod.register_overlay(engine, spec2)  # type: ignore[arg-type]


# ── G-T7 端到端: 多选当次跑通 ─────────────────────────────────────


def test_overlay_backtest_runs_without_saving_composite(svc) -> None:
    """G-T8: 不强制先 save —— 仓里没有任何 composite_* 也能跑, 且不落盘。"""
    service, comp_dir = svc
    before = sorted(p.name for p in comp_dir.glob("*.py"))
    assert before == [], "夹具前提: composite 目录初始为空"

    result = service.run(_cfg(_overlay_children(*_PAIR)))

    assert result.error is None, f"叠加回测失败: {result.error}"
    after = sorted(p.name for p in comp_dir.glob("*.py"))
    assert after == [], "ephemeral overlay 不得在 composite 目录落盘"


def test_overlay_result_carries_attribution(svc) -> None:
    """G-T7: 结果带 composite_children 归因 + ephemeral 标记。"""
    service, _ = svc
    result = service.run(_cfg(_overlay_children(*_PAIR)))
    assert result.error is None, result.error
    info = result.strategy_info
    assert info["composite_children"] == [
        {"id": _PAIR[0], "weight": 1.0},
        {"id": _PAIR[1], "weight": 1.0},
    ]
    assert info["ephemeral_overlay"] is True
    assert info["merge_mode"] == "intersect"
    assert info["source"] == "composite"
    assert info["execution_backend"] == "composite"


def test_overlay_strategy_id_is_pinned_to_placeholder(svc) -> None:
    """请求里传的 strategy_id 被忽略, 统一钉死占位 id (避免两个真源)。"""
    service, _ = svc
    cfg = _cfg(_overlay_children(*_PAIR))
    assert cfg.strategy_id == EPHEMERAL_OVERLAY_STRATEGY_ID
    result = service.run(cfg)
    assert result.error is None, result.error
    assert result.strategy_info["id"] == EPHEMERAL_OVERLAY_STRATEGY_ID


def test_union_and_intersect_produce_different_entry_counts(svc) -> None:
    """合并语义复用 merge_signal_matrices: union ⊇ intersect (默认 min_confirm 下取子集)。"""
    service, _ = svc

    def _trades(mode: str, confirm: int) -> int:
        cfg = _cfg(_overlay_children(*_PAIR), merge_mode=mode, min_confirm=confirm)
        res = service.run(cfg)
        # 清理临时注册, 保证下一次按新参数重新合成
        service.strategy_engine._strategies.pop(EPHEMERAL_OVERLAY_STRATEGY_ID, None)
        assert res.error is None, f"{mode}/{confirm} 失败: {res.error}"
        return len(res.trades)

    union_trades = _trades("union", 0)
    intersect_trades = _trades("intersect", 0)
    assert union_trades > 0, "union 应产出交易"
    assert union_trades >= intersect_trades, (
        f"intersect 必须是 union 的子集: union={union_trades} intersect={intersect_trades}"
    )


def test_intersect_without_common_hits_reports_no_signal_not_fake_trades(svc) -> None:
    """默认 intersect 要求全部子策略同日命中; 无交集时必须明确报无信号。

    这条钉的是"诚实口径": 叠加跑不出东西时不许用 union 兜底假装成功 ——
    用户看到的"无买入信号"就是真相, 换成并集才是改了口径。
    """
    service, _ = svc
    cfg = _cfg(_overlay_children("ma_golden_cross", "macd_golden"), merge_mode="intersect")
    result = service.run(cfg)
    assert result.trades == []
    assert result.error is not None and "未产生买入信号" in result.error


def test_min_confirm_equal_to_child_count_equals_full_intersect(svc) -> None:
    """min_confirm == 子策略数 与默认 intersect 语义一致 (都要求全部命中)。"""
    service, _ = svc

    def _run(confirm: int):
        res = service.run(
            _cfg(_overlay_children(*_PAIR), merge_mode="intersect", min_confirm=confirm)
        )
        service.strategy_engine._strategies.pop(EPHEMERAL_OVERLAY_STRATEGY_ID, None)
        return res

    default_res = _run(0)
    full_res = _run(len(_PAIR))
    assert default_res.error == full_res.error
    assert len(default_res.trades) == len(full_res.trades)


def test_overlay_uses_config_merge_mode_not_params(svc) -> None:
    """叠加路径的 merge_mode 来自请求字段; params 里的同名键不得覆盖它。

    两层断言缺一不可:
    - strategy_info 里的归因字段 (证明报告的数字对)
    - **实际交易数** (证明信号矩阵真的是按 config 合并的) —— 归因字段是从 config
      直接抄的, 只断言它会漏掉"合并时其实读了 params"这种变异。
    """
    service, _ = svc

    def _run(**kw):
        service.strategy_engine._strategies.pop(EPHEMERAL_OVERLAY_STRATEGY_ID, None)
        res = service.run(_cfg(_overlay_children(*_PAIR), **kw))
        assert res.error is None, res.error
        return res

    union_only = _run(merge_mode="union")
    intersect_only = _run(merge_mode="intersect")
    # params 与 config 相反: 结果必须跟随 config (union = 交易更多)
    cfg_union_params_intersect = _run(
        merge_mode="union", params={"merge_mode": "intersect", "min_confirm": 2}
    )
    cfg_intersect_params_union = _run(
        merge_mode="intersect", params={"merge_mode": "union", "min_confirm": 0}
    )

    assert len(union_only.trades) > len(intersect_only.trades), (
        "前提: 两种合并模式在这份合成行情上必须产出不同交易数, 否则本测无鉴别力"
    )
    assert len(cfg_union_params_intersect.trades) == len(union_only.trades), (
        "params 里塞相反的 merge_mode 不得改变叠加结果"
    )
    assert len(cfg_intersect_params_union.trades) == len(intersect_only.trades), (
        "params 里塞 union 也不得把 intersect 的叠加救活"
    )
    assert cfg_union_params_intersect.strategy_info["merge_mode"] == "union"
    assert cfg_union_params_intersect.strategy_info["min_confirm"] == 0


def test_overlay_registration_is_scoped_to_the_engine(svc) -> None:
    """合成只写当次引擎的注册表; 另一份从磁盘加载的引擎看不到它。"""
    service, _ = svc
    other = StrategyEngine(strategy_dirs=[_BUILTIN_DIR])
    assert not other.has(EPHEMERAL_OVERLAY_STRATEGY_ID)
    result = service.run(_cfg(_overlay_children(*_PAIR)))
    assert result.error is None, result.error
    assert service.strategy_engine.has(EPHEMERAL_OVERLAY_STRATEGY_ID)
    assert not other.has(EPHEMERAL_OVERLAY_STRATEGY_ID), "不得跨引擎泄漏临时策略"


def test_unknown_child_reports_clear_error(svc) -> None:
    service, _ = svc
    result = service.run(_cfg(_overlay_children("ma_golden_cross", "no_such_strategy")))
    assert result.error is not None
    assert "no_such_strategy" in result.error and "不存在" in result.error


def test_overlay_does_not_break_plain_single_strategy_run(svc) -> None:
    """G-T11 回归: 不传 overlay_children 时行为不变。"""
    service, _ = svc
    single = _PAIR[0]
    cfg = _cfg(None, strategy_id=single)
    assert cfg.strategy_id == single, "无叠加时不得改写 strategy_id"
    result = service.run(cfg)
    assert result.error is None, result.error
    assert len(result.trades) > 0
    assert "composite_children" not in result.strategy_info
    assert "ephemeral_overlay" not in result.strategy_info


def test_stored_composite_keeps_params_merge_mode(svc) -> None:
    """G-T11 回归: 落盘 composite 单选仍从 params 读 merge_mode (默认 union)。"""
    service, comp_dir = svc
    (comp_dir / "composite_legacy.py").write_text(
        'META = {"id": "composite_legacy", "name": "legacy", "asset_types": ["stock"], '
        '"timeframes": ["1d"], "params": [{"id": "merge_mode", "type": "select", '
        '"options": ["union", "intersect"], "default": "union"}, '
        '{"id": "min_confirm", "type": "int", "default": 0}], '
        '"children": [{"strategy_id": "ma_golden_cross", "weight": 1}, '
        '{"strategy_id": "macd_golden", "weight": 1}]}\n'
        'EXECUTION_BACKEND = "composite"\n',
        encoding="utf-8",
    )
    service.strategy_engine.reload()
    result = service.run(_cfg(None, strategy_id="composite_legacy"))
    assert result.error is None, result.error
    info = result.strategy_info
    # 落盘 composite 的归因里不出现 ephemeral 专有字段 —— 两者形态相近但不能混为一谈。
    assert "ephemeral_overlay" not in info
    assert info["id"] == "composite_legacy"
    assert info["composite_children"] == [
        {"id": "ma_golden_cross", "weight": 1.0},
        {"id": "macd_golden", "weight": 1.0},
    ]

    # 口径未被本拍改动: 落盘 composite 仍从 params 读 merge_mode / min_confirm。
    # 传 intersect+min_confirm=2 后这条对在合成面板上没有同日交集 → 报无信号;
    # 说明 params 确实生效了 (若被 config 的默认 intersect 覆盖, 或反之被忽略成
    # union, 都会得到"有交易"的结果, 断言就会红)。
    union_res = service.run(_cfg(None, strategy_id="composite_legacy"))
    intersect_res = service.run(
        _cfg(
            None,
            strategy_id="composite_legacy",
            params={"merge_mode": "intersect", "min_confirm": 2},
        )
    )
    assert union_res.error is None, union_res.error
    assert len(union_res.trades) > 0
    assert intersect_res.error is not None and "未产生买入信号" in intersect_res.error, (
        "落盘 composite 必须仍按 params 读合并模式; 若这里出交易说明 params 被忽略"
    )


# ── API 层: 400 与字段命名 ────────────────────────────────────────


def test_api_rejects_single_child_with_400() -> None:
    from app.api.backtest import OverlayChild, _overlay_children_payload

    with pytest.raises(HTTPException) as exc:
        _overlay_children_payload([OverlayChild(strategy_id="ma_golden_cross")])
    assert exc.value.status_code == 400
    assert "单选" in exc.value.detail


def test_api_accepts_two_children() -> None:
    from app.api.backtest import OverlayChild, _overlay_children_payload

    payload = _overlay_children_payload([
        OverlayChild(strategy_id="ma_golden_cross"),
        OverlayChild(strategy_id="macd_golden", weight=2.0),
    ])
    assert payload == [
        {"strategy_id": "ma_golden_cross", "weight": 1.0},
        {"strategy_id": "macd_golden", "weight": 2.0},
    ]


def test_api_rejects_malformed_overlay_json() -> None:
    from app.api.backtest import _parse_overlay_children_query

    with pytest.raises(HTTPException) as exc:
        _parse_overlay_children_query("{not json")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc2:
        _parse_overlay_children_query('"a string"')
    assert exc2.value.status_code == 400


def test_api_merge_mode_and_min_confirm_guards() -> None:
    from app.api.backtest import _checked_merge_mode, _checked_min_confirm_query

    assert _checked_merge_mode(None) == "intersect"
    assert _checked_merge_mode("union") == "union"
    with pytest.raises(HTTPException) as exc:
        _checked_merge_mode("and")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        _checked_min_confirm_query(3, [{"strategy_id": "a"}, {"strategy_id": "b"}])


def test_request_model_declares_pinned_fields() -> None:
    """把"约定"写成"签名": 字段名与默认值不靠人记。"""
    from app.api.backtest import StrategyBacktestRequest

    fields = StrategyBacktestRequest.model_fields
    assert fields["overlay_children"].default is None
    assert fields["merge_mode"].default == "intersect"
    assert fields["min_confirm"].default == 0


def test_config_normalizes_strategy_id_only_when_overlay_present() -> None:
    children = _overlay_children("a", "b")
    with_overlay = _cfg(children, strategy_id="whatever")
    assert with_overlay.strategy_id == EPHEMERAL_OVERLAY_STRATEGY_ID
    without = _cfg(None, strategy_id="ma_golden_cross")
    assert without.strategy_id == "ma_golden_cross"


def test_job_key_includes_overlay_fields() -> None:
    """不同叠加组合不得命中同一个 job (否则用户看到别人的结果)。"""
    from app.api.backtest import _make_job_key

    common = (
        "s", None, None, None, "open_t+1", None, None,
        0.0002, 5.0, 10, 1.0, 1e6, "equal", None, None,
    )
    two = '[{"strategy_id":"a"},{"strategy_id":"b"}]'
    k1 = _make_job_key(*common, overlay_children=two, merge_mode="union")
    k2 = _make_job_key(*common, overlay_children=two, merge_mode="intersect")
    k3 = _make_job_key(*common)
    k4 = _make_job_key(
        *common,
        overlay_children='[{"strategy_id":"a"},{"strategy_id":"c"}]',
        merge_mode="union",
    )
    assert len({k1, k2, k3, k4}) == 4


def test_job_key_stable_for_same_overlay() -> None:
    """同一叠加参数必须得到同一个 key (否则重连/取消会找不到任务)。"""
    from app.api.backtest import _make_job_key

    common = (
        "s", None, None, None, "open_t+1", None, None,
        0.0002, 5.0, 10, 1.0, 1e6, "equal", None, None,
    )
    two = '[{"strategy_id":"a"},{"strategy_id":"b"}]'
    assert _make_job_key(*common, overlay_children=two) == _make_job_key(
        *common, overlay_children=two
    )


# ── 零碰自检 ──────────────────────────────────────────────────────


def test_overlay_module_never_writes_files() -> None:
    """模块本身不得出现写盘调用 —— 不落盘是语义, 不是口头承诺。"""
    src = inspect.getsource(overlay_mod)
    for needle in ("write_text", "open(", "mkdir", "unlink", "shutil", "os.remove"):
        assert needle not in src, f"overlay 模块出现写盘调用: {needle}"


def test_scored_merge_results_untouched() -> None:
    """选股 merge_results 的合并口径不得被本拍改动 (只复用, 不改)。"""
    from app.strategy import composite

    src = inspect.getsource(composite.merge_results)
    assert "merge_mode == \"intersect\"" in src
    # 本拍只新增常量与 normalize 函数, 不触碰 merge_results 内部逻辑。
    assert "MERGE_MODES" not in src


def test_no_sample_ai_strategy_is_added() -> None:
    repo = Path(__file__).resolve().parents[2]
    hits = [p for p in repo.rglob("*") if "ai_chaodie_fantan" in p.name]
    assert hits == [], f"不得新增样本 AI 策略: {hits}"


# ── SSE 侧参数传递 ────────────────────────────────────────────────


def _stream_capture_config(monkeypatch, query: dict) -> tuple[int, dict | None, str]:
    """请求一次 SSE 端点, 把落进 StrategyBacktestConfig 的字段抓出来。

    回测在后台线程里跑, 直接 monkeypatch worker 不保险。改成卡住重任务槽:
    线程会在进槽时直接抛异常, 回测不会真跑, 但 config 已经构造完毕。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.services.heavy_job_limiter as limiter_mod
    from app.api import backtest as backtest_api

    captured: dict = {}
    import app.backtest.strategy as strategy_mod

    real_cfg = strategy_mod.StrategyBacktestConfig

    class _CapturingConfig(real_cfg):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    class _StopError(Exception):
        pass

    class _Slot:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            raise _StopError("被测: 不进重任务槽")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(strategy_mod, "StrategyBacktestConfig", _CapturingConfig)
    monkeypatch.setattr(
        limiter_mod, "shared_heavy_job_limiter", type("L", (), {"slot": lambda *a, **k: _Slot()})()
    )

    app = FastAPI()
    app.include_router(backtest_api.router)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/backtest/strategy/stream", params=query)
    return resp.status_code, captured or None, resp.text


def test_stream_passes_overlay_fields_into_config(monkeypatch) -> None:
    """SSE 端点必须把 overlay_children / merge_mode / min_confirm 传进配置。

    worker 子进程只认 `StrategyBacktestConfig`; query 参数漏传就是"界面勾了叠加,
    实际跑的是单选" —— 最难察觉的一类串味。
    """
    import json as _json

    status, cfg, _body = _stream_capture_config(monkeypatch, {
        "strategy_id": "whatever",
        "start": "2025-01-01",
        "end": "2025-02-01",
        "overlay_children": _json.dumps([
            {"strategy_id": "a", "weight": 1},
            {"strategy_id": "b", "weight": 2},
        ]),
        "merge_mode": "union",
        "min_confirm": "1",
    })
    assert status == 200
    assert cfg is not None, "config 未被构造, 说明请求在校验阶段就被拒了"
    assert cfg["overlay_children"] == [
        {"strategy_id": "a", "weight": 1},
        {"strategy_id": "b", "weight": 2},
    ]
    assert cfg["merge_mode"] == "union"
    assert cfg["min_confirm"] == 1


def test_stream_defaults_to_intersect_when_overlay_given(monkeypatch) -> None:
    """不传 merge_mode 时 SSE 必须落到 intersect, 不能是 union。"""
    import json as _json

    _status, cfg, _body = _stream_capture_config(monkeypatch, {
        "strategy_id": "whatever",
        "start": "2025-01-01",
        "end": "2025-02-01",
        "overlay_children": _json.dumps([{"strategy_id": "a"}, {"strategy_id": "b"}]),
    })
    assert cfg is not None
    assert cfg["merge_mode"] == "intersect"
    assert cfg["min_confirm"] == 0


def test_stream_rejects_bad_overlay_before_starting_job() -> None:
    """SSE 侧非法叠加参数必须是 400, 不能带着坏参数把任务起起来。"""
    import json as _json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import backtest as backtest_api

    app = FastAPI()
    app.include_router(backtest_api.router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/backtest/strategy/stream", params={
        "strategy_id": "whatever",
        "start": "2025-01-01",
        "end": "2025-02-01",
        "overlay_children": _json.dumps([{"strategy_id": "only_one"}]),
    })
    assert resp.status_code == 400, resp.text
    assert "单选" in resp.json()["detail"]

    resp2 = client.get("/api/backtest/strategy/stream", params={
        "strategy_id": "whatever",
        "start": "2025-01-01",
        "end": "2025-02-01",
        "overlay_children": _json.dumps([{"strategy_id": "a"}, {"strategy_id": "b"}]),
        "merge_mode": "and",
    })
    assert resp2.status_code == 400, resp2.text
    assert "merge_mode" in resp2.json()["detail"]
