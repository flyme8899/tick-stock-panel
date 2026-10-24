"""行业内质量相对分位 + 白名单试跑测试(批次 20261025g2 ②)。

守四件事:

1. 行业内相对分位可算、可单测; 跨行业**互不影响**
2. 缺行业 / 缺分 / 质量缺失 → missing, **不填 0**(0 是"行业垫底", 不是"不知道")
3. 白名单是**试跑** p40, `locked` 为空, 文案里不许出现"已锁定"
4. 本模块不 import `t_rounds` / 提醒层, 也不改权重

全部人造数据, 不联网、不读 `/data`、不依赖 Pro 分钟 capability。
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest
from app.buy_sell_signals.quality_proxy import QUALITY_WEIGHT_STATUS, QualityProxy
from app.buy_sell_signals.quality_rank import (
    RANK_MISSING_REASONS,
    WHITELIST_PERCENTILE_LOCKED,
    WHITELIST_PERCENTILE_TRIAL,
    WHITELIST_THRESHOLD_LOCKED,
    WHITELIST_TRIAL_QUANTILE,
    IndustryRankEntry,
    QualityRankError,
    percentile_rank,
    rank_within_industry,
)
from app.buy_sell_signals.sw_pit import IndustryPit

PKG_DIR = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
ASOF = date(2026, 10, 23)


def _proxy(symbol: str, q_score: int | None, *, missing: bool = False, asof: date = ASOF) -> QualityProxy:
    return QualityProxy(
        symbol=symbol,
        asof=asof,
        q_score=q_score,
        q_missing_flag=missing or q_score is None,
        q_missing_reasons=("no_financials_known_at_asof",) if missing else (),
    )


def _pit(symbol: str, l1: str | None) -> IndustryPit:
    return IndustryPit(symbol=symbol, asof=ASOF, industry_code_sw_l1=l1)


# =========================================================== 百分位纯函数


def test_percentile_rank_endpoints_and_monotonic():
    assert percentile_rank([10, 20, 30, 40]) == [0.0, 1 / 3, 2 / 3, 1.0]


def test_percentile_rank_ties_get_average():
    """并列取平均秩 —— 否则同一个分数会因输入顺序拿到不同分位。"""
    assert percentile_rank([10, 20, 20, 30]) == [0.0, 0.5, 0.5, 1.0]


def test_percentile_rank_all_equal_is_half():
    """全员同分时谁也没排在谁前面 → 0.5, 不是 0 也不是 1。"""
    assert percentile_rank([5, 5, 5]) == [0.5, 0.5, 0.5]


def test_percentile_rank_is_permutation_invariant():
    """打乱输入顺序, 每只票拿到的分位不变。"""
    values = [3, 7, 7, 1, 9]
    order = [4, 0, 3, 2, 1]
    base = percentile_rank(values)
    shuffled = percentile_rank([values[i] for i in order])
    assert [base[i] for i in order] == shuffled


def test_percentile_rank_empty_and_single():
    assert percentile_rank([]) == []
    assert percentile_rank([42]) == [0.5]


# =========================================================== 行业内相对分位


def test_rank_within_one_industry():
    proxies = [_proxy("A", 1), _proxy("B", 2), _proxy("C", 3), _proxy("D", 4)]
    result = rank_within_industry(proxies, {p.symbol: _pit(p.symbol, "801010") for p in proxies})

    assert result.entries["A"].q_rank_within_ind == 0.0
    assert result.entries["D"].q_rank_within_ind == 1.0
    assert result.entries["B"].q_rank_within_ind == pytest.approx(1 / 3)
    assert all(e.industry_n_ranked == 4 for e in result.entries.values())
    assert all(e.q_rank_missing is False for e in result.entries.values())


def test_industries_are_independent():
    """同一分数在两个行业里可以有完全不同(甚至相反)的相对位置。"""
    proxies = [
        _proxy("A1", 5), _proxy("A2", 4), _proxy("A3", 4),  # 行业 A: 5 最高
        _proxy("B1", 1), _proxy("B2", 1), _proxy("B3", 2),  # 行业 B: 1 最低
    ]
    inds = {
        "A1": _pit("A1", "801010"), "A2": _pit("A2", "801010"), "A3": _pit("A3", "801010"),
        "B1": _pit("B1", "801020"), "B2": _pit("B2", "801020"), "B3": _pit("B3", "801020"),
    }
    r = rank_within_industry(proxies, inds)
    assert r.entries["A1"].q_rank_within_ind == 1.0
    # B 组 [1, 1, 2]: 两个 1 并列, 平均秩 (1+2)/2 = 1.5 → (1.5-1)/(3-1) = 0.25
    assert r.entries["B1"].q_rank_within_ind == pytest.approx(0.25)
    assert r.entries["B1"].q_rank_within_ind < r.entries["A1"].q_rank_within_ind
    assert r.entries["B1"].industry_n_ranked == 3
    assert r.entries["A1"].industry_n_ranked == 3


def test_independent_across_industries_even_when_counts_differ():
    """行业 A 有 4 只、行业 B 有 2 只; B 里的排名不因 A 的规模被稀释。"""
    proxies = [_proxy(f"A{i}", i) for i in range(1, 5)] + [_proxy("B1", 1), _proxy("B2", 2)]
    inds = {f"A{i}": "801010" for i in range(1, 5)} | {"B1": "801020", "B2": "801020"}
    r = rank_within_industry(proxies, inds)
    assert r.entries["B1"].q_rank_within_ind == 0.0
    assert r.entries["B2"].q_rank_within_ind == 1.0
    assert r.entries["B2"].industry_n_ranked == 2


# =========================================================== missing 不填 0


def test_missing_industry_is_missing_not_zero():
    proxies = [_proxy("A", 1), _proxy("B", 2)]
    inds = {"A": _pit("A", "801010"), "B": _pit("B", None)}
    r = rank_within_industry(proxies, inds)
    entry = r.entries["B"]
    assert entry.q_rank_within_ind is None
    assert entry.q_rank_missing is True
    assert entry.q_rank_missing_reason == "no_industry"
    assert entry.whitelist_pass is False


def test_no_industry_key_at_all_is_missing():
    """industries 里根本没有这只票 —— 与"有记录但没映射"同样处理。"""
    r = rank_within_industry([_proxy("A", 1), _proxy("B", 2)], {"A": "801010"})
    assert r.entries["B"].q_rank_missing_reason == "no_industry"
    assert r.entries["B"].q_rank_within_ind is None


def test_missing_q_score_is_missing_not_zero():
    proxies = [_proxy("A", 1), _proxy("B", None)]
    inds = {p.symbol: "801010" for p in proxies}
    r = rank_within_industry(proxies, inds)
    assert r.entries["B"].q_rank_within_ind is None
    assert r.entries["B"].q_rank_missing_reason == "no_q_score"


def test_q_missing_flag_blocks_rank_even_with_score():
    """有分数但质量代理自己标了缺失 → 不参与排名。"""
    proxies = [_proxy("A", 1), _proxy("B", 2), _proxy("C", 3, missing=True)]
    inds = {p.symbol: "801010" for p in proxies}
    r = rank_within_industry(proxies, inds)
    assert r.entries["C"].q_rank_within_ind is None
    assert r.entries["C"].q_rank_missing_reason == "q_missing"
    # C 不进入分母: 剩两只, 分位是 0 和 1
    assert r.entries["A"].q_rank_within_ind == 0.0
    assert r.entries["B"].q_rank_within_ind == 1.0
    assert r.entries["B"].industry_n_ranked == 2


def test_missing_tickets_are_never_filled_with_zero():
    """核心断言: 任何 missing 的票, 分位都必须是 None 而不是 0.0。

    填 0 会让它们看起来是"行业垫底", 从而在白名单里被静默排除 —— 而它们其实
    只是"没算出来"。这两种情况在效果上一样, 在原因上完全不同。
    """
    proxies = [_proxy("OK1", 3), _proxy("OK2", 1), _proxy("N1", None), _proxy("N2", 2, missing=True)]
    inds = {"OK1": "801010", "OK2": "801010", "N1": "801010", "N2": "801010"}
    r = rank_within_industry(proxies, inds)
    for sym in ("N1", "N2"):
        assert r.entries[sym].q_rank_within_ind is None
        assert r.entries[sym].q_rank_within_ind != 0.0
        assert r.entries[sym].whitelist_pass is False


def test_singleton_industry_is_missing():
    """本行业只有一只有资格的票: 相对位置无从谈起, 不能给 1.0。"""
    proxies = [_proxy("S", 4), _proxy("A1", 1), _proxy("A2", 2)]
    inds = {"S": "801099", "A1": "801010", "A2": "801010"}
    r = rank_within_industry(proxies, inds)
    assert r.entries["S"].q_rank_within_ind is None
    assert r.entries["S"].q_rank_missing_reason == "singleton_industry"
    assert r.entries["S"].whitelist_pass is False


def test_missing_reason_codes_are_closed_set():
    """原因码必须全部落在 RANK_MISSING_REASONS 里, 不允许随手编新码。"""
    proxies = [
        _proxy("A", 1), _proxy("B", None), _proxy("C", 2, missing=True),
        _proxy("D", 3), _proxy("E", 5),
    ]
    inds = {"A": "801010", "B": "801010", "C": "801010", "D": "801010", "E": "801099"}
    r = rank_within_industry(proxies, inds)
    for e in r.entries.values():
        if e.q_rank_missing:
            assert e.q_rank_missing_reason in RANK_MISSING_REASONS


# =========================================================== 白名单试跑


def test_trial_quantile_matches_trial_label():
    """`p40` 与 0.40 必须对得上 —— 两处定义分叉是最典型的静默 bug。"""
    assert WHITELIST_PERCENTILE_TRIAL == "p40"
    assert WHITELIST_TRIAL_QUANTILE == 0.40


def test_whitelist_threshold_is_not_locked():
    assert WHITELIST_PERCENTILE_LOCKED is None
    assert WHITELIST_THRESHOLD_LOCKED is False


def test_whitelist_pass_is_inclusive_ge():
    """判定式是 `>= 0.40`(闭区间)。恰好 0.40 的票必须过 —— 方向写反会静默砍掉一半。"""
    # 6 只等距分布: 分位 0, .2, .4, .6, .8, 1.0 —— 恰有一只落在 0.40
    proxies = [_proxy(f"X{i}", i) for i in range(6)]
    inds = {p.symbol: "801010" for p in proxies}
    r = rank_within_industry(proxies, inds)
    on_boundary = r.entries["X2"]
    assert on_boundary.q_rank_within_ind == pytest.approx(0.40)
    assert on_boundary.whitelist_pass is True


def test_whitelist_lists_only_ranked_and_above_threshold():
    proxies = [_proxy("P1", 1), _proxy("P2", 2), _proxy("P3", 3), _proxy("P4", 4), _proxy("P5", None)]
    inds = {p.symbol: "801010" for p in proxies}
    r = rank_within_industry(proxies, inds)
    assert r.whitelist_symbols == ("P3", "P4")
    assert "P5" not in r.whitelist_symbols
    assert "P5" in r.missing_symbols


def test_whitelist_entry_exposes_trial_and_locked_fields():
    r = rank_within_industry([_proxy("A", 1), _proxy("B", 2)], {"A": "801010", "B": "801010"})
    entry = r.entries["B"]
    assert entry.whitelist_percentile_trial == "p40"
    assert entry.whitelist_threshold_locked is False
    payload = entry.to_dict()
    assert payload["whitelist_percentile_trial"] == "p40"
    assert payload["whitelist_percentile_locked"] is None
    assert payload["whitelist_threshold_locked"] is False


def test_result_to_dict_counts():
    proxies = [_proxy("A", 1), _proxy("B", 2), _proxy("C", 3), _proxy("D", None)]
    inds = {p.symbol: "801010" for p in proxies}
    payload = rank_within_industry(proxies, inds).to_dict()
    assert payload["n_ranked"] == 3
    assert payload["n_missing"] == 1
    assert payload["whitelist_percentile_trial"] == "p40"
    assert payload["whitelist_percentile_locked"] is None


# =========================================================== 不碰权重


def test_rank_does_not_touch_weights():
    """白名单不是权重: 进不进名单, `w_quality` / `weighting_status` 都不变。"""
    r = rank_within_industry([_proxy("A", 1), _proxy("B", 2)], {"A": "801010", "B": "801010"})
    for entry in r.entries.values():
        assert entry.w_quality is None
        assert entry.weighting_status == QUALITY_WEIGHT_STATUS == "design_pending"


def test_entry_construction_rejects_nothing_but_keeps_defaults():
    """直接构造也走同样的默认: 试跑 p40, 未锁定。"""
    entry = IndustryRankEntry(symbol="A", asof=ASOF)
    assert entry.whitelist_percentile_trial == "p40"
    assert entry.whitelist_threshold_locked is False
    assert entry.w_quality is None


# =========================================================== 输入校验


def test_duplicate_symbol_rejected():
    with pytest.raises(QualityRankError, match="重复"):
        rank_within_industry([_proxy("A", 1), _proxy("A", 2)], {"A": "801010"})


def test_mixed_asof_rejected():
    """跨日混算会让"同一天内的相对位置"这个语义失效, 且很难在结果里看出来。"""
    proxies = [_proxy("A", 1), _proxy("B", 2, asof=date(2026, 10, 22))]
    with pytest.raises(QualityRankError, match="asof"):
        rank_within_industry(proxies, {"A": "801010", "B": "801010"})


def test_blank_industry_string_treated_as_missing():
    """空串会被当成一个合法行业码, 把一堆没归属的票聚成一组互相比较。"""
    r = rank_within_industry([_proxy("A", 1), _proxy("B", 2)], {"A": "  ", "B": "801010"})
    assert r.entries["A"].q_rank_missing_reason == "no_industry"


def test_empty_input():
    r = rank_within_industry([], {})
    assert r.entries == {}
    assert r.whitelist_symbols == ()


def test_industry_pit_accepted_directly():
    """直接喂 `IndustryPit` 而不是裸字符串 —— 生产路径就是这么用的。"""
    proxies = [_proxy("A", 1), _proxy("B", 2)]
    r = rank_within_industry(proxies, {p.symbol: _pit(p.symbol, "801010") for p in proxies})
    assert r.entries["B"].q_rank_within_ind == 1.0


# =========================================================== 隔离


def _identifiers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
                if alias.asname:
                    names.add(alias.asname)
    return names


def test_quality_rank_does_not_import_neighbour_packages():
    """本模块不得 import `t_rounds` / 提醒层 / `trade_marks` / `news.push`。"""
    ids = _identifiers(PKG_DIR / "quality_rank.py")
    for forbidden in ("t_rounds", "trade_marks", "push", "news"):
        assert forbidden not in ids, f"quality_rank.py 里出现了 {forbidden!r}"


def test_quality_rank_has_no_reminder_constants():
    """提醒层数值不得渗进门禁: `VWAP_BAND` / `RANGE_BAND` / `T_COOLDOWN_MIN`。"""
    ids = _identifiers(PKG_DIR / "quality_rank.py")
    for forbidden in ("VWAP_BAND", "RANGE_BAND", "T_COOLDOWN_MIN"):
        assert forbidden not in ids


def test_quality_rank_has_no_t_cooldown_semantics():
    """冷却/轮次这类名字不属于本模块。"""
    text = (PKG_DIR / "quality_rank.py").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    body = "\n".join(lines)
    for forbidden in ("cooldown", "rounds_consumed"):
        assert forbidden not in body


def test_no_locked_claim_in_text():
    """文档/代码里不得出现「已锁定」这类结论声明。

    否定语境(「未锁定」「不许说已锁定」)放过 —— 否则护栏句本身会被判违规,
    结果就是有人把护栏删掉。
    """
    path = PKG_DIR / "quality_rank.py"
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if "已锁定" not in line:
            continue
        assert any(marker in line for marker in ("未", "不", "禁止", "没有")), (
            f"quality_rank.py:{lineno} 出现未加否定的「已锁定」: {line.strip()}"
        )
