"""做 T 轮次注册表 / 状态机 / Store 骨架测试 (批次 20261024h 任务①)。

不依赖 Pro 分钟 capability, 不访问真实 `/data/tsp-reports`, 不接线
`build_feature_frame`, 不联网。

本文件守四件事:

1. 状态机: 合法转移; **未完成/已放弃不得进已实现 KPI**; 正 T 第二腿必须 old_lot
2. Store: 内存往返; 同日同票超 `max_rounds_per_day` 拒绝
3. ε = 0.30 且 version 串含 pin + eps; 无提醒常量耦合
4. 隔离: 不 import `buy_sell_signals`; 冷却/轮次字段名分列; 无「已测通」文案
"""
from __future__ import annotations

import ast
import dataclasses
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from app.market_time import CN_TZ
from app.t_rounds import (
    DISCLAIMER_TEXT,
    EPSILON_DEFINITION,
    EPSILON_VALUE_PCT,
    MAX_ROUNDS_PER_DAY,
    NAMESPACE,
    RESEARCH_WINDOW_START,
    T_ROUND_DESIGN_PIN,
    DiskTRoundRegistryStore,
    InMemoryTRoundRegistryStore,
    InvalidTRoundTransitionError,
    LegState,
    PersistDisabledError,
    PositionTag,
    RoundCost,
    RoundQuotaExceeded,
    TRoundRecord,
    TRoundStateError,
    TRoundValidationError,
    complete_round,
    design_version,
    tag_position,
    transition,
)
from app.t_rounds import constants as C
from app.t_rounds import state_machine
from app.t_rounds import store as store_module

PKG_DIR = Path(__file__).resolve().parents[1] / "app" / "t_rounds"
TODAY = date(2026, 10, 24)


def _record(**kwargs) -> TRoundRecord:
    base = {
        "t_round_id": "tr.demo",
        "symbol": "600000.SH",
        "trade_date": TODAY,
        "mode": "zheng_t",
    }
    base.update(kwargs)
    # incomplete / abandoned 必须带原因(模型层的硬校验)。夹具里默认补一句, 免得
    # 每个"只想构造个处于该状态的记录"的用例都要重复写原因 —— 那会把测试的重点
    # 从"转移是否合法"稀释成"记得填 reason"。
    if base.get("round_status") == "incomplete":
        base.setdefault("incomplete_reason", "夹具: 第二腿未达门禁")
    if base.get("round_status") == "abandoned":
        base.setdefault("abandoned_reason", "夹具: 主动放弃")
    return TRoundRecord(**base)


def _open_leg2(record: TRoundRecord | None = None) -> TRoundRecord:
    return transition(record or _record(), "open_leg2", leg2_source_lot="old_lot")


# =========================================================== 身份 / 默认值


def test_defaults_are_safe():
    rec = _record()
    assert rec.round_status == "open_leg1"
    assert rec.max_rounds_per_day == MAX_ROUNDS_PER_DAY == 1
    assert rec.formal_push_allowed is False, "正式推送默认必须关"
    assert rec.paper_track_only is True
    assert rec.virtual_position_flag is True, "离线虚拟底仓默认 true"
    assert rec.signal_universe_filtered_by_position is False
    assert rec.zero_cross_ref_only is True
    assert rec.position_tag == "observe_only"
    assert rec.count_in_realized_kpi is False
    assert rec.net_spread_realized is False


def test_epsilon_defaults():
    rec = _record()
    assert rec.epsilon_value == EPSILON_VALUE_PCT == 0.30
    assert rec.epsilon_definition == EPSILON_DEFINITION == "pct_of_vwap_or_mean"
    assert rec.epsilon_locked_in_version is True


def test_version_string_carries_pin_and_epsilon():
    version = design_version()
    assert T_ROUND_DESIGN_PIN in version
    assert "eps_pct:0.30" in version
    assert _record().version == version == "design_pin_v1+eps_pct:0.30"


def test_unknown_mode_or_status_rejected():
    with pytest.raises(TRoundValidationError, match="mode"):
        _record(mode="swing")
    with pytest.raises(TRoundValidationError, match="round_status"):
        _record(round_status="done")


