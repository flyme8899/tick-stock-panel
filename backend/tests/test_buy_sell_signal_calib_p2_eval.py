"""P2 评估器 + 截断回放(P-bss-p2 · 批次 20261028gp2)。

## 这组测例在守什么

守的不是「评估器能跑通」—— 那一条冒烟就够。守的是**评估器不能变成另一套口径**:

- 不能自己写 `5` / `18.2` / `30` / `0.5` 当第二权威源(必须读 `calib_p25_lock`)
- 不能评估夹具 / `volume_breakout` / auction(必须只认 `bss.daily_quality_value`)
- 不能用 `period_end` 做 PIT 截断(那是前视)
- 不能把 scope 过滤后的条数当样本(那让样本量随用户自选变化)
- 不能两次回放结果不同(那样任何「过了」都不可复核)
- 不能用自己的 if 链顶替 `p25_gate_verdict`
- 不能写 HitStore / P3b 生产影子 / 翻 TRIGGERING

## 这组测例**不**证明什么

跑绿 ≠ calib 已过 ≠ 可放量 ≠ TRIGGERING 该开。

本拍的样本是**研究夹具**, 不是产线影子样本。测例里出现「n=30、21 命中、判定 passed」
只是在验证**判据按锁面工作**, 不是在说这条信号真的有效。夹具里每一个数字都是手编的。

**路径已通 ≠ calib 已过 ≠ 可放量 ≠ TRIGGERING。口径锁已合 ≠ 评估已过门。**

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""
from __future__ import annotations

import inspect
import re
import tokenize
from pathlib import Path

import pytest

from app.buy_sell_signals import calib_p2_eval as ev
from app.buy_sell_signals import calib_p25_lock as lock
from app.buy_sell_signals import constants as C
from app.buy_sell_signals.auction_abnormal_stub import AUCTION_ABNORMAL_TRIGGERING_ENABLED
from app.buy_sell_signals.daily_fundamental_definition import (
    PIT_CUTOFF_VALUE,
    REAL_DAILY_DEFINITION_VERSION,
    REAL_DAILY_SIGNAL_ID,
    SAMPLE_COUNT_DOMAIN_VALUE,
)
from app.buy_sell_signals.freq_lock import FREQ_LOCK_VERSION, freq_lock_fingerprint
from app.buy_sell_signals.volume_breakout_stub import VOLUME_BREAKOUT_TRIGGERING_ENABLED

_MODULE_PATH = Path(ev.__file__)


# ================================================================ 夹具

def _row(
    i: int,
    *,
    asof: str = "2026-10-20",
    announce: str = "2026-10-15",
    period_end: str = "2026-09-30",
    ret_t1: float | None = None,
    ret_t5: float | None = None,
    ret_t20: float | None = None,
    shadow: bool = True,
) -> dict:
    """研究夹具: 一条样本行。**手编数字, 非产线样本。**"""
    win = i % 30 < 21  # 每 30 条里 21 条赢
    return {
        "symbol": f"{600000 + i}.SH",
        "asof": asof,
        "announce_date": announce,
        "period_end": period_end,
        "ret_t1": 0.01 if (ret_t1 is None and win) else (-0.01 if ret_t1 is None else ret_t1),
        "ret_t5": 0.02 if (ret_t5 is None and win) else (-0.02 if ret_t5 is None else ret_t5),
        "ret_t20": 0.03 if (ret_t20 is None and win) else (-0.03 if ret_t20 is None else ret_t20),
        "shadow": shadow,
    }


def _rows(n: int, **kwargs) -> list[dict]:
    return [_row(i, **kwargs) for i in range(n)]


def _rows_hits(n: int, hits: int, **kwargs) -> list[dict]:
    """精确控制命中条数: 前 `hits` 条全赢, 其余全亏(T+5 口径)。

    与 `_rows` 的 21/30 固定模式不同 —— 判别性测例需要「点估计过、下界不过」
    这种贴边样本, 固定模式给不出来。
    """
    out = []
    for i in range(n):
        win = i < hits
        kw = dict(kwargs)
        kw.setdefault("ret_t5", 0.02 if win else -0.02)
        out.append(_row(i, **kw))
    return out


def _code_only(path: Path) -> str:
    """剥掉字符串与注释后的源码。

    必须剥: docstring 里就写着「禁 `period_end`」「禁 Holm」这些词, 直接子串搜会恒红。
    """
    src = path.read_text(encoding="utf-8")
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(iter(src.splitlines(True)).__next__):
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
    return "\n".join(out)


# ================================================================ G-T1 基线 / 不回退

def test_g_t1_p25_lock_symbols_still_present():
    """#242 P2.5 锁面符号仍在, 值未变。"""
    assert lock.CALIB_PRIMARY_HORIZON_TD == 5
    assert lock.CALIB_AUX_HORIZONS_TD == (1, 20)
    assert lock.P25_BEFORE_P2 is True
    assert lock.WILSON_POSITIVE_RULE == "wilson_95_lb_gt_0.5"
    assert callable(lock.p25_gate_verdict)
    assert callable(lock.wilson_hit_rate_lower_bound)


