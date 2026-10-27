"""gtja191 N2 标签映射：照抄 FIX 写死表、优先级、半窗反号、成本口径、下线候选只提示。

这个文件守的是**判定逻辑本身**，不是公式能不能算（那在 `test_factors_gtja191.py`）。
判定门槛来自 `FIX_TASKS_20261027ggtja191.md`（sha256 `7b5cac07…`）N2 节的写死表，
不是"看起来合理"的另一张表。三类失效方式各有一组测例：

1. **样本不够判 alive** —— V8：30 个门全过但只有 29 个 IC 日。判 alive 就是把噪声
   当信号，这是这套标签最危险的失效方式。
2. **时间不稳定被当成没用，或者被别的东西冒充** —— V11：前半窗 IC 均值 -0.05、后半窗
   +0.05。这是"因子在时间上反向"，处置是查 regime 或放弃，判 `dead` 会丢掉信息。
   **反例守卫**：IC 为正而费后多空为负**不是** reversed（那是换手问题，不是方向问题）。
3. **显著性门用错口径** —— `alive` 必须读 `|ir|` / `|t_newey_west|` / `q_value` 三者。
   用 `|ic_mean|` 或费后多空冒充显著性门，会让"高 IC 高换手"或"低 IC 但稳定"的因子
   得到错误处置，而且**看不出错**。

另有三条硬约束：
- 本模块**不许出现 `unregister_factor`**。标签是判断，下线是人的决定。
- 评估脚本必须用 `len(result.ic_series)` 当 `n_ic_dates`，不能用 `result.n_dates`
  （那是面板交易日数，因子截面恒定时仍是 287 而 IC 日为 0）。
- 不许 skip / xfail 装绿。

`degenerate` 是 FIX 7 个标签之外的扩展（截面恒定 → IC 全无定义），本文件单独钉住它
的**唯一触发条件**，防止它被拿来当万能兜底。
"""
from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

from app.backtest.factor import FactorBatchItem
from app.factors.gtja191 import GTJA191_COST_BP_ROUNDTRIP, cost_gate_cost_bp, ic_table_cost_bp
from app.factors.gtja191_labels import (
    DELIST_CANDIDATE_LABELS,
    MAX_Q_VALUE,
    MIN_ABS_IC,
    MIN_ABS_IR,
    MIN_ABS_NW_T,
    MIN_IC_DATES,
    MIN_NET_LONG_SHORT,
    HalfWindowFlip,
    half_window_of,
    label_of,
)

COST = GTJA191_COST_BP_ROUNDTRIP

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
LABELS_MODULE = BACKEND / "app" / "factors" / "gtja191_labels.py"
EVAL_SCRIPT = REPO_ROOT / "scripts" / "eval_gtja191.py"


def _item(
    ic: float | None = 0.05,
    *,
    ir: float | None = 0.60,
    nw_t: float | None = 2.50,
    q_value: float | None = 0.05,
    error: str | None = None,
) -> FactorBatchItem:
    """默认值刻意选成**三门全过**，这样每个测例只需改一处就能精确推到某个分支。

    如果默认值选成"三门都不过"，那么测 `alive` 时要同时改三个参数，一旦测例失败
    就分不清是哪个门写错了。
    """
    return FactorBatchItem(
        factor_name="gtja_2",
        label="影线相对位置-一阶差分",
        group="GTJA191（实验）",
        ic_mean=ic,
        ir=ir,
        t_newey_west=nw_t,
        q_value=q_value,
        error=error,
    )


def _ic_series(
    values: list[float], *, start: date = date(2025, 1, 1),
) -> list[dict]:
    """按日递增造 `ic_series`。形态与 `FactorResult.ic_series` 一致。"""
    return [
        {"date": (start + timedelta(days=index)).isoformat(), "ic": value}
        for index, value in enumerate(values)
    ]


def _flat_series(ic: float, days: int) -> list[dict]:
    """全窗同一个 IC，保证半窗均值相等 → 不可能构成反号。"""
    return _ic_series([ic] * days)


def _eval(verdict_kwargs: dict | None = None, **overrides):
    """统一入口：默认 30 个 IC 日、费后拿到 0.0、cost_bp 正确。"""
    kwargs = {
        "n_ic_dates": MIN_IC_DATES,
        "net_long_short_return": 0.0,
        "cost_bp": COST,
    }
    if verdict_kwargs:
        kwargs.update(verdict_kwargs)
    kwargs.update(overrides)
    return kwargs