def test_leg_sides_must_match_mode():
    """正 T 必须 sell→buy, 倒 T 必须 buy→sell。"""
    with pytest.raises(TRoundValidationError, match="腿方向"):
        _record(mode="zheng_t", leg1_side="buy", leg2_side="sell")
    ok = _record(mode="dao_t", leg1_side="buy", leg2_side="sell")
    assert (ok.leg1_side, ok.leg2_side) == ("buy", "sell")


def test_hard_false_flags():
    for name in ("formal_push_allowed", "signal_universe_filtered_by_position"):
        with pytest.raises(TRoundValidationError, match=name):
            _record(**{name: True})
    with pytest.raises(TRoundValidationError, match="max_rounds_per_day"):
        _record(max_rounds_per_day=2)


def test_epsilon_cannot_drift_on_record():
    """记录上写别的 ε 等于悄悄换口径。"""
    with pytest.raises(TRoundValidationError, match="epsilon_value"):
        _record(epsilon_value=0.5)
    with pytest.raises(TRoundValidationError, match="epsilon_definition"):
        _record(epsilon_definition="bp_of_close")


def test_record_is_frozen():
    rec = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.round_status = "completed"  # type: ignore[misc]


def test_compliance_flags_hard_true():
    for name in ("not_investment_advice", "mcp_trade_forbidden", "zero_cross_ref_only"):
        with pytest.raises(TRoundValidationError, match=name):
            _record(**{name: False})


# =========================================================== KPI 硬禁


@pytest.mark.parametrize("status", ["open_leg1", "open_leg2", "incomplete", "abandoned", "expired_review", "void_data"])
def test_non_completed_cannot_count_in_realized_kpi(status):
    kwargs = {"round_status": status}
    if status in ("incomplete",):
        kwargs["incomplete_reason"] = "第二腿未达门禁"
    if status == "abandoned":
        kwargs["abandoned_reason"] = "主动放弃"
    with pytest.raises(TRoundValidationError, match="count_in_realized_kpi"):
        _record(count_in_realized_kpi=True, **kwargs)


@pytest.mark.parametrize("status", ["open_leg1", "incomplete", "abandoned", "expired_review", "void_data"])
def test_non_completed_cannot_mark_net_spread_realized(status):
    kwargs = {"round_status": status}
    if status == "incomplete":
        kwargs["incomplete_reason"] = "x"
    if status == "abandoned":
        kwargs["abandoned_reason"] = "x"
    with pytest.raises(TRoundValidationError, match="net_spread_realized|count_in_realized_kpi"):
        _record(net_spread_realized=True, **kwargs)


def test_only_completed_is_kpi_eligible():
    assert C.REALIZED_KPI_ELIGIBLE_STATUS == frozenset({"completed"})
    completed = complete_round(_open_leg2(), leg1_price=10.0, leg2_price=9.9, qty=100, leg2_source_lot="old_lot")
    assert completed.kpi_eligible is True
    for status in C.ROUND_STATUSES - {"completed"}:
        assert status not in C.REALIZED_KPI_ELIGIBLE_STATUS


def test_transition_clears_kpi_flags():
    """从 open_leg2 转出去后, KPI 标记必须是 False。

    注意 `completed` 是终态, 没有出边 —— 已经结算的轮次要作废, 只能重新构造一条
    `void_data` 记录(并保留原 t_round_id), 不能"从 completed 转过去"。这条限制是
    刻意的: 已实现 KPI 一旦计数就不该被事后改写, 否则每天都能"复核后修正"。
    """
    open2 = _open_leg2()
    voided = transition(open2, "void_data", note="数据源回滚")
    assert voided.count_in_realized_kpi is False
    assert voided.net_spread_realized is False

    completed = complete_round(open2, leg1_price=10.0, leg2_price=9.9, qty=100, leg2_source_lot="old_lot")
    with pytest.raises(InvalidTRoundTransitionError):
        transition(completed, "void_data", note="数据源回滚")


# =========================================================== 正 T 老底仓硬禁


def test_zheng_t_rejects_same_day_buy_at_construction():
    with pytest.raises(TRoundValidationError, match="old_lot"):
        _record(leg2_source_lot="same_day_buy")