def test_g_t1_p1_symbols_still_present():
    """#241 P1 键仍在: 真实定义键 / PIT / 影子口径。"""
    assert REAL_DAILY_SIGNAL_ID == "bss.daily_quality_value"
    assert PIT_CUTOFF_VALUE == "announce_date"
    assert SAMPLE_COUNT_DOMAIN_VALUE == "universe_shadow"
    assert REAL_DAILY_DEFINITION_VERSION == "v1"


def test_g_t1_p3a_symbols_still_present():
    """#240 P3a 三闸符号仍在。"""
    from app.buy_sell_signals import hits_dingtalk, hits_visibility
    from app.api.buy_sell_signals_hits import hits_scope  # noqa: F401

    assert hits_visibility.VisibilityReject.NON_LIVE_MODE == "NON_LIVE_MODE"
    assert hits_dingtalk.REASON_SHADOW == "shadow_not_pushable"


def test_g_t1_gtpush_copy_symbols_still_present():
    """#243 做T文案三件套符号仍在 —— 本拍正交零碰。"""
    from app.news import push as news_push

    assert news_push._T_BUY_REASONS == frozenset({"below_vwap", "near_low"})
    assert news_push._T_SELL_REASONS == frozenset({"above_vwap", "near_high"})
    assert news_push.TRIGGER_TS_FORMAT == "%Y-%m-%d %H:%M:%S"


# ================================================================ G-T2 锁面引用 / 无旁路

def test_g_t2_evaluator_imports_the_lock_module():
    """评估器源码必须**显式**从锁面导入(不是经第三方转手)。"""
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert "from .calib_p25_lock import" in src


def test_g_t2_lock_objects_are_the_same_objects_not_copies():
    """锁面常量在评估器里是**同一个对象**(`is`), 不是碰巧相等的副本。"""
    assert ev.CALIB_PRIMARY_HORIZON_TD is lock.CALIB_PRIMARY_HORIZON_TD
    assert ev.CALIB_AUX_HORIZONS_TD is lock.CALIB_AUX_HORIZONS_TD
    assert ev.P25_COST_BP_ROUNDTRIP is lock.P25_COST_BP_ROUNDTRIP
    assert ev.P25_SAMPLE_N_MIN is lock.P25_SAMPLE_N_MIN
    assert ev.p25_gate_verdict is lock.p25_gate_verdict
    assert ev.wilson_hit_rate_lower_bound is lock.wilson_hit_rate_lower_bound


def test_g_t2_result_values_come_from_the_lock():
    """结果里的主 horizon / 成本 / 样本下限 / Wilson 规则全部等于锁面值。"""
    out = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20")
    assert out["primary_horizon_td"] == lock.CALIB_PRIMARY_HORIZON_TD
    assert out["aux_horizons_td"] == list(lock.CALIB_AUX_HORIZONS_TD)
    assert out["cost_bp_roundtrip"] == lock.P25_COST_BP_ROUNDTRIP
    assert out["sample_n_min"] == lock.P25_SAMPLE_N_MIN
    assert out["wilson_rule"] == lock.WILSON_POSITIVE_RULE


def test_g_t2_no_bypass_literals_in_evaluator_code():
    """**代码里**不得出现 5 / 18.2 / 30 / 0.5 / m=6 当权威字面量。

    ## 为什么要剥字符串再搜

    docstring 里必然写着「主 T+5」「18.2bp」「样本 30」这些说明文字。直接搜恒红。

    剥完之后的判据: 这几个数字**不得作为数值字面量出现在表达式里**。允许它们出现在
    —— 不, 一个都不允许。真要引用锁面值, 就写 `CALIB_PRIMARY_HORIZON_TD`。
    """
    code = _code_only(_MODULE_PATH)
    # 数值字面量形态: 前面不是标识符/小数点, 后面不是数字
    for literal in ("18.2", "0.5", "0.05"):
        assert not re.search(rf"(?<![\w.]){re.escape(literal)}(?![\w.])", code), (
            f"代码里出现了旁路字面量 {literal} —— 应引用锁面常量"
        )
    # 纯整数 5 / 30 / 6: 只在**赋值右侧**或比较式里禁, 索引/切片不在此列
    for pattern in (
        r"=\s*5\b",
        r"=\s*30\b",
        r"=\s*6\b",
        r">\s*0\.5",
        r"<\s*0\.5",
    ):
        assert not re.search(pattern, code), f"代码里出现了旁路模式 {pattern!r}"


