"""gtja191 N2 标签映射：纯函数、优先级、成本口径、下线候选只提示。

这个文件守的是**判定逻辑本身**，不是公式能不能算（那在 `test_factors_gtja191.py`）。
它要回答三个问题：

1. **样本不够时会不会误判 alive？** —— `V8`：29 个 IC 日、IC=0.15、费后多空 +50%，
   看起来漂亮得不像话，但只有 29 天。这种情况必须判 `underpowered`。**判 alive 就是
   把噪声当信号**，是这套标签最危险的失效方式。
2. **方向反了会不会被当成没用？** —— `V11`：IC 为正而费后多空为负。这是"因子有效但
   用反了"，处置是改方向重验，不是退役。判成 `dead` 会把一个可能有用的因子扔掉。
3. **成本口径错了会不会悄悄放过？** —— 传 14bp 进来必须抛错。`long_short_return`
   字段扣的就是 14bp，直接拿它过 18.2bp 的门每天少扣 4.2bp，结果系统性偏乐观，
   而且**看不出错**。

另有一条硬约束：本模块**不许出现 `unregister_factor`**。标签是判断，下线是人的决定。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.backtest.factor import FactorBatchItem
from app.factors.gtja191 import GTJA191_COST_BP_ROUNDTRIP, cost_gate_cost_bp, ic_table_cost_bp
from app.factors.gtja191_labels import (
    DELIST_CANDIDATE_LABELS,
    MIN_ABS_IC,
    MIN_IC_DATES,
    MIN_NET_LONG_SHORT,
    label_of,
)

COST = GTJA191_COST_BP_ROUNDTRIP

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
LABELS_MODULE = BACKEND / "app" / "factors" / "gtja191_labels.py"
EVAL_SCRIPT = REPO_ROOT / "scripts" / "eval_gtja191.py"


def _item(ic: float | None, *, error: str | None = None) -> FactorBatchItem:
    return FactorBatchItem(
        factor_name="gtja_2",
        label="影线相对位置-一阶差分",
        group="GTJA191（实验）",
        ic_mean=ic,
        error=error,
    )


# ── 阈值常量 ──


def test_thresholds_are_the_pinned_values() -> None:
    """任务书钉死 30 个 IC 日。改动这个数必须先改任务书，不能顺手放宽。"""
    assert MIN_IC_DATES == 30
    assert MIN_ABS_IC == 0.02
    assert MIN_NET_LONG_SHORT == 0.0


def test_cost_gate_uses_the_n5_anchor_not_the_ic_table_default() -> None:
    """成本门用 18.2bp，IC 表默认 14bp。两者不相等，必须同时钉住。"""
    assert cost_gate_cost_bp() == COST == 18.2
    assert ic_table_cost_bp() == 14.0
    assert cost_gate_cost_bp() != ic_table_cost_bp()


# ── V8：样本太薄不得判 alive ──


def test_v8_29_ic_dates_is_underpowered_not_alive() -> None:
    """V8：29 个 IC 日、IC=0.15、费后多空 +50%，一切看起来都过门 —— 但样本只有 29 天。

    IC 0.15 在 358 天里是顶尖因子，在 29 天里可能只是运气。判 alive 就等于拿 29 天的
    噪声去支持"这个因子能用"。**必须 underpowered。**
    """
    verdict = label_of(
        _item(0.15), n_ic_dates=29, net_long_short_return=0.50, cost_bp=COST,
    )
    assert verdict.label == "underpowered"
    assert verdict.label != "alive"
    assert not verdict.is_delist_candidate, "样本不够不是下线理由"
    assert "29" in verdict.reason


def test_v8_boundary_30_days_becomes_alive() -> None:
    """同一条输入，只差 1 天：30 日即可判 alive。守边界，防止把 < 写成 <= 或反之。"""
    verdict = label_of(
        _item(0.15), n_ic_dates=MIN_IC_DATES, net_long_short_return=0.50, cost_bp=COST,
    )
    assert verdict.label == "alive"


def test_underpowered_beats_dead_and_reversed() -> None:
    """样本不够时，既不判 dead 也不判 reversed —— 噪声的方向和幅度都没有意义。"""
    for net in (-0.9, 0.0, 0.9):
        verdict = label_of(
            _item(0.40), n_ic_dates=5, net_long_short_return=net, cost_bp=COST,
        )
        assert verdict.label == "underpowered", f"net={net}"
        assert not verdict.is_delist_candidate


def test_zero_ic_dates_is_underpowered_not_alive() -> None:
    verdict = label_of(_item(0.0), n_ic_dates=0, net_long_short_return=None, cost_bp=COST)
    assert verdict.label == "underpowered"


# ── V11：方向反了判 reversed，不判 dead ──


def test_v11_sign_flip_is_reversed_not_dead() -> None:
    """V11：IC 为正 (+0.08)、费后多空为负 (-0.12)。

    因子有效（|IC| 够大）但排序方向与收益方向脱节。判 `dead` 会把它当废物扔掉；
    正确处置是 `reversed` —— 换个方向重验。
    """
    verdict = label_of(
        _item(0.08), n_ic_dates=250, net_long_short_return=-0.12, cost_bp=COST,
    )
    assert verdict.label == "reversed"
    assert verdict.label != "dead"
    assert verdict.is_delist_candidate, "按当前方向做会亏，属下线候选"
    assert verdict.net_long_short_return == -0.12


def test_v11_negative_ic_positive_spread_is_also_reversed() -> None:
    """反号的另一半：IC 为负、多空为正。同样是方向脱节。"""
    verdict = label_of(
        _item(-0.06), n_ic_dates=250, net_long_short_return=0.10, cost_bp=COST,
    )
    assert verdict.label == "reversed"


def test_same_sign_never_reversed() -> None:
    """同号不判 reversed。IC 正/多空正、IC 负/多空负都算方向一致。"""
    assert label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.10, cost_bp=COST).label == "alive"
    assert label_of(_item(-0.05), n_ic_dates=250, net_long_short_return=-0.10, cost_bp=COST).label == "dead"


def test_tiny_magnitude_side_does_not_trigger_reversed() -> None:
    """一侧贴近 0 时不判 reversed —— 噪声的符号没有意义。

    IC=0.001 时符号是正还是负纯属偶然，拿它判"方向反了"会误伤。
    """
    verdict = label_of(
        _item(0.001), n_ic_dates=250, net_long_short_return=-0.30, cost_bp=COST,
    )
    assert verdict.label != "reversed"
    assert verdict.label == "dead"


# ── alive / dead 门槛 ──


def test_alive_requires_both_ic_and_net_after_cost() -> None:
    verdict = label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.03, cost_bp=COST)
    assert verdict.label == "alive"
    assert verdict.cost_bp == COST
    assert not verdict.is_delist_candidate


def test_dead_when_abs_ic_below_floor_even_if_net_positive() -> None:
    """费后碰巧为正也不行：|IC| 不到地板说明排序能力本身不足，正值是运气。"""
    verdict = label_of(_item(0.005), n_ic_dates=250, net_long_short_return=0.40, cost_bp=COST)
    assert verdict.label == "dead"
    assert verdict.is_delist_candidate


def test_dead_when_net_is_exactly_zero() -> None:
    """费后多空恰好为 0 不算过门（> 0 才过）。"""
    verdict = label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.0, cost_bp=COST)
    assert verdict.label == "dead"


def test_dead_when_net_is_none() -> None:
    """没有分层结果（样本不足以分组）不能证明费后为正，判 dead 而不是 alive。"""
    verdict = label_of(_item(0.05), n_ic_dates=250, net_long_short_return=None, cost_bp=COST)
    assert verdict.label == "dead"
    assert "无法证明" in verdict.reason


def test_ic_floor_boundary_is_inclusive() -> None:
    """|IC| 恰好等于 0.02 时算达标（>= 而不是 >）。"""
    verdict = label_of(_item(MIN_ABS_IC), n_ic_dates=250, net_long_short_return=0.10, cost_bp=COST)
    assert verdict.label == "alive"


# ── blocked / error 优先级 ──


def test_blocked_beats_everything() -> None:
    """blocked 优先于 error、优先于 IC 判定 —— 没算的东西谈不上好坏。"""
    verdict = label_of(
        _item(0.90, error="boom"),
        n_ic_dates=999,
        net_long_short_return=9.9,
        cost_bp=COST,
        blocked_reason="ops",
    )
    assert verdict.label == "blocked:ops"
    assert not verdict.is_delist_candidate
    assert verdict.cost_bp is None


def test_blocked_label_carries_the_reason() -> None:
    for reason in ("ops", "data"):
        verdict = label_of(
            _item(0.05), n_ic_dates=250, net_long_short_return=0.1,
            cost_bp=COST, blocked_reason=reason,
        )
        assert verdict.label == f"blocked:{reason}"


def test_error_beats_underpowered() -> None:
    """算失败了，不是"样本不够"。这两个处置不同：error 要修，underpowered 要等。"""
    verdict = label_of(
        _item(0.05, error="polars 崩了"), n_ic_dates=3, net_long_short_return=None, cost_bp=COST,
    )
    assert verdict.label == "error"
    assert "polars" in verdict.reason


def test_none_ic_with_zero_ic_dates_is_degenerate() -> None:
    """因子值在截面上恒定 → Rank IC 分母为 0 → 全部日期无定义 → `ic_mean` 为 None。

    这不是 `error`（代码没挂），也不是 `dead`（一次都没测过），更不是 `underpowered`
    （不是样本少，是样本全是同一个值）。单独归类，处置是修因子/换数据。
    """
    verdict = label_of(_item(None), n_ic_dates=0, net_long_short_return=None, cost_bp=COST)
    assert verdict.label == "degenerate"
    assert verdict.label not in {"error", "dead", "underpowered", "alive"}
    assert not verdict.is_delist_candidate


def test_degenerate_is_not_confused_with_underpowered() -> None:
    """`underpowered` 是"有数据但天数不够"，`degenerate` 是"一条有效数据都没有"。"""
    assert label_of(_item(0.05), n_ic_dates=0, net_long_short_return=None, cost_bp=COST).label == "underpowered"
    assert label_of(_item(None), n_ic_dates=0, net_long_short_return=None, cost_bp=COST).label == "degenerate"


def test_none_ic_with_positive_ic_dates_is_inconsistent_error() -> None:
    """ic_mean 为 None 却说有 250 个 IC 日 —— 结果自相矛盾，必须报 error 让人查。"""
    verdict = label_of(_item(None), n_ic_dates=250, net_long_short_return=0.1, cost_bp=COST)
    assert verdict.label == "error"
    assert "自相矛盾" in verdict.reason


def test_error_and_blocked_are_not_delist_candidates() -> None:
    """缺证据 ≠ 有反证。这两种都不是下线候选。"""
    for verdict in (
        label_of(_item(0.05, error="x"), n_ic_dates=250, net_long_short_return=0.1, cost_bp=COST),
        label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.1, cost_bp=COST, blocked_reason="ops"),
    ):
        assert not verdict.is_delist_candidate


# ── 成本口径：14bp 必须被拒 ──


def test_14bp_cost_is_rejected_loudly() -> None:
    """`long_short_return` 扣的是 14bp。拿它过 18.2bp 的门每天少扣 4.2bp。

    这种错**看不出来**（结果仍是个合理数字），所以必须抛错而不是打折放行。
    """
    with pytest.raises(ValueError, match="成本口径错误"):
        label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.10, cost_bp=ic_table_cost_bp())


def test_only_the_18_2_anchor_passes() -> None:
    with pytest.raises(ValueError):
        label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.10, cost_bp=0.0)
    assert label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.10, cost_bp=COST).label == "alive"


def test_cost_mismatch_checked_before_blocked() -> None:
    """口径错误优先于 blocked 判定 —— 传错成本说明调用方用错了 API，不是数据问题。"""
    with pytest.raises(ValueError):
        label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.1, cost_bp=14.0, blocked_reason="ops")


# ── 纯函数：不重算 IC ──


def test_mapping_never_recomputes_ic() -> None:
    """映射里不许出现 IC 计算痕迹。重算会让同一因子在不同调用点得到不同标签。

    扫的是 AST 里的**调用名**，不是文本子串 —— 文本扫描会把文档里"不许重算 IC"这句
    说明本身当成违规，逼人把约束写成暗号（参照 `_code_lines` 的同类教训）。
    """
    tree = ast.parse(LABELS_MODULE.read_text(encoding="utf-8"))
    forbidden = {"corr", "spearmanr", "pearsonr", "rolling_corr", "mean", "std", "polyfit"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        assert name not in forbidden, f"映射里出现了计算调用 {name}()，这是重算 IC 的痕迹"
    # 反向确认：模块根本不该 import numpy / polars —— 没有数值库就没法重算。
    import ast as _ast
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "numpy" not in imported
    assert "polars" not in imported


def test_mapping_does_not_touch_the_registry() -> None:
    """标签模块不许 import 任何注销/登记入口。下线是人的决定，不是脚本的副作用。"""
    source = LABELS_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(alias.name for alias in node.names)
    assert "unregister_factor" not in imported
    assert "register_factor" not in imported
    assert "registry" not in imported


def test_mapping_does_not_mutate_its_input() -> None:
    """输入 dataclass 原样不动 —— 纯函数不能有副作用。"""
    item = _item(0.05)
    before = (item.ic_mean, item.error, item.n_dates)
    label_of(item, n_ic_dates=250, net_long_short_return=0.1, cost_bp=COST)
    assert (item.ic_mean, item.error, item.n_dates) == before


def test_verdict_is_immutable() -> None:
    """判定结果不可改。有人想在后面顺手 `verdict.label = "alive"` 时应当直接炸掉。"""
    import dataclasses

    verdict = label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.1, cost_bp=COST)
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.label = "alive"  # type: ignore[misc]


# ── 下线候选：只提示 ──


def test_delist_candidate_set_is_exactly_dead_and_reversed() -> None:
    assert frozenset({"dead", "reversed"}) == DELIST_CANDIDATE_LABELS


def test_alive_underpowered_error_blocked_are_not_candidates() -> None:
    cases = {
        "alive": label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.1, cost_bp=COST),
        "underpowered": label_of(_item(0.05), n_ic_dates=10, net_long_short_return=0.1, cost_bp=COST),
        "error": label_of(_item(0.05, error="x"), n_ic_dates=250, net_long_short_return=0.1, cost_bp=COST),
        "blocked": label_of(_item(0.05), n_ic_dates=250, net_long_short_return=0.1, cost_bp=COST, blocked_reason="ops"),
    }
    for name, verdict in cases.items():
        assert verdict.label.startswith(name), f"{verdict.label} 不以 {name} 开头"
        assert not verdict.is_delist_candidate, f"{name} 不该是下线候选"


def test_eval_script_never_unregisters() -> None:
    """脚本路径里不许出现 unregister_factor —— 任务书原文「路径中禁止」。"""
    source = EVAL_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "unregister_factor", "自评脚本不许注销因子"
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            assert name != "unregister_factor"


def test_eval_script_prints_candidates_but_does_not_declare_them_removed() -> None:
    """提示文案必须写清「只是提示」，否则读报告的人会以为已经下线了。"""
    source = EVAL_SCRIPT.read_text(encoding="utf-8")
    assert "下线候选" in source
    assert "只是提示" in source or "不注销任何因子" in source


def test_eval_script_cost_gate_uses_the_anchor_constants() -> None:
    """成本门必须用 `_COST_*`（18.2bp），不许用 `_IC_TABLE_*`（14bp）或字面量。

    这是本补丁最容易悄悄错的一处：两个常量名字长得很像，改错了脚本照样跑出漂亮结果，
    只是每天都少扣 4.2bp。**没有这条测例，14bp 混进成本门是查不出来的。**
    """
    source = EVAL_SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.factors.gtja191":
            imported.update(alias.name for alias in node.names)

    assert "_COST_FEES_PCT" in imported, "成本门必须显式用成本锚的费率"
    assert "_COST_SLIPPAGE_BPS" in imported, "成本门必须显式用成本锚的滑点"
    assert "_IC_TABLE_SLIPPAGE_BPS" not in imported, "IC 表滑点（14bp 口径）不许进成本门"
    assert "_IC_TABLE_FEES_PCT" not in imported, "IC 表费率（14bp 口径）不许进成本门"

    # 成本门那一遍的 FactorConfig 必须两个成本参数都给，不能落回默认值。
    gate_configs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "FactorConfig"
        and any(kw.arg == "fees_pct" for kw in node.keywords)
    ]
    assert gate_configs, "脚本里找不到带自定义成本的 FactorConfig"
    for call in gate_configs:
        keys = {kw.arg for kw in call.keywords}
        assert {"fees_pct", "slippage_bps"} <= keys, "成本门的 FactorConfig 两个参数都要给"


def test_eval_script_uses_len_ic_series_not_n_dates() -> None:
    """V8 能不能真的生效，全看这一处：`n_ic_dates` 必须来自 `len(result.ic_series)`。

    `FactorResult.n_dates` 是 `panel["date"].n_unique()` —— **面板交易日数**，不是
    有效 IC 日数。因子在截面上恒定（无方差）时 Rank IC 全是 NaN，`n_dates` 仍然是 287，
    `ic_mean` 却为 None。

    如果这里写成 `n_ic_dates=result.n_dates`，29 天的样本会被读成 287 天，`underpowered`
    这条线**彻底失效**，29 天一路判成 `alive`。这是本补丁最容易写错也最难看出来的地方。
    """
    tree = ast.parse(EVAL_SCRIPT.read_text(encoding="utf-8"))

    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "label_of":
            continue
        for kw in node.keywords:
            if kw.arg != "n_ic_dates":
                continue
            found = True
            # 必须是 len(<something>.ic_series)
            assert isinstance(kw.value, ast.Call), "n_ic_dates 必须是一个 len(...) 调用"
            assert getattr(kw.value.func, "id", None) == "len", "n_ic_dates 必须是 len(...)"
            arg = kw.value.args[0] if kw.value.args else None
            assert isinstance(arg, ast.Attribute), "len 的参数必须是某个属性"
            assert arg.attr == "ic_series", (
                f"n_ic_dates 必须取 ic_series，不许取 {arg.attr}。"
                "`n_dates` 是面板交易日数，不是有效 IC 日数。"
            )
    assert found, "脚本里找不到 label_of 的 n_ic_dates 参数"


def test_eval_script_labels_blocked_ids_as_blocked_ops() -> None:
    """blocked 的 ID 要能落到 `blocked:ops`，而不是被当成"没数据"或"算挂了"。

    这里查的是 `verdict = label_of(...)` 这种**直接赋值**，不是"文件里出现过 label_of"。
    光查出现会把 `verdict = None and label_of(...)`（短路后永远拿不到标签）放过。
    """
    tree = ast.parse(EVAL_SCRIPT.read_text(encoding="utf-8"))

    direct_assign = False
    blocked_passed = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "verdict" not in targets:
            continue
        value = node.value
        # verdict = label_of(...)
        if isinstance(value, ast.Call) and getattr(value.func, "id", None) == "label_of":
            direct_assign = True
            if any(
                kw.arg == "blocked_reason"
                and isinstance(kw.value, ast.Call)
                and getattr(kw.value.func, "id", None) == "blocked_reason"
                for kw in value.keywords
            ):
                blocked_passed = True

    assert direct_assign, "verdict 必须直接由 label_of(...) 赋值，不许自己另写一套判定"
    assert blocked_passed, "判定必须把 blocked_reason(...) 的结果传给映射"
