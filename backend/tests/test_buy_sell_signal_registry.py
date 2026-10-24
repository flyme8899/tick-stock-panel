"""买卖信号注册表 / 状态机 / Store 骨架测试 (批次 20261024g 任务①)。

不依赖 Pro 分钟 capability, 不访问真实 `/data/tsp-reports`, 不联网。

本文件守三件事:

1. 状态机: 合法转移、**禁止自动晋升**、`limit_reopen` 不能 publishable、
   `manual_publish_required` 默认 true
2. Store: 内存往返、publish_pointer 互斥、默认不落盘
3. 分家: 本包不得 import 提醒层常量, 路径不得落在 `custom/dsa`
"""
from __future__ import annotations

import ast
import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest
from app.buy_sell_signals import (
    BONFERRONI_M,
    BONFERRONI_SCHEME_ID,
    DISCLAIMER_TEXT,
    MANUAL_PUBLISH_CHECKLIST,
    DiskSignalRegistryStore,
    InMemorySignalRegistryStore,
    ManualPublishRejected,
    PersistDisabledError,
    PublishPointerConflict,
    SignalRecord,
    SignalRegistryError,
    SignalStateError,
    SignalTransitionError,
    SignalValidationError,
    design_version,
    manual_publish,
    transition,
)
from app.buy_sell_signals import state_machine
from app.buy_sell_signals import store as store_module
from app.market_time import cn_now

PKG_DIR = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"


def _record(**kwargs) -> SignalRecord:
    base = {
        "signal_id": "sig.demo",
        "display_name": "演示信号",
        "family": "composite",
        "event_class": "other",
        "spec_ref": "PHASE1 §2",
    }
    base.update(kwargs)
    return SignalRecord(**base)


def _full_checklist() -> dict[str, bool]:
    return {item: True for item in MANUAL_PUBLISH_CHECKLIST}


# =========================================================== 记录字段默认值


def test_manual_publish_required_defaults_true():
    """人工闸默认必须关着。默认 False 等于每条新信号天然可发布。"""
    assert _record().manual_publish_required is True


def test_hard_true_flags_cannot_be_false():
    for name in ("auto_promote_forbidden", "not_investment_advice", "mcp_trade_forbidden"):
        with pytest.raises(SignalValidationError, match=name):
            _record(**{name: False})


def test_default_status_is_candidate_and_not_pushable():
    rec = _record()
    assert rec.status == "candidate"
    assert rec.push_gate == "manual_pending"
    assert rec.push_eligible is False
    assert rec.publish_pointer is False


def test_version_string_carries_bonferroni_metadata():
    version = design_version()
    assert BONFERRONI_SCHEME_ID in version
    assert f"m{BONFERRONI_M}" in version
    assert _record().version == version


def test_health_and_weighting_are_pending_by_default():
    """健康与加权都还没做完, 默认必须是 pending, 不能是"已通过"。"""
    rec = _record()
    assert rec.health_veto_rule_version == "design_pending"
    assert rec.health_veto_hit is None
    assert rec.weighting_status == "design_pending"
    assert rec.w_quality is None and rec.w_value is None


def test_unknown_family_or_event_class_rejected():
    with pytest.raises(SignalValidationError, match="family"):
        _record(family="magic")
    with pytest.raises(SignalValidationError, match="event_class"):
        _record(event_class="earnings_drift")


def test_naive_datetime_rejected():
    from datetime import datetime

    with pytest.raises(SignalValidationError, match="带时区"):
        _record(created_at=datetime(2026, 10, 24, 9, 30))


def test_publishable_requires_manual_signature():
    """构造层面再守一次: 没有签字人就没有 publishable 记录。"""
    with pytest.raises(SignalValidationError, match="manual_published_at"):
        _record(status="publishable", push_gate="armed")


# =========================================================== 噪声分列


def test_auction_bucket_requires_auction_event_class():
    with pytest.raises(SignalValidationError, match="只属于 event_class='auction'"):
        _record(event_class="other", auction_stat_bucket="auction_0925")


def test_auction_bucket_is_closed_set():
    with pytest.raises(SignalValidationError, match="只允许 'auction_0925'"):
        _record(event_class="auction", auction_stat_bucket="auction_0930")