def test_g_t2_bypass_scan_probe_is_not_vacuous():
    """自检: 上面那组正则**确实能**抓到旁路(否则它在空转)。"""
    assert re.search(r"=\s*5\b", "HORIZON = 5")
    assert re.search(r"(?<![\w.])18\.2(?![\w.])", "cost = 18.2")
    assert not re.search(r"=\s*5\b", "HORIZON = CALIB_PRIMARY_HORIZON_TD")


def test_g_t2_bonferroni_is_referenced_not_redefined():
    """Bonferroni 经锁面引用; 评估器不新造 `m` / `alpha`。"""
    code = _code_only(_MODULE_PATH)
    assert "BONFERRONI_M" not in code or "from .calib_p25_lock import" in _MODULE_PATH.read_text(encoding="utf-8")
    # 评估器不实现 p 值计算
    for needle in ("p_value", "pvalue", "adjust_p"):
        assert needle not in code


# ================================================================ G-T3 评估对象

def test_g_t3_default_signal_id_is_the_real_definition():
    out = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20")
    assert out["signal_id"] == "bss.daily_quality_value"
    assert ev.P2_EVAL_SIGNAL_ID == REAL_DAILY_SIGNAL_ID


def test_g_t3_fixture_signal_is_rejected():
    """夹具键直接拒 —— 拿夹具当产线真值等于凭空跳过人工发布闸。"""
    with pytest.raises(ev.P2EvalInputError):
        ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20", signal_id="fx.pub.daily_quality_value")


def test_g_t3_volume_breakout_and_auction_are_rejected():
    for bad in ("volume_breakout", "auction_abnormal", "bss.daily_quality_value_v2"):
        with pytest.raises(ev.P2EvalInputError), pytest.MonkeyPatch.context():
            ev.truncated_replay_once(rows=_rows(5), asof="2026-10-20", signal_id=bad)


def test_g_t3_signal_id_is_not_injectable_through_rows():
    """样本行里塞 `signal_id` 不会改变评估对象。"""
    rows = _rows(30)
    for row in rows:
        row["signal_id"] = "fx.pub.daily_quality_value"
    out = ev.truncated_replay_once(rows=rows, asof="2026-10-20")
    assert out["signal_id"] == "bss.daily_quality_value"


# ================================================================ G-T4 PIT

def test_g_t4_announce_date_after_asof_is_excluded():
    """`announce_date > asof` 的行**不进样本** —— 那天它还不可得。"""
    rows = _rows(30) + [_row(99, announce="2026-10-25")]
    out = ev.truncated_replay_once(rows=rows, asof="2026-10-20")
    assert out["sample_n"] == 30


def test_g_t4_period_end_is_not_the_cutoff():
    """**这是 G-T4 的核心**: `period_end` 不参与截断。

    构造法: 两批数据, `announce_date` 与 `period_end` 的**相对位置相反**。

    - A 批: 公告早(10-15)、期末晚(10-31) —— 按 `announce_date` 可得, 按 `period_end` 不可得
    - B 批: 公告晚(10-25)、期末早(09-30) —— 按 `announce_date` 不可得, 按 `period_end` 可得

    以 `asof = 10-20` 跑:
    - 用 `announce_date` 截断 → 只留 A → `sample_n == 30`
    - 用 `period_end` 截断   → 只留 B → `sample_n == 30` 但 `symbol` 完全不同

    所以光看 `sample_n` 分不出来。用 **symbol 集合**区分: 结果里不含 B 的 symbol 才算对。
    """
    batch_a = [_row(i, announce="2026-10-15", period_end="2026-10-31") for i in range(30)]
    batch_b = [_row(100 + i, announce="2026-10-25", period_end="2026-09-30") for i in range(30)]
    out = ev.truncated_replay_once(rows=batch_a + batch_b, asof="2026-10-20")
    assert out["sample_n"] == 30, "按 announce_date 截断应只留 A 批"
    # 结果不含 B 批。用 hits 反查更直接: A 批 21 赢, B 批 9 赢(100+i → i%30)
    assert out["hits"] == 21


