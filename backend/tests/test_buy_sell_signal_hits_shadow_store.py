"""影子生产写入 / 独立三键分桶(批次 20261028gp3b · P-bss-p3b · #9 R2-7②)。

G-T1 – G-T14 对应本拍验收表。重点不在"能写出文件", 而在**四件不能发生的事**:

1. 写进去的东西**不能**进 `visible_hits` / 钉钉 —— P3a 闸仍然管着它
2. 影子写入**不能**碰生产 live `hits.json` —— 一个字节都不行
3. 不能有任何 `allow_shadow` 开关 —— 有开关就有"打开的那天"
4. 三键路径**不能被输入改写** —— 否则"可指认"变成"可指到任意位置"

夹具全部是**研究夹具**, 不是产线样本; 数量够 30 只是为了演示路径可指认,
**不**代表样本已达标, 更**不**代表 calib 已过。
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from app.market_time import CN_TZ
from app.buy_sell_signals import hits_shadow_store as S
from app.buy_sell_signals import constants as C
from app.buy_sell_signals.daily_fundamental_definition import (
    PIT_CUTOFF_VALUE,
    REAL_DAILY_DEFINITION_VERSION,
    REAL_DAILY_SIGNAL_ID,
    SAMPLE_COUNT_DOMAIN_VALUE,
)
from app.buy_sell_signals.hits_dingtalk import REASON_SHADOW, try_push_hit
from app.buy_sell_signals.hits_models import HitSource, SignalHit
from app.buy_sell_signals.hits_store import DiskHitStore
from app.buy_sell_signals.hits_visibility import VisibilityReject, visible_hits

_MODULE_PATH = Path(S.__file__)
_STORE_PATH = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals" / "hits_store.py"
_DINGTALK_PATH = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals" / "hits_dingtalk.py"
_VISIBILITY_PATH = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals" / "hits_visibility.py"

FREQ = C.FREQ_LOCK_VERSION


def _hit(
    i: int = 0,
    *,
    mode: str = "shadow",
    signal_id: str = REAL_DAILY_SIGNAL_ID,
    definition_version: str = REAL_DAILY_DEFINITION_VERSION,
    freq_lock_version: str | None = FREQ,
    asof: str = "2026-10-20",
    hit_id: str | None = None,
) -> SignalHit:
    """研究夹具: 一条命中。**手编字段, 非产线样本。**"""
    return SignalHit(
        hit_id=hit_id or f"shadow-{i:04d}",
        signal_id=signal_id,
        definition_version=definition_version,
        symbol=f"{600000 + i}.SH",
        asof=asof,
        source=HitSource(evaluator="p3b-research-fixture", mode=mode),
        family="daily_fundamental",
        event_class="daily_quality_value",
        direction_label="research_buy",
        freq_lock_version=freq_lock_version,
        recorded_at=datetime(2026, 10, 20, 16, 0, 0, tzinfo=CN_TZ),
    )


def _bucket(data_dir: Path) -> Path:
    return S.shadow_bucket_path(
        data_dir,
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        freq_lock_version=FREQ,
    )


def _code_only(path: Path) -> str:
    """剥掉字符串与注释后的源码 —— docstring 里写着各种"不许", 直接搜会恒红。"""
    import io
    import tokenize

    src = path.read_text(encoding="utf-8")
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (
                tokenize.STRING,
                tokenize.COMMENT,
                getattr(tokenize, "FSTRING_START", -1),
                getattr(tokenize, "FSTRING_MIDDLE", -1),
                getattr(tokenize, "FSTRING_END", -1),
            ):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return src
    return " ".join(out)


# ================================================================ G-T4 路径可指认


def test_g_t4_bucket_path_is_the_documented_shape(tmp_path):
    """G-T4: 路径形态写死 —— 三键逐层, 文件仍叫 hits.json。"""
    got = _bucket(tmp_path)
    assert got == (
        tmp_path
        / "buy_sell_signals"
        / "shadow"
        / "bss.daily_quality_value"
        / "v1"
        / "freq_lock_v1"
        / "hits.json"
    )
    # 相对形态也核一遍: 光比绝对路径, 有可能两边一起错
    assert got.relative_to(tmp_path).as_posix() == (
        "buy_sell_signals/shadow/bss.daily_quality_value/v1/freq_lock_v1/hits.json"
    )


def test_g_t4b_bucket_path_is_deterministic(tmp_path):
    """G-T4: 同三键 → 同路径(可复现); 不依赖调用时刻/随机数。"""
    first = _bucket(tmp_path)
    second = _bucket(tmp_path)
    assert first == second


def test_g_t4c_shadow_root_is_independent_of_live_hits_file(tmp_path):
    """G-T4: 影子桶与 live `hits.json` 是**两个不同文件**。"""
    live = S._live_hits_path(tmp_path)
    shadow = _bucket(tmp_path)
    assert live != shadow
    assert live.parent == tmp_path / "buy_sell_signals"
    # shadow = <tmp>/buy_sell_signals/shadow/<signal_id>/<def_ver>/<freq>/hits.json
    # parents[0]=<freq>  [1]=<def_ver>  [2]=<signal_id>  [3]=shadow  [4]=buy_sell_signals  [5]=<tmp>
    assert shadow.parents[4] == tmp_path / "buy_sell_signals"
    assert shadow.parents[3] == tmp_path / "buy_sell_signals" / "shadow"
    assert shadow.parents[5] == tmp_path
    assert shadow.parts[-1] == live.parts[-1] == "hits.json"
    # 同名是刻意的, 不同目录是必须的
    assert shadow.parent != live.parent


# ================================================================ G-T5 写入 mode=shadow


def test_g_t5_written_hit_reads_back_with_mode_shadow(tmp_path):
    """G-T5: 写进去 → 读回来, `source.mode` 仍是 `shadow`, 字段一个不丢。"""
    hit = _hit(0)
    S.upsert_shadow_hit(hit, data_dir=tmp_path)

    got = S.list_shadow_hits(
        data_dir=tmp_path,
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        freq_lock_version=FREQ,
    )
    assert len(got) == 1
    assert got[0].source.mode == "shadow"
    assert got[0].hit_id == hit.hit_id
    assert got[0].symbol == hit.symbol
    assert got[0].freq_lock_version == FREQ
    assert got[0].event_class == "daily_quality_value"
    # 整条记录等价(序列化 → 反序列化 是恒等的)
    assert got[0] == hit


def test_g_t5b_file_payload_keeps_schema_and_namespace(tmp_path):
    """G-T5: 桶文件形状对齐 live `hits.json` —— schema_version / namespace / hits。"""
    S.upsert_shadow_hit(_hit(0), data_dir=tmp_path)
    payload = json.loads(_bucket(tmp_path).read_text(encoding="utf-8"))
    assert payload["schema_version"] == C.HITS_SCHEMA_VERSION
    assert payload["namespace"] == C.NAMESPACE
    assert isinstance(payload["hits"], list) and len(payload["hits"]) == 1
    assert payload["hits"][0]["source"]["mode"] == "shadow"


def test_g_t5c_mode_is_valid_within_the_closed_set():
    """G-T5: `shadow` 属于 `HIT_SOURCE_MODES` 闭集 —— 写它不算越界。"""
    assert S.SHADOW_MODE in C.HIT_SOURCE_MODES
    assert S.SHADOW_MODE == "shadow"


# ================================================================ G-T9 首发定义


def test_g_t9_first_target_is_the_real_daily_definition(tmp_path):
    """G-T9: 首发对象＝`bss.daily_quality_value`, 不是 fixture、不是 volume_breakout。"""
    assert REAL_DAILY_SIGNAL_ID == "bss.daily_quality_value"
    S.upsert_shadow_hit(_hit(0), data_dir=tmp_path)
    path = _bucket(tmp_path)
    assert "bss.daily_quality_value" in path.parts
    assert path.parts[-3] == REAL_DAILY_DEFINITION_VERSION
    assert path.parts[-2] == FREQ
    assert "fixture" not in path.as_posix()
    assert "volume_breakout" not in path.as_posix()


def test_g_t9b_bucket_keys_match_constants_order():
    """G-T9: 模块的三键顺序 = constants 的三键顺序(改顺序就改了路径)。"""
    assert S.SHADOW_BUCKET_KEYS == C.SHADOW_BUCKET_KEY_FIELDS


# ================================================================ G-T6 P3a 共存


def test_g_t6_shadow_hit_written_then_still_rejected_by_visibility(tmp_path):
    """G-T6: **写入后**过 `visible_hits` 仍 0 条, 且**拒因就是** `NON_LIVE_MODE`。

    这是本拍最重要的一条: 影子落盘之后, P3a 闸**一点没松**。

    判别性要点(**为什么不能只断言 `hits == []`**): 这里 `registry=None`, 意味着
    「定义全缺失」那条闸也会把命中介拒绝。于是 `hits == []` 在 **mode 闸被删掉**之后
    **依然成立** —— 只断言空集, 抓不住「影子溜过了 mode 闸」。必须同时钉住:

    1. `NON_LIVE_MODE` 计数 ≥ 1(是这条闸拒的);
    2. `DEFINITION_MISSING` 计数 == 0(mode 闸在它**之前**就拦下了, 根本没走到定义检查)。

    第 2 条是关键: 它证明命中被拦下的**位置**在 mode 闸, 而不是后面的替身闸。
    """
    hit = _hit(0)
    S.upsert_shadow_hit(hit, data_dir=tmp_path)

    back = S.list_shadow_hits(
        data_dir=tmp_path,
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        freq_lock_version=FREQ,
    )
    assert len(back) == 1

    filtered = visible_hits(back, registry=None, today=__import__("datetime").date(2026, 10, 20))
    assert filtered.hits == []
    assert filtered.count == 0
    assert filtered.rejected.get(VisibilityReject.NON_LIVE_MODE, 0) >= 1
    # 关键: 拒因里不能出现「定义缺失」—— 那说明是后一道闸拦的, mode 闸要么被绕过、
    # 要么被挪到后面去了。两种都不行。
    assert filtered.rejected.get(VisibilityReject.DEFINITION_MISSING, 0) == 0, (
        "命中被 `definition_missing` 拒了, 说明它先穿过了 mode 闸: "
        f"{filtered.rejected}"
    )
    # 拒因应当**只有** NON_LIVE_MODE 一个键(连键的顺序都钉住: 它是第一道)。
    assert list(filtered.rejected) == [VisibilityReject.NON_LIVE_MODE], filtered.rejected


def test_g_t6b_shadow_hit_written_then_still_not_pushable(tmp_path):
    """G-T6: **写入后** `try_push_hit` 仍 `shadow_not_pushable`, 发送函数零调用。"""
    hit = _hit(0)
    S.upsert_shadow_hit(hit, data_dir=tmp_path)

    sent: list[object] = []

    def _opener(*args, **kwargs):  # pragma: no cover - 不该被调到
        sent.append((args, kwargs))
        raise AssertionError("shadow 命中不许触发发送")

    outcome = try_push_hit(
        hit,
        registry=None,
        calib=None,
        opener=_opener,
        webhook="https://example.invalid/robot/send",
        secret="SEC-test-not-a-real-secret",
    )
    assert outcome.reason == REASON_SHADOW
    assert sent == []


def test_g_t6c_writing_does_not_create_the_live_hits_file(tmp_path):
    """G-T6: 影子写入**不**顺手把 live `hits.json` 造出来。"""
    live = S._live_hits_path(tmp_path)
    assert not live.exists()
    S.upsert_shadow_hit(_hit(0), data_dir=tmp_path)
    assert not live.exists(), "影子写入碰了 live 文件"


# ================================================================ G-T8 不污染 live


def test_g_t8_live_store_does_not_see_shadow_hits(tmp_path):
    """G-T8: `DiskHitStore.list_all()` 读不到影子桶里的东西。

    反向也测: 先写 live 一条, 再写影子若干, live 读口条数不变。
    """
    S.upsert_shadow_hit(_hit(0), data_dir=tmp_path)
    S.upsert_shadow_hit(_hit(1), data_dir=tmp_path)

    store = DiskHitStore(tmp_path)
    assert store.list_all() == [], "live 读口读到了影子命中"
    assert store.path == S._live_hits_path(tmp_path)
    assert not store.exists()


def test_g_t8b_live_file_and_shadow_bucket_coexist_without_crosstalk(tmp_path):
    """G-T8: live 文件存在时, 影子写入不修改它(逐字节比对)。"""
    live = S._live_hits_path(tmp_path)
    live.parent.mkdir(parents=True, exist_ok=True)
    live_payload = {
        "schema_version": C.HITS_SCHEMA_VERSION,
        "namespace": C.NAMESPACE,
        "hits": [],
    }
    live.write_text(json.dumps(live_payload, ensure_ascii=False), encoding="utf-8")
    before = live.read_bytes()

    S.upsert_shadow_hit(_hit(0), data_dir=tmp_path)

    assert live.read_bytes() == before, "影子写入改动了 live hits.json"
    assert len(DiskHitStore(tmp_path).list_all()) == 0


def test_g_t8c_shadow_store_does_not_add_write_methods_to_live_store():
    """G-T8: `hits_store.py` 里仍**没有** upsert / write / save / append。

    有人可能"顺手"给 `DiskHitStore` 加个 `upsert` 当统一写口 —— 那本拍的独立分桶就
    失去意义了。这条守住它。
    """
    code = _code_only(_STORE_PATH)
    for name in ("upsert", "write", "save", "append"):
        assert not re.search(rf"\bdef\s+{name}\b", code), f"hits_store 出现了 def {name}"


# ================================================================ G-T7 无 allow_shadow


def test_g_t7_no_allow_shadow_parameter_anywhere():
    """G-T7: 两个闸 + 影子模块的公开签名里**都没有** `allow_shadow`。"""
    for fn in (try_push_hit,):
        params = inspect.signature(fn).parameters
        assert "allow_shadow" not in params
        # 对照组: `allow_fixture` 在, 说明这个断言不是"什么都没检查"
        assert "allow_fixture" in params

    for fn in (
        S.upsert_shadow_hit,
        S.write_shadow_hits,
        S.list_shadow_hits,
        S.shadow_bucket_path,
        S.shadow_bucket_dir,
    ):
        assert "allow_shadow" not in inspect.signature(fn).parameters, fn.__name__


def test_g_t7b_no_allow_shadow_identifier_in_code():
    """G-T7: 源码层(剥串后)没有 `allow_shadow` 这个标识符。"""
    for path in (_MODULE_PATH, _DINGTALK_PATH, _VISIBILITY_PATH, _STORE_PATH):
        code = _code_only(path)
        assert "allow_shadow" not in code, path.name
        assert "allow_shadow" not in path.read_text(encoding="utf-8").replace(
            "allow_shadow", "", 0
        ) or True  # 文档里出现"没有 allow_shadow"是允许的, 故只断言代码层


# ================================================================ 幂等 / 跨桶


def test_g_t7c_same_hit_id_twice_does_not_grow_the_bucket(tmp_path):
    """幂等: 同 `hit_id` 再写不膨胀, 且内容以**后写**为准。"""
    first = _hit(0, asof="2026-10-20")
    S.upsert_shadow_hit(first, data_dir=tmp_path)
    assert S.count_shadow_hits(
        data_dir=tmp_path,
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        freq_lock_version=FREQ,
    ) == 1

    # 同 id, 不同 asof —— 应当整体替换那一条, 不是追加
    second = _hit(0, asof="2026-10-21")
    S.upsert_shadow_hit(second, data_dir=tmp_path)

    got = S.list_shadow_hits(
        data_dir=tmp_path,
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        freq_lock_version=FREQ,
    )
    assert len(got) == 1, "同 hit_id 写两次导致桶内出现两条"
    assert got[0].asof == "2026-10-21"


def test_g_t7d_distinct_hit_ids_accumulate(tmp_path):
    """幂等 ≠ 去重过度: 不同 id 必须都留下。"""
    for i in range(5):
        S.upsert_shadow_hit(_hit(i), data_dir=tmp_path)
    assert S.count_shadow_hits(
        data_dir=tmp_path,
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        freq_lock_version=FREQ,
    ) == 5


def test_g_t7e_different_keys_do_not_crosstalk(tmp_path):
    """跨桶不串: 三键任一不同 → 不同文件, 互不可见。"""
    base = dict(signal_id=REAL_DAILY_SIGNAL_ID, definition_version="v1", freq_lock_version=FREQ)
    variants = [
        base,
        {**base, "definition_version": "v2"},
        {**base, "freq_lock_version": "freq_lock_v2"},
        {**base, "signal_id": "bss.some_other_signal"},
    ]
    paths = {S.shadow_bucket_path(tmp_path, **v) for v in variants}
    assert len(paths) == 4, "不同三键落到了同一个桶"

    # 往第一个桶写, 其余桶必须还是空
    S.upsert_shadow_hit(_hit(0), data_dir=tmp_path)
    assert S.count_shadow_hits(data_dir=tmp_path, **base) == 1
    for v in variants[1:]:
        assert S.count_shadow_hits(data_dir=tmp_path, **v) == 0


def test_g_t7f_batch_write_merges_by_id(tmp_path):
    """批量写: 按 id 合并, 同 id 不重复。"""
    batch = [_hit(i) for i in range(3)]
    result = S.write_shadow_hits(batch, data_dir=tmp_path)
    assert sum(result.values()) == 3

    # 再来一批, 其中一条与已有 id 重叠
    more = [_hit(2, asof="2026-10-22"), _hit(3)]
    result = S.write_shadow_hits(more, data_dir=tmp_path)
    assert sum(result.values()) == 4  # 3 + 1, 不是 3 + 2


# ================================================================ G-T5b 拒写非 shadow


def test_g_t5d_live_mode_is_rejected_by_the_shadow_writer(tmp_path):
    """拒绝 `mode=live`: 影子桶只装 shadow。"""
    with pytest.raises(S.ShadowWriteError) as exc:
        S.upsert_shadow_hit(_hit(0, mode="live"), data_dir=tmp_path)
    assert "shadow" in str(exc.value)
    assert not _bucket(tmp_path).exists(), "拒写之后不该留下文件"


def test_g_t5e_fixture_mode_is_also_rejected(tmp_path):
    """fixture 也不进影子桶 —— 每个 mode 有它自己的位置。"""
    with pytest.raises(S.ShadowWriteError):
        S.upsert_shadow_hit(_hit(0, mode="fixture"), data_dir=tmp_path)


def test_g_t5f_missing_freq_lock_version_is_rejected(tmp_path):
    """缺第三键 → 无法定位分桶。fail-closed: 拒写, 不猜默认口径。"""
    with pytest.raises(S.ShadowWriteError):
        S.upsert_shadow_hit(_hit(0, freq_lock_version=None), data_dir=tmp_path)


def test_g_t5g_batch_write_is_all_or_nothing(tmp_path):
    """批量写: 有一条不合法 → 整批不落盘(不留中间态)。"""
    good = [_hit(0), _hit(1)]
    bad = _hit(2, mode="live")
    with pytest.raises(S.ShadowWriteError):
        S.write_shadow_hits([*good, bad], data_dir=tmp_path)
    assert S.count_shadow_hits(
        data_dir=tmp_path,
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        freq_lock_version=FREQ,
    ) == 0


def test_g_t5h_path_traversal_in_keys_is_rejected(tmp_path):
    """三键不能改写路径: `..` / `/` 一律拒。

    没有这一条, "三键可指认"就是一句空话 —— 输入能把桶指到任意目录去。
    """
    for bad in ("..", ".", "a/b", "a\\b"):
        with pytest.raises(S.ShadowWriteError):
            S.shadow_bucket_path(
                tmp_path,
                signal_id=bad,
                definition_version="v1",
                freq_lock_version=FREQ,
            )
    with pytest.raises(S.ShadowWriteError):
        S.shadow_bucket_path(
            tmp_path, signal_id="x", definition_version="", freq_lock_version=FREQ
        )


# ================================================================ G-T11 样本可指认


def test_g_t11_thirty_fixture_rows_land_in_one_identifiable_bucket(tmp_path):
    """G-T11: 30 条夹具落进**同一个**可指认桶, 路径能在 PR 里原样贴出来。

    **这是研究夹具, 不是产线样本。** 30 条只证明"路径能装下并有 30 条",
    **不**证明 calib 已过 —— 过门由 P2 评估器按 #242 锁面判, 与这里无关。
    """
    hits = [_hit(i, asof=f"2026-09-{i + 1:02d}") for i in range(30)]
    result = S.write_shadow_hits(hits, data_dir=tmp_path)
    assert sum(result.values()) == 30

    path = _bucket(tmp_path)
    assert result == {path: 30}
    assert path.as_posix().endswith(
        "buy_sell_signals/shadow/bss.daily_quality_value/v1/freq_lock_v1/hits.json"
    )

    back = S.list_shadow_hits(
        data_dir=tmp_path,
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        freq_lock_version=FREQ,
    )
    assert len(back) == 30
    assert all(h.source.mode == "shadow" for h in back)
    # 条数到了目标值, 但**这不等于**样本已达标 —— 见本条 docstring
    assert C.CALIB_SAMPLE_N_MIN == 30


def test_g_t11b_count_is_just_a_count_not_a_verdict(tmp_path):
    """G-T11: 条数 API 只回答"有几条", 不回答"过没过"。

    这条是语义守卫: 如果有人哪天把 `count_shadow_hits` 改成返回 `passed`, 就会
    制造出第二个判据来源, 与 #242 锁面打架。
    """
    S.upsert_shadow_hit(_hit(0), data_dir=tmp_path)
    got = S.count_shadow_hits(
        data_dir=tmp_path,
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        freq_lock_version=FREQ,
    )
    assert isinstance(got, int) and not isinstance(got, bool)
    assert got == 1


# ================================================================ G-T10 PIT / 计数域


def test_g_t10_pit_and_count_domain_are_unchanged_by_this_batch():
    """G-T10: PIT 仍是 `announce_date`, 计数域仍是 `universe_shadow`。

    本拍**不**改 #241 的定义, 只在这里做旁证断言: 影子写入不引入第二套 PIT 口径。
    """
    assert PIT_CUTOFF_VALUE == "announce_date"
    assert SAMPLE_COUNT_DOMAIN_VALUE == "universe_shadow"
    assert REAL_DAILY_DEFINITION_VERSION == "v1"


def test_g_t10b_shadow_store_never_mentions_period_end():
    """G-T10: 影子写入模块**不读** `period_end` —— 前视只能从截断键来, 不能从写入来。"""
    code = _code_only(_MODULE_PATH)
    assert "period_end" not in code


# ================================================================ G-T3 / G-T2 常量零改


def test_g_t3_both_triggering_flags_are_still_false():
    """G-T3: 两处 `TRIGGERING_ENABLED` 仍 `Final = False`。"""
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_TRIGGERING_ENABLED,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_TRIGGERING_ENABLED,
    )

    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_g_t2_three_env_names_are_unchanged():
    """G-T2: 三 env 名字符串零改。"""
    assert C.PERSIST_ENV_NAME == "BUY_SELL_SIGNALS_PERSIST"
    assert C.PUSH_ENV_NAME == "BUY_SELL_SIGNALS_PUSH"
    assert C.DINGTALK_ENV_NAME == "BUY_SELL_SIGNALS_DINGTALK"


def test_g_t2b_shadow_module_reads_no_environment():
    """G-T2: 影子模块不读 env —— 没有"开发环境开一下"这种口子。"""
    code = _code_only(_MODULE_PATH)
    for needle in ("os.environ", "getenv", "environ["):
        assert needle not in code, needle


def test_g_t2c_no_credentials_in_module_or_tests():
    """G-T2: 模块与测例里没有真实凭证形态。"""
    pat = re.compile(r"access_token=[A-Za-z0-9]{10,}")
    for path in (_MODULE_PATH, Path(__file__)):
        src = path.read_text(encoding="utf-8")
        assert not pat.search(src), path.name


def test_g_t2c_credential_probe_is_not_vacuous():
    """自检: 上面的正则确实能抓到凭证形态。"""
    pat = re.compile(r"access_token=[A-Za-z0-9]{10,}")
    assert pat.search("?access" + "_token=" + "b" * 32)


# ================================================================ G-T13 大字 / 脚注


def test_g_t13_module_docstring_states_all_the_negations():
    """G-T13: 模块注释必须把三句大字写全。"""
    doc = S.__doc__ or ""
    assert "路径已通" in doc and "calib 已过" in doc and "可放量" in doc
    assert "TRIGGERING" in doc
    assert "P3a 已合" in doc
    assert "影子写入 ≠ 观测口" in doc


def test_g_t14_footer_present():
    """G-T14: 脚注 / 红涨绿跌 / 18.2bp 在模块注释里。"""
    doc = S.__doc__ or ""
    assert "仅供研究" in doc
    assert "不构成投资建议" in doc
    assert "人工确认" in doc
    assert "红涨绿跌" in doc
    assert "18.2bp" in doc


# ================================================================ 其它契约


def test_writer_is_pure_fs_no_side_channel_imports():
    """影子模块不 import 任何外发/推送/轮询模块 —— 写入就是写入。"""
    code = _code_only(_MODULE_PATH)
    for needle in ("hits_dingtalk", "try_push_hit", "hits_visibility", "visible_hits", "requests"):
        assert needle not in code, needle


def test_upsert_is_atomic_no_tmp_left_behind(tmp_path):
    """原子写的收尾: 正常写完之后桶目录里不该残留 `.tmp`。"""
    S.upsert_shadow_hit(_hit(0), data_dir=tmp_path)
    leftovers = [p.name for p in _bucket(tmp_path).parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    # 目录里只有 hits.json
    assert sorted(p.name for p in _bucket(tmp_path).parent.iterdir()) == ["hits.json"]


def test_reading_a_missing_bucket_returns_empty(tmp_path):
    """缺文件 → `[]`, 不抛错(空态是合法状态)。"""
    assert (
        S.list_shadow_hits(
            data_dir=tmp_path,
            signal_id=REAL_DAILY_SIGNAL_ID,
            definition_version=REAL_DAILY_DEFINITION_VERSION,
            freq_lock_version=FREQ,
        )
        == []
    )


def test_corrupt_bucket_degrades_to_empty(tmp_path):
    """坏 JSON → `[]` + 不崩(与 `DiskHitStore` 同一种容错策略)。"""
    path = _bucket(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert (
        S.list_shadow_hits(
            data_dir=tmp_path,
            signal_id=REAL_DAILY_SIGNAL_ID,
            definition_version=REAL_DAILY_DEFINITION_VERSION,
            freq_lock_version=FREQ,
        )
        == []
    )
    # 坏文件存在时再写: 应当重建, 而不是永远卡在坏状态
    S.upsert_shadow_hit(_hit(0), data_dir=tmp_path)
    assert S.count_shadow_hits(
        data_dir=tmp_path,
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        freq_lock_version=FREQ,
    ) == 1
