"""校准闸的**可见性 AND 矩阵**（批次 20261026gbsscalib · P-calib）。

`test_buy_sell_signal_calib.py` 测的是"登记本身对不对"，本文件测的是"闸到底按什么放行"。
两者分开是因为它们的失败含义不同：那边红了说明门槛写错了，这边红了说明**监控会显示
不该显示的东西**——或者该显示的没显示。

## 矩阵（M1–M12）

| # | 场景 | 期望 |
|---|---|---|
| M1 | publishable + **无** calib 登记 | 不可见 |
| M2 | publishable + pending / failed | 不可见 |
| M3 | candidate + passed | 不可见 |
| M4 | limit_reopen + passed（定义被手改 publishable） | 不可见 |
| M5 | fixture 只进测例；生产盘店无夹具写入口 | 结构上不成立 |
| M6 | 改阈值参数但伪称同一 `freq_lock_version` | 三键对得上，但**不是同一套口径** |
| M7 | `sample_n=29` 写 passed | 拒绝 |
| M8 | `cost_bp_roundtrip≠18.2` 写 passed | 拒绝 |
| M9 | 空态文案改成「今天没信号」 | 单测失败 |
| M10 | `VOLUME_BREAKOUT_TRIGGERING_ENABLED` 设 True | 常量测失败 |
| M11 | `SignalHit` 加 `calibrated=True` 并仅靠它可见 | 字段不存在; 闸不认 |
| M12 | 仅靠 `regime_caliber_version` 有值宣称已校准 | 不可见 |

M7 / M8 的纯函数部分在 `test_buy_sell_signal_calib.py`；这里补的是"它挡住之后,
命中确实不可见"这一端的连通性。

全部用夹具 + `tmp_path`, **不依赖**真实行情、真实回测库、真实 webhook。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.buy_sell_signals import constants as C  # noqa: N812
from app.buy_sell_signals import hits_fixtures as fx
from app.buy_sell_signals.calib_models import CalibValidationError
from app.buy_sell_signals.calib_store import CalibIndex, InMemoryCalibStore, build_index
from app.buy_sell_signals.hits_models import SignalHit
from app.buy_sell_signals.hits_visibility import (
    EXPOSED_FAMILIES,
    VisibilityReject,
    build_response,
    visible_hits,
)
from app.buy_sell_signals.store import InMemorySignalRegistryStore

SCOPE = frozenset({"000001", "600000"})


def _registry(*records) -> InMemorySignalRegistryStore:
    reg = InMemorySignalRegistryStore()
    for rec in records:
        reg.upsert(rec)
    return reg


def _full_registry() -> InMemorySignalRegistryStore:
    return _registry(
        fx.publishable_definition(),
        fx.candidate_definition(),
        fx.observe_definition(),
        fx.limit_reopen_definition(),
        fx.daily_fundamental_definition(),
        fx.composite_definition(),
    )


def _index(*records) -> CalibIndex:
    return CalibIndex(list(records))


def _passed_all() -> CalibIndex:
    return _index(
        fx.passed_calib(signal_id="fx.pub.intraday_volume"),
        fx.passed_calib(signal_id="fx.pub.daily_quality_value"),
        fx.passed_calib(signal_id="fx.cand.intraday_volume"),
        fx.passed_calib(signal_id="fx.limit_reopen"),
    )


def _live(hit):
    """把夹具命中转成 `source.mode="live"`。

    P-bss-p3a 可见性闸只放 live（见 `hits_visibility.VisibilityReject.NON_LIVE_MODE`），
    而 `hits_fixtures` 默认造的是 `mode=fixture`。所以本文件里凡「期望可见」的命中都要
    过这一层 —— 否则测的就不是"命中能不能显示"，而是"夹具能不能显示"（后者恒否）。

    要测 fixture / shadow 被挡的负向路径时，**不要**用它，直接在夹具上 `replace` 回
    `mode="fixture"` / `"shadow"`。
    """
    return replace(hit, source=replace(hit.source, mode="live"))


# ======================================================= M1 · 无登记


def test_m1_publishable_without_any_calibration_is_invisible():
    """M1: 定义 publishable、命中带口径标签, 但 calib 里**一条都没有** → 不可见。"""
    filtered = visible_hits(
        [_live(fx.calibrated_hit())],
        _full_registry(),
        scope_symbols=SCOPE,
        calib=CalibIndex([]),
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.CALIB_NOT_PASSED) == 1


def test_m1b_calib_none_means_empty_not_skip():
    """`calib=None` 等同于空索引, **不是**"跳过这道闸"。"""
    filtered = visible_hits([_live(fx.calibrated_hit())], _full_registry(), scope_symbols=SCOPE, calib=None)
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.CALIB_NOT_PASSED) == 1


def test_m1c_publishable_without_freq_lock_label_is_invisible():
    """连按哪套口径算的都不知道（标签缺失）→ 不可见, 不许拿"当前戳"顶上去。"""
    filtered = visible_hits(
        [_live(fx.unlabelled_hit())], _full_registry(), scope_symbols=SCOPE, calib=_passed_all()
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.CALIB_NOT_PASSED) == 1


# ======================================================= M2 · pending / failed


@pytest.mark.parametrize("factory", [fx.pending_calib, fx.failed_calib])
def test_m2_publishable_with_pending_or_failed_is_invisible(factory):
    """M2: `pending` / `failed` 与"没有登记"**完全等价** —— 一条都不可见。"""
    filtered = visible_hits(
        [_live(fx.calibrated_hit())],
        _full_registry(),
        scope_symbols=SCOPE,
        calib=_index(factory()),
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.CALIB_NOT_PASSED) == 1


def test_m2b_pending_does_not_show_as_a_softer_state():
    """不存在"显示但标灰"这种中间态 —— 闸只有放行/不放行。"""
    filtered = visible_hits(
        [_live(fx.calibrated_hit())],
        _full_registry(),
        scope_symbols=SCOPE,
        calib=_index(fx.pending_calib()),
    )
    payload = build_response(filtered)
    assert payload["hits"] == []
    assert payload["count"] == 0


# ======================================================= M3 · candidate + passed


def test_m3_candidate_with_passed_is_still_invisible():
    """M3: 给 candidate 配齐 `passed` 也不可见 —— 定义状态那道闸没有被新闸削弱。"""
    filtered = visible_hits(
        [_live(fx.candidate_calibrated_hit())],
        _full_registry(),
        scope_symbols=SCOPE,
        calib=_passed_all(),
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.NOT_PUBLISHABLE) == 1
    assert filtered.rejected.get(VisibilityReject.CALIB_NOT_PASSED) is None


# ======================================================= M4 · limit_reopen


def test_m4_limit_reopen_with_passed_is_still_invisible_even_if_definition_forged():
    """M4: 定义被手改成 publishable + 登记 passed, 炸板/回封**仍然**不可见。"""
    forged = fx.publishable_definition(signal_id="fx.limit_reopen")
    assert forged.status == "publishable"

    filtered = visible_hits(
        [_live(fx.limit_reopen_calibrated_hit())], _registry(forged), scope_symbols=SCOPE, calib=_passed_all()
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.LIMIT_REOPEN) == 1


# ======================================================= 可见的正例


def test_publishable_with_passed_is_visible():
    """AND 的另一半: 两条件都满足时必须可见 —— 否则这道闸就是个"全隐藏"开关。"""
    filtered = visible_hits(
        [_live(fx.calibrated_hit())], _full_registry(), scope_symbols=SCOPE, calib=_passed_all()
    )
    assert [h.hit_id for h in filtered.hits] == ["fx.hit.pub.000001"]


def test_visible_count_reports_passed_keys():
    filtered = visible_hits(
        [_live(fx.calibrated_hit())], _full_registry(), scope_symbols=SCOPE, calib=_passed_all()
    )
    assert filtered.calib_passed_keys == 4


def test_daily_fundamental_still_needs_both_scope_and_passed():
    """基本面: 范围闸与校准闸是**并列**的两道, 缺任一条都不可见。"""
    hit = _live(fx.daily_calibrated_hit(symbol="600000"))
    calib = _index(fx.passed_calib(signal_id="fx.pub.daily_quality_value"))

    inside = visible_hits([hit], _full_registry(), scope_symbols=frozenset({"600000"}), calib=calib)
    assert len(inside.hits) == 1

    outside = visible_hits([hit], _full_registry(), scope_symbols=frozenset({"999999"}), calib=calib)
    assert outside.hits == []
    assert outside.rejected.get(VisibilityReject.OUT_OF_SCOPE) == 1


def test_daily_fundamental_in_scope_but_uncalibrated_is_invisible():
    hit = _live(fx.daily_calibrated_hit(symbol="600000"))
    filtered = visible_hits(
        [hit], _full_registry(), scope_symbols=frozenset({"600000"}), calib=CalibIndex([])
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.CALIB_NOT_PASSED) == 1


def test_event_hit_still_ignores_scope_but_not_calibration():
    """事件命中不过范围闸, 但仍要过校准闸 —— 两道闸的适用面不同, 不互相顶替。"""
    hit = _live(fx.calibrated_hit(symbol="999999"))
    ok = visible_hits(
        [hit], _full_registry(), scope_symbols=frozenset({"600000"}), calib=_passed_all()
    )
    assert [h.symbol for h in ok.hits] == ["999999"]

    blocked = visible_hits(
        [hit], _full_registry(), scope_symbols=frozenset({"600000"}), calib=CalibIndex([])
    )
    assert blocked.hits == []


# ======================================================= M6 · 伪称同一版本


def test_m6_same_version_label_but_different_params_is_a_different_caliber():
    """M6: 改阈值参数但伪称同一 `freq_lock_version`。

    三键对得上（标签没改），但指纹不同 —— 戳侧的 `matches()` 必须返回 False，
    说明"同一标签"不足以宣称同一套口径。旧登记的 `passed` **不能**给新参数背书。
    """
    old_stamp = fx.freq_lock_stamp()
    sneaky = fx.freq_lock_stamp(params=fx.FIXTURE_FREQ_PARAMS_TWEAKED)

    assert sneaky.freq_lock_version == old_stamp.freq_lock_version  # 标签一致
    assert sneaky.align_key == old_stamp.align_key  # 三键一致
    assert sneaky.fingerprint != old_stamp.fingerprint  # 但指纹不一致
    assert old_stamp.matches(sneaky) is False


def test_m6b_a_record_whose_version_does_not_match_the_stamp_cannot_align():
    """登记写的是 `freq_lock_v9`，命中的是 `freq_lock_v1` → 三键对不上 → 不可见。"""
    stale = fx.passed_calib(freq_lock_version="freq_lock_v9")
    filtered = visible_hits(
        [_live(fx.calibrated_hit())], _full_registry(), scope_symbols=SCOPE, calib=_index(stale)
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.CALIB_NOT_PASSED) == 1


def test_m6c_definition_version_mismatch_blocks_alignment():
    """定义改版后旧 `passed` 不许顺延: 登记的 `definition_version` 是 v1，命中是 v2。"""
    old = fx.passed_calib(definition_version="fixture_v1")
    new_hit = _live(fx.calibrated_hit(definition_version="fixture_v2"))
    filtered = visible_hits([new_hit], _full_registry(), scope_symbols=SCOPE, calib=_index(old))
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.CALIB_NOT_PASSED) == 1


# ======================================================= M7 / M8 连通性


def test_m7_sample_n_29_blocks_the_write_and_the_hit_never_shows():
    """M7 端到端: 想写 sample_n=29 的 passed → 写入被拒 → 索引里没有 → 命中不可见。"""
    with pytest.raises(CalibValidationError):
        fx.passed_calib(sample_n=29)

    store = InMemoryCalibStore()
    filtered = visible_hits(
        [_live(fx.calibrated_hit())], _full_registry(), scope_symbols=SCOPE, calib=build_index(store)
    )
    assert filtered.hits == []


def test_m8_wrong_cost_blocks_the_write_and_the_hit_never_shows():
    """M8 端到端: `cost_bp_roundtrip=0` 的 passed 写不进去 → 命中不可见。"""
    with pytest.raises(CalibValidationError):
        fx.passed_calib(cost_bp_roundtrip=0)

    filtered = visible_hits(
        [_live(fx.calibrated_hit())], _full_registry(), scope_symbols=SCOPE, calib=build_index(InMemoryCalibStore())
    )
    assert filtered.hits == []


def test_hand_written_calib_file_with_bad_gates_is_dropped(tmp_path):
    """手改 JSON 塞一条 `sample_n=5` 的 passed → 反序列化失败 → 该条被丢 → 不可见。"""
    path = tmp_path / "buy_sell_signals" / "calib.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    bad = fx.passed_calib().to_dict()
    bad["sample_n"] = 5
    path.write_text(
        json.dumps({"schema_version": "1", "namespace": "buy_sell_signals", "entries": [bad]}),
        encoding="utf-8",
    )
    from app.buy_sell_signals.calib_store import DiskCalibStore

    idx = build_index(DiskCalibStore(tmp_path))
    assert len(idx) == 0
    filtered = visible_hits([_live(fx.calibrated_hit())], _full_registry(), scope_symbols=SCOPE, calib=idx)
    assert filtered.hits == []


def test_hand_edited_fingerprint_is_rejected_on_read():
    """手改 params 但留着旧 fingerprint → 读取时报错（不是"默默接受"）。"""
    from app.buy_sell_signals.freq_lock import FreqLockError, FreqLockStamp

    payload = fx.freq_lock_stamp().to_dict()
    payload["params"] = dict(fx.FIXTURE_FREQ_PARAMS_TWEAKED)
    with pytest.raises(FreqLockError):
        FreqLockStamp.from_dict(payload)


# ======================================================= M9 · 空态文案


def test_m9_empty_message_is_byte_identical():
    """M9: 空态文案改一个字就红。"""
    payload = build_response(
        visible_hits([], _full_registry(), scope_symbols=SCOPE, calib=CalibIndex([]))
    )
    assert payload["empty_message"] == "暂无已校准的买卖信号"
    assert payload["empty_message"].encode("utf-8") == C.EMPTY_HITS_MESSAGE.encode("utf-8")
    for forbidden in ("今天没", "今日无", "暂无信号", "暂时没有信号", "无信号"):
        assert forbidden not in payload["empty_message"]


def test_m9b_empty_message_present_even_when_hits_exist():
    payload = build_response(
        visible_hits([_live(fx.calibrated_hit())], _full_registry(), scope_symbols=SCOPE, calib=_passed_all())
    )
    assert payload["count"] == 1
    assert payload["empty_message"] == C.EMPTY_HITS_MESSAGE


def test_empty_state_when_nothing_calibrated_is_honest_not_broken():
    """生产最可能的状态: 有命中、但没有任何 passed → count=0、文案照旧、不 500。"""
    payload = build_response(
        visible_hits(
            [_live(fx.calibrated_hit()), _live(fx.daily_calibrated_hit())],
            _full_registry(),
            scope_symbols=SCOPE,
            calib=CalibIndex([]),
        )
    )
    assert payload["count"] == 0
    assert payload["empty_message"] == C.EMPTY_HITS_MESSAGE
    assert payload["rejected"]["calib_not_passed"] >= 1


# ======================================================= M10 · TRIGGERING


def test_m10_triggering_flags_are_still_false():
    """M10: 本拍**没有**翻任何 TRIGGERING。翻了这条红。"""
    from app.buy_sell_signals import auction_abnormal_stub as ab
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert vb.VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert ab.AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_m10b_threshold_lock_pending_is_still_pending():
    """给了一个戳 ≠ 阈值已验明。`threshold_lock_data_pending` 一条都不许清。"""
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert "threshold_lock_data_pending" in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING
    # 批次 20261101gevid 清了触发表那条，剩 3（阈值锁**不在**被清之列）
    assert len(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING) == 3


# ======================================================= M11 · calibrated 字段


def test_m11_signal_hit_has_no_calibrated_field():
    """M11: `SignalHit` 上**没有** `calibrated`。一个可手改的布尔会让所有闸形同虚设。"""
    assert not hasattr(_live(fx.calibrated_hit()), "calibrated")
    assert "calibrated" not in SignalHit.__dataclass_fields__
    payload = _live(fx.calibrated_hit()).to_dict()
    assert "calibrated" not in payload


def test_m11b_injecting_a_calibrated_key_does_not_make_a_hit_visible():
    """即便有人在命中 JSON 里塞 `calibrated: true`, 闸也不认 —— 它只看 calib 登记。"""
    payload = _live(fx.calibrated_hit()).to_dict()
    payload["calibrated"] = True
    revived = SignalHit.from_dict(payload)
    assert not hasattr(revived, "calibrated")

    filtered = visible_hits([revived], _full_registry(), scope_symbols=SCOPE, calib=CalibIndex([]))
    assert filtered.hits == []


# ======================================================= M12 · regime_caliber


def test_m12_regime_caliber_version_does_not_imply_calibration():
    """M12: `SignalRecord.regime_caliber_version` 有值 ── 与信号是否已校准**毫无关系**。

    做法: 真的把定义上的 `regime_caliber_version` 填上（这是"市场环境口径走的是第几版"
    的标签, 与 BSS 信号校准无关）, 然后断言在 `CalibIndex([])` 下命中仍不可见。

    这条测的是**语义**而不是字段存在性: 如果哪天有人把校准闸写成
    `if definition.regime_caliber_version: 放行`, 这里立刻红。
    """
    base = fx.publishable_definition()
    forged = replace(base, regime_caliber_version="regime_caliber_v2")
    assert forged.regime_caliber_version == "regime_caliber_v2"
    assert forged.status == "publishable"

    filtered = visible_hits(
        [_live(fx.calibrated_hit())], _registry(forged), scope_symbols=SCOPE, calib=CalibIndex([])
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.CALIB_NOT_PASSED) == 1


def test_m12a_regime_caliber_version_does_not_substitute_for_a_passed_record():
    """反向再钉一次: 环境和口径标签都齐, 但没有 `passed` —— 就是不可见。"""
    forged = replace(fx.publishable_definition(), regime_caliber_version="regime_caliber_v2")
    hit = replace(_live(fx.calibrated_hit()), freq_lock_version=C.FREQ_LOCK_VERSION)
    filtered = visible_hits([hit], _registry(forged), scope_symbols=SCOPE, calib=CalibIndex([fx.pending_calib()]))
    assert filtered.hits == []


def test_m12b_visibility_module_does_not_reference_regime_caliber():
    """闸的判定逻辑里不出现 `regime_caliber` —— 两个口径不许混。"""
    import ast

    root = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
    tree = ast.parse((root / "hits_visibility.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "regime_caliber_version", node.lineno
        elif isinstance(node, ast.Name):
            assert node.id != "regime_caliber", node.lineno


# ======================================================= 组合与契约


def test_mixed_batch_each_hit_judged_independently():
    """一批里混着五种情形 —— 每个命中各自判, 不因为有一个通过就整体放行。

    注意 `limit_reopen` 的计数是 2 而不是 1: `candidate_calibrated_hit()` 指向的是
    `fx.cand.intraday_volume`(candidate), `limit_reopen_calibrated_hit()` 指向的
    `fx.limit_reopen` 定义本身状态是 `observe` —— 两条都会先撞上 `not_publishable`,
    炸板那条还要再被 `limit_reopen` 拦一次(双保险的顺序问题)。

    这里如实断言这个顺序, 而不是把期望值改成"看起来更整齐"的样子: 记录真实行为,
    才好在将来守序变化时立刻发现。
    """
    hits = [
        _live(fx.calibrated_hit(hit_id="ok")),  # 可见
        _live(fx.unlabelled_hit()),  # 无标签
        _live(fx.candidate_calibrated_hit()),  # candidate
        _live(fx.limit_reopen_calibrated_hit()),  # limit_reopen(定义为 observe)
        _live(fx.composite_hit()),  # composite 不暴露
    ]
    filtered = visible_hits(hits, _full_registry(), scope_symbols=SCOPE, calib=_passed_all())
    assert [h.hit_id for h in filtered.hits] == ["ok"]
    assert filtered.rejected == {
        VisibilityReject.CALIB_NOT_PASSED: 1,
        VisibilityReject.NOT_PUBLISHABLE: 2,
        VisibilityReject.FAMILY_NOT_EXPOSED: 1,
    }


def test_limit_reopen_reported_after_definition_is_publishable():
    """把炸板定义手改成 publishable 之后, 拒绝原因就变成 `limit_reopen` —— 双保险生效。"""
    forged = fx.publishable_definition(signal_id="fx.limit_reopen")
    filtered = visible_hits(
        [_live(fx.limit_reopen_calibrated_hit())], _registry(forged), scope_symbols=SCOPE, calib=_passed_all()
    )
    assert filtered.rejected == {VisibilityReject.LIMIT_REOPEN: 1}


def test_response_carries_calib_gate_summary():
    payload = build_response(
        visible_hits([_live(fx.calibrated_hit())], _full_registry(), scope_symbols=SCOPE, calib=_passed_all())
    )
    assert payload["calib_gate"] == "applied"
    assert payload["calib_passed_keys"] == 4

    # "只回条数不回内容"这条只约束**闸的摘要字段**, 不约束命中本身(命中当然带 signal_id)。
    # 做法: 把 hits 摘掉再看剩余字段, 里面不该出现任何 signal_id 形状的键。
    summary = {k: v for k, v in payload.items() if k != "hits"}
    assert not any(isinstance(v, (list, tuple)) for v in summary.values()), summary
    assert isinstance(summary["calib_passed_keys"], int)


def test_exposed_families_unchanged():
    """本拍**没有**偷偷扩暴露面。"""
    assert EXPOSED_FAMILIES == ("minute_event", "daily_fundamental")


def test_all_reject_reasons_still_present():
    """原有六条 reject 原因一条都没丢, 新加的是第七条, P3a 再加一条。"""
    for reason in (
        VisibilityReject.DEFINITION_MISSING,
        VisibilityReject.NOT_PUBLISHABLE,
        VisibilityReject.LIMIT_REOPEN,
        VisibilityReject.FAMILY_NOT_EXPOSED,
        VisibilityReject.UNKNOWN_FAMILY_FILTER,
        VisibilityReject.OUT_OF_SCOPE,
        VisibilityReject.CALIB_NOT_PASSED,
        VisibilityReject.NON_LIVE_MODE,
    ):
        assert reason in VisibilityReject.ALL
    # 8 条: 上表 8 个常量。这条是**封闭集**断言 —— 加原因码必须同步改这里,
    # 不能只在代码里悄悄多一条而让运维的口径表对不上。
    assert len(VisibilityReject.ALL) == 8


# ======================================================= P3a · 只放 live(批次 20261028gp3a)


def _with_mode(hit, mode):
    """把命中的 `source.mode` 换掉。用于显式测非 live 路径。"""
    return replace(hit, source=replace(hit.source, mode=mode))


def test_p3a_live_hit_is_visible_when_everything_else_passes():
    """对照面：live + publishable + passed + 其余闸过 → 可见。

    没有这一条，下面那些"0 可见"的断言就无法区分「被 mode 闸挡的」和
    「被别的什么挡的」。
    """
    filtered = visible_hits(
        [_live(fx.calibrated_hit())], _full_registry(), scope_symbols=SCOPE, calib=_passed_all()
    )
    assert filtered.count == 1
    assert VisibilityReject.NON_LIVE_MODE not in filtered.rejected


def test_p3a_shadow_hit_is_invisible_even_with_passed_calib():
    """影子命中：三键对齐到 passed 也不行 —— 校准成绩不给来源背书。"""
    hit = _with_mode(fx.calibrated_hit(), "shadow")
    filtered = visible_hits([hit], _full_registry(), scope_symbols=SCOPE, calib=_passed_all())
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.NON_LIVE_MODE) == 1


def test_p3a_fixture_hit_is_invisible_even_with_passed_calib():
    """夹具命中：同上。注意这是**新的**默认结果 —— 夹具不再天然可见。"""
    hit = _with_mode(fx.calibrated_hit(), "fixture")
    filtered = visible_hits([hit], _full_registry(), scope_symbols=SCOPE, calib=_passed_all())
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.NON_LIVE_MODE) == 1


def test_p3a_missing_mode_is_fail_closed():
    """`source` 整个没有 / `mode` 是空串 → 一律不可见（N4 fail-closed）。

    `HitSource.__post_init__` 会校验 mode ∈ {fixture, shadow, live}，所以正常构造
    造不出这两种形态。这里用 `object.__new__` + `object.__setattr__` 绕过 —— 模拟的是
    **手写 JSON 或旧版本记录**读进来的样子。闸若只在构造期生效，这类数据就能漏过去。
    """

    class _Bare:
        """只有 family/symbol 等必要字段、没有 source 的最小对象。"""

    base = _live(fx.calibrated_hit())

    bare = _Bare()
    for field in (
        "hit_id",
        "signal_id",
        "definition_version",
        "symbol",
        "asof",
        "family",
        "event_class",
        "direction_label",
        "definition_status_at_hit",
        "freq_lock_version",
    ):
        setattr(bare, field, getattr(base, field))
    # 刻意不给 source

    filtered = visible_hits([bare], _full_registry(), scope_symbols=SCOPE, calib=_passed_all())
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.NON_LIVE_MODE) == 1


def test_p3a_empty_string_mode_is_fail_closed():
    """`mode=""`（绕过构造校验）同样落进 NON_LIVE_MODE，而不是"当没写、放过去"。"""
    hit = _live(fx.calibrated_hit())
    object.__setattr__(hit.source, "mode", "")
    filtered = visible_hits([hit], _full_registry(), scope_symbols=SCOPE, calib=_passed_all())
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.NON_LIVE_MODE) == 1


def test_p3a_unknown_future_mode_is_fail_closed():
    """将来新增的 mode 值默认不可见 —— 白名单写法的意义就在这里。

    如果实现写成 `if mode in {"shadow", "fixture"}`，一个新 mode 就会悄悄可见。
    """
    hit = _live(fx.calibrated_hit())
    object.__setattr__(hit.source, "mode", "replay")
    filtered = visible_hits([hit], _full_registry(), scope_symbols=SCOPE, calib=_passed_all())
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.NON_LIVE_MODE) == 1


def test_p3a_mixed_store_only_live_survives():
    """同一个列表里混着 live / shadow / fixture → 只有 live 留下。

    这正是生产 store 可能出现的形态：`HitStore.list_all()` 不按 mode 过滤。
    """
    live = _live(fx.calibrated_hit(hit_id="fx.hit.live"))
    shadow = _with_mode(fx.calibrated_hit(hit_id="fx.hit.shadow"), "shadow")
    fixture = _with_mode(fx.calibrated_hit(hit_id="fx.hit.fixture"), "fixture")

    filtered = visible_hits(
        [live, shadow, fixture], _full_registry(), scope_symbols=SCOPE, calib=_passed_all()
    )
    assert [h.hit_id for h in filtered.hits] == ["fx.hit.live"]
    assert filtered.rejected.get(VisibilityReject.NON_LIVE_MODE) == 2


def test_p3a_non_live_reject_does_not_leak_symbol_into_reason():
    """拒因里不含个股字段（`rejected` 是可以进日志的）。"""
    hit = _with_mode(fx.calibrated_hit(symbol="600519"), "shadow")
    filtered = visible_hits([hit], _full_registry(), scope_symbols=SCOPE, calib=_passed_all())
    assert set(filtered.rejected) == {VisibilityReject.NON_LIVE_MODE}
    assert "600519" not in str(filtered.rejected)
