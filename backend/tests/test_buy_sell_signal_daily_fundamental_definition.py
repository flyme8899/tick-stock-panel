"""`bss.daily_quality_value` 真实日频定义注册(P-bss-p1 · 批次 20261028gp1)。

## 这组测例在守什么

守的不是「工厂能跑」—— 那一条冒烟就够。守的是**这条定义不能变成别的东西**:

- 不能变成夹具键(否则「真实定义在场」这句话就假了)
- 不能变成 `publishable`(那会凭空跳过人工发布闸)
- 不能丢掉 PIT 口径(否则将来评估会悄悄前视)
- 不能把 ≥30 换成 scope 后计数(那会让不同用户看到不同的「已达标」)
- 不能把 `volume_breakout` 说成日频(它是 minute_event 桩, R2-5 明文不选)

每一条都写成**断言具体值**, 不写成「字段存在」。字段存在只能说明有人填了, 说明不了
填的是什么。

## 这组测例**不**证明什么

跑绿 ≠ calib 已过 ≠ 可放量 ≠ TRIGGERING 该开。本拍只注册一条口径声明, 没有评估器,
没有影子写入, 没有回放。

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""
from __future__ import annotations

import inspect

import pytest

from app.buy_sell_signals import daily_fundamental_definition as dd
from app.buy_sell_signals import hits_fixtures as fx
from app.buy_sell_signals.auction_abnormal_stub import AUCTION_ABNORMAL_TRIGGERING_ENABLED
from app.buy_sell_signals.constants import (
    CALIB_SAMPLE_N_MIN,
    EVENT_CLASSES,
    FREQ_LOCK_VERSION,
    SIGNAL_FAMILIES,
)
from app.buy_sell_signals.freq_lock import FREQ_LOCK_CADENCES, FreqLockStamp
from app.buy_sell_signals.models import SIGNAL_STATUSES, SignalRecord
from app.buy_sell_signals.quality_proxy import QUALITY_METRIC_COLUMNS
from app.buy_sell_signals.store import InMemorySignalRegistryStore
from app.buy_sell_signals.volume_breakout_stub import (
    VOLUME_BREAKOUT_FAMILY,
    VOLUME_BREAKOUT_SIGNAL_ID,
    VOLUME_BREAKOUT_TRIGGERING_ENABLED,
)

FIXTURE_DAILY_ID = "fx.pub.daily_quality_value"


# ============================================================ 1. 构造与合法性


def test_factory_builds_valid_signal_record():
    """工厂返回合法 `SignalRecord` —— 构造期不抛(校验在 `__post_init__` 里)。"""
    record = dd.build_daily_quality_value_record()
    assert isinstance(record, SignalRecord)
    assert record.signal_id.strip()
    assert record.display_name.strip()


def test_record_fields_are_pinned():
    """身份六件套逐个钉死。改任何一个都会让这条红 —— 那是期待中的红。"""
    record = dd.build_daily_quality_value_record()
    assert record.signal_id == "bss.daily_quality_value"
    assert record.version == "v1"
    assert record.family == "daily_fundamental"
    assert record.event_class == "daily_quality_value"


def test_constants_are_in_closed_sets():
    """family / event_class / status 都必须在仓内的封闭集里。

    只断言"等于某个字面量"不够 —— 字面量对了但集合里没有, 模型层照样拒。两边都断,
    才能说明这条记录是**真的**合法, 而不是恰好长得像。
    """
    assert dd.REAL_DAILY_FAMILY in SIGNAL_FAMILIES
    assert dd.REAL_DAILY_EVENT_CLASS in EVENT_CLASSES
    assert "candidate" in SIGNAL_STATUSES


# ============================================================ 2. 非夹具


def test_real_key_is_not_the_fixture_key():
    """G-T3: 真实定义键 ≠ 夹具键。"""
    assert dd.REAL_DAILY_SIGNAL_ID != FIXTURE_DAILY_ID
    assert dd.build_daily_quality_value_record().signal_id != FIXTURE_DAILY_ID


@pytest.mark.parametrize("prefix", ["fx.", "fx.pub."])
def test_real_key_does_not_use_fixture_prefix(prefix):
    """真实键不带 `fx.` 前缀。

    `fx.` 在仓里是夹具命名空间。真实定义借用它, 事后读注册表就分不出"这条是测例造的
    还是生产声明的"—— 而那恰恰是本拍要能分清的东西。
    """
    assert not dd.REAL_DAILY_SIGNAL_ID.startswith(prefix)


def test_fixture_daily_definition_still_works_and_stays_distinct():
    """夹具保留且未被本拍改动: 默认键仍是 `fx.pub.daily_quality_value`。

    这条是**反向保护**: 如果哪天有人图省事, 把夹具的默认键改成真实键(让两边"对齐"),
    这条会红。两边必须同时在场且互相可区分 —— 少一边, 或者两边合并, 都不行。
    """
    fixture = fx.daily_fundamental_definition()
    assert fixture.signal_id == FIXTURE_DAILY_ID
    # 夹具是 publishable(这是它存在的理由), 真实定义是 candidate
    assert fixture.status == "publishable"
    assert dd.build_daily_quality_value_record().status == "candidate"
    assert fixture.signal_id != dd.REAL_DAILY_SIGNAL_ID


def test_factory_does_not_reuse_fixture_published_record():
    """真实定义不能通过夹具的 `_published_record` 造出来。

    表面上这条测不出来(两条记录长得像), 但 `_published_record` 一定会写
    `manual_publisher` / `manual_published_at`。真实定义这两项必须是 `None` ——
    否则它就是"被发布过的", 而本拍没有任何人签过字。
    """
    record = dd.build_daily_quality_value_record()
    assert record.manual_publisher is None
    assert record.manual_published_at is None
    assert record.spec_ref != "fixture://spec"


# ============================================================ 3. 初态与人工闸


def test_initial_status_is_candidate_not_publishable():
    """G-T4: 初态 `candidate`, 不是 `publishable`。"""
    record = dd.build_daily_quality_value_record()
    assert record.status == "candidate"
    assert record.status != "publishable"


def test_auto_promote_forbidden_and_manual_publish_required():
    """两个人工闸都是 True —— 模型层恒真, 这里再断一次。"""
    record = dd.build_daily_quality_value_record()
    assert record.auto_promote_forbidden is True
    assert record.manual_publish_required is True


def _code_without_strings_and_comments(module) -> str:
    """把模块源码里的字符串字面量与注释剥掉, 只留可执行代码。

    直接对源码做 `"manual_publish(" not in src` 会被**文字**骗到: 本模块的 docstring 里
    就写着「唯一入口是 `manual_publish()`」, 那是解释, 不是调用。不剥就永远红, 而唯一
    "修复"方式是把解释删掉 —— 那反而降低了可读性。

    用 `tokenize` 而不是正则: 正则切三引号字符串会漏掉带引号的嵌套, 也分不清 `#` 是
    注释还是字符串里的字符。
    """
    import io
    import tokenize

    # f-string 在 3.12 才拆成 FSTRING_START/MIDDLE/END 三个 token; 3.11 里它整体就是
    # 一个 STRING。写成 getattr 兼容两边, 免得这段代码在 3.11 上直接 AttributeError。
    skip = {tokenize.STRING, tokenize.COMMENT}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        tok_type = getattr(tokenize, name, None)
        if tok_type is not None:
            skip.add(tok_type)

    src = inspect.getsource(module)
    kept: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in skip:
            continue
        kept.append(tok.string)
    return " ".join(kept)


def test_factory_module_never_calls_manual_publish():
    """**源码级**断言: 本模块不调用 `manual_publish`。

    只断 `record.status != "publishable"` 是不够的 —— 有人可以造 candidate 再在别处
    晋升。本拍的要求是「这条路径上根本没有晋升动作」, 所以查源码更贴题。

    查的是 `dd` 模块自身的源码, 不包括它 import 的 `state_machine`(那里有实现, 很正常)。
    剥掉字符串与注释后查, 见 `_code_without_strings_and_comments` 的理由。
    """
    code = _code_without_strings_and_comments(dd)
    assert "manual_publish" not in code


def test_manual_publish_probe_is_not_vacuous():
    """上一条的**自检**: 剥字符串的方法不能把代码也一起剥光。

    如果 `_code_without_strings_and_comments` 写错(比如把整个函数体都当字符串扔掉),
    上一条会永远绿 —— 那种绿是假的。这里用同一方法检查一个**已知含有**该名字的模块
    (`state_machine`, 那里真有 `def manual_publish`), 必须仍能查到。
    """
    from app.buy_sell_signals import state_machine

    code = _code_without_strings_and_comments(state_machine)
    assert "manual_publish" in code, "剥字符串把代码也剥掉了, 上一条断言失效"


def test_register_refuses_to_write_publishable(monkeypatch):
    """`register_*` 在写库前拦 publishable。

    模拟"有人把工厂改成产 publishable"(monkeypatch 工厂), 期望 `register_*` 直接抛,
    且注册表里**什么都没写** —— 拦在写库前, 不是写进去再回滚。
    """
    import dataclasses

    original = dd.build_daily_quality_value_record

    def _bad() -> SignalRecord:
        rec = original()
        # 造一条"看起来 publishable"的记录: 必须带签字, 否则模型层就拒了
        return dataclasses.replace(
            rec,
            status="publishable",
            manual_publisher="test",
            manual_published_at=rec.created_at,
        )

    monkeypatch.setattr(dd, "build_daily_quality_value_record", _bad)

    store = InMemorySignalRegistryStore()
    with pytest.raises(AssertionError):
        dd.register_daily_fundamental_definition(store)
    assert store.get(dd.REAL_DAILY_SIGNAL_ID) is None


# ============================================================ 4. 可注册可读


def test_register_then_read_back():
    """InMemory upsert 后按 (signal_id, "v1") 能读回, 字段对齐。"""
    store = InMemorySignalRegistryStore()
    written = dd.register_daily_fundamental_definition(store)

    got = store.get(dd.REAL_DAILY_SIGNAL_ID, "v1")
    assert got is not None
    assert got.signal_id == written.signal_id
    assert got.version == "v1"
    assert got.family == "daily_fundamental"
    assert got.event_class == "daily_quality_value"
    assert got.status == "candidate"


def test_register_is_idempotent():
    """重复注册不炸、不产生第二条。"""
    store = InMemorySignalRegistryStore()
    first = dd.register_daily_fundamental_definition(store)
    second = dd.register_daily_fundamental_definition(store)
    assert first.signal_id == second.signal_id
    rows = [r for r in store.list() if r.signal_id == dd.REAL_DAILY_SIGNAL_ID]
    assert len(rows) == 1


def test_registered_record_is_not_visible_as_publishable_in_registry_list():
    """注册表里按 `status="publishable"` 过滤, 找不到它。"""
    store = InMemorySignalRegistryStore()
    dd.register_daily_fundamental_definition(store)
    publishable = [r for r in store.list(status="publishable") if r.signal_id == dd.REAL_DAILY_SIGNAL_ID]
    assert publishable == []


# ============================================================ 5. FreqLock 对齐


def test_freq_lock_cadence_is_daily_fundamental():
    """G-T5: cadence 必须 ∈ `FREQ_LOCK_CADENCES` 且等于 `daily_fundamental`。"""
    stamp = dd.build_daily_quality_value_freq_lock()
    assert isinstance(stamp, FreqLockStamp)
    assert stamp.cadence == "daily_fundamental"
    assert stamp.cadence in FREQ_LOCK_CADENCES


def test_freq_lock_three_keys_align_with_record():
    """三键对齐: stamp 的 `(signal_id, definition_version, freq_lock_version)`
    必须与记录的 `(signal_id, version, FREQ_LOCK_VERSION)` 相等。

    可见性闸与校准登记都靠这三键认「是不是同一套口径」。任一处错开, 记录就在闸外面 ——
    而且从输出上看不出来为什么。
    """
    record = dd.build_daily_quality_value_record()
    stamp = dd.build_daily_quality_value_freq_lock()

    assert stamp.align_key == (record.signal_id, record.version, FREQ_LOCK_VERSION)
    assert stamp.definition_version == record.version == "v1"
    assert stamp.freq_lock_version == FREQ_LOCK_VERSION


def test_stamp_asof_default_is_an_explicit_placeholder():
    """stamp 的 `asof` 默认是明显的占位值, 不是"今天"。"""
    stamp = dd.build_daily_quality_value_freq_lock()
    assert stamp.asof == "1970-01-01"


def test_stamp_accepts_caller_asof():
    """调用方可覆写 `asof` —— 将来 P2 跑评估时用真实时点。"""
    stamp = dd.build_daily_quality_value_freq_lock(asof="2026-10-20")
    assert stamp.asof == "2026-10-20"


def test_stamp_params_carry_no_volume_keys():
    """stamp.params 不含任何 `volume_*` 键。

    日频质量口径没有量相关阈值。凭空加一个不仅没意义, 还会撞上 K4 的
    `volume_unit` 强制声明 —— 那是给"真的要用量"的信号准备的闸, 不该被一条日频定义
    无端触发。
    """
    stamp = dd.build_daily_quality_value_freq_lock()
    volume_keys = [k for k in stamp.params if str(k).startswith("volume_")]
    assert volume_keys == []


# ============================================================ 6. PIT


def test_pit_cutoff_is_announce_date():
    """G-T6: 截断键写死 `announce_date`。"""
    assert dd.PIT_CUTOFF_VALUE == "announce_date"


@pytest.mark.parametrize("where", ["params", "metadata"])
def test_pit_cutoff_declared_in_both_stamp_and_record(where):
    """口径在 stamp.params 与 record.metadata 两处都在。

    只写一处不够: `FreqLockStamp` 可能被单独序列化流转, `SignalRecord` 才是注册表里
    最常被读的那个。任一处缺失, 读的人就会以为"这条定义没声明 PIT"。
    """
    if where == "params":
        container = dd.build_daily_quality_value_freq_lock().params
    else:
        container = dd.build_daily_quality_value_record().metadata
    assert container[dd.PIT_CUTOFF_KEY] == "announce_date"


def test_pit_lookahead_forbidden_flag_is_true():
    """禁前视的标志必须是 `True`(而不是"存在但为 False")。"""
    assert dd.build_daily_quality_value_freq_lock().params[dd.PIT_LOOKAHEAD_FORBIDDEN_KEY] is True
    assert dd.build_daily_quality_value_record().metadata[dd.PIT_LOOKAHEAD_FORBIDDEN_KEY] is True


def test_pit_cutoff_is_never_period_end():
    """明确否掉 `period_end`。

    这条是本组里最该写死的一条: `period_end` 与 `announce_date` 都是日期、都长得像
    "财报的某个时点", 用错了代码照跑、结果照出, 只是永远比市场早知道几天。
    """
    assert dd.PIT_CUTOFF_VALUE != "period_end"
    stamp_cutoff = dd.build_daily_quality_value_freq_lock().params[dd.PIT_CUTOFF_KEY]
    meta_cutoff = dd.build_daily_quality_value_record().metadata[dd.PIT_CUTOFF_KEY]
    assert stamp_cutoff != "period_end"
    assert meta_cutoff != "period_end"


def test_pit_rule_text_uses_le_operator_on_announce_date():
    """判据原文是 `announce_date <= asof` —— 含 `<=` 与 `announce_date`。

    只断 `announce_date in text` 的话, `announce_date > asof` 也能过。
    """
    rule = dd.PIT_RULE_TEXT
    assert "announce_date" in rule
    assert "<=" in rule
    assert "period_end" not in rule


# ============================================================ 7. ≥30 口径


def test_sample_n_min_aligns_with_calib_constant():
    """G-T7: 样本下限**引用** `CALIB_SAMPLE_N_MIN`, 不另写字面量。"""
    stamp = dd.build_daily_quality_value_freq_lock()
    record = dd.build_daily_quality_value_record()
    assert stamp.params[dd.SAMPLE_N_MIN_KEY] == CALIB_SAMPLE_N_MIN
    assert record.metadata[dd.SAMPLE_N_MIN_KEY] == CALIB_SAMPLE_N_MIN
    assert CALIB_SAMPLE_N_MIN == 30


def test_sample_count_domain_is_universe_shadow():
    """计数域是**全市场影子**, 不是 scope 后。"""
    assert dd.SAMPLE_COUNT_DOMAIN_VALUE == "universe_shadow"
    assert dd.build_daily_quality_value_freq_lock().params[dd.SAMPLE_COUNT_DOMAIN_KEY] == "universe_shadow"
    assert dd.build_daily_quality_value_record().metadata[dd.SAMPLE_COUNT_DOMAIN_KEY] == "universe_shadow"


def test_scope_filtered_counting_is_forbidden():
    """`sample_count_scope_filtered_forbidden` 为 True。

    拿 `/hits` 按自选/持仓过滤后的可见条数凑 30, 等于把「这只票在你的自选里」当成统计
    前提: 同一个信号, 用户 A 的自选有 40 只 → "已达标", 用户 B 只有 5 只 → "没达标"。
    门槛跟着用户配置变, 那就不是门槛。
    """
    assert dd.SAMPLE_SCOPE_FILTERED_FORBIDDEN_KEY == "sample_count_scope_filtered_forbidden"
    assert (
        dd.build_daily_quality_value_freq_lock().params[dd.SAMPLE_SCOPE_FILTERED_FORBIDDEN_KEY]
        is True
    )
    assert (
        dd.build_daily_quality_value_record().metadata[dd.SAMPLE_SCOPE_FILTERED_FORBIDDEN_KEY]
        is True
    )


def test_sample_domain_value_does_not_mention_scope():
    """域名的字面量里不许出现 scope / visible 字样 —— 防止有人改名换义。"""
    value = dd.SAMPLE_COUNT_DOMAIN_VALUE.lower()
    assert "scope" not in value
    assert "visible" not in value


# ============================================================ 8. 禁选 volume_breakout


def test_volume_breakout_is_not_registered_as_daily():
    """G-T8: `volume_breakout` 不得被注册成日频真实定义。

    它是 `minute_event` 家族的桩。R2-5 明文不选它 —— 本拍用负向断言把这条写死:
    真实定义的 key 不是它, family 也不是它那个家族。
    """
    record = dd.build_daily_quality_value_record()
    assert record.signal_id != VOLUME_BREAKOUT_SIGNAL_ID
    assert record.signal_id != "volume_breakout"
    assert record.family != VOLUME_BREAKOUT_FAMILY
    assert VOLUME_BREAKOUT_FAMILY == "minute_event"


def test_volume_breakout_stub_triggering_stays_false():
    """放量桩的 TRIGGERING 仍 False。"""
    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False


def test_auction_stub_triggering_stays_false():
    """竞价桩的 TRIGGERING 仍 False。"""
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


# ============================================================ 9. N1 字段细化不挡注册


def test_quality_metric_columns_are_referenced_not_redefined():
    """七列**引用** `quality_proxy.QUALITY_METRIC_COLUMNS`, 不在这里另抄一份字面量。

    抄一份的后果: 将来往 `QUALITY_METRIC_COLUMNS` 加列(比如补一个能对上
    `announce_date` 的字段), 声明里的那份不会跟着变, 两处悄悄分家。
    """
    record = dd.build_daily_quality_value_record()
    stamp = dd.build_daily_quality_value_freq_lock()
    assert tuple(record.metadata["quality_metric_columns"]) == QUALITY_METRIC_COLUMNS
    assert tuple(stamp.params["quality_metric_columns"]) == QUALITY_METRIC_COLUMNS
    assert len(QUALITY_METRIC_COLUMNS) == 7


def test_weighting_status_is_design_pending_not_a_number():
    """权重状态 `design_pending`; `w_quality` / `w_value` 保持 `None`。

    N1 拍板: 字段细化不挡最小注册。但"不挡"不等于"可以先填个 0.5 凑着"——
    填了 0.5, 下游就会把它当成已标定的权重。
    """
    record = dd.build_daily_quality_value_record()
    assert record.weighting_status == "design_pending"
    assert record.w_quality is None
    assert record.w_value is None


# ============================================================ 10. 跨批次: P3a / #240 不回退


def test_p3a_symbols_still_present():
    """#240 P3a 三符号仍在, 本拍没有回退它。"""
    from app.buy_sell_signals import hits_dingtalk as hd
    from app.buy_sell_signals.hits_visibility import VisibilityReject

    assert VisibilityReject.NON_LIVE_MODE == "NON_LIVE_MODE"
    assert hd.REASON_SHADOW == "shadow_not_pushable"
    assert hd.REASON_SHADOW in hd.REASON_KEYS


def test_p3a_visibility_gate_still_rejects_non_live():
    """行为层: 非 live 命中仍不可见(不是只断符号在不在)。"""
    from app.buy_sell_signals import hits_fixtures as fx2
    from app.buy_sell_signals.calib_store import CalibIndex
    from app.buy_sell_signals.hits_visibility import VisibilityReject, visible_hits

    hit = fx2.daily_fundamental_hit()
    filtered = visible_hits(
        [hit], None, scope_symbols=frozenset(), calib=CalibIndex([])
    )
    assert filtered.hits == []
    assert filtered.rejected.get(VisibilityReject.NON_LIVE_MODE) == 1


def test_disclaimer_key_is_the_research_only_one():
    """合规脚注键在场。"""
    from app.buy_sell_signals.constants import DISCLAIMER_KEY

    assert dd.build_daily_quality_value_record().disclaimer_key == DISCLAIMER_KEY


def test_record_carries_batch_and_boundary_note():
    """记录自带批次号与边界说明 —— 事后读注册表就知道它是什么时候、在什么前提下加的。"""
    meta = dd.build_daily_quality_value_record().metadata
    assert meta["batch"] == "20261028gp1"
    assert "注册完成" in meta["note"]
    assert "可放量" in meta["note"]