def test_auction_record_cannot_carry_post_open_bucket():
    """混桶的唯一下手处: auction 类信号带上开盘后桶。必须拒。"""
    with pytest.raises(SignalValidationError, match="不得携带 post_open_noise_bucket"):
        _record(
            event_class="auction",
            auction_stat_bucket="auction_0925",
            post_open_noise_bucket="open_0930_1000",
        )


def test_post_open_bucket_is_closed_set():
    with pytest.raises(SignalValidationError, match="post_open_noise_bucket"):
        _record(post_open_noise_bucket="lunch_break")


def test_serialized_record_exposes_noise_mix_forbidden():
    payload = _record(post_open_noise_flag=True, post_open_noise_bucket="open_0930_1000").to_dict()
    assert payload["noise_mix_forbidden"] is True
    assert payload["post_open_noise_bucket"] == "open_0930_1000"
    assert payload["auction_stat_bucket"] is None


# =========================================================== 状态机: 合法转移


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("candidate", "observe"),
        ("candidate", "retired"),
        ("candidate", "data_abnormal"),
        ("observe", "retired"),
        ("observe", "data_abnormal"),
        ("data_abnormal", "retired"),
        ("publishable", "observe"),
        ("publishable", "retired"),
    ],
)
def test_legal_transitions(source, target):
    rec = _record(status=source, **({"manual_publisher": "qa", "manual_published_at": cn_now()} if source == "publishable" else {}))
    out = transition(rec, target, note="原因" if target == "data_abnormal" else None)
    assert out.status == target
    assert out is not rec
    assert rec.status == source, "transition 必须返回新记录, 不得原地改"


def test_publishable_to_observe_clears_pointer_and_gate():
    rec = manual_publish(
        transition(_record(), "observe"),
        publisher="qa",
        checklist=_full_checklist(),
        note="ok",
    )
    assert rec.status == "publishable" and rec.publish_pointer is True
    out = transition(rec, "observe")
    assert out.publish_pointer is False
    assert out.push_gate == "manual_pending"
    assert out.push_eligible is False


def test_data_abnormal_requires_note():
    with pytest.raises(SignalValidationError, match="note"):
        transition(_record(), "data_abnormal")
    with pytest.raises(SignalValidationError, match="note"):
        transition(_record(), "data_abnormal", note="   ")


def test_data_abnormal_blocks_push_gate():
    out = transition(_record(), "data_abnormal", note="北向数据缺失")
    assert out.push_gate == "blocked"
    assert out.metadata["last_transition_note"] == "北向数据缺失"


def test_unknown_target_status_rejected():
    with pytest.raises(SignalValidationError, match="未知目标状态"):
        transition(_record(), "archived")


# =========================================================== 状态机: 硬禁


@pytest.mark.parametrize("source", ["candidate", "observe", "data_abnormal", "retired"])
def test_no_automatic_path_to_publishable(source):
    """任何状态调用 transition('publishable') 都必须失败。

    这是本批次最要紧的一条: 一旦有自动路径, 人工闸就形同虚设, 而且不会有任何
    报错提示"这里本不该通"。
    """
    rec = _record(status=source)
    with pytest.raises(SignalTransitionError, match="manual_publish"):
        transition(rec, "publishable")


def test_transition_table_has_no_inbound_edge_to_publishable():
    """守表本身: 任何人给转移表加一条通往 publishable 的边, 这条会红。"""
    for source, targets in state_machine.ALLOWED_TRANSITIONS.items():
        assert "publishable" not in targets, f"{source} -> publishable 出现在转移表里"


def test_limit_reopen_cannot_be_published_even_with_full_checklist():
    rec = _record(event_class="limit_reopen")
    observing = transition(rec, "observe")
    with pytest.raises(ManualPublishRejected, match="limit_reopen"):
        manual_publish(observing, publisher="qa", checklist=_full_checklist(), note="x")


def test_limit_reopen_cannot_enter_publishable_via_transition():
    observing = transition(_record(event_class="limit_reopen"), "observe")
    with pytest.raises(SignalTransitionError):
        transition(observing, "publishable")


# =========================================================== 人工发布闸