# ════════════════════════════════════════════════════ 阈值常量：与 FIX 逐数字对齐


def test_thresholds_match_the_fix_table() -> None:
    """FIX N2 表里的 5 个数字，一个都不许动。改门槛必须先改 FIX，不能顺手放宽。"""
    assert MIN_IC_DATES == 30
    assert MIN_ABS_IR == 0.50
    assert MIN_ABS_NW_T == 1.96
    assert MAX_Q_VALUE == 0.10
    assert MIN_ABS_IC == 0.02
    assert MIN_NET_LONG_SHORT == 0.0


def test_cost_gate_uses_the_n5_anchor_not_the_ic_table_default() -> None:
    """成本门用 18.2bp，IC 表默认 14bp。两者不相等，必须同时钉住。"""
    assert cost_gate_cost_bp() == COST == 18.2
    assert ic_table_cost_bp() == 14.0
    assert cost_gate_cost_bp() != ic_table_cost_bp()


def test_delist_candidate_labels_are_exactly_dead_and_reversed() -> None:
    """FIX 只说 `dead` / `reversed` 进候选。`underpowered` 是缺证据，不是该下线。"""
    assert frozenset({"dead", "reversed"}) == DELIST_CANDIDATE_LABELS


# ════════════════════════════════════════════════════ V8：样本太薄不得判 alive


def test_v8_29_ic_dates_is_underpowered_not_alive() -> None:
    """V8：29 个 IC 日，但 IR / NW-t / q **三门全过**。

    IC 0.15 在 358 天里是顶尖因子，在 29 天里可能只是运气。判 alive 就等于拿 29 天的
    噪声去支持"这个因子能用"。**必须 underpowered**，且不得因费率低而例外。
    """
    verdict = label_of(
        _item(0.15, ir=0.90, nw_t=3.20, q_value=0.01, error=None),
        **_eval(n_ic_dates=29, net_long_short_return=0.50),
    )
    assert verdict.label == "underpowered"
    assert verdict.label != "alive"
    assert not verdict.is_delist_candidate, "样本不够不是下线理由"
    assert "29" in verdict.reason


def test_v8_boundary_30_days_becomes_alive() -> None:
    """同一条输入，只差 1 天：30 日即可判 alive。守边界，防止 `<` 写成 `<=`。"""
    verdict = label_of(_item(0.15), **_eval(n_ic_dates=MIN_IC_DATES))
    assert verdict.label == "alive"


def test_underpowered_beats_dead_and_reversed() -> None:
    """样本不够时，既不判 dead 也不判 reversed —— 噪声的方向和幅度都没有意义。"""
    series = _ic_series([-0.10] * 3 + [0.10] * 2)  # 反号，但只有 5 天
    for net in (-0.9, 0.0, 0.9):
        verdict = label_of(
            _item(0.40, ir=0.10, nw_t=0.30, q_value=0.90),
            **_eval(n_ic_dates=5, net_long_short_return=net, ic_series=series),
        )
        assert verdict.label == "underpowered", f"net={net}"
        assert not verdict.is_delist_candidate


# ════════════════════════════════════════════════════ V11：半窗反号才是 reversed


def test_v11_half_window_sign_flip_is_reversed_not_dead() -> None:
    """V11：前半窗 IC 均值 -0.05，后半窗 +0.05，两侧幅度都够 0.02。

    因子"前半段反向有效、后半段正向有效" —— 它在时间上不稳定，按当前方向部署会在
    某个 regime 里持续亏钱。处置是查 regime 变化或放弃，不是当成废物扔掉。
    """
    series = _ic_series([-0.05] * 20 + [0.05] * 20)
    verdict = label_of(_item(0.02), **_eval(ic_series=series))
    assert verdict.label == "reversed"
    assert verdict.label != "dead"
    assert verdict.half_window is not None
    assert verdict.half_window.first_mean < 0 < verdict.half_window.second_mean
    assert "符号相反" in verdict.reason


