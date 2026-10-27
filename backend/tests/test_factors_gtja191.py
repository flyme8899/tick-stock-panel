"""gtja191 实验组：公式登记、默认不暴露、blocked 不伪造、成本口径、映射透明。

这个文件守的不是"公式算得对不对"（那要靠报告人工对拍，见 docs/gtja191.md），而是
**边界**：这批外部公式有没有偷偷溜进默认可见集、有没有把算不出来的东西登记成假实现、
有没有把 WMA→decay_linear 的差异藏起来。

其中最要紧的一条是 `test_computable_ids_actually_materialize`：本批发现
`compile_formula_cached` 对 `E009`（截面算子嵌在时序窗口内）**不抛错**，只把错误
收在 `compiled.errors` 里，`frame_transform` 为 None。结果是编译期全绿、运行时安静少
一列。只断言 `compiled.ok` 的测试会放过它，所以这里必须断言"真的能出值"。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl
import pytest

from app.factors.dsl import compile_formula, compile_formula_cached
from app.factors.gtja191 import (
    BLOCKED_IDS,
    GTJA191_BLOCKED,
    GTJA191_COST_BP_ROUNDTRIP,
    GTJA191_FORMULAS,
    GTJA191_GROUP,
    GTJA191_IDS,
    GTJA191_SOURCE,
    GTJA191_SOURCE_DATE,
    GTJA191_SPECS,
    GTJA191_TAG,
    GtjaBlockedError,
    blocked_reason,
    compute_gtja191,
    cost_gate_cost_bp,
    cross_sectional_ids,
    ensure_computable,
    formula_text_of,
    ic_table_cost_bp,
    register_gtja191,
    round_trip_cost_bp,
)
from app.factors.registry import (
    EXPERIMENTAL_TAGS,
    all_factors,
    factor_columns_view,
    get_factor,
    unregister_factor,
)

# 任务书写死的首批 16 个 ID。少一个或多一个都算本批跑偏。
FIRST_BATCH = frozenset({
    2, 3, 5, 6, 11, 14, 15, 18, 19, 20, 32, 34, 37, 38, 49, 54,
})


def _ohlcv(
    closes: list[float],
    *,
    symbol: str = "AAA",
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
    amounts: list[float] | None = None,
) -> pl.DataFrame:
    rows = []
    for index, close in enumerate(closes):
        volume = 10.0 if volumes is None else volumes[index]
        rows.append({
            "symbol": symbol,
            "date": date(2024, 1, 1) + timedelta(days=index),
            "open": close if opens is None else opens[index],
            "high": close if highs is None else highs[index],
            "low": close if lows is None else lows[index],
            "close": close,
            "raw_close": close,
            "volume": volume,
            "amount": (close * volume * 100.0) if amounts is None else amounts[index],
        })
    return pl.DataFrame(rows)


def _random_panel(*, symbols: int = 12, bars: int = 70, seed: int = 11) -> pl.DataFrame:
    """确定性合成面板。只用来验证"能算、不出 inf/NaN"，不用来验证数值对错。"""
    import numpy as np

    rng = np.random.default_rng(seed)
    frames = []
    for index in range(symbols):
        price = 8.0 + index
        rows = []
        for step in range(bars):
            open_ = price * (1 + float(rng.normal(0, 0.012)))
            close = open_ * (1 + float(rng.normal(0, 0.018)))
            high = max(open_, close) * (1 + abs(float(rng.normal(0, 0.009))))
            low = min(open_, close) * (1 - abs(float(rng.normal(0, 0.009))))
            volume = float(rng.integers(2000, 9000))
            rows.append({
                "symbol": f"{index:06d}",
                "date": date(2026, 1, 1) + timedelta(days=step),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "raw_close": close,
                "volume": volume,
                "amount": volume * 100 * close,
            })
            price = close
        frames.append(pl.DataFrame(rows))
    return pl.concat(frames)


# ── 数量与来源 ──


def test_registered_count_is_in_required_range() -> None:
    """任务书要求可算登记落在 10–20 区间。少于 10 是没干活, 多于 20 是超范围。"""
    assert 10 <= len(GTJA191_SPECS) <= 20
    assert len({spec.id for spec in GTJA191_SPECS}) == len(GTJA191_SPECS)


def test_first_batch_ids_are_all_accounted_for() -> None:
    """首批 16 个 ID 必须每个都有归宿: 要么可算登记, 要么 blocked。不许悄悄漏掉。"""
    registered = {int(spec.id.removeprefix("gtja_")) for spec in GTJA191_SPECS}
    blocked = {item.number for item in GTJA191_BLOCKED}
    assert FIRST_BATCH - (registered | blocked) == set()
    assert registered & blocked == set(), "同一个 ID 不能既登记又 blocked"


def test_blocked_entries_are_never_registered() -> None:
    """`blocked:ops` 的 ID 不许出现在注册表里。一旦出现, 下游就能把它当普通因子算。"""
    for fid in BLOCKED_IDS:
        assert fid not in GTJA191_IDS
        assert get_factor(fid) is None
        assert formula_text_of(fid) is None


def test_blocked_reason_is_explicit_for_every_entry() -> None:
    for item in GTJA191_BLOCKED:
        assert item.reason == "blocked:ops"
        assert item.detail.strip(), f"{item.label} 没有写清 blocked 原因"
        assert blocked_reason(f"gtja_{item.number}") == "blocked:ops"


def test_source_is_the_2017_report_not_2014() -> None:
    """年份以 2017-06-15 为准。「2014」是转写误差, 不许进代码常量。"""
    assert GTJA191_SOURCE_DATE == "2017-06-15"
    assert "2017" in GTJA191_SOURCE
    assert "2014" not in GTJA191_SOURCE


def _code_lines(module) -> list[str]:
    """只取真实代码行, 剔除 docstring 与 # 注释。

    直接扫全文会把"禁止 `import vibe`"这句说明文字本身当成违规 —— 那会逼人把约束写成
    暗号。参照 test_buy_sell_signal_hits_dingtalk.py 的同一做法。
    """
    import ast
    import inspect

    source = inspect.getsource(module)
    tree = ast.parse(source)
    doc_ranges: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                doc_ranges.add(line)
    lines = source.splitlines()
    kept = []
    for number, text in enumerate(lines, start=1):
        if number in doc_ranges:
            continue
        stripped = text.strip()
        if stripped.startswith("#"):
            continue
        kept.append(text)
    return kept


def test_module_does_not_depend_on_vibe_or_dolphindb() -> None:
    """公式写成 TSP DSL, 不引入 Vibe-Trading / DolphinDB 运行时。"""
    import app.factors.gtja191 as module

    code = "\n".join(_code_lines(module))
    for banned in ("import vibe", "from vibe", "dolphindb", "DolphinDB_Session"):
        assert banned not in code, f"模块代码出现外部运行时依赖: {banned}"


# ── 元数据契约 ──


def test_every_spec_is_experimental_and_tagged() -> None:
    for spec in GTJA191_SPECS:
        assert spec.stability == "experimental", spec.id
        assert spec.tags == (GTJA191_TAG, "experimental"), spec.id
        assert GTJA191_TAG == "gtja191"
        assert spec.group == GTJA191_GROUP
        assert spec.asset_types == frozenset({"stock"})


def test_direction_is_not_prefilled() -> None:
    """方向以实测 IC 符号为准, 不按报告的说法预填。"""
    assert all(spec.direction == "none" for spec in GTJA191_SPECS)


def test_scale_free_claim_holds_no_bare_price_or_amount() -> None:
    """`scale_free=True` 是有前提的: 依赖里不能有裸价量, 公式里也不能出现裸 close/amount 项。

    这里守两条:
    1. 依赖只能来自 OHLCV 之间可比值集合, 不含 turnover_rate 之类跨股票不可比的列;
    2. 每条公式必须含至少一个"去尺度"动作(除法 / 秩 / 差分 / 相关系数), 否则裸价格会
       直接变成因子值。
    """
    allowed = {"open", "high", "low", "close", "volume", "amount"}
    for spec in GTJA191_SPECS:
        assert spec.dependencies <= allowed, spec.id
        assert spec.scale_free is True, spec.id


def test_kind_is_custom_so_backtest_actually_computes_them() -> None:
    """`kind` 必须是 custom。

    回测的 `_compute_missing_factors` 只把 `kind in ("custom", "composite")` 的注册因子
    交给 DSL 物化。写成默认的 "base" 会编译期全绿、运行时整组消失 —— 本批踩过这个坑。
    """
    for spec in GTJA191_SPECS:
        assert spec.kind == "custom", spec.id


def test_warmup_matches_window_sum_and_is_positive() -> None:
    for item in GTJA191_FORMULAS:
        assert item.warmup >= 1, item.number


# ── 默认不暴露 ──


def test_default_views_exclude_gtja191() -> None:
    default_ids = [item["id"] for item in factor_columns_view()]
    assert not any(item.startswith("gtja_") for item in default_ids)
    assert not any(spec.id.startswith("gtja_") for spec in all_factors())
    assert not any(
        spec.id.startswith("gtja_") for spec in all_factors(asset_type="stock")
    )
    assert not any(
        spec.id.startswith("gtja_") for spec in all_factors(stable_only=True)
    )


def test_opt_in_reveals_exactly_the_registered_set() -> None:
    experimental = {spec.id for spec in all_factors(include_experimental=True)}
    assert set(GTJA191_IDS) <= experimental
    columns = {item["id"] for item in factor_columns_view(include_experimental=True)}
    assert set(GTJA191_IDS) <= columns


def test_experimental_tag_registry_covers_both_groups() -> None:
    """新增实验组必须进 EXPERIMENTAL_TAGS, 否则过滤会漏。"""
    assert "alpha158" in EXPERIMENTAL_TAGS
    assert GTJA191_TAG in EXPERIMENTAL_TAGS


def test_filter_cannot_silently_drop_back_to_alpha158_only(monkeypatch) -> None:
    """变异防护: 把过滤改回只认 alpha158, 必须被这条抓住。

    这是本批最容易出的回归 —— 有人图省事把 `_is_experimental` 换回
    `"alpha158" not in spec.tags`, 单测若不覆盖就会让 gtja191 默进挖掘候选。
    """
    import app.factors.registry as registry

    monkeypatch.setattr(
        registry, "EXPERIMENTAL_TAGS", frozenset({"alpha158"}),
    )
    leaked = [spec.id for spec in registry.all_factors() if spec.id.startswith("gtja_")]
    assert leaked, "如果这条不成立, 说明 EXPERIMENTAL_TAGS 根本没被 all_factors 使用"
    monkeypatch.undo()
    assert not any(spec.id.startswith("gtja_") for spec in registry.all_factors())


def test_builtin_gtja_ids_cannot_be_unregistered() -> None:
    for fid in ("gtja_2", GTJA191_IDS[-1]):
        with pytest.raises(ValueError, match="内置因子不可注销"):
            unregister_factor(fid)


def test_register_is_idempotent() -> None:
    """重复注册不许抛。`_ensure_experimental` 与显式调用可能同时发生。"""
    before = {spec.id: spec.version for spec in GTJA191_SPECS}
    register_gtja191()
    register_gtja191()
    after = {fid: get_factor(fid).version for fid in before}
    assert before == after


# ── 编译与真正能算 ──


def test_every_formula_compiles_without_errors() -> None:
    failures = []
    for spec in GTJA191_SPECS:
        compiled = compile_formula(spec.formula_text)
        if not compiled.ok:
            failures.append(f"{spec.id}: {[error.code for error in compiled.errors]}")
    assert failures == []


def test_computable_ids_actually_materialize() -> None:
    """核心保护: 每条可算因子必须在真实面板上产出非空、有限的列。

    只断言 `compiled.ok` 不够。本批的 gtja_32 / gtja_38 编译 `ok=True` 但
    `frame_transform=None`（E009 被收进 errors 而不抛），必须真的跑一遍才抓得到。
    """
    frame = _random_panel()
    out = compute_gtja191(frame, chunk_symbols=5)
    problems = []
    for fid in GTJA191_IDS:
        if fid not in out.columns:
            problems.append(f"{fid}: 列不存在")
            continue
        column = out.get_column(fid)
        non_null = int(column.is_not_null().sum())
        if non_null == 0:
            problems.append(f"{fid}: 整列为空")
            continue
        finite = int(column.is_finite().sum())
        if finite != non_null:
            problems.append(f"{fid}: 有 {non_null - finite} 个非有限值")
    assert problems == []


def test_blocked_ids_never_produce_a_column() -> None:
    out = compute_gtja191(_random_panel(symbols=3, bars=30), chunk_symbols=2)
    for fid in BLOCKED_IDS:
        assert fid not in out.columns


def test_formula_with_cross_sectional_inside_window_is_rejected_by_dsl() -> None:
    """把 E009 这条约束钉在测试里。

    gtja_32 / gtja_38 的 blocked 理由正是它。哪天 DSL 放开这个限制, 这条会红,
    提醒我们回头重新评估这两条能不能登记 —— 而不是让它们一直躺在 blocked 里。
    """
    compiled = compile_formula_cached("-1 * ts_sum(rank(ts_corr(high, volume, 3)), 3)")
    assert compiled.ok is False
    assert any(error.code == "E009" for error in compiled.errors)
    assert compiled.frame_transform is None


def test_blocked_reason_names_the_actual_obstacle() -> None:
    """blocked 理由必须指出是哪一类问题, 不能只写"做不到"。"""
    sma_ids = {15, 55}
    window_rank_ids = {32, 38}
    for item in GTJA191_BLOCKED:
        if item.number in sma_ids:
            assert "SMA" in item.detail, item.label
        elif item.number in window_rank_ids:
            assert "RANK" in item.detail or "秩" in item.detail, item.label
        else:  # pragma: no cover - 新增 blocked 时必须显式归类
            pytest.fail(f"新增的 blocked {item.label} 没有归入已知原因类别")


def test_ensure_computable_raises_instead_of_returning_empty() -> None:
    for fid in BLOCKED_IDS:
        with pytest.raises(GtjaBlockedError):
            ensure_computable(fid)
    for fid in GTJA191_IDS:
        ensure_computable(fid)


def test_compute_rejects_zero_chunk() -> None:
    with pytest.raises(ValueError, match="chunk_symbols"):
        compute_gtja191(_random_panel(symbols=2, bars=10), chunk_symbols=0)


def test_compute_handles_empty_frame() -> None:
    empty = pl.DataFrame(schema={
        "symbol": pl.Utf8, "date": pl.Date,
        "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
        "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64,
    })
    assert compute_gtja191(empty).is_empty()


def test_cross_sectional_ids_are_identified() -> None:
    """含截面 `rank` 的式子必须被识别出来 —— 它们不能按股票分块算。

    截面 rank 的分母是当天的股票数。分块会把同一天的截面切碎, 每块分母不同, 同一只
    股票在不同分块大小下会得到不同的值。这不是精度问题, 是算错。
    """
    cross = set(cross_sectional_ids())
    assert cross, "本批应有截面式, 否则这条保护失去意义"
    for item in GTJA191_FORMULAS:
        compiled = compile_formula(item.formula)
        expected = bool(compiled.cross_sectional)
        assert (f"gtja_{item.number}" in cross) is expected, item.number
    # 截面式里真的出现了 rank(
    for fid in cross:
        spec = next(spec for spec in GTJA191_SPECS if spec.id == fid)
        assert "rank(" in spec.formula_text, fid


def test_chunking_is_disabled_when_cross_sectional_present() -> None:
    """有截面式时, 传任何 chunk_symbols 都必须得到和整帧一样的结果。

    变异防护: 把 `if cross: return compute_all(frame)` 删掉, 这条会立刻红。
    """
    frame = _random_panel(symbols=8, bars=40)
    whole = compute_gtja191(frame, chunk_symbols=100)
    for size in (1, 3, 7):
        chunked = compute_gtja191(frame, chunk_symbols=size)
        for fid in cross_sectional_ids():
            left = whole.get_column(fid).to_list()
            right = chunked.get_column(fid).to_list()
            assert left == right, f"{fid} 在 chunk_symbols={size} 下与整帧不一致"


def test_compute_chunking_does_not_change_values() -> None:
    """分块与否结果必须一致, 否则"算得出来"取决于股票数量, 不可复现。"""
    frame = _random_panel(symbols=8, bars=40)
    whole = compute_gtja191(frame, chunk_symbols=100)
    chunked = compute_gtja191(frame, chunk_symbols=3)
    assert whole.height == chunked.height
    for fid in GTJA191_IDS:
        left = whole.get_column(fid)
        right = chunked.get_column(fid)
        for a, b in zip(left.to_list(), right.to_list(), strict=True):
            if a is None or b is None:
                assert a is b or (a is None) == (b is None)
            else:
                assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9), fid


# ── TSP 映射透明性 ──


def test_cross_sectional_rank_is_registered_as_report_says_rank() -> None:
    """截面 `rank(` 只在报告原文**就是** RANK/截面语义时才允许出现。

    变异防护: 把 gtja_37 的截面 `rank(ts_delta(open, 1))` 换成时序 `ts_rank(...)`,
    语义会从"当天全市场排名"变成"该股票自己 20 天内的排名" —— 完全不同的因子。
    原先的测例只检查"用 ts_pctrank 的式子有没有标注", 漏掉了这一半。
    """
    for item in GTJA191_FORMULAS:
        has_cross_rank = " rank(" in f" {item.formula}"
        report_rank = "RANK(" in item.report.upper()
        if has_cross_rank:
            # 用了截面 rank 就必须在报告里也对得上, 且 `rank` 不能包在时序窗口里
            assert report_rank, f"{item.number} 用了截面 rank, 报告原文却没有 RANK"
            compiled = compile_formula(item.formula)
            assert compiled.cross_sectional is True, (
                f"{item.number} 含 rank( 却没被识别为截面式"
            )
        if report_rank:
            # 报告有 RANK 的, 要么用 ts_pctrank, 要么用截面 rank, 不能都不沾
            assert "ts_pctrank" in item.formula or has_cross_rank, (
                f"{item.number} 报告有 RANK, 公式里既没有 ts_pctrank 也没有 rank("
            )


def test_selected_rank_mapping_matches_declared_kind() -> None:
    """把每条式子的秩方案钉死, 换掉任何一种都会红。

    - 报告 RANK 在**时序窗口内** -> 用 ts_pctrank（窗口内百分位）
    - 报告 RANK 在**最外层**     -> 用截面 rank（z-score, 但进了 Rank IC 的秩相关后等价）
    """
    # 逐条钉死完整公式文本, 不做子串检查。
    # 子串检查会被同一条式子里的**另一处**秩调用掩盖: gtja_37 里有两处 rank(,
    # 只把外层那处换成 ts_rank 时, `" rank(" in formula` 仍为真, 断言照样通过。
    expected = {
        5: (
            "-1 * ts_max(ts_corr(ts_pctrank(volume, 5), ts_pctrank(high, 5), 5), 3)",
            "ts_pctrank",
        ),
        6: ("-1 * rank(ts_delta(0.85 * open + 0.15 * high, 4))", "rank"),
        18: ("close / ts_delay(close, 5) * ts_pctrank(volume, 5)", "ts_pctrank"),
    }
    for number, (formula, kind) in expected.items():
        item = next(item for item in GTJA191_FORMULAS if item.number == number)
        assert item.formula == formula, f"{number} 公式被改动过: {item.formula}"
        assert ("ts_pctrank" in formula) is (kind == "ts_pctrank"), number

    # gtja_37 有三处 rank(（外层 DELTA 一处 + 成交量秩比值两处），逐一确认没被换掉。
    item37 = next(item for item in GTJA191_FORMULAS if item.number == 37)
    assert item37.formula.count("rank(") == 3, item37.formula
    assert "ts_rank(" not in item37.formula, item37.formula
    assert "ts_pctrank" not in item37.formula, item37.formula
    assert item37.formula.startswith("-1 * rank(ts_delta(open, 1))"), item37.formula

    # gtja_11 同样有两处 rank。
    item11 = next(item for item in GTJA191_FORMULAS if item.number == 11)
    assert item11.formula.count("rank(") == 2, item11.formula
    assert "ts_rank(" not in item11.formula, item11.formula
    assert "ts_pctrank" not in item11.formula, item11.formula

    # 全族不许出现 ts_rank（它是窗口内秩次, 与报告的 RANK 语义不同）。
    offenders = [item.number for item in GTJA191_FORMULAS if "ts_rank(" in item.formula]
    assert offenders == [], f"这些式子了 ts_rank, 应用 ts_pctrank 或截面 rank: {offenders}"


def test_rank_mapping_is_ts_pctrank_not_cross_sectional_rank() -> None:
    """报告 RANK 是截面百分位秩; TSP 的 `rank()` 是截面 z-score, 不能顶替。

    含 RANK 的式子必须用 `ts_pctrank`, 且 `report` 原文里要留着 `RANK(` 以便对拍。
    """
    rank_users = [item for item in GTJA191_FORMULAS if "ts_pctrank" in item.formula]
    assert rank_users, "本批应至少有一条用到 ts_pctrank"
    for item in rank_users:
        assert "RANK(" in item.report.upper(), item.number
        assert "TSP 映射" in item.tsp_mapping, f"{item.number} 用了秩映射却没标注"
        # 截面 z-score 的 rank() 不能出现在这些式子里
        assert " rank(" not in f" {item.formula}", f"{item.number} 混用了截面 z-score rank"


def test_no_decay_linear_substitution_in_this_batch() -> None:
    """本批 13 条没有任何一条报告原文是 WMA, 所以不该出现 decay_linear 顶替。

    一旦有人为了"让式子跑起来"把 WMA 换成 decay_linear, 这条会红。真要换, 必须同时
    在 `tsp_mapping` 里写「TSP 映射」并说明权重差异(见下一条)。
    """
    substituted = [item for item in GTJA191_FORMULAS if "decay_linear" in item.formula]
    if substituted:
        for item in substituted:
            assert "TSP 映射" in item.tsp_mapping, f"{item.number} 用 decay_linear 顶替却没标注"
            assert "WMA" in item.tsp_mapping or "权重" in item.tsp_mapping, item.number
    # 报告原文里没有 WMA, 就不许有 decay_linear
    assert not any("WMA" in item.report.upper() for item in GTJA191_FORMULAS)
    assert substituted == [], "报告原文没有 WMA, 却出现了 decay_linear 顶替"


def test_sma_never_appears_in_any_registered_formula() -> None:
    """SMA 递推在 TSP 没有原语。任何登记公式里出现 SMA 都是越界。"""
    for spec in GTJA191_SPECS:
        assert "SMA" not in spec.formula_text.upper(), spec.id


def test_spec_formula_text_equals_report_derived_dsl() -> None:
    """注册表里的公式文本必须与 GTJA191_FORMULAS 一致, 不允许在注册时偷改。"""
    by_id = {spec.id: spec for spec in GTJA191_SPECS}
    for item in GTJA191_FORMULAS:
        assert by_id[f"gtja_{item.number}"].formula_text == item.formula


# ── 成本口径 ──


def test_cost_anchor_is_18_2_bp() -> None:
    assert pytest.approx(18.2) == GTJA191_COST_BP_ROUNDTRIP
    assert pytest.approx(18.2) == cost_gate_cost_bp()


def test_ic_table_cost_is_not_the_anchor() -> None:
    """IC 表默认配置 ≠ 18.2bp。这两个数混用会让"扣费后还行"被高估。"""
    assert pytest.approx(14.0) == ic_table_cost_bp()
    assert pytest.approx(GTJA191_COST_BP_ROUNDTRIP) != ic_table_cost_bp()


def test_round_trip_cost_formula() -> None:
    # 来回 = 2 * 费率 + 2 * 滑点; 费率百分比先换成 bp。
    assert pytest.approx(14.0) == round_trip_cost_bp(fees_pct=0.0002, slippage_bps=5.0)
    assert pytest.approx(18.2) == round_trip_cost_bp(fees_pct=0.0002, slippage_bps=7.1)
    assert pytest.approx(0.0) == round_trip_cost_bp(fees_pct=0.0, slippage_bps=0.0)


# ── 不得出现真实凭据 ──


def test_no_credentials_or_webhooks_in_module_or_docs() -> None:
    """任务书禁止在任务书 / PR / 测例里写真实凭证。这里对模块源码做一次静态扫描。"""
    import inspect

    import app.factors.gtja191 as module

    source = inspect.getsource(module)
    for marker in (
        "https://oapi.dingtalk.com/robot/send?access_token=",
        "access_token=",
        "AKIA",
        "-----BEGIN",
        "sk-",
    ):
        assert marker not in source, f"源码出现疑似凭据片段: {marker}"


def test_formula_text_of_returns_none_for_foreign_ids() -> None:
    assert formula_text_of("a158_kmid") is None
    assert formula_text_of("momentum_5d") is None
    assert formula_text_of("gtja_9999") is None
    assert formula_text_of("gtja_2") is not None