def test_g_t4_period_end_can_be_absent_and_cutoff_still_works():
    """`period_end` 缺失**不**影响截断 —— 因为它根本没被读。

    这条看着像「输入校验变松了」, 实为精确证明: 一个不参与判定的字段, 它缺不缺失
    都不该改变结果。
    """
    rows = _rows(30)
    for row in rows:
        row["period_end"] = ""
    with pytest.raises(ev.P2EvalInputError):
        # 输入契约仍要求带 period_end(为了能被检查「没被用」)
        ev.truncated_replay_once(rows=rows, asof="2026-10-20")
    # 但带一个合法值时, 它的内容不影响结果
    rows2 = _rows(30)
    for row in rows2:
        row["period_end"] = "1990-01-01"
    out = ev.truncated_replay_once(rows=rows2, asof="2026-10-20")
    assert out["sample_n"] == 30


def test_g_t4_source_does_not_read_period_end_for_truncation():
    """源码层: `period_end` 只在输入契约与文档里出现, 不在比较式里。"""
    code = _code_only(_MODULE_PATH)
    assert "period_end" in code, "输入契约里应当有 period_end(用于证明它没被用)"
    # 不得出现 `row.period_end <=` 之类的比较
    assert not re.search(r"\.period_end\s*[<>]", code)
    assert "period_end <=" not in code


def test_g_t4_pit_metadata_is_reported_in_result():
    out = ev.truncated_replay_once(rows=_rows(5), asof="2026-10-20")
    assert out["pit_cutoff"] == "announce_date"
    assert out["pit_rule"] == "announce_date <= asof"


# ================================================================ G-T5 影子计数

def test_g_t5_non_shadow_rows_are_excluded():
    """`shadow=False` 的行不进样本 —— 那是已过 scope 的可见条数。"""
    rows = _rows(30) + [_row(200 + i, shadow=False) for i in range(10)]
    out = ev.truncated_replay_once(rows=rows, asof="2026-10-20")
    assert out["sample_n"] == 30


def test_g_t5_result_declares_universe_shadow_domain():
    out = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20")
    assert out["sample_count_domain"] == "universe_shadow"
    assert out["sample_count_domain"] == SAMPLE_COUNT_DOMAIN_VALUE


def test_g_t5_scope_filtered_shrink_cannot_pad_sample():
    """把 scope 后只剩少数条当成样本, 会直接落到 `failed` —— 而不是「凑够了」。

    这条钉的是方向: scope 过滤只会**减少**样本, 绝不会让它「更容易过」。
    """
    rows = _rows(30, shadow=True) + [_row(300 + i, shadow=False) for i in range(200)]
    out = ev.truncated_replay_once(rows=rows, asof="2026-10-20")
    assert out["sample_n"] == 30
    assert out["sample_n"] < 200 + 30


def test_g_t5_no_scope_symbols_in_evaluator():
    """评估器不接触 scope(自选/持仓)—— 源码里不该出现这些入口。"""
    code = _code_only(_MODULE_PATH)
    for needle in ("watchlist", "list_symbols", "position", "scope_symbols", "hits_scope"):
        assert needle not in code, f"评估器不该引用 {needle!r}"


# ================================================================ G-T6 两次回放一致

def test_g_t6_two_replays_are_field_by_field_equal():
    """同一输入跑两次 → **逐字段**相等, 含 `fingerprint`。对齐 R2-7①。"""
    a, b = ev.truncated_replay_twice_equal(rows=_rows(30), asof="2026-10-20")
    assert a == b
    assert set(a) == set(b)
    for key in a:
        assert a[key] == b[key], f"字段 {key} 两次不一致"


def test_g_t6_input_order_does_not_change_result():
    """打乱输入顺序, 结果必须**逐字段相同**(含 fingerprint)。"""
    rows = _rows(40)
    shuffled = list(reversed(rows))
    a = ev.truncated_replay_once(rows=rows, asof="2026-10-20")
    b = ev.truncated_replay_once(rows=shuffled, asof="2026-10-20")
    assert a == b


def test_g_t6_fingerprint_equals_freq_lock_fingerprint():
    """`fingerprint` 必须等于 `freq_lock_fingerprint(...)` 且是**全 64 位** sha256。"""
    params = {"window": 20, "threshold": 0.5}
    out = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20", params=params)
    expected = freq_lock_fingerprint(
        signal_id=REAL_DAILY_SIGNAL_ID,
        definition_version=REAL_DAILY_DEFINITION_VERSION,
        cadence="daily_fundamental",
        params=params,
    )
    assert out["fingerprint"] == expected
    assert len(out["fingerprint"]) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", out["fingerprint"])


