"""P-arg-snap 隔离测：ledger 四字段 + snapshot 冻结 + late 排除。

守的是**判定契约本身**，不是上游结算（那在 `test_dsa_ledger_audit.py`）。
四类失效方式各有一组，缺一类就等于放行一种错误结论：

1. **缺失被读成通过** —— `risk_veto: null` 是"没有风控结论"，`{veto: false}`
   是"明确未否决"。把前者渲染成后者，等于给一个从没被风控看过的建议盖章。
2. **迟到被读成合法** —— 快照晚于建议时刻，说明论据用了当时还不存在的数据。
   它照样可以算胜率（A1–A7 不看论据来源），但**不能**进论据质量分母。
3. **编造** —— 缺快照就用 `evaluated_at` / 结算日 / "今天" 补一个。这是把
   lookahead 伪装成 PIT，比留空危险得多。
4. **时区漂移** —— 裸 datetime 当 UTC 解释会整体偏 8 小时，让本该 late 的
   快照看起来合法，而且**看不出错**（数字仍然合理）。

本文件零网络、零生产库、零凭证；上游 items 用内存 dict 注入。
禁 skip / xfail 装绿。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.custom.dsa import ledger_audit

_BACKEND = Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"
_ARG_SNAP_MODULE = _APP / "custom" / "dsa" / "ledger_audit.py"

ADVICE = "2026-10-27T09:30:00+08:00"


def _item(**overrides) -> dict:
    """上游 ``/backtest/results`` 的最小合法 item，带建议时刻。

    默认**不带**任何四字段键 —— 每个测例只改自己要测的那一处，
    这样失败时一眼能看出是哪个解析器的问题。
    """
    base = {
        "analysis_history_id": 11,
        "code": "600000",
        "outcome": "win",
        "start_price": 10.0,
        "created_at": ADVICE,
    }
    base.update(overrides)
    return base


def _row(**overrides) -> ledger_audit.AuditedRow:
    audit = ledger_audit.audit_rows([_item(**overrides)])
    assert len(audit.rows) == 1
    return audit.rows[0]


# ═══════════════════════════════════════════ T-keys：键名稳定，一个都不许改名


NEW_ROW_KEYS = frozenset(
    {
        "bull_case",
        "bear_case",
        "risk_veto",
        "snapshot_asof",
        "advice_generated_at",
        "snapshot_late",
        "in_argument_quality_denominator",
    }
)

NEW_SUMMARY_KEYS = frozenset(
    {
        "argument_quality_denominator",
        "argument_quality_excluded_late",
        "argument_quality_excluded_missing_snapshot",
        "argument_quality_suppressed",
        "argument_quality_pct",
    }
)


def test_row_to_dict_exposes_the_new_keys() -> None:
    payload = _row().to_dict()
    missing = set(NEW_ROW_KEYS) - set(payload)
    assert not missing, sorted(missing)


def test_summary_exposes_the_quality_keys() -> None:
    summary = ledger_audit.audit_rows([_item()]).summary()
    missing = set(NEW_SUMMARY_KEYS) - set(summary)
    assert not missing, sorted(missing)


def test_existing_a1_a7_keys_are_not_renamed() -> None:
    """增量只能是**加**键。既有键少一个，前端的 A1–A7 面板就整块塌掉。"""
    payload = _row().to_dict()
    legacy = {
        "analysis_history_id",
        "code",
        "outcome",
        "entry_close",
        "entry_next_open",
        "entry_gap_pct",
        "entry_basis",
        "unfillable",
        "in_denominator",
        "source",
        "source_note",
        "simulated_return_pct",
    }
    assert legacy <= set(payload), sorted(legacy - set(payload))


# ═══════════════════════════════════════════ T-freeze-ok / T-late-excl / T-missing-excl


def test_snapshot_before_advice_is_not_late_and_counts() -> None:
    """快照 ≤ 建议时刻 → 未迟到，进论据质量分母。"""
    row = _row(snapshot_asof="2026-10-27T09:29:59+08:00")
    assert row.snapshot_late is False
    assert row.snapshot_asof == "2026-10-27T09:29:59+08:00"
    assert row.in_argument_quality_denominator is True

    summary = ledger_audit.audit_rows([_item(snapshot_asof="2026-10-27T09:29:59+08:00")]).summary()
    assert summary["argument_quality_denominator"] == 1
    assert summary["argument_quality_excluded_late"] == 0
    assert summary["argument_quality_excluded_missing_snapshot"] == 0


def test_snapshot_equal_to_advice_is_not_late() -> None:
    """相等不算迟到。写 `<` 和写 `<=` 在这一秒上结果完全相反。"""
    row = _row(snapshot_asof=ADVICE)
    assert row.snapshot_late is False
    assert row.in_argument_quality_denominator is True


def test_snapshot_one_second_after_advice_is_late_and_excluded() -> None:
    """T-late-excl：+1s → late 且**不**进质量分母。"""
    row = _row(snapshot_asof="2026-10-27T09:30:01+08:00")
    assert row.snapshot_late is True
    assert row.in_argument_quality_denominator is False

    summary = ledger_audit.audit_rows([_item(snapshot_asof="2026-10-27T09:30:01+08:00")]).summary()
    assert summary["argument_quality_denominator"] == 0
    assert summary["argument_quality_excluded_late"] == 1


def test_late_row_still_counts_for_win_rate() -> None:
    """**T-a2-orthogonal**：late 行照样按 A2 规则进胜率分母。

    迟到的是"论据用了未来数据"，不是"这笔交易买不到"。两套账合并，
    会让迟到污染胜率 —— 那等于用论据质量问题去改交易统计。
    """
    items = [
        _item(
            analysis_history_id=11,
            outcome="win",
            snapshot_asof="2026-10-27T09:30:01+08:00",  # late
        )
    ]
    audit = ledger_audit.audit_rows(items)
    row = audit.rows[0]
    assert row.snapshot_late is True
    assert row.unfillable is None
    assert row.in_denominator is True, "late 不进胜率分母的排除理由"
    assert row.in_argument_quality_denominator is False

    summary = audit.summary()
    assert summary["denominator"] == 1
    assert summary["wins"] == 1
    assert summary["argument_quality_denominator"] == 0


def test_missing_snapshot_is_excluded_and_never_fabricated() -> None:
    """T-missing-excl：无快照 → null，不进质量分母，且绝不编一个时间。"""
    row = _row()  # 没有 snapshot_asof 键
    assert row.snapshot_asof is None
    assert row.snapshot_late is False, "缺失靠『不计入』排除，不靠 late 旗"
    assert row.in_argument_quality_denominator is False

    summary = ledger_audit.audit_rows([_item()]).summary()
    assert summary["argument_quality_denominator"] == 0
    assert summary["argument_quality_excluded_missing_snapshot"] == 1
    assert summary["argument_quality_excluded_late"] == 0


def test_evaluated_at_cannot_stand_in_for_snapshot() -> None:
    """**禁事后回填**：`evaluated_at` 是结算时刻，不是快照时刻。

    上游结算一定发生在建议之后，拿它当快照等于自己给自己发 late 豁免 ——
    而且会一路绿灯。所以它**不在**候选键里。
    """
    row = _row(evaluated_at="2026-09-01T15:00:00+08:00")
    assert row.snapshot_asof is None
    assert row.in_argument_quality_denominator is False

    row = _row(analysis_date="2026-09-01")
    assert row.snapshot_asof is None


def test_quality_percent_is_withheld_this_round() -> None:
    """论据质量**率**本拍不给：分母门槛与多空非空门槛都还挂档。

    留 `null` 而不是给一个数，前端才能诚实渲染成「—」。
    """
    rows = [
        _item(analysis_history_id=i, snapshot_asof="2026-10-27T09:00:00+08:00") for i in range(40)
    ]
    summary = ledger_audit.audit_rows(rows).summary()
    assert summary["argument_quality_denominator"] == 40
    assert summary["argument_quality_pct"] is None


def test_small_quality_sample_is_flagged_suppressed() -> None:
    summary = ledger_audit.audit_rows([_item(snapshot_asof="2026-10-27T09:00:00+08:00")]).summary()
    assert summary["argument_quality_suppressed"] is True
    assert summary["min_sample_for_percent"] == ledger_audit.MIN_SAMPLE_FOR_PERCENT


# ═══════════════════════════════════════════ N4：秒级 + Asia/Shanghai


def test_same_second_is_not_late() -> None:
    assert (
        ledger_audit.is_snapshot_late(
            snapshot_asof="2026-10-27T09:30:00+08:00",
            advice_generated_at="2026-10-27T09:30:00+08:00",
        )
        is False
    )


def test_sub_second_difference_does_not_flip_the_verdict() -> None:
    """亚秒被截断：09:30:00.900 在同秒内，不算晚于 09:30:00。

    四舍五入就会把它进位成 09:30:01 → 误判 late。截断是刻意的。
    """
    assert (
        ledger_audit.is_snapshot_late(
            snapshot_asof="2026-10-27T09:30:00.900000+08:00",
            advice_generated_at="2026-10-27T09:30:00+08:00",
        )
        is False
    )


def test_one_second_later_is_late() -> None:
    assert (
        ledger_audit.is_snapshot_late(
            snapshot_asof="2026-10-27T09:30:01+08:00",
            advice_generated_at="2026-10-27T09:30:00+08:00",
        )
        is True
    )


def test_naive_datetime_is_read_as_asia_shanghai() -> None:
    """裸 datetime 按上海解释。当 UTC 会整体偏 8 小时，late 永远判不出来。"""
    # 裸 09:30 = 上海 09:30，与 +08:00 的 09:30 同一时刻 → 不 late
    assert (
        ledger_audit.is_snapshot_late(
            snapshot_asof="2026-10-27T09:30:00",
            advice_generated_at="2026-10-27T09:30:00+08:00",
        )
        is False
    )
    # 裸 09:30:01 晚 1 秒 → late
    assert (
        ledger_audit.is_snapshot_late(
            snapshot_asof="2026-10-27T09:30:01",
            advice_generated_at="2026-10-27T09:30:00+08:00",
        )
        is True
    )


def test_utc_offsets_are_converted_not_compared_raw() -> None:
    """`Z`（UTC）与 +08:00 混比必须先换算：01:30Z == 09:30+08:00。"""
    assert (
        ledger_audit.is_snapshot_late(
            snapshot_asof="2026-10-27T01:30:00Z",
            advice_generated_at="2026-10-27T09:30:00+08:00",
        )
        is False
    )
    assert (
        ledger_audit.is_snapshot_late(
            snapshot_asof="2026-10-27T01:30:01Z",
            advice_generated_at="2026-10-27T09:30:00+08:00",
        )
        is True
    )


def test_cross_day_but_earlier_is_not_late() -> None:
    """跨日但 asof 更早 → 不 late。比较的是时刻，不是日期。"""
    assert (
        ledger_audit.is_snapshot_late(
            snapshot_asof="2026-10-26T23:59:59+08:00",
            advice_generated_at="2026-10-27T09:30:00+08:00",
        )
        is False
    )


def test_date_only_is_read_as_shanghai_midnight() -> None:
    """只有日期 → 当日上海 00:00（N4 第 3 条）。当天的 09:30 建议不算 late。"""
    assert (
        ledger_audit.is_snapshot_late(
            snapshot_asof="2026-10-27",
            advice_generated_at="2026-10-27T09:30:00+08:00",
        )
        is False
    )


def test_normalize_ts_returns_shanghai_second_precision() -> None:
    parsed = ledger_audit.normalize_ts("2026-10-27T01:30:45.123456Z")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=8)
    assert parsed.microsecond == 0
    assert parsed.hour == 9 and parsed.minute == 30 and parsed.second == 45


def test_normalize_ts_rejects_junk_without_guessing() -> None:
    """解析不了就是 None。**绝不**返回"现在"或"今天"顶替。"""
    for junk in (None, "", "   ", "unknown", "N/A", "2026-13-45T99:99:99", True, 123, [], {}):
        assert ledger_audit.normalize_ts(junk) is None, junk


def test_iso_output_carries_explicit_offset() -> None:
    assert ledger_audit.iso_or_none("2026-10-27T09:30:00") == "2026-10-27T09:30:00+08:00"
    assert ledger_audit.iso_or_none("2026-10-27T01:30:00Z") == "2026-10-27T09:30:00+08:00"


# ═══════════════════════════════════════════ N5：null ≠ false


def test_no_risk_material_yields_null() -> None:
    assert _row().risk_veto is None


def test_explicit_not_vetoed_is_an_object() -> None:
    row = _row(trigger="none")
    assert row.risk_veto == {"veto": False, "reason": None}
    assert row.risk_veto is not None, "明确未否决 ≠ 无结论"


def test_null_and_false_are_distinguishable() -> None:
    """T-null-ne-false：两者必须能区分。混淆等于把"没看过"说成"看过了"."""
    missing = _row().risk_veto
    passed = _row(trigger="none").risk_veto
    assert missing is None
    assert passed == {"veto": False, "reason": None}
    assert missing != passed


def test_veto_with_reason_is_output_as_is() -> None:
    """T-veto-ok：veto=true + 非空 reason → 原样输出。"""
    row = _row(trigger="risk_veto", guardrail_reason="单票集中度超限")
    assert row.risk_veto == {"veto": True, "reason": "单票集中度超限"}


def test_veto_without_reason_is_downgraded_to_null() -> None:
    """T-veto-missing-reason：否决却说不清为什么 → 不是结论，是缺证据。"""
    for reason in (None, "", "   "):
        row = _row(trigger="risk_veto", guardrail_reason=reason)
        assert row.risk_veto is None, reason


def test_explicit_veto_true_without_reason_is_rejected() -> None:
    """显式对象也一样：禁止输出 `{veto: true, reason: null/""}`。"""
    assert _row(risk_veto={"veto": True, "reason": None}).risk_veto is None
    assert _row(risk_veto={"veto": True, "reason": "  "}).risk_veto is None
    assert _row(risk_veto={"veto": True}).risk_veto is None


def test_explicit_veto_false_object_is_accepted() -> None:
    assert _row(risk_veto={"veto": False, "reason": None}).risk_veto == {
        "veto": False,
        "reason": None,
    }


def test_non_boolean_veto_is_rejected() -> None:
    """`"true"` / `1` 都不是布尔结论 —— 字符串"false"是真值，尤其危险。"""
    for bad in ("true", "false", 1, 0, None):
        assert _row(risk_veto={"veto": bad, "reason": "x"}).risk_veto is None, bad


def test_risk_downgrade_is_not_a_veto() -> None:
    """降级是"仓位打折"，不是"风控放行"也不是"否决"。

    输出 `{veto: false}` 会被读成"审过了没问题" —— 那是比没有结论更强的断言，
    而证据只有"降级"。所以整段留空（与 FIX N1 的推荐选项一致）。
    """
    row = _row(trigger="risk_downgrade", downgrade_reason="波动率抬升")
    assert row.risk_veto is None


def test_unknown_trigger_is_not_a_conclusion() -> None:
    for trigger in ("", "whatever", "risk_ok"):
        assert _row(trigger=trigger).risk_veto is None, trigger


# ═══════════════════════════════════════════ N1：只读解析与查找面


def test_bull_and_bear_case_are_read_from_candidate_keys() -> None:
    row = _row(bull_case="订单拐点 + 毛利修复", bear_case="估值分位偏高")
    assert row.bull_case == "订单拐点 + 毛利修复"
    assert row.bear_case == "估值分位偏高"


def test_candidate_key_priority_is_respected() -> None:
    """同一层里先命中的先用：`bull_case` 优先于 `strongest_bullish_signal`。"""
    row = _row(bull_case="首选", strongest_bullish_signal="次选")
    assert row.bull_case == "首选"


def test_blank_strings_are_treated_as_absent() -> None:
    row = _row(bull_case="   ", bear_case="")
    assert row.bull_case is None
    assert row.bear_case is None


def test_nested_layers_are_searched_in_order() -> None:
    """顶层没有就下探 `raw_result` / `context_snapshot`，但只下探一层。"""
    row = _row(raw_result={"bull_case": "来自 raw_result"})
    assert row.bull_case == "来自 raw_result"

    row = _row(context_snapshot={"bear_case": "来自 context_snapshot"})
    assert row.bear_case == "来自 context_snapshot"


def test_toplevel_wins_over_nested() -> None:
    row = _row(bull_case="顶层", raw_result={"bull_case": "嵌套"})
    assert row.bull_case == "顶层"


def test_non_string_values_are_not_evidence() -> None:
    """论据是文案。数字 / list / dict 不是论据，收下就是编。"""
    row = _row(bull_case=0.85, bear_case=["a"], raw_result={"bull_case": {"x": 1}})
    assert row.bull_case is None
    assert row.bear_case is None


def test_empty_raw_produces_all_null() -> None:
    """T-empty-no-fabricate：缺全部候选键 → 三个字段都是 null，一个字都不编。"""
    row = ledger_audit.audit_rows(
        [{"analysis_history_id": 1, "code": "600000", "outcome": "win", "start_price": 10.0}]
    ).rows[0]
    assert row.bull_case is None
    assert row.bear_case is None
    assert row.risk_veto is None
    assert row.snapshot_asof is None
    assert row.advice_generated_at is None
    assert row.snapshot_late is False


def test_advice_generated_at_defaults_to_created_at() -> None:
    """FIX 写死：默认来源是 `AnalysisHistory.created_at`。"""
    row = _row(created_at="2026-10-27T09:30:00")
    assert row.advice_generated_at == "2026-10-27T09:30:00+08:00"


def test_explicit_advice_generated_at_wins_over_created_at() -> None:
    row = _row(advice_generated_at="2026-10-27T09:00:00+08:00", created_at=ADVICE)
    assert row.advice_generated_at == "2026-10-27T09:00:00+08:00"


def test_snapshot_asof_accepts_documented_aliases() -> None:
    for key in ("snapshot_asof", "data_asof", "market_data_asof", "as_of"):
        row = _row(**{key: "2026-10-27T09:00:00+08:00"})
        assert row.snapshot_asof == "2026-10-27T09:00:00+08:00", key


def test_unparseable_snapshot_is_null_not_an_error() -> None:
    row = _row(snapshot_asof="unknown")
    assert row.snapshot_asof is None
    assert row.in_argument_quality_denominator is False


# ═══════════════════════════════════════════ T-freeze-immutable：冻结不被改写


def test_fields_do_not_change_when_settlement_fields_change() -> None:
    """T-freeze-immutable：结算侧字段变了，已冻结的四字段一动不动。

    审计层只读建议时刻侧材料。``start_price`` / ``outcome`` / ``simulated_return_pct``
    属于结算侧，它们变化**不得**带动多空 / 否决 / 快照。
    """
    before = _row(
        bull_case="多头 A",
        bear_case="空头 B",
        trigger="risk_veto",
        guardrail_reason="集中度超限",
        snapshot_asof="2026-10-27T09:00:00+08:00",
    )
    after = _row(
        bull_case="多头 A",
        bear_case="空头 B",
        trigger="risk_veto",
        guardrail_reason="集中度超限",
        snapshot_asof="2026-10-27T09:00:00+08:00",
        start_price=99.0,
        outcome="loss",
        simulated_return_pct=-8.8,
        evaluated_at="2026-09-30T15:00:00+08:00",
    )
    for field in (
        "bull_case",
        "bear_case",
        "risk_veto",
        "snapshot_asof",
        "advice_generated_at",
        "snapshot_late",
    ):
        assert getattr(before, field) == getattr(after, field), field


def test_module_has_no_write_path_for_the_four_fields() -> None:
    """源码级钉子：本模块不写库、不建表、不改上游。"""
    source = _ARG_SNAP_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("commit", "execute", "write_text", "INSERT", "UPDATE"):
        assert forbidden not in called, f"审计层不该调用 {forbidden}"


# ═══════════════════════════════════════════ T-scan：禁项扫描


def test_module_never_imports_tradingagents() -> None:
    """借机制，零代码搬运。import 一行就够打回。"""
    tree = ast.parse(_ARG_SNAP_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any("tradingagents" in name.lower() for name in imported), imported
    assert not any("tauric" in name.lower() for name in imported), imported


def test_module_never_calls_backtest_run() -> None:
    """读钩只打 results。为读钩触发 run 会写库、会污染账本。"""
    source = _ARG_SNAP_MODULE.read_text(encoding="utf-8")
    assert "backtest/run" not in source
    assert "backtest/results" in source


def test_module_touches_no_env_or_hits_or_triggering() -> None:
    """三 env / hits ding / DSA_REVIEW / TRIGGERING 本拍零碰（字面量都不许出现）。"""
    tree = ast.parse(_ARG_SNAP_MODULE.read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("getenv", "environ", "putenv"):
        assert forbidden not in called, f"审计层不该读环境变量（{forbidden}）"

    string_consts = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for token in (
        "BUY_SELL_SIGNALS_PERSIST",
        "BUY_SELL_SIGNALS_PUSH",
        "BUY_SELL_SIGNALS_DINGTALK",
        "BUY_SELL_SIGNALS_HITS_DINGTALK",
        "DSA_REVIEW_",
        "TRIGGERING_ENABLED",
        "VOLUME_BREAKOUT_TRIGGERING_ENABLED",
        "AUCTION_ABNORMAL_TRIGGERING_ENABLED",
    ):
        hits = [value for value in string_consts if token in value]
        assert not hits, f"{token}: {hits}"


def test_module_keeps_the_compliance_footnote() -> None:
    source = _ARG_SNAP_MODULE.read_text(encoding="utf-8")
    assert "仅供研究，不构成投资建议" in source
    assert "执行须人工确认" in source
    assert "18.2bp" in source


# ═══════════════════════════════════════════ 与 A1–A7 正交


def test_argument_quality_is_independent_of_unfillable() -> None:
    """不可成交影响胜率分母，**不**影响论据质量分母。两套账分账。"""
    items = [
        _item(
            analysis_history_id=11,
            snapshot_asof="2026-10-27T09:00:00+08:00",
        )
    ]
    audit = ledger_audit.audit_rows(
        items,
        prev_close_by_history_id={11: 9.09},  # 一字涨停场景由 OHLC 判定，这里不带 OHLC
    )
    row = audit.rows[0]
    assert row.unfillable is None
    assert row.in_argument_quality_denominator is True
    assert audit.summary()["argument_quality_denominator"] == 1


def test_quality_counts_do_not_change_win_rate_keys() -> None:
    """既有胜率口径的键值不因新字段而变。"""
    items = [
        _item(analysis_history_id=i, snapshot_asof="2026-10-27T09:00:00+08:00") for i in range(40)
    ]
    summary = ledger_audit.audit_rows(items).summary()
    assert summary["denominator"] == 40
    assert summary["wins"] == 40
    assert summary["win_rate_pct"] == 100.0
    assert summary["argument_quality_denominator"] == 40


def test_audit_items_keep_seven_entries_and_a5_mentions_snapshot() -> None:
    """A5 只扩展 detail，不新增条目 —— 既有 7 条契约不许撑破。"""
    payload = ledger_audit.ledger_audit_payload(items=[_item()])
    items = payload["audit_items"]
    assert len(items) == 7
    a5 = next(item for item in items if item["id"] == "A5")
    assert "snapshot_asof" in a5["detail"]
    assert "advice_generated_at" in a5["detail"]


def test_audit_status_stays_unaudited() -> None:
    """新字段不改审计徽章语义（A7）。"""
    payload = ledger_audit.ledger_audit_payload(
        items=[_item(snapshot_asof="2026-10-27T09:00:00+08:00", bull_case="x")]
    )
    assert payload["summary"]["audit_status"] == "unaudited"
    assert payload["summary"]["badge"] == ledger_audit.TSP_AUDIT_BADGE


# ═══════════════════════════════════════════ 混合场景：三条规则同时生效


def test_mixed_rows_account_for_every_bucket() -> None:
    """四条行分别落进：合法 / 迟到 / 缺快照 / 不可成交但有快照。"""
    rows = [
        _item(analysis_history_id=1, outcome="win", snapshot_asof="2026-10-27T09:00:00+08:00"),
        _item(analysis_history_id=2, outcome="win", snapshot_asof="2026-10-27T09:30:01+08:00"),
        _item(analysis_history_id=3, outcome="loss"),
        _item(analysis_history_id=4, outcome="neutral", snapshot_asof="2026-10-27T09:00:00+08:00"),
    ]
    summary = ledger_audit.audit_rows(rows).summary()
    assert summary["rows_total"] == 4
    assert summary["argument_quality_denominator"] == 2, "行 1 与行 4"
    assert summary["argument_quality_excluded_late"] == 1
    assert summary["argument_quality_excluded_missing_snapshot"] == 1
    assert summary["denominator"] == 3, "行 1/2/3 计入胜率（win, win, loss）；行 4 neutral 不计入"
    assert summary["wins"] == 2
    assert summary["losses"] == 1


def test_parse_helpers_are_pure_and_repeatable() -> None:
    """同一个 item 解析两次结果必须一致 —— 纯函数，没有隐藏状态。"""
    item = _item(
        bull_case="A",
        bear_case="B",
        trigger="risk_veto",
        guardrail_reason="R",
        snapshot_asof="2026-10-27T09:00:00+08:00",
    )
    first = ledger_audit.audit_rows([item]).rows[0].to_dict()
    second = ledger_audit.audit_rows([item]).rows[0].to_dict()
    assert first == second


def test_timezone_constant_matches_documented_name() -> None:
    assert ledger_audit.TZ_NAME == "Asia/Shanghai"
    assert ledger_audit.ASIA_SHANGHAI.utcoffset(None) == timedelta(hours=8)
    # 固定偏移而非 zoneinfo：中国无夏令时，且宿主缺 tzdata 时不会静默退化成 UTC。
    utc_now = datetime.now(UTC)
    assert utc_now.astimezone(ledger_audit.ASIA_SHANGHAI).utcoffset() == timedelta(hours=8)
