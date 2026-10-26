"""频率锁定戳 + 校准登记的门槛与存储（批次 20261026gbsscalib · P-calib）。

本文件只测**闸与登记本身**：指纹稳定性、`passed` 的硬门槛、store 的 fail-closed 退化、
序列化往返。可见性 AND 的对照矩阵在 `test_buy_sell_signal_calib_visibility.py`。

三个"说清楚"的断言贯穿全文件:

1. **门槛是硬拒, 不是降级** —— `sample_n=29` 写 `passed` 必须抛错, 不能悄悄变成
   `pending`。理由见 `constants.CALIB_SAMPLE_N_MIN`。
2. **成本不可自报** —— `cost_bp_roundtrip` 只认 18.2。允许自报的话, 第一个想让它过的人
   就会填 5bp。
3. **指纹是内容算的** —— 改任何一个参与字段必变; 同一个字段顺序不同的两份参数集必须
   算出同一个指纹（否则指纹会被"键序噪音"搞到不可信）。

全部用夹具 + `tmp_path`, **不依赖**真实行情、真实回测库、真实 webhook。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.buy_sell_signals import constants as C  # noqa: N812
from app.buy_sell_signals import hits_fixtures as fx
from app.buy_sell_signals.calib_models import (
    CalibRecord,
    CalibValidationError,
    calib_record_from_dict,
)
from app.buy_sell_signals.calib_store import (
    CalibIndex,
    DiskCalibStore,
    InMemoryCalibStore,
    build_calib_store,
    build_index,
)
from app.buy_sell_signals.freq_lock import (
    FREQ_LOCK_FINGERPRINT_FIELDS,
    FreqLockError,
    FreqLockStamp,
    canonical_json,
    freq_lock_fingerprint,
)

SHANGHAI = timezone(timedelta(hours=8))


# ======================================================= 1. 常量钉死（N2）


def test_n2_constants_are_pinned():
    """N2 写死值。任何一条被改动都必须让本文件红。"""
    assert C.CALIB_SAMPLE_N_MIN == 30
    assert C.CALIB_EVAL_WINDOW_UNIT == "trading_days"
    assert C.CALIB_COST_BP_ROUNDTRIP == 18.2


def test_calib_status_closed_set_is_exactly_three():
    """封闭集只有三个值, 而且**没有** `ok` / `true` / `verified` 这类近义词。"""
    assert frozenset({"passed", "pending", "failed"}) == C.CALIB_STATUSES
    for synonym in ("ok", "true", "verified", "calibrated", "success", "pass"):
        assert synonym not in C.CALIB_STATUSES, synonym


def test_calib_filename_is_pinned():
    assert C.CALIB_FILENAME == "calib.json"


def test_empty_message_untouched_by_this_batch():
    """本拍**不许**碰空态文案。这条与 hits 测里那条互为双保险。"""
    assert C.EMPTY_HITS_MESSAGE == "暂无已校准的买卖信号"


def test_dedupe_key_is_not_the_push_cooldown():
    """频率锁定的对齐键**不是**播报去重窗口 —— 两件事不许混。"""
    assert "push_dedupe_cooldown_until" not in C.CALIB_HIT_DEDUPE_KEY_FIELDS
    assert C.CALIB_HIT_DEDUPE_KEY_FIELDS == (
        "signal_id",
        "definition_version",
        "symbol",
        "asof",
        "freq_lock_version",
    )
    # 播报冷却那组常量原封不动
    assert C.PUSH_DEDUPE_COOLDOWN_FIELD == "push_dedupe_cooldown_until"
    assert C.PUSH_DEDUPE_COOLDOWN_MIN == 30


# ======================================================= 2. 指纹（G-T2）


def test_fingerprint_is_stable_for_same_params():
    a = fx.freq_lock_stamp()
    b = fx.freq_lock_stamp()
    assert a.fingerprint == b.fingerprint
    assert len(a.fingerprint) == 64  # 全 64 位, 不截断


def test_fingerprint_changes_when_a_threshold_changes():
    """M6 的纯函数版: 改一个阈值参数, 指纹必变。"""
    base = fx.freq_lock_stamp()
    tweaked = fx.freq_lock_stamp(params=fx.FIXTURE_FREQ_PARAMS_TWEAKED)
    assert base.fingerprint != tweaked.fingerprint


@pytest.mark.parametrize("field", FREQ_LOCK_FINGERPRINT_FIELDS)
def test_fingerprint_changes_when_any_participating_field_changes(field):
    """四个参与字段逐个改, 指纹都要变 —— 漏一个就说明它其实没进指纹。"""
    base = fx.freq_lock_stamp()
    overrides = {
        "signal_id": {"signal_id": "fx.pub.other"},
        "definition_version": {"definition_version": "fixture_v2"},
        "cadence": {"cadence": "daily_fundamental"},
        "params": {"params": fx.FIXTURE_FREQ_PARAMS_TWEAKED},
    }
    other = fx.freq_lock_stamp(**overrides[field])
    assert base.fingerprint != other.fingerprint, field


def test_fingerprint_ignores_key_order():
    """同一份参数集, 键序不同 → 必须同一个指纹。

    不这样的话, 一次"重新序列化"就会被误判成"参数被改了", 指纹就从可信工具变成噪音源。
    """
    a = freq_lock_fingerprint(
        signal_id="s",
        definition_version="v1",
        cadence="minute_event",
        params={"a": 1, "b": 2, "c": 3},
    )
    b = freq_lock_fingerprint(
        signal_id="s",
        definition_version="v1",
        cadence="minute_event",
        params={"c": 3, "b": 2, "a": 1},
    )
    assert a == b


def test_fingerprint_distinguishes_numeric_near_misses():
    """2.5 vs 2.55 必须算出不同指纹 —— 不做浮点归一。"""
    a = freq_lock_fingerprint(
        signal_id="s", definition_version="v1", cadence="minute_event", params={"x": 2.5}
    )
    b = freq_lock_fingerprint(
        signal_id="s", definition_version="v1", cadence="minute_event", params={"x": 2.55}
    )
    assert a != b


def test_fingerprint_rejects_unknown_cadence():
    with pytest.raises(FreqLockError):
        freq_lock_fingerprint(signal_id="s", definition_version="v1", cadence="weekly", params={})


def test_fingerprint_rejects_empty_signal_id():
    with pytest.raises(FreqLockError):
        freq_lock_fingerprint(signal_id="  ", definition_version="v1", cadence="minute_event", params={})


def test_canonical_json_is_stable_and_sorted():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    # 不含空格, 不含 \u 转义
    assert canonical_json({"k": "中文"}) == '{"k":"中文"}'


def test_cadences_do_not_include_composite():
    """`composite` 没有定义好的评估节拍 —— 不许给它随便挑一个。"""
    assert frozenset({"minute_event", "daily_fundamental"}) == C.FREQ_LOCK_CADENCES
    assert "composite" not in C.FREQ_LOCK_CADENCES
    assert "composite" in C.SIGNAL_FAMILIES  # 但它在信号家族里


# ======================================================= 3. 戳本身


def test_stamp_pins_definition_version():
    stamp = fx.freq_lock_stamp(definition_version="fixture_v1")
    assert stamp.definition_version == "fixture_v1"
    assert stamp.align_key == ("fx.pub.intraday_volume", "fixture_v1", C.FREQ_LOCK_VERSION)


def test_stamp_roundtrip_preserves_fingerprint():
    stamp = fx.freq_lock_stamp()
    payload = stamp.to_dict()
    back = FreqLockStamp.from_dict(payload)
    assert back.fingerprint == stamp.fingerprint
    assert back.align_key == stamp.align_key
    assert back.params == dict(stamp.params)


def test_tampered_stamp_file_is_rejected():
    """文件里声明一个假的 `fingerprint` → 读的时候重算, 对不上就报错。"""
    payload = fx.freq_lock_stamp().to_dict()
    payload["params"] = dict(fx.FIXTURE_FREQ_PARAMS_TWEAKED)  # 改了参数, 没改 fingerprint
    with pytest.raises(FreqLockError, match="指纹"):
        FreqLockStamp.from_dict(payload)


def test_stamp_matches_requires_both_key_and_fingerprint():
    """三键相同但参数不同 → `matches()` 必须 False（M6）。"""
    base = fx.freq_lock_stamp()
    same_key_diff_params = fx.freq_lock_stamp(params=fx.FIXTURE_FREQ_PARAMS_TWEAKED)
    assert same_key_diff_params.align_key == base.align_key
    assert same_key_diff_params.fingerprint != base.fingerprint
    assert base.matches(same_key_diff_params) is False
    assert base.matches(fx.freq_lock_stamp()) is True


def test_stamp_without_timezone_is_rejected():
    with pytest.raises(FreqLockError, match="locked_at"):
        FreqLockStamp(
            signal_id="s",
            definition_version="v1",
            cadence="minute_event",
            asof="2026-10-26",
            params={},
            locked_at=datetime(2026, 10, 26, 9, 30),  # 裸时间
        )


def test_stamp_asof_accepts_date_and_aware_datetime():
    assert fx.freq_lock_stamp(asof="2026-10-26").asof_in_shanghai is True
    assert fx.freq_lock_stamp(asof="2026-10-26T09:31:00+08:00").asof_in_shanghai is True
    assert fx.freq_lock_stamp(asof="2026-10-26T09:31:00").asof_in_shanghai is False
    assert fx.freq_lock_stamp(asof="not-a-date").asof_in_shanghai is False


def test_empty_params_is_allowed_but_means_nothing_pinned():
    """空 `params` 合法 —— "节拍已定、阈值未标定"是诚实中间态。"""
    stamp = FreqLockStamp(
        signal_id="s", definition_version="v1", cadence="minute_event", asof="2026-10-26"
    )
    assert stamp.params == {}
    assert len(stamp.fingerprint) == 64


# ======================================================= 4. 登记字段与门槛


def test_passed_record_carries_all_required_fields():
    rec = fx.passed_calib()
    payload = rec.to_dict()
    for key in (
        "signal_id",
        "definition_version",
        "freq_lock_version",
        "status",
        "eval_window",
        "eval_window_unit",
        "sample_n",
        "cost_bp_roundtrip",
        "baseline_ref",
        "interval",
        "report_id",
        "source",
        "passed_at",
    ):
        assert key in payload, key
    assert payload["status"] == "passed"
    assert payload["eval_window_unit"] == "trading_days"
    assert payload["cost_bp_roundtrip"] == 18.2


def test_eval_window_unit_is_written_into_the_file():
    """单位必须落进文件, 不能只靠文档约定。"""
    assert fx.passed_calib().to_dict()["eval_window_unit"] == "trading_days"


def test_sample_n_29_cannot_be_passed():
    """M7: `sample_n=29` 写 `passed` → **拒绝**（抛错, 不是降级）。"""
    with pytest.raises(CalibValidationError, match="sample_n"):
        fx.passed_calib(sample_n=29)


def test_sample_n_30_can_be_passed():
    rec = fx.passed_calib(sample_n=30)
    assert rec.is_passed is True


def test_sample_n_below_minimum_is_fine_for_pending():
    """门槛只在 `passed` 时卡 —— `pending` 带 0 样本是合法且诚实的。"""
    rec = fx.pending_calib()
    assert rec.sample_n == 0
    assert rec.is_passed is False


@pytest.mark.parametrize("cost", [0, 5, 18.19, 18.21, 20, 36.4])
def test_cost_other_than_18_2_cannot_be_passed(cost):
    """M8: `cost_bp_roundtrip != 18.2` 写 `passed` → **拒绝**。"""
    with pytest.raises(CalibValidationError, match="cost_bp_roundtrip"):
        fx.passed_calib(cost_bp_roundtrip=cost)


def test_cost_equal_to_18_2_passes_the_gate():
    assert fx.passed_calib(cost_bp_roundtrip=18.2).is_passed is True


@pytest.mark.parametrize("missing", ["baseline_ref", "report_id", "source"])
def test_missing_reference_fields_cannot_be_passed(missing):
    kwargs = {
        "signal_id": "s",
        "definition_version": "v1",
        "freq_lock_version": C.FREQ_LOCK_VERSION,
        "status": "passed",
        "eval_window": 60,
        "sample_n": 40,
        "cost_bp_roundtrip": 18.2,
        "baseline_ref": "b",
        "interval": {"lo": 0.1, "hi": 0.9},
        "report_id": "r",
        "source": "s3",
        "passed_at": datetime.now(SHANGHAI),
    }
    kwargs[missing] = ""
    with pytest.raises(CalibValidationError):
        CalibRecord(**kwargs)


def test_missing_interval_cannot_be_passed():
    with pytest.raises(CalibValidationError, match="interval"):
        CalibRecord(
            signal_id="s",
            definition_version="v1",
            freq_lock_version=C.FREQ_LOCK_VERSION,
            status="passed",
            eval_window=60,
            sample_n=40,
            cost_bp_roundtrip=18.2,
            baseline_ref="b",
            interval={},
            report_id="r",
            source="s3",
            passed_at=datetime.now(SHANGHAI),
        )


def test_missing_passed_at_cannot_be_passed():
    with pytest.raises(CalibValidationError, match="passed_at"):
        CalibRecord(
            signal_id="s",
            definition_version="v1",
            freq_lock_version=C.FREQ_LOCK_VERSION,
            status="passed",
            eval_window=60,
            sample_n=40,
            cost_bp_roundtrip=18.2,
            baseline_ref="b",
            interval={"lo": 0.1, "hi": 0.9},
            report_id="r",
            source="s3",
            passed_at=None,
        )


def test_naive_passed_at_cannot_be_passed():
    with pytest.raises(CalibValidationError, match="passed_at"):
        CalibRecord(
            signal_id="s",
            definition_version="v1",
            freq_lock_version=C.FREQ_LOCK_VERSION,
            status="passed",
            eval_window=60,
            sample_n=40,
            cost_bp_roundtrip=18.2,
            baseline_ref="b",
            interval={"lo": 0.1, "hi": 0.9},
            report_id="r",
            source="s3",
            passed_at=datetime(2026, 10, 26, 15, 0),  # 无时区
        )


@pytest.mark.parametrize("bad_status", ["ok", "true", "verified", "calibrated", "PASSED", ""])
def test_unknown_status_is_rejected(bad_status):
    with pytest.raises(CalibValidationError, match="status"):
        CalibRecord(
            signal_id="s",
            definition_version="v1",
            freq_lock_version=C.FREQ_LOCK_VERSION,
            status=bad_status,
            eval_window=60,
            sample_n=40,
            cost_bp_roundtrip=18.2,
            baseline_ref="b",
        )


@pytest.mark.parametrize("field", ["signal_id", "definition_version", "freq_lock_version"])
def test_three_keys_must_be_non_empty(field):
    kwargs = {
        "signal_id": "s",
        "definition_version": "v1",
        "freq_lock_version": C.FREQ_LOCK_VERSION,
        "status": "pending",
        "eval_window": 60,
        "sample_n": 0,
        "cost_bp_roundtrip": 18.2,
        "baseline_ref": "",
    }
    kwargs[field] = " "
    with pytest.raises(CalibValidationError, match=field):
        CalibRecord(**kwargs)


@pytest.mark.parametrize("window", [0, -1, 1.5, True])
def test_eval_window_must_be_positive_int(window):
    with pytest.raises(CalibValidationError, match="eval_window"):
        CalibRecord(
            signal_id="s",
            definition_version="v1",
            freq_lock_version=C.FREQ_LOCK_VERSION,
            status="pending",
            eval_window=window,
            sample_n=0,
            cost_bp_roundtrip=18.2,
            baseline_ref="",
        )


def test_eval_window_unit_is_closed():
    with pytest.raises(CalibValidationError, match="eval_window"):
        CalibRecord(
            signal_id="s",
            definition_version="v1",
            freq_lock_version=C.FREQ_LOCK_VERSION,
            status="pending",
            eval_window=20,
            sample_n=0,
            cost_bp_roundtrip=18.2,
            baseline_ref="",
            eval_window_unit="calendar_days",  # 自然日 —— 明确拒绝
        )


def test_interval_rejects_inverted_bounds():
    with pytest.raises(CalibValidationError, match="下界"):
        CalibRecord(
            signal_id="s",
            definition_version="v1",
            freq_lock_version=C.FREQ_LOCK_VERSION,
            status="failed",
            eval_window=60,
            sample_n=10,
            cost_bp_roundtrip=18.2,
            baseline_ref="b",
            interval={"lo": 2.0, "hi": 1.0},
        )


def test_interval_accepts_array_form_on_deserialization():
    payload = fx.passed_calib().to_dict()
    payload["interval"] = [-0.5, 1.5]
    back = calib_record_from_dict(payload)
    assert back.interval == {"lo": -0.5, "hi": 1.5}


def test_record_roundtrip_keeps_passed_at_in_shanghai():
    rec = fx.passed_calib()
    back = calib_record_from_dict(json.loads(json.dumps(rec.to_dict(), ensure_ascii=False)))
    assert back.passed_at is not None
    assert back.passed_at.tzinfo is not None
    assert back.to_dict()["passed_at"].endswith("+08:00")
    assert back.is_passed is True


def test_deserializing_a_bad_passed_record_still_fails():
    """反序列化**不是**绕过门槛的后门: 手改 JSON 塞一个 sample_n=3 的 passed 也读不进来。"""
    payload = fx.passed_calib().to_dict()
    payload["sample_n"] = 3
    with pytest.raises(CalibValidationError, match="sample_n"):
        calib_record_from_dict(payload)


def test_pending_and_failed_carry_no_passed_at_by_default():
    assert fx.pending_calib().passed_at is None
    assert fx.failed_calib().passed_at is None
    assert fx.pending_calib().is_passed is False
    assert fx.failed_calib().is_passed is False


def test_failed_interval_can_cross_zero():
    """`failed` 的区间跨零是合法的 —— 那正是"不劣于基线不成立"的样子。"""
    rec = fx.failed_calib()
    assert rec.interval["lo"] < 0 < rec.interval["hi"]
    assert rec.is_passed is False


def test_disclaimer_present_on_every_record():
    for rec in (fx.passed_calib(), fx.pending_calib(), fx.failed_calib()):
        payload = rec.to_dict()
        assert payload["disclaimer_key"] == "research_only_v1"
        assert "仅供研究" in payload["disclaimer"]


# ======================================================= 5. 存储（fail-closed）


def test_disk_store_path_is_pinned(tmp_path):
    store = DiskCalibStore(tmp_path)
    assert store.path == tmp_path / "buy_sell_signals" / "calib.json"


def test_build_calib_store_ignores_persist_env(tmp_path, monkeypatch):
    """**不**绑 `BUY_SELL_SIGNALS_PERSIST`：那个 env 管定义注册表, 与校准无关。"""
    monkeypatch.setenv("BUY_SELL_SIGNALS_PERSIST", "0")
    assert isinstance(build_calib_store(tmp_path), DiskCalibStore)
    monkeypatch.setenv("BUY_SELL_SIGNALS_PERSIST", "1")
    assert isinstance(build_calib_store(tmp_path), DiskCalibStore)


def test_missing_calib_file_means_no_entries(tmp_path):
    assert DiskCalibStore(tmp_path).list_all() == []


def test_broken_json_degrades_to_no_entries(tmp_path):
    path = tmp_path / "buy_sell_signals" / "calib.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert DiskCalibStore(tmp_path).list_all() == []


def test_wrong_top_level_shape_degrades_to_no_entries(tmp_path):
    path = tmp_path / "buy_sell_signals" / "calib.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert DiskCalibStore(tmp_path).list_all() == []


def test_dirty_entry_is_skipped_not_fatal(tmp_path):
    """一条脏登记只丢它自己 —— 但那条信号因此不可见（不是"降级成 pending"）。"""
    store = DiskCalibStore(tmp_path)
    store.upsert(fx.passed_calib())
    path = store.path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"].append({"signal_id": "broken"})
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    rows = DiskCalibStore(tmp_path).list_all()
    assert len(rows) == 1
    assert rows[0].is_passed is True


def test_disk_roundtrip_through_upsert(tmp_path):
    store = DiskCalibStore(tmp_path)
    store.upsert(fx.passed_calib())
    store.upsert(fx.pending_calib(signal_id="fx.other"))
    rows = DiskCalibStore(tmp_path).list_all()
    assert {r.signal_id for r in rows} == {"fx.pub.intraday_volume", "fx.other"}


def test_upsert_same_key_overwrites(tmp_path):
    store = DiskCalibStore(tmp_path)
    store.upsert(fx.pending_calib())
    store.upsert(fx.passed_calib())
    rows = DiskCalibStore(tmp_path).list_all()
    assert len(rows) == 1
    assert rows[0].is_passed is True


def test_upsert_rejects_a_record_that_bypasses_gates(tmp_path):
    """`upsert` 不重复判门槛, 但 `CalibRecord` 构造时已经拦过一次 —— 这里验那条拦是硬的。"""
    with pytest.raises(CalibValidationError):
        fx.passed_calib(sample_n=10)


def test_in_memory_store_matches_disk_behaviour():
    store = InMemoryCalibStore()
    store.upsert(fx.passed_calib())
    store.upsert(fx.passed_calib())  # 同键覆盖
    assert len(store.list_all()) == 1


# ======================================================= 6. 索引


def test_index_only_contains_passed():
    idx = CalibIndex([fx.passed_calib(), fx.pending_calib(signal_id="fx.a"), fx.failed_calib(signal_id="fx.b")])
    assert len(idx) == 1
    assert idx.source_count == 3
    assert "fx.a" not in {k[0] for k in idx.passed_keys()}


def test_index_lookup_requires_all_three_keys():
    idx = CalibIndex([fx.passed_calib()])
    assert idx.get("fx.pub.intraday_volume", "fixture_v1", C.FREQ_LOCK_VERSION) is not None
    assert idx.get("fx.pub.intraday_volume", "fixture_v2", C.FREQ_LOCK_VERSION) is None
    assert idx.get("fx.pub.intraday_volume", "fixture_v1", "freq_lock_v9") is None
    assert idx.get("fx.pub.other", "fixture_v1", C.FREQ_LOCK_VERSION) is None
    assert idx.get("", "fixture_v1", C.FREQ_LOCK_VERSION) is None


def test_index_prefers_latest_passed_at():
    old = fx.passed_calib(sample_n=30)
    new = CalibRecord(
        signal_id=old.signal_id,
        definition_version=old.definition_version,
        freq_lock_version=old.freq_lock_version,
        status="passed",
        eval_window=old.eval_window,
        sample_n=90,
        cost_bp_roundtrip=18.2,
        baseline_ref=old.baseline_ref,
        interval=old.interval,
        report_id=old.report_id,
        source=old.source,
        passed_at=old.passed_at + timedelta(days=1),
    )
    idx = CalibIndex([new, old])
    got = idx.get(old.signal_id, old.definition_version, old.freq_lock_version)
    assert got is not None
    assert got.sample_n == 90


def test_build_index_from_none_is_empty():
    assert len(build_index(None)) == 0


def test_build_index_from_empty_store_is_empty():
    assert len(build_index(InMemoryCalibStore())) == 0


# ======================================================= 7. 不 import regime_caliber


def test_freq_lock_does_not_import_regime_caliber():
    """G-T11: 指纹范式可以参考, **语义不许盗用**。新模块不得 import 那个包。"""
    import ast

    root = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
    for name in ("freq_lock.py", "calib_models.py", "calib_store.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "regime_caliber" not in alias.name, f"{name}: import {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert "regime_caliber" not in (node.module or ""), f"{name}: from {node.module}"


def test_new_modules_do_not_import_t_rounds():
    """G-T9: 新文件不得 import `app.t_rounds*`。"""
    import ast

    root = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
    for name in ("freq_lock.py", "calib_models.py", "calib_store.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "t_rounds" not in alias.name, f"{name}: import {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert "t_rounds" not in (node.module or ""), f"{name}: from {node.module}"


# ======================================================= 8. 不产生 passed 的路径


def test_no_module_in_the_package_writes_passed_outside_explicit_upsert():
    """**生产路径上没有任何代码产 `passed`。**

    做法: 扫包里所有文件, 凡出现 `status="passed"` 或 `CALIB_STATUS_PASSED` 赋值的地方,
    必须落在 `calib_models.py`(校验)、`constants.py`(常量)、`calib_store.py`(索引过滤)
    或 `hits_fixtures.py`(夹具) 之内。任何别的文件出现它, 就说明有人在别处造结论。
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
    allowed = {"calib_models.py", "constants.py", "calib_store.py", "hits_fixtures.py"}
    offenders: list[str] = []

    def _is_passed_literal(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and node.value == "passed"

    for path in sorted(root.glob("*.py")):
        if path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 只抓两种形态: `status="passed"` 与 `"status": "passed"`。
            # 裸的 `"passed"` 字符串不算 —— 别的模块有同名的布尔字段
            # (`health_veto_stub` 的 `passed`), 与本拍的校准状态无关,
            # 扫太宽会逼人给无关模块改名。
            if isinstance(node, ast.keyword) and node.arg == "status" and _is_passed_literal(node.value):
                offenders.append(f"{path.name}:{node.value.lineno}")
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=False):
                    if isinstance(key, ast.Constant) and key.value == "status" and _is_passed_literal(value):
                        offenders.append(f"{path.name}:{value.lineno}")
    assert offenders == [], offenders


def test_no_scheduler_or_crontab_writes_calib():
    """G-T8: 本拍不引入任何调度写路径。"""
    import re

    root = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
    pattern = re.compile(r"APScheduler|add_job|crontab|BackgroundScheduler")
    for name in ("freq_lock.py", "calib_models.py", "calib_store.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert not pattern.search(text), name


def test_disk_store_has_no_fixture_import():
    """M5: 生产盘店不得 import 夹具 —— 否则"夹具写进生产"这条缝就开了。"""
    import ast

    root = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
    for name in ("calib_store.py", "hits_store.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 三种写法都要抓: `import x.hits_fixtures` / `from .. import hits_fixtures` /
            # `from .x import y`。只查 `node.module` 会漏掉 `from . import hits_fixtures`
            # —— 那正是最自然的一种写法, 也正是最容易漏过去的那一种。
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.endswith("hits_fixtures"), f"{name}: import {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").endswith("hits_fixtures"), f"{name}: from {node.module}"
                for alias in node.names:
                    assert alias.name != "hits_fixtures", f"{name}: from {node.module} import {alias.name}"