def test_zheng_t_rejects_same_day_buy_on_transition():
    with pytest.raises(InvalidTRoundTransitionError, match="old_lot"):
        transition(_record(), "open_leg2", leg2_source_lot="same_day_buy")


def test_zheng_t_rejects_same_day_buy_on_complete():
    """即使绕过 transition 直接调 complete_round, 也要拒。"""
    with pytest.raises(InvalidTRoundTransitionError, match="old_lot"):
        complete_round(_open_leg2(), leg1_price=10.0, leg2_price=9.9, qty=100, leg2_source_lot="same_day_buy")


def test_dao_t_allows_same_day_buy_source():
    """倒 T 先买后卖, 第二腿来源可以是当日买入(受 T+1 约束, 只能纸面)。"""
    dao = _record(mode="dao_t", leg1_side="buy", leg2_side="sell")
    out = transition(dao, "open_leg2", leg2_source_lot="same_day_buy")
    assert out.round_status == "open_leg2"
    assert out.leg2_source_lot == "same_day_buy"


def test_unknown_source_lot_rejected():
    dao = _record(mode="dao_t", leg1_side="buy", leg2_side="sell")
    with pytest.raises(TRoundValidationError, match="leg2_source_lot"):
        transition(dao, "open_leg2", leg2_source_lot="borrowed")


# =========================================================== 状态机: 合法转移


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("open_leg1", "open_leg2"),
        ("open_leg1", "abandoned"),
        ("open_leg1", "void_data"),
        ("open_leg2", "incomplete"),
        ("open_leg2", "abandoned"),
        ("open_leg2", "void_data"),
        ("incomplete", "expired_review"),
        ("incomplete", "void_data"),
    ],
)
def test_legal_transitions(source, target):
    rec = _record(round_status=source)
    if source == "incomplete":
        rec = _record(round_status="incomplete", incomplete_reason="已记录")
    out = transition(rec, target, note="原因")
    assert out.round_status == target
    assert rec.round_status == source, "transition 必须返回新记录"


def test_completed_is_reachable_only_through_complete_round():
    with pytest.raises(InvalidTRoundTransitionError, match="complete_round"):
        transition(_open_leg2(), "completed")


def test_open_leg1_cannot_go_straight_to_completed():
    """第一腿都没走完就完成, 是典型的"账先记上"错误。"""
    with pytest.raises(InvalidTRoundTransitionError):
        complete_round(_record(), leg1_price=10.0, leg2_price=9.9, qty=100, leg2_source_lot="old_lot")


@pytest.mark.parametrize("source", ["completed", "abandoned", "expired_review", "void_data"])
def test_terminal_states_have_no_exit(source):
    rec = _record(round_status=source) if source != "abandoned" else _record(round_status="abandoned", abandoned_reason="x")
    if source == "completed":
        rec = complete_round(_open_leg2(), leg1_price=10.0, leg2_price=9.9, qty=100, leg2_source_lot="old_lot")
    for target in C.ROUND_STATUSES:
        with pytest.raises(InvalidTRoundTransitionError):
            transition(rec, target, note="x")


def test_abandoned_and_incomplete_require_reason():
    with pytest.raises(TRoundValidationError, match="note"):
        transition(_record(), "abandoned")
    with pytest.raises(TRoundValidationError, match="note"):
        transition(_record(round_status="open_leg2"), "incomplete")


def test_constructing_abandoned_without_reason_rejected():
    """绕过 `_record` 的自动补 reason, 直接构造 —— 模型层必须自己拒。"""
    with pytest.raises(TRoundValidationError, match="abandoned_reason"):
        TRoundRecord(
            t_round_id="x", symbol="600000.SH", trade_date=TODAY, mode="zheng_t",
            round_status="abandoned",
        )
    with pytest.raises(TRoundValidationError, match="incomplete_reason"):
        TRoundRecord(
            t_round_id="x", symbol="600000.SH", trade_date=TODAY, mode="zheng_t",
            round_status="incomplete",
        )


def test_void_data_keeps_reason_in_metadata():
    out = transition(_record(), "void_data", note="分钟分区缺失")
    assert out.metadata["last_void_reason"] == "分钟分区缺失"


def test_unknown_target_rejected():
    with pytest.raises(TRoundValidationError, match="未知目标状态"):
        transition(_record(), "settled")