def test_v11_is_not_triggered_by_ic_vs_long_short_sign() -> None:
    """**反例守卫**：IC 为正、费后多空为负 —— 这**不是** reversed。

    这是"因子能排序但扣费后不赚钱"，通常是换手太高，属于成本问题（N5 下线候选），
    不是方向问题。拿它冒充 reversed 会把"高换手"误诊成"方向错"，
    处置手段（改方向 vs 降换手）完全不同。
    """
    series = _flat_series(0.06, 60)  # 半窗同号，不可能 reversed
    verdict = label_of(
        _item(0.06), **_eval(net_long_short_return=-0.35, ic_series=series),
    )
    assert verdict.label == "alive", "显著性三门过了就是 alive，与费后无关"
    assert verdict.is_delist_candidate is False


def test_reversed_needs_both_sides_above_the_floor() -> None:
    """一侧只有 0.001：符号确实反了，但幅度不够 0.02 —— 噪声的符号没有意义。"""
    series = _ic_series([-0.001] * 20 + [0.05] * 20)
    verdict = label_of(_item(0.03), **_eval(ic_series=series))
    assert verdict.label == "alive"
    assert verdict.label != "reversed"


def test_reversed_does_not_require_the_significance_gates() -> None:
    """FIX 表第 5 行只看天数与半窗幅度，不叠 IR/NW/q。

    一个 IR 很低的因子也可能半窗反号 —— 那更说明它在飘。优先级上 reversed 在
    alive/dead 之前，所以先命中 reversed。
    """
    series = _ic_series([-0.06] * 20 + [0.06] * 20)
    verdict = label_of(
        _item(0.00, ir=0.01, nw_t=0.05, q_value=0.99),
        **_eval(ic_series=series),
    )
    assert verdict.label == "reversed"


def test_half_window_of_splits_by_date_and_reports_counts() -> None:
    """偶数日平均分，奇数日前半少 1 个 —— `n // 2`。"""
    half = half_window_of(_ic_series([0.04] * 4))
    assert half is not None
    assert (half.first_days, half.second_days) == (2, 2)
    assert half.first_mean == 0.04 and half.second_mean == 0.04

    half = half_window_of(_ic_series([0.04] * 5))
    assert half is not None
    assert (half.first_days, half.second_days) == (2, 3)


def test_half_window_is_order_independent() -> None:
    """按日期排序后再切，不依赖入参顺序 —— 否则上游换个排序就换个标签。"""
    forward = _ic_series([-0.05] * 10 + [0.05] * 10)
    backward = list(reversed(forward))
    half_a = half_window_of(forward)
    half_b = half_window_of(backward)
    assert half_a is not None and half_b is not None
    assert (half_a.first_mean, half_a.second_mean) == (
        half_b.first_mean, half_b.second_mean,
    )


def test_half_window_needs_enough_points() -> None:
    """少于 4 个点不切：每半不到 2 个点，均值没有意义。"""
    assert half_window_of(None) is None
    assert half_window_of([]) is None
    assert half_window_of(_ic_series([0.05] * 3)) is None


def test_half_window_ignores_null_ic_rows() -> None:
    rows = _ic_series([-0.05] * 10 + [0.05] * 10)
    rows[3]["ic"] = None
    half = half_window_of(rows)
    assert half is not None
    assert half.first_days + half.second_days == 19


def test_half_window_flip_treats_zero_as_no_sign() -> None:
    """一侧恰好 0.0：符号无意义，不算翻转。"""
    half = HalfWindowFlip(
        first_mean=0.0, second_mean=0.05, first_days=10, second_days=10,
    )
    assert half.flipped is False
    assert half.significant_flip() is False


# ════════════════════════════════════════════════════ alive：IR / NW-t / q 三门


def test_alive_requires_all_three_gates() -> None:
    verdict = label_of(_item(0.05, ir=0.50, nw_t=1.96, q_value=0.10), **_eval())
    assert verdict.label == "alive"
    assert "IR" in verdict.reason and "NW-t" in verdict.reason and "q" in verdict.reason


def test_dead_when_ir_weak() -> None:
    verdict = label_of(_item(0.05, ir=0.49, nw_t=2.50, q_value=0.05), **_eval())
    assert verdict.label == "dead"
    assert "IR" in verdict.reason


def test_dead_when_newey_west_not_significant() -> None:
    verdict = label_of(_item(0.05, ir=0.60, nw_t=1.95, q_value=0.05), **_eval())
    assert verdict.label == "dead"
    assert "NW-t" in verdict.reason