def test_manual_publish_success_marks_signature_and_pointer():
    observing = transition(_record(), "observe")
    published = manual_publish(
        observing,
        publisher="  researcher-a  ",
        checklist=_full_checklist(),
        note="  回测 2019-2025, 无未来函数  ",
    )
    assert published.status == "publishable"
    assert published.push_gate == "armed"
    assert published.publish_pointer is True
    assert published.manual_publisher == "researcher-a", "两端空格应被去掉"
    assert published.manual_note == "回测 2019-2025, 无未来函数"
    assert published.manual_published_at is not None
    assert published.manual_publish_required is True, "发布后人工闸标记不回退"
    assert published.updated_at >= observing.updated_at


def test_manual_publish_rejects_missing_checklist_item():
    observing = transition(_record(), "observe")
    partial = _full_checklist()
    partial["no_lookahead"] = False
    with pytest.raises(ManualPublishRejected, match="no_lookahead"):
        manual_publish(observing, publisher="qa", checklist=partial, note="x")


def test_manual_publish_rejects_missing_key_entirely():
    """缺项(而非 False)同样拒 —— 不能靠"没传就是通过"。"""
    observing = transition(_record(), "observe")
    partial = _full_checklist()
    del partial["sample_adequate"]
    with pytest.raises(ManualPublishRejected, match="sample_adequate"):
        manual_publish(observing, publisher="qa", checklist=partial, note="x")


def test_manual_publish_rejects_truthy_but_not_true():
    """1 / "yes" 不算确认。只认真正的 True, 免得 "false" 这种字符串混进来。"""
    observing = transition(_record(), "observe")
    checklist = _full_checklist()
    checklist["risk_reviewed"] = "yes"  # type: ignore[assignment]
    with pytest.raises(ManualPublishRejected, match="risk_reviewed"):
        manual_publish(observing, publisher="qa", checklist=checklist, note="x")


def test_manual_publish_rejects_unknown_checklist_key():
    """拼错名字的 checklist 看起来完整, 实际有一项没确认。必须拒。"""
    observing = transition(_record(), "observe")
    checklist = _full_checklist()
    checklist["no_look_ahead"] = True
    with pytest.raises(ManualPublishRejected, match="未知项"):
        manual_publish(observing, publisher="qa", checklist=checklist, note="x")


def test_manual_publish_rejects_blank_publisher_or_note():
    observing = transition(_record(), "observe")
    with pytest.raises(ManualPublishRejected, match="publisher"):
        manual_publish(observing, publisher=" ", checklist=_full_checklist(), note="x")
    with pytest.raises(ManualPublishRejected, match="note"):
        manual_publish(observing, publisher="qa", checklist=_full_checklist(), note=" ")


def test_manual_publish_rejects_candidate():
    """candidate 必须先经一轮 observe。"""
    with pytest.raises(ManualPublishRejected, match="candidate"):
        manual_publish(_record(), publisher="qa", checklist=_full_checklist(), note="x")


def test_data_abnormal_cannot_go_back_to_candidate_silently():
    abnormal = transition(_record(), "data_abnormal", note="财务表为空")
    with pytest.raises(SignalTransitionError):
        transition(abnormal, "candidate")


def test_rerun_from_abnormal_requires_operator_and_reason():
    abnormal = transition(_record(), "data_abnormal", note="财务表为空")
    with pytest.raises(SignalValidationError, match="operator"):
        state_machine.rerun_from_abnormal(abnormal, operator=" ", reason="已补数据")
    with pytest.raises(SignalValidationError, match="reason"):
        state_machine.rerun_from_abnormal(abnormal, operator="qa", reason=" ")
    out = state_machine.rerun_from_abnormal(abnormal, operator="qa", reason="已补数据")
    assert out.status == "candidate"
    assert out.push_gate == "manual_pending"
    assert out.metadata["last_rerun"]["operator"] == "qa"


def test_rerun_rejects_non_abnormal_record():
    with pytest.raises(SignalTransitionError):
        state_machine.rerun_from_abnormal(_record(), operator="qa", reason="x")


def test_cannot_arm_push_gate_before_publishable():
    with pytest.raises(SignalStateError, match="armed"):
        state_machine.set_push_gate(_record(), "armed")


def test_push_eligible_requires_all_three_conditions():
    published = manual_publish(
        transition(_record(), "observe"),
        publisher="qa",
        checklist=_full_checklist(),
        note="x",
    )
    assert published.push_eligible is True

    cooling = published.with_fields(push_dedupe_cooldown_until=cn_now() + timedelta(minutes=10))
    assert cooling.push_eligible is False, "去重冷却期内不得推送"
    assert cooling.push_dedupe_cooldown_active is True

    expired = published.with_fields(push_dedupe_cooldown_until=cn_now() - timedelta(minutes=1))
    assert expired.push_eligible is True