# =========================================================== 净价差方向


def test_zheng_t_spread_direction():
    """正 T 先卖后买: 卖价 > 买价才赚。"""
    out = complete_round(
        _open_leg2(), leg1_price=10.0, leg2_price=9.9, qty=100, leg2_source_lot="old_lot"
    )
    assert out.gross_spread == pytest.approx(10.0)
    assert out.net_spread == pytest.approx(10.0)
    assert out.count_in_realized_kpi is True


def test_dao_t_spread_direction():
    """倒 T 先买后卖: 卖价 > 买价才赚 —— 腿序反了, 符号也必须反。"""
    dao = _record(mode="dao_t", leg1_side="buy", leg2_side="sell")
    open2 = transition(dao, "open_leg2", leg2_source_lot="same_day_buy")
    out = complete_round(
        open2, leg1_price=9.9, leg2_price=10.0, qty=100, leg2_source_lot="same_day_buy"
    )
    assert out.gross_spread == pytest.approx(10.0), "倒 T 应算 (leg2 - leg1) * qty"


def test_losing_round_still_completes():
    """亏了也要能完成 —— 不能只让赚钱的那轮完成, 那是幸存者偏差。"""
    out = complete_round(_open_leg2(), leg1_price=9.9, leg2_price=10.0, qty=100, leg2_source_lot="old_lot")
    assert out.round_status == "completed"
    assert out.gross_spread == pytest.approx(-10.0)


def test_cost_defaults_to_design_pending():
    out = complete_round(_open_leg2(), leg1_price=10.0, leg2_price=9.9, qty=100, leg2_source_lot="old_lot")
    assert out.cost_bp_tier == "design_pending"
    assert out.cost_amount == 0.0


def test_real_cost_tier_rejected_this_batch():
    """本批次没有分档成本实测, 不许填实测档位。"""
    with pytest.raises(TRoundValidationError, match="design_pending"):
        complete_round(
            _open_leg2(),
            leg1_price=10.0, leg2_price=9.9, qty=100, leg2_source_lot="old_lot",
            cost=RoundCost(cost_amount=5.0, cost_bp_tier="tier_a_measured"),
        )


def test_cost_deducted_from_net():
    out = complete_round(
        _open_leg2(), leg1_price=10.0, leg2_price=9.9, qty=100, leg2_source_lot="old_lot",
        cost=RoundCost(cost_amount=3.0),
    )
    assert out.net_spread == pytest.approx(out.gross_spread - 3.0)


def test_bad_prices_rejected():
    with pytest.raises(TRoundValidationError, match="正数"):
        complete_round(_open_leg2(), leg1_price=0, leg2_price=9.9, qty=100, leg2_source_lot="old_lot")


# =========================================================== Store 内存往返


def test_store_roundtrip():
    store = InMemoryTRoundRegistryStore()
    rec = _record()
    store.upsert(rec)
    assert store.get("tr.demo") == rec
    assert [r.t_round_id for r in store.list()] == ["tr.demo"]
    assert len(store) == 1


def test_store_list_filters():
    store = InMemoryTRoundRegistryStore()
    store.upsert(_record(t_round_id="a", symbol="600000.SH"))
    store.upsert(_record(t_round_id="b", symbol="000001.SZ"))
    store.upsert(_record(t_round_id="c", symbol="600000.SH", trade_date=date(2026, 10, 25)))

    assert [r.t_round_id for r in store.list(symbol="600000.SH")] == ["a", "c"]
    # list 按 (symbol, trade_date, t_round_id) 排序: 000001.SZ 排在 600000.SH 之前。
    assert [r.t_round_id for r in store.list(trade_date=TODAY)] == ["b", "a"]
    assert store.list(round_status="completed") == []


def test_store_get_returns_latest_when_no_version():
    store = InMemoryTRoundRegistryStore()
    store.upsert(_record())
    moved = transition(_record(), "open_leg2", leg2_source_lot="old_lot")
    store.upsert(moved)
    assert store.get("tr.demo").round_status == "open_leg2"
    assert store.get("tr.demo", _record().version).round_status in ("open_leg1", "open_leg2")