def test_dead_when_q_too_large() -> None:
    """BH-FDR 没过：多重检验下这个 p 值不稀奇。"""
    verdict = label_of(_item(0.05, ir=0.60, nw_t=2.50, q_value=0.11), **_eval())
    assert verdict.label == "dead"
    assert "q" in verdict.reason


def test_dead_lists_every_failing_gate() -> None:
    """三个门都不过时 reason 要把三个都写出来，只报一个会让人以为只差一项。"""
    verdict = label_of(_item(0.05, ir=0.10, nw_t=0.50, q_value=0.80), **_eval())
    assert verdict.label == "dead"
    for token in ("IR", "NW-t", "q"):
        assert token in verdict.reason


def test_alive_does_not_look_at_ic_magnitude() -> None:
    """`|ic_mean|` 是效应量不是显著性：IC 0.005 只要 IR/NW/q 过硬就可以 alive。

    反过来 IC 0.30 但 IR 不够也必须 dead —— 见 `test_dead_when_ir_weak` 的变体。
    """
    tiny_but_stable = label_of(_item(0.005, ir=0.80, nw_t=3.00, q_value=0.02), **_eval())
    assert tiny_but_stable.label == "alive"

    big_but_noisy = label_of(_item(0.30, ir=0.20, nw_t=0.90, q_value=0.40), **_eval())
    assert big_but_noisy.label == "dead"


def test_alive_does_not_look_at_net_long_short() -> None:
    """费后多空（N5）**不进** alive 定义。扣费亏钱 ≠ 没有排序能力。"""
    for net in (None, -1.0, -0.01, 0.0, 5.0):
        verdict = label_of(_item(0.05), **_eval(net_long_short_return=net))
        assert verdict.label == "alive", f"net={net}"


def test_negative_ir_and_nw_use_absolute_value() -> None:
    """负向因子同样是因子：IR/NW-t 取绝对值，不因为方向为负就判死。"""
    verdict = label_of(_item(-0.05, ir=-0.60, nw_t=-2.50, q_value=0.05), **_eval())
    assert verdict.label == "alive"


def test_missing_significance_fields_are_underpowered() -> None:
    """FIX 表第 4 行后半句：显著性字段为 None → 判不了，归 underpowered，不许猜。"""
    cases = (
        {"ir": None},
        {"nw_t": None},
        {"q_value": None},
        {"ir": None, "nw_t": None, "q_value": None},
    )
    for override in cases:
        verdict = label_of(_item(0.05, **override), **_eval())
        assert verdict.label == "underpowered", override
        assert not verdict.is_delist_candidate


def test_blocked_beats_everything() -> None:
    verdict = label_of(
        _item(0.05, error="boom"), **_eval(blocked_reason="ops"),
    )
    assert verdict.label == "blocked:ops"
    assert verdict.cost_bp is None, "没算就谈不上成本口径"


def test_error_beats_significance() -> None:
    verdict = label_of(_item(0.05, error="物化失败"), **_eval())
    assert verdict.label == "error"


def test_wrong_cost_bp_raises() -> None:
    """传 14bp（`long_short_return` 实际扣的口径）必须抛错，不能静默偏乐观。"""
    for bad in (14.0, 18.0, 18.3, 0.0):
        try:
            label_of(_item(0.05), n_ic_dates=MIN_IC_DATES,
                     net_long_short_return=0.0, cost_bp=bad)
        except ValueError as exc:
            assert "成本口径" in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"cost_bp={bad} 应当抛 ValueError")


# ════════════════════════════════════════════════════ degenerate：FIX 枚举外的窄扩展


def test_degenerate_only_when_no_ic_at_all() -> None:
    """截面恒定 → 全窗 IC 无定义 → `ic_mean is None` 且 `n_ic_dates == 0`。"""
    verdict = label_of(
        _item(None, ir=None, nw_t=None, q_value=None),
        **_eval(n_ic_dates=0, net_long_short_return=None),
    )
    assert verdict.label == "degenerate"


def test_degenerate_does_not_shadow_underpowered() -> None:
    """有 IC 只是天数不够 → 仍是 underpowered。degenerate 只切走"一条 IC 都没有"。"""
    verdict = label_of(_item(0.20), **_eval(n_ic_dates=7))
    assert verdict.label == "underpowered"
    assert verdict.label != "degenerate"