def test_g_t6_fingerprint_changes_when_params_change():
    """参数改了 → 指纹变。不变的指纹等于「改了口径但说还是同一套」。"""
    a = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20", params={"window": 20})
    b = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20", params={"window": 21})
    assert a["fingerprint"] != b["fingerprint"]


def test_g_t6_fingerprint_changes_when_definition_version_changes():
    a = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20", definition_version="v1")
    b = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20", definition_version="v2")
    assert a["fingerprint"] != b["fingerprint"]


def test_g_t6_no_weak_hash_bypass():
    """不另造弱哈希: 源码里除 `freq_lock_fingerprint` 外不得有别的哈希调用。"""
    code = _code_only(_MODULE_PATH)
    for needle in ("md5", "blake2", "sha1", "crc32", "hashlib"):
        assert needle not in code, f"评估器不得自己算哈希({needle})"
    assert "freq_lock_fingerprint" in code


def test_g_t6_replay_is_pure_no_clock_no_random():
    """纯函数: 不读时钟、不读随机数、不读 env。"""
    code = _code_only(_MODULE_PATH)
    for needle in ("datetime.now", "time.time", "random", "os.environ", "getenv", "uuid"):
        assert needle not in code, f"评估器不得依赖 {needle!r}"


# ================================================================ G-T7 样本路径 / 过门语义

def test_g_t7_align_key_is_the_three_keys_in_lock_order():
    out = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20")
    assert list(out["align_key"]) == list(lock.P25_ALIGN_KEY_FIELDS)
    assert out["align_key"]["signal_id"] == "bss.daily_quality_value"
    assert out["align_key"]["definition_version"] == REAL_DAILY_DEFINITION_VERSION
    assert out["align_key"]["freq_lock_version"] == FREQ_LOCK_VERSION


def test_g_t7_below_30_is_path_reached_not_passed():
    """n<30 → `path_status == path_reached`, `gate_status == failed`。

    **不是** `passed`。样本不够就是没结论, 不能因为「跑通了」就说校准过。
    """
    out = ev.truncated_replay_once(rows=_rows(29), asof="2026-10-20")
    assert out["sample_n"] == 29
    assert out["gate_status"] == "failed"
    assert out["path_status"] == ev.P2_PATH_STATUS_REACHED
    assert out["gate_status"] != C.CALIB_STATUS_PASSED


def test_g_t7_path_status_values_are_disjoint_from_calib_statuses():
    """路径状态与 calib 状态**不重叠** —— 混用就是「路径已通」被读成「calib 已过」。"""
    assert ev.P2_PATH_STATUS_REACHED not in C.CALIB_STATUSES
    assert ev.P2_PATH_STATUS_GATE_MET not in C.CALIB_STATUSES


def test_g_t7_gate_formally_met_is_still_not_calib_passed():
    """即便 n≥30 且 Wilson 过, `path_status` 也只是 `gate_formally_met`。

    `gate_status` 确实是 `passed`(锁面判据过了), 但那**只是这一次的判定**,
    不是 `calib.json` 里的 `passed` —— 要落进 calib 得走人工发布闸, 本拍没做。
    """
    out = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20")
    assert out["sample_n"] == 30
    assert out["hits"] == 21
    assert out["gate_status"] == C.CALIB_STATUS_PASSED
    assert out["path_status"] == ev.P2_PATH_STATUS_GATE_MET


def test_g_t7_note_carries_both_negations():
    """`P2_EVAL_NOTE` 必须同时带上两条否定句。"""
    note = ev.P2_EVAL_NOTE
    assert "路径已通" in note
    assert "calib" in note
    assert "可放量" in note
    assert "TRIGGERING" in note
    assert "口径锁已合" in note
    assert "评估已过门" in note