def test_store_get_missing_returns_none():
    assert InMemoryTRoundRegistryStore().get("nope") is None


def test_store_rejects_non_record():
    with pytest.raises(TRoundValidationError):
        InMemoryTRoundRegistryStore().upsert({"t_round_id": "x"})  # type: ignore[arg-type]


def test_store_state_progression_does_not_reconsume_quota():
    """同一条记录从 open_leg1 推到 open_leg2 不应被判超配额。"""
    store = InMemoryTRoundRegistryStore()
    store.upsert(_record())
    store.upsert(transition(_record(), "open_leg2", leg2_source_lot="old_lot"))
    assert store.count_rounds("600000.SH", TODAY) == 1


# =========================================================== 轮次上限


def test_second_round_same_symbol_same_day_rejected():
    store = InMemoryTRoundRegistryStore()
    store.upsert(_record(t_round_id="r1"))
    with pytest.raises(RoundQuotaExceeded, match="max_rounds_per_day"):
        store.upsert(_record(t_round_id="r2"))


def test_second_round_other_symbol_allowed():
    store = InMemoryTRoundRegistryStore()
    store.upsert(_record(t_round_id="r1", symbol="600000.SH"))
    store.upsert(_record(t_round_id="r2", symbol="000001.SZ"))
    assert store.count_rounds("600000.SH", TODAY) == 1
    assert store.count_rounds("000001.SZ", TODAY) == 1


def test_second_round_next_day_allowed():
    store = InMemoryTRoundRegistryStore()
    store.upsert(_record(t_round_id="r1"))
    store.upsert(_record(t_round_id="r2", trade_date=date(2026, 10, 25)))
    assert len(store) == 2


def test_completed_round_still_consumes_quota():
    """完成也算用掉一轮 —— 否则"做完再来一轮"就绕过了上限。"""
    store = InMemoryTRoundRegistryStore()
    store.upsert(_record(t_round_id="r1"))
    store.upsert(transition(_record(t_round_id="r1"), "open_leg2", leg2_source_lot="old_lot"))
    store.upsert(
        complete_round(
            _open_leg2(_record(t_round_id="r1")),
            leg1_price=10.0, leg2_price=9.9, qty=100, leg2_source_lot="old_lot",
        )
    )
    assert store.count_rounds("600000.SH", TODAY) == 1
    with pytest.raises(RoundQuotaExceeded):
        store.upsert(_record(t_round_id="r2"))


def test_void_data_does_not_consume_quota():
    """作废那一轮什么都没发生, 不该占掉当天唯一的名额。"""
    store = InMemoryTRoundRegistryStore()
    store.upsert(_record(t_round_id="r1", round_status="void_data"))
    assert store.count_rounds("600000.SH", TODAY) == 0
    store.upsert(_record(t_round_id="r2"))
    assert store.count_rounds("600000.SH", TODAY) == 1


def test_moving_round_to_another_day_rechecks_quota():
    """把已计数记录改到另一天, 相当于把名额挪过去 —— 也要查。"""
    store = InMemoryTRoundRegistryStore()
    store.upsert(_record(t_round_id="r1", trade_date=TODAY))
    store.upsert(_record(t_round_id="r2", trade_date=date(2026, 10, 25)))
    with pytest.raises(RoundQuotaExceeded):
        store.upsert(_record(t_round_id="r2", trade_date=TODAY))


def test_store_consuming_set_matches_state_machine():
    """Store 里的两个集合必须与状态机一致。"""
    assert store_module._CONSUMING == state_machine.STATUSES_CONSUMING_QUOTA


def test_check_round_quota_ignores_void():
    existing = [_record(t_round_id="x", round_status="void_data")]
    state_machine.check_round_quota(existing, "600000.SH", TODAY)  # 不抛


# =========================================================== 默认不落盘


def test_disk_store_requires_opt_in(tmp_path):
    with pytest.raises(PersistDisabledError, match="enable_persist"):
        DiskTRoundRegistryStore(tmp_path)


def test_disk_store_enabled_still_not_implemented(tmp_path):
    with pytest.raises(NotImplementedError):
        DiskTRoundRegistryStore(tmp_path, enable_persist=True)
    assert list(tmp_path.iterdir()) == []