def test_degenerate_needs_both_null_ic_and_zero_days() -> None:
    """`ic_mean is None` **但** `n_ic_dates > 0` 是自相矛盾的结果，归 `error` 级问题。

    这不是"测量失败"，是上游把数算坏了。放宽成只看 `ic_mean is None` 会让这类矛盾
    结果被一句"截面恒定"掩盖过去，所以必须单独钉住。
    """
    verdict = label_of(
        _item(None, ir=None, nw_t=None, q_value=None),
        **_eval(n_ic_dates=40, net_long_short_return=None),
    )
    assert verdict.label != "degenerate", "有 40 个 IC 日却说 ic_mean 是 None，不能算测量失败"
    assert verdict.label == "underpowered"


# ════════════════════════════════════════════════════ 下线候选：标签 ∧ 费后


def test_dead_is_candidate_only_when_net_fails_the_gate() -> None:
    """三条 AND：标签 ∈ {dead, reversed} ∧ 费后没过门。费后过门的 dead 不是候选。"""
    dead_net_negative = label_of(
        _item(0.05, ir=0.10, nw_t=0.50, q_value=0.80),
        **_eval(net_long_short_return=-0.20),
    )
    assert dead_net_negative.label == "dead"
    assert dead_net_negative.is_delist_candidate

    dead_net_positive = label_of(
        _item(0.05, ir=0.10, nw_t=0.50, q_value=0.80),
        **_eval(net_long_short_return=0.40),
    )
    assert dead_net_positive.label == "dead"
    assert not dead_net_positive.is_delist_candidate, "费后赚钱的 dead 先留观，不退役"


def test_reversed_is_a_candidate_when_net_fails() -> None:
    series = _ic_series([-0.05] * 20 + [0.05] * 20)
    verdict = label_of(
        _item(0.02), **_eval(net_long_short_return=-0.10, ic_series=series),
    )
    assert verdict.label == "reversed"
    assert verdict.is_delist_candidate


def test_missing_net_cannot_prove_the_gate_passed() -> None:
    """拿不到费后值 = 不能证明过门 → 仍是候选。缺证据时按"该查"处理，不按"没事"处理。"""
    verdict = label_of(
        _item(0.05, ir=0.10, nw_t=0.50, q_value=0.80),
        **_eval(net_long_short_return=None),
    )
    assert verdict.label == "dead"
    assert verdict.is_delist_candidate


def test_alive_and_underpowered_are_never_candidates() -> None:
    assert not label_of(_item(0.05), **_eval(net_long_short_return=-1.0)).is_delist_candidate
    assert not label_of(_item(0.05), **_eval(n_ic_dates=3)).is_delist_candidate


# ════════════════════════════════════════════════════ 源码级钉子（AST / 文本）


def _module_ast() -> ast.Module:
    return ast.parse(LABELS_MODULE.read_text(encoding="utf-8"))


def test_mapping_reads_the_significance_fields() -> None:
    """H13 回归：`label_of` 必须真的读 `item.ir` / `item.t_newey_west` / `item.q_value`。

    抓"文档写了三门、代码只用 |ic_mean| 过门"这种同名不同物的洗绿。
    """
    tree = _module_ast()
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "label_of"
    )
    read_fields = {
        node.attr
        for node in ast.walk(func)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "item"
    }
    for field in ("ir", "t_newey_west", "q_value", "ic_mean", "error"):
        assert field in read_fields, f"label_of 没读 item.{field}"