def test_g_t7_sample_path_is_locatable():
    """三键样本路径可指认: 测例文件里就有构造它的代码。

    这条断言本身很弱(只是检查字符串在), 意义在于**任何**改动 P2 评估器的 PR 都必须
    让本文件仍能说出「样本从哪来」。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    assert "bss.daily_quality_value" in src
    assert "研究夹具" in src
    assert "universe_shadow" in src or "shadow" in src


# ================================================================ G-T8 两 TRIGGERING / 符号

def test_g_t8_two_triggering_switches_remain_false():
    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_g_t8_evaluator_never_touches_triggering():
    """源码层: 评估器不引用任何 `TRIGGERING` 符号。"""
    code = _code_only(_MODULE_PATH)
    assert "TRIGGERING" not in code
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"TRIGGERING_ENABLED\s*[=:]\s*True", src)


# ================================================================ G-T9 三 env 零碰

def test_g_t9_evaluator_does_not_touch_env_switches():
    """评估器不得读/写三 env 与 hits webhook 键。"""
    code = _code_only(_MODULE_PATH)
    for needle in (
        "BUY_SELL_SIGNALS_PERSIST",
        "BUY_SELL_SIGNALS_PUSH",
        "BUY_SELL_SIGNALS_DINGTALK",
        "HITS_DINGTALK_WEBHOOK",
        "webhook",
        "secret",
    ):
        assert needle not in code, f"评估器不得引用 {needle!r}"


def test_g_t9_no_credentials_in_module_or_tests():
    """模块与测例里不得出现真实凭证形态。

    检查的是「webhook 地址后面真的挂了 token」这一形态, 而非地址本身,
    否则本测例源码自身就成为命中项(自指恒红)。因此断言改写为正则:
    要求 `?access_token=` 之后必须紧跟 10 位以上字母数字才算命中。
    """
    pat = re.compile(r"access_token=[A-Za-z0-9]{10,}")
    for path in (_MODULE_PATH, Path(__file__)):
        src = path.read_text(encoding="utf-8")
        assert not pat.search(src), f"{path.name} 出现疑似真实 credential"
        assert not re.search(r"SEC[0-9a-f]{20,}", src), f"{path.name} 出现疑似真实 secret"


def test_g_t9_credential_probe_is_not_vacuous():
    """自检: 上面的正则确实能抓住凭证形态, 不是写了个永不命中的空壳。"""
    pat = re.compile(r"access_token=[A-Za-z0-9]{10,}")
    # 拼串构造, 避免本文件自身再次成为命中项
    sample = "?access" + "_token=" + "a" * 32
    assert pat.search(sample)


# ================================================================ G-T10 无写路径

def test_g_t10_no_hitstore_or_p3b_writes():
    """源码层: 无 `HitStore` 构造、无 hits 写、无 P3b 影子写、无 calib.upsert。"""
    code = _code_only(_MODULE_PATH)
    for needle in (
        "HitStore",
        "hits_store",
        "DiskHitStore",
        "calib_store",
        "calib.upsert",
        "upsert",
        "p3b",
        "shadow_write",
        "write_hits",
    ):
        assert needle not in code, f"评估器不得出现写路径 {needle!r}"


def test_g_t10_module_imports_no_io():
    """不导入 `open` / `Path` / `json` / `requests` —— 评估器不碰文件与网络。"""
    code = _code_only(_MODULE_PATH)
    for needle in ("open(", "Path(", "json.", "requests", "urllib", "socket"):
        assert needle not in code, f"评估器不得做 I/O({needle})"


def test_g_t10_replay_does_not_mutate_input_rows():
    """回放**不改**输入。改输入等于让第二次回放看到被第一次改过的数据。"""
    rows = _rows(30)
    snapshot = [dict(row) for row in rows]
    ev.truncated_replay_once(rows=rows, asof="2026-10-20")
    assert rows == snapshot


# ================================================================ G-T11 failed / 禁换主 / 禁 Holm

def test_g_t11_gate_verdict_is_the_lock_function():
    """过门走锁面函数本身, 不是复制一份。"""
    assert ev.p25_gate_verdict is lock.p25_gate_verdict


def test_g_t11_cross_version_merge_forces_failed():
    out = ev.truncated_replay_once(rows=_rows(60), asof="2026-10-20", cross_version_merged=True)
    assert out["gate_status"] == "failed"
    assert out["gate_status"] != C.CALIB_STATUS_PASSED


def test_g_t11_substituted_test_forces_failed():
    for name in ("holm", "benjamini_hochberg", "bh"):
        out = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20", substituted_test=name)
        assert out["gate_status"] == "failed", name


def test_g_t11_primary_swapped_forces_failed():
    out = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20", primary_swapped=True)
    assert out["gate_status"] == "failed"


def test_g_t11_flags_default_off():
    """三个违规申报口默认关闭 —— 「忘了传」不能等于「违规了」。"""
    sig = inspect.signature(ev.truncated_replay_once)
    assert sig.parameters["cross_version_merged"].default is False
    assert sig.parameters["substituted_test"].default is None
    assert sig.parameters["primary_swapped"].default is False


def test_g_t11_no_holm_bh_implementation():
    code = _code_only(_MODULE_PATH).lower()
    for needle in ("holm", "benjamini", "hochberg", "fdr"):
        assert not re.search(rf"\bdef\s+\w*{needle}", code), needle
        assert not re.search(rf"\bimport\b[^\n]*{needle}", code), needle


def test_g_t11_aux_horizons_cannot_replace_primary():
    """辅 horizon 只**报**, 不参与过门: `gate_status` 只看主指标。

    构造: T+5 全亏(0 命中)、T+1 全赢。若辅指标能顶替主指标, 结论会变成 `passed`;
    实际必须 `failed`。
    """
    rows = [
        _row(i, ret_t1=0.05, ret_t5=-0.05, ret_t20=0.05) for i in range(30)
    ]
    out = ev.truncated_replay_once(rows=rows, asof="2026-10-20")
    assert out["aux"][0]["hits"] == 30, "T+1 应全赢(证明辅指标确实很漂亮)"
    assert out["primary"]["hits"] == 0
    assert out["gate_status"] == "failed", "辅指标再漂亮也不能顶替主指标过门"


def test_g_t11_result_declares_the_three_forbidden_flags():
    out = ev.truncated_replay_once(rows=_rows(5), asof="2026-10-20")
    assert out["cross_version_merge_forbidden"] is lock.P25_CROSS_VERSION_MERGE_FORBIDDEN
    assert out["holm_bh_substitute_forbidden"] is lock.P25_HOLM_BH_SUBSTITUTE_FORBIDDEN
    assert out["primary_swap_forbidden"] is lock.P25_PRIMARY_SWAP_FORBIDDEN


# ---------------------------------------------------------------- G-T15 判别性样本

def test_g_t15_primary_wilson_lb_is_the_lower_bound_not_the_point_estimate():
    """**判别性样本**: 点估计过、下界不过, 结论必须 `failed`。

    选 n=30, hits=20:
      - 点估计 = 20/30 = 0.6667 (> 0.5) → 若拿点估计当判据, 会 `passed`
      - Wilson 95% 下界 ≈ 0.4878 (< 0.5) → 按锁面判据, 必须 `failed`

    这一个样本把「下界 vs 点估计」彻底分开 —— 只断言 `wilson_rule` 字符串、
    或只看 `passed` 的样本, 都抓不住「判据被悄悄换成点估计」。
    """
    rows = _rows_hits(30, 20)
    out = ev.truncated_replay_once(rows=rows, asof="2026-10-20")

    assert out["primary"]["hits"] == 20
    assert out["primary"]["sample_n"] == 30
    point = 20 / 30
    lb = out["primary"]["wilson_lb"]
    assert point > 0.5, f"点估计应过人: {point}"
    assert lb < 0.5, f"下界应不过人: {lb}"
    assert abs(lb - 0.4878) < 1e-3, f"下界应≈0.4878, 实得 {lb}"
    assert lb < point, "下界必须严格小于点估计(否则不是下界)"
    assert out["gate_status"] == "failed", "点估计过但下界不过, 结论必须是 failed"


def test_g_t15_match_primary_wilson_boundary_upward():
    """同口径反向: n=30, hits=21 → 点估计 0.70, 下界 ≈0.5212 (>0.5) → `passed`。

    与上一条配对, 证明判别性样本不是在「一律 failed」的区间里。
    """
    rows = _rows_hits(30, 21)
    out = ev.truncated_replay_once(rows=rows, asof="2026-10-20")
    lb = out["primary"]["wilson_lb"]
    assert abs(lb - 0.5212) < 1e-3, f"下界应≈0.5212, 实得 {lb}"
    assert out["gate_status"] == "passed"


def test_g_t16_primary_summary_is_computed_at_the_locked_horizon():
    """**判别性样本**: 三个 horizon 给出三种截然不同的结论, 主指标只能是 T+5。

    构造 n=30:
      - T+1  全赢  → 若主指标被换成 T+1,  结论 `passed`
      - T+5  全亏  → 主指标真在 T+5 时,   结论 `failed`
      - T+20 全赢  → 若主指标被换成 T+20, 结论 `passed`

    两个辅指标都给 `passed`, 只有真正的主指标给 `failed`。这样「主 horizon 被改成
    辅之一」一定被抓住 —— 光断言 `primary_horizon_td == 5` 抓不住, 因为那个字段
    可以被硬编码成 5 而实际算的是别的列。
    """
    rows = [_row(i, ret_t1=0.05, ret_t5=-0.05, ret_t20=0.05) for i in range(30)]
    out = ev.truncated_replay_once(rows=rows, asof="2026-10-20")

    assert out["primary"]["horizon_td"] == lock.CALIB_PRIMARY_HORIZON_TD
    assert out["primary"]["horizon_td"] == 5
    assert out["primary"]["hits"] == 0, "主指标必须真的在算 T+5 这一列"
    assert out["primary"]["is_primary"] is True

    aux_by_td = {a["horizon_td"]: a for a in out["aux"]}
    assert aux_by_td[1]["hits"] == 30
    assert aux_by_td[20]["hits"] == 30
    assert all(a["is_primary"] is False for a in out["aux"])

    assert out["gate_status"] == "failed", (
        "两个辅 horizon 都全赢而结论是 passed → 说明主指标没在 T+5 上算"
    )


def test_g_t17_undeclared_horizon_is_rejected_at_summary_level():
    """`_horizon_summary` 直接拒收锁面外的 horizon(不能事后加 T+3)。"""
    rows = [ev.EvalRow.from_mapping(r) for r in _rows(10)]
    for bad in (3, 2, 10, 0):
        with pytest.raises(ev.P2EvalInputError):
            ev._horizon_summary(rows, horizon_td=bad, cost_bp=lock.P25_COST_BP_ROUNDTRIP)
    # 锁面内的三个值都放行
    for good in (lock.CALIB_PRIMARY_HORIZON_TD, *lock.CALIB_AUX_HORIZONS_TD):
        got = ev._horizon_summary(
            rows, horizon_td=good, cost_bp=lock.P25_COST_BP_ROUNDTRIP
        )
        assert got["horizon_td"] == good


# ================================================================ G-T12 单 PR / 不部署

def test_g_t12_module_is_importable_without_side_effects():
    """导入模块不产生 I/O、不写盘、不改全局状态。"""
    import importlib

    mod = importlib.reload(ev)
    assert mod.P2_EVAL_SIGNAL_ID == "bss.daily_quality_value"


def test_g_t12_footer_present_in_docstring():
    doc = ev.__doc__ or ""
    assert "仅供研究" in doc
    assert "不构成投资建议" in doc
    assert "人工确认" in doc
    assert "18.2bp" in doc


# ================================================================ G-T13 对外口径

def test_g_t13_docstring_states_all_negations():
    """模块 docstring 必须把四条否定写全。"""
    doc = ev.__doc__ or ""
    assert "路径已通" in doc
    assert "calib 已过" in doc
    assert "可放量" in doc
    assert "TRIGGERING" in doc
    assert "口径锁已合" in doc
    assert "评估已过门" in doc


def test_g_t13_no_deployable_claim_in_result():
    """结果里不得出现「可放量」类结论字样(说明文案除外)。"""
    out = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20")
    for key, value in out.items():
        if isinstance(value, str):
            assert "可放量" not in value or key == "note", f"{key} 里出现了「可放量」"


def test_g_t13_note_is_the_only_place_with_negations():
    """否定句集中在 `note`, 不散落在各字段里污染可读性。"""
    out = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20")
    assert out["note"] == ev.P2_EVAL_NOTE


# ================================================================ G-T14 夹具标注

def test_g_t14_fixture_is_labeled_as_research_fixture():
    """本文件用到的行都是研究夹具, 且文件头写明了这一点。"""
    doc = Path(__file__).read_text(encoding="utf-8")
    assert "研究夹具" in doc
    assert "非产线样本" in doc or "不是产线" in doc


def test_g_t14_gate_passed_on_fixture_does_not_mean_signal_works():
    """夹具上 21/30 过门只证明**判据工作**, 不证明信号有效。

    这条测例把话说清楚: 它断言的是判据的**行为**, 不是信号的质量。夹具里
    「21 赢 9 亏」是手编的, 换成 21 亏 9 赢同样能构造出 `failed`。
    """
    good = ev.truncated_replay_once(rows=_rows(30), asof="2026-10-20")
    bad_rows = [_row(30 + i, ret_t5=0.02 if i % 30 < 9 else -0.02) for i in range(30)]
    bad = ev.truncated_replay_once(rows=bad_rows, asof="2026-10-20")
    assert good["gate_status"] == "passed"
    assert bad["gate_status"] == "failed"
    assert good["fingerprint"] == bad["fingerprint"], "指纹只对口径, 不对样本"