def test_not_wired_into_app():
    backend = PKG_DIR.parents[1]
    for rel in ("app/main.py", "app/jobs/daily_pipeline.py", "app/strategy/intraday_features.py"):
        text = (backend / rel).read_text(encoding="utf-8")
        for symbol in ("DiskTRoundRegistryStore", "InMemoryTRoundRegistryStore", "t_rounds"):
            assert symbol not in text, f"{rel} 不应接线 {symbol}"


def test_store_does_not_call_partition_writer():
    tree = ast.parse((PKG_DIR / "store.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "partition_writer" not in names | attrs


# =========================================================== 隔离: 与 g / 提醒层分家


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_package_does_not_import_buy_sell_signals():
    """全包 AST: 不得 import 买卖信号包的任何模块。"""
    for path in sorted(PKG_DIR.glob("*.py")):
        for module in _imported_modules(path):
            assert "buy_sell_signals" not in module, f"{path.name} import 了 {module}"


def test_package_does_not_import_reminder_constants():
    banned = {"VWAP_BAND", "RANGE_BAND", "T_COOLDOWN_MIN"}
    for path in sorted(PKG_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert alias.name not in banned, f"{path.name}: from ... import {alias.name}"
        source = path.read_text(encoding="utf-8")
        # 常量表里出现名字是为了"禁止名单", 不算耦合; 但不得出现数值拷贝。
        for value in ("0.015", "0.003", "60)"):
            assert value not in source.replace("0.30", ""), f"{path.name} 出现疑似提醒常量数值 {value}"


def test_package_does_not_import_trade_marks_or_news_push():
    for path in sorted(PKG_DIR.glob("*.py")):
        for module in _imported_modules(path):
            assert not module.endswith("trade_marks"), f"{path.name} import 了 {module}"
            assert not module.endswith("news.push"), f"{path.name} import 了 {module}"


def test_namespace_separated_from_signals():
    assert NAMESPACE == "t_rounds"
    assert NAMESPACE != "buy_sell_signals"


def test_round_limit_field_names_are_distinct():
    """轮次上限字段名必须与 g 的推送去重字段分列。"""
    fields = {f.name for f in dataclasses.fields(TRoundRecord)}
    assert "max_rounds_per_day" in fields
    assert "push_dedupe_cooldown_until" not in fields
    assert "cooldown_until" not in fields
    assert "cooldown" not in fields
    assert C.ROUNDS_CONSUMED_FIELD == "rounds_consumed_on_trade_date"


def test_forbidden_names_absent_from_package_symbols():
    """禁止名只扫**标识符**(字段名 / 变量名 / 函数名), 不扫 docstring。

    说明文字里必须能写出"不叫 push_dedupe_cooldown_until" —— 否则没人知道这条
    规则存在。真正要拦的是把它当成一个字段名来用。
    """
    for path in sorted(PKG_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
            elif isinstance(node, ast.keyword) and node.arg:
                identifiers.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                identifiers.add(node.name)
        for banned in ("push_dedupe_cooldown_until", "cooldown_until", "cooldown"):
            assert banned not in identifiers, f"{path.name} 把 {banned} 当标识符用了"


def test_package_not_under_custom_dsa():
    assert "custom" not in PKG_DIR.parts
    assert "dsa" not in PKG_DIR.parts


# =========================================================== 持仓三态


def test_tag_position_executable_when_old_lot_available():
    tag = tag_position(old_lot_qty_available=500)
    assert isinstance(tag, PositionTag)
    assert tag.tag == "executable"
    assert tag.can_execute_zheng_t is True


def test_tag_position_tomorrow_ref_for_same_day_buy():
    tag = tag_position(old_lot_qty_available=0, same_day_buy_qty=300)
    assert tag.tag == "tomorrow_ref"
    assert tag.can_execute_zheng_t is False


def test_tag_position_observe_only_when_empty():
    for kwargs in (
        {"old_lot_qty_available": None},
        {"old_lot_qty_available": 0},
        {"old_lot_qty_available": 0, "same_day_buy_qty": 0},
    ):
        assert tag_position(**kwargs).tag == "observe_only"


def test_tag_position_prefers_old_lot():
    """既有老底仓又有当日买入 → executable(老底仓今天就能卖)。"""
    tag = tag_position(old_lot_qty_available=100, same_day_buy_qty=900)
    assert tag.tag == "executable"
    assert tag.position_qty == 1000


def test_tag_position_zero_lot_is_not_executable():
    tag = tag_position(old_lot_qty_available=0)
    assert tag.can_execute_zheng_t is False


def test_tag_position_rejects_negative():
    with pytest.raises(TRoundValidationError, match="不能为负"):
        tag_position(old_lot_qty_available=-1)


def test_signal_universe_not_filtered_by_position():
    assert C.SIGNAL_UNIVERSE_FILTERED_BY_POSITION is False
    assert _record().signal_universe_filtered_by_position is False


# =========================================================== 合规 / 文案


def test_disclaimer_present_in_serialized_record():
    payload = _record().to_dict()
    assert payload["disclaimer"] == DISCLAIMER_TEXT
    assert "不构成投资建议" in payload["disclaimer"]
    assert "人工确认" in payload["disclaimer"]


def test_package_docstrings_carry_disclaimer():
    import app.t_rounds as pkg

    assert "不构成投资建议" in (pkg.__doc__ or "")
    for path in sorted(PKG_DIR.glob("*.py")):
        assert "不构成投资建议" in path.read_text(encoding="utf-8"), f"{path.name} 缺合规脚注"


NEGATION_MARKERS = ("不得", "不许", "不要", "不是", "皆未", "均未", "未测通", "都不该", "才是错")


def test_no_claim_that_anything_is_verified():
    """全包不得**断言**"已测通"。

    允许出现否定语境("不得声称已测通")—— 那是护栏本身; 要拦的是把它当结论写。
    逐行判断该行有没有否定标记, 比全文匹配靠谱: 全文匹配会连护栏一起拦掉,
    逼着人把"别声称已测通"这句删掉, 反而更危险。
    """
    banned = ("已测通", "已通过验证", "实盘可卖已通", "特征回测已通")
    for path in sorted(PKG_DIR.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for phrase in banned:
                if phrase not in line:
                    continue
                assert any(marker in line for marker in NEGATION_MARKERS), (
                    f"{path.name}:{lineno} 把「{phrase}」写成了结论: {line.strip()}"
                )


def test_virtual_position_flag_does_not_claim_real_availability():
    rec = _record()
    assert rec.virtual_position_flag is True
    text = (PKG_DIR / "models.py").read_text(encoding="utf-8")
    assert "不得" in text and "实盘可卖老股" in text


# =========================================================== 软提醒落进代码


def test_soft_reminders_present_in_constants():
    text = (PKG_DIR / "constants.py").read_text(encoding="utf-8")
    assert "同尺后再 VWAP" in text
    assert "禁冒充竞价" in text or "AUCTION_IMPERSONATION_FORBIDDEN" in text
    assert "2025, 7, 25" in text


def test_research_window_start_is_2025_07_25():
    assert RESEARCH_WINDOW_START == date(2025, 7, 25)


def test_auction_impersonation_forbidden_constant():
    assert C.AUCTION_IMPERSONATION_FORBIDDEN is True
    assert C.MINUTE_EXPORT_EARLIEST == "09:30"


def test_no_auction_impersonation_helpers():
    """不得出现把 09:30 标成 09:25 的辅助函数 / 默认参数。"""
    banned_names = {"as_auction", "relabel_0925", "auction_from_open", "open_1m_as_0915", "shift_to_0925"}
    for path in sorted(PKG_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not (names & banned_names), f"{path.name} 出现冒充竞价辅助: {sorted(names & banned_names)}"


def test_leg_state_validation():
    with pytest.raises(TRoundValidationError, match="side"):
        LegState(side="hold")
    with pytest.raises(TRoundValidationError, match="qty"):
        LegState(side="buy", qty=0)
    ok = LegState(side="buy", status="filled", qty=100, price=10.0, ts=datetime(2026, 10, 24, 10, 0, tzinfo=CN_TZ))
    assert ok.filled is True
    with pytest.raises(TRoundValidationError, match="带时区"):
        LegState(side="buy", ts=datetime(2026, 10, 24, 10, 0))