def _called_names(tree: ast.Module) -> set[str]:
    """收集所有被调用函数的名字，兼容 `f(...)` 与 `mod.f(...)`。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _assert_no_unregister(
    tree: ast.Module, module_name: str, *, forbid_registry_import: bool,
) -> None:
    """不靠文本匹配（docstring 里会提到这个词），只看**是否真的调用/导入**。

    评估脚本**合法地**导入 `app.factors.registry`（要 `get_factor` 读因子定义），
    所以只在标签模块上禁止导入 registry；两边都禁止调用注销函数。
    """
    assert "unregister_factor" not in _called_names(tree), f"{module_name} 调用了注销"
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert "unregister_factor" not in imported, f"{module_name} 导入了注销函数"
    if forbid_registry_import:
        assert not any(
            "registry" in name for name in imported
        ), f"{module_name} 不该碰注册表: {imported}"


def test_mapping_never_unregisters() -> None:
    """标签是判断，下线是人的决定。本模块不许出现注销调用。"""
    _assert_no_unregister(
        _module_ast(), "gtja191_labels", forbid_registry_import=True,
    )


def _eval_ast() -> ast.Module:
    return ast.parse(EVAL_SCRIPT.read_text(encoding="utf-8"))


def test_eval_script_uses_len_ic_series_not_n_dates() -> None:
    """`n_dates` 是面板交易日数，`len(ic_series)` 才是有效 IC 日数。用错会把
    截面恒定（IC 全 NaN）的因子当成 287 天样本而过门。"""
    tree = _eval_ast()
    assigned = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "n_ic_dates"
    ]
    assert assigned, "脚本里找不到 n_ic_dates=... 传参"
    for value in assigned:
        assert isinstance(value, ast.Call), ast.dump(value)[:80]
        assert isinstance(value.func, ast.Name) and value.func.id == "len"
        assert isinstance(value.args[0], ast.Attribute)
        assert value.args[0].attr == "ic_series"

    source = EVAL_SCRIPT.read_text(encoding="utf-8")
    assert "n_ic_dates=result.n_dates" not in source
    assert "n_ic_dates=len(result.ic_series)" in source


def test_eval_script_passes_ic_series_to_the_mapping() -> None:
    """半窗反号需要原始 IC 序列，脚本必须把它传进去，否则 reversed 永不命中。"""
    source = EVAL_SCRIPT.read_text(encoding="utf-8")
    assert "ic_series=result.ic_series" in source


def test_eval_script_computes_bh_fdr_per_batch() -> None:
    """q 值是批内统计量（m = 本批可检验因子数），必须整批 p 值齐了再算。"""
    called = _called_names(_eval_ast())
    for name in ("bh_fdr_qvalues", "newey_west_t"):
        assert name in called, f"脚本没调用 {name}"

    # 判定必须在 q 值算完之后：`label_of(` 的调用行号要大于 `bh_fdr_qvalues(` 的。
    source_lines = EVAL_SCRIPT.read_text(encoding="utf-8").splitlines()
    q_line = next(
        index for index, line in enumerate(source_lines)
        if "bh_fdr_qvalues(" in line and not line.lstrip().startswith(("#", "from", "import"))
    )
    label_line = next(
        index for index, line in enumerate(source_lines) if "verdict = label_of(" in line
    )
    assert label_line > q_line, "label_of 在 q 值算出来之前就被调用了"


def test_eval_script_cost_gate_uses_the_anchor_constants() -> None:
    """费后重扣必须显式用 18.2bp 的锚，不能吃 `FactorConfig` 默认的 14bp。"""
    source = EVAL_SCRIPT.read_text(encoding="utf-8")
    assert "fees_pct=" in source and "slippage_bps=" in source
    assert GTJA191_COST_BP_ROUNDTRIP == 18.2


def test_eval_script_never_unregisters() -> None:
    _assert_no_unregister(
        _eval_ast(), "eval_gtja191", forbid_registry_import=False,
    )


def test_batch_item_carries_optional_ic_series() -> None:
    """`FactorBatchItem` 必须能带 `ic_series`，否则脚本没法把半窗序列交给映射。

    `_evaluate_panel` 返回的是 `FactorResult`，**没有** `t_newey_west` / `q_value`
    三个字段（那是 `run_batch` 才挂到 `FactorBatchItem` 的）。脚本因此只能显式
    组一个 `FactorBatchItem`：如果这个类不接受 `ic_series`，`reversed` 就永远拿不到
    半窗序列、永不命中 —— 而且脚本会直接 TypeError 崩掉（实测踩过）。
    """
    item = FactorBatchItem(
        factor_name="gtja_2",
        label="影线相对位置-一阶差分",
        group="GTJA191（实验）",
        ic_series=_ic_series([0.01, 0.02]),
    )
    assert item.ic_series == _ic_series([0.01, 0.02])

    # 默认值为空列表（= 未提供），不影响 `run_batch` 的原有载荷。
    bare = FactorBatchItem(
        factor_name="gtja_2", label="x", group="g",
    )
    assert bare.ic_series == []
    assert bare.t_newey_west is None and bare.q_value is None