def test_push_gate_not_armed_blocks_push_eligibility():
    published = manual_publish(
        transition(_record(), "observe"),
        publisher="qa",
        checklist=_full_checklist(),
        note="x",
    )
    assert state_machine.set_push_gate(published, "cooldown").push_eligible is False


def test_record_is_frozen():
    """状态只能经状态机改。允许原地改字段等于允许绕开所有检查。"""
    rec = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.status = "publishable"  # type: ignore[misc]


# =========================================================== Store: 内存往返


def test_store_roundtrip_upsert_get_list():
    store = InMemorySignalRegistryStore()
    a = _record(signal_id="sig.a")
    b = _record(signal_id="sig.b", version="v2", family="minute_event", event_class="intraday_volume")
    store.upsert(a)
    store.upsert(b)

    assert store.get("sig.a") == a
    assert store.get("sig.b", "v2") == b
    assert [r.signal_id for r in store.list()] == ["sig.a", "sig.b"]
    assert len(store) == 2


def test_store_list_filters_by_status():
    store = InMemorySignalRegistryStore()
    store.upsert(_record(signal_id="sig.a"))
    store.upsert(transition(_record(signal_id="sig.b"), "observe"))
    assert [r.signal_id for r in store.list(status="observe")] == ["sig.b"]
    assert store.list(status="publishable") == []


def test_store_get_missing_returns_none():
    assert InMemorySignalRegistryStore().get("nope") is None


def test_store_publish_pointer_is_mutually_exclusive():
    store = InMemorySignalRegistryStore()
    store.upsert(_record(signal_id="sig.a", version="v1"))
    store.upsert(_record(signal_id="sig.a", version="v2"))

    store.set_publish_pointer("sig.a", "v1")
    assert store.get("sig.a", "v1").publish_pointer is True

    store.set_publish_pointer("sig.a", "v2")
    assert store.get("sig.a", "v1").publish_pointer is False
    assert store.get("sig.a", "v2").publish_pointer is True
    pointers = [r for r in store.list() if r.publish_pointer]
    assert len(pointers) == 1, "同一 signal_id 至多一条 publish_pointer"


def test_store_get_without_version_prefers_pointer():
    """不传 version 时优先返回指针那一版, 而不是随便挑一条。"""
    store = InMemorySignalRegistryStore()
    store.upsert(_record(signal_id="sig.a", version="v1").with_fields(updated_at=cn_now()))
    store.upsert(
        _record(signal_id="sig.a", version="v2").with_fields(
            updated_at=cn_now() + timedelta(hours=1)
        )
    )
    assert store.get("sig.a").version == "v2", "无指针时取最新"
    store.set_publish_pointer("sig.a", "v1")
    assert store.get("sig.a").version == "v1", "有指针时取指针版"


def test_store_pointer_unknown_version_raises():
    store = InMemorySignalRegistryStore()
    with pytest.raises(SignalRegistryError, match="没有版本"):
        store.set_publish_pointer("sig.a", "v9")


def test_store_rejects_second_pointer_via_upsert():
    """绕过 set_publish_pointer 手塞一条带指针的记录, 同样要被拒。"""
    store = InMemorySignalRegistryStore()
    store.upsert(_record(signal_id="sig.a", version="v1", publish_pointer=True))
    with pytest.raises(PublishPointerConflict):
        store.upsert(_record(signal_id="sig.a", version="v2", publish_pointer=True))


def test_store_upsert_rejects_non_record():
    with pytest.raises(SignalValidationError):
        InMemorySignalRegistryStore().upsert({"signal_id": "x"})  # type: ignore[arg-type]


def test_store_versions_of():
    store = InMemorySignalRegistryStore()
    for ver in ("v2", "v1"):
        store.upsert(_record(signal_id="sig.a", version=ver))
    assert store.versions_of("sig.a") == ["v1", "v2"]


# =========================================================== Store: 默认不落盘


def test_disk_store_requires_explicit_opt_in(tmp_path):
    with pytest.raises(PersistDisabledError, match="enable_persist"):
        DiskSignalRegistryStore(tmp_path)


def test_disk_store_even_when_enabled_is_not_implemented(tmp_path):
    """显式打开也只是拿到 NotImplementedError —— 本批次没有落盘实现。"""
    with pytest.raises(NotImplementedError):
        DiskSignalRegistryStore(tmp_path, enable_persist=True)
    assert list(tmp_path.iterdir()) == [], "不得写任何文件"


def test_disk_store_is_not_wired_into_app():
    """本批次禁止接线: main.py / daily_pipeline 不得引用磁盘 Store。"""
    backend = PKG_DIR.parents[1]
    for rel in ("app/main.py", "app/jobs/daily_pipeline.py"):
        text = (backend / rel).read_text(encoding="utf-8")
        assert "DiskSignalRegistryStore" not in text, f"{rel} 不应引用磁盘 Store"
        assert "buy_sell_signals" not in text, f"{rel} 本批次不应接线 buy_sell_signals"


def test_store_module_does_not_use_partition_writer():
    """磁盘空壳不得调用 partition_writer 去写生产分区。"""
    tree = ast.parse((PKG_DIR / "store.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "partition_writer" not in names | attrs


# =========================================================== 分家: 提醒层常量


REMINDER_CONSTANTS = ("VWAP_BAND", "RANGE_BAND", "T_COOLDOWN_MIN")


@pytest.mark.parametrize("constant", REMINDER_CONSTANTS)
def test_package_does_not_import_reminder_constants(constant):
    """任何本包文件 import 提醒层常量都会让这条红。

    这是本批次最容易被"顺手复用"污染的地方: 门禁里出现一个 0.015, 看起来无害,
    但它把"推送静音"和"信号判定"绑在了一起。
    """
    for path in sorted(PKG_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert f"import {constant}" not in source, f"{path.name} 引入了 {constant}"
        assert f" {constant}," not in source.split('"""')[0], f"{path.name} 引入了 {constant}"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert alias.name != constant, f"{path.name}: from ... import {constant}"


def test_package_does_not_import_trade_marks_or_news_push():
    for path in sorted(PKG_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for module in modules:
                # disclaimer.py 里提到 trade_marks 只出现在注释/docstring, 不构成 import。
                assert not module.endswith("trade_marks"), f"{path.name} import 了 {module}"
                assert not module.endswith("news.push"), f"{path.name} import 了 {module}"


def test_constant_names_do_not_collide_with_reminder_layer():
    """本包常量名不得与提醒层同名, 避免被 from ... import 混用。"""
    from app.news import push as news_push
    from app.services import trade_marks

    ours = {name for name in vars(__import__("app.buy_sell_signals.constants", fromlist=["x"])) if name.isupper()}
    theirs = {
        name
        for module in (news_push, trade_marks)
        for name in vars(module)
        if name.isupper()
    }
    assert not (ours & theirs), f"常量重名: {sorted(ours & theirs)}"


def test_package_is_not_under_custom_dsa():
    assert "custom" not in PKG_DIR.parts
    assert "dsa" not in PKG_DIR.parts


def test_cooldown_field_is_push_dedupe_named():
    """冷却字段必须叫 push_dedupe_cooldown_until, 不得用泛名。"""
    fields = {f.name for f in dataclasses.fields(SignalRecord)}
    assert "push_dedupe_cooldown_until" in fields
    assert "cooldown_until" not in fields
    assert "cooldown" not in fields


# =========================================================== 合规脚注


def test_disclaimer_present_in_serialized_record():
    payload = _record().to_dict()
    assert payload["disclaimer"] == DISCLAIMER_TEXT
    assert "不构成投资建议" in payload["disclaimer"]
    assert "人工确认" in payload["disclaimer"]


def test_disclaimer_text_is_stable():
    assert DISCLAIMER_TEXT == "仅供研究，不构成投资建议；执行须人工确认。"


def test_package_docstring_carries_disclaimer():
    import app.buy_sell_signals as pkg

    assert "不构成投资建议" in (pkg.__doc__ or "")
    for path in sorted(PKG_DIR.glob("*.py")):
        assert "不构成投资建议" in path.read_text(encoding="utf-8"), f"{path.name} 缺合规脚注"


def test_store_protocol_is_structural():
    """没有继承也能当 Store 用(结构子类型), 与 contracts.py 的风格一致。"""
    store: store_module.SignalRegistryStore = InMemorySignalRegistryStore()
    store.upsert(_record())
    assert store.get("sig.demo") is not None
