"""P2.5 口径预登记锁面(P-bss-p2.5 · 批次 20261028gp25)。

## 这组测例在守什么

守的不是「常量能读到」—— 那一条冒烟就够。守的是**口径不能被悄悄换掉**:

- 主 horizon 只能有一个(T+5), 换成 T+1 / T+20 必须红
- 辅 horizon 两个都要在, 不许只挑一个
- 成本必须是**同一个对象**(`is`), 不是碰巧相等的两个数字
- 命中率判据必须是 Wilson **95% 下界**, 不是点估计、不是 90%
- 样本下限必须是**同一个对象**(`is`)
- 三键字段序必须与 `CalibRecord` / `FreqLockStamp` 的实际 `align_key` 逐位一致
- 跨版本合并必须被禁, 而且判定函数真的会因此返回 `failed`
- Bonferroni 必须引用既有钉, Holm/BH 替换必须被禁
- 任一门未过**必须是 `failed`**, 不许是 `pending`
- 禁事后换主指标
- 定序必须 P2.5 ≺ P2
- 「口径锁 ≠ 可放量」这句话必须真的在代码里

每一条都写成**断言具体值**, 不写成「字段存在」。

## 这组测例**不**证明什么

跑绿 ≠ 样本凑够 ≠ 评估跑过 ≠ calib 过了 ≠ 可放量 ≠ TRIGGERING 该开。
本拍只锁口径, 没有评估器、没有回放、没有影子写入、没有写路径。

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""
from __future__ import annotations

import inspect
import math
import re
import tokenize
from pathlib import Path

import pytest

from app.buy_sell_signals import calib_p25_lock as p25
from app.buy_sell_signals import constants as C
from app.buy_sell_signals.auction_abnormal_stub import AUCTION_ABNORMAL_TRIGGERING_ENABLED
from app.buy_sell_signals.calib_models import CalibRecord
from app.buy_sell_signals.freq_lock import FreqLockStamp
from app.buy_sell_signals.volume_breakout_stub import VOLUME_BREAKOUT_TRIGGERING_ENABLED

_MODULE_PATH = Path(p25.__file__)


def _code_only(path: Path) -> str:
    """剥掉字符串字面量与注释后的源码。

    必须剥: docstring 里就写着「Holm / BH」「pending」这些词, 直接子串搜会恒红
    (P-bss-p1 踩过同一个坑)。剥完之后剩下的是**真的在跑**的代码。
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


# ================================================================ G-T1 主 horizon


def test_g_t1_primary_horizon_is_five_trading_days_exactly():
    """主 horizon 是 **T+5**, 写死。不是 1, 不是 20, 不是「可配置」。"""
    assert p25.CALIB_PRIMARY_HORIZON_TD == 5
    assert isinstance(p25.CALIB_PRIMARY_HORIZON_TD, int)
    assert not isinstance(p25.CALIB_PRIMARY_HORIZON_TD, bool)


def test_g_t1_primary_horizon_is_not_any_aux_value():
    """主 horizon **不在**辅值里 —— 否则「主/辅」这个区分就没有意义了。"""
    assert p25.CALIB_PRIMARY_HORIZON_TD not in p25.CALIB_AUX_HORIZONS_TD


def test_g_t1_primary_horizon_unit_is_trading_days():
    """单位是**交易日**, 与 `CALIB_EVAL_WINDOW_UNIT` 一致。

    不写「天」: 5 个自然日跨不过一个交易周, 5 个交易日才是一个完整交易周。
    """
    assert C.CALIB_EVAL_WINDOW_UNIT == "trading_days"
    assert p25.CALIB_EVAL_WINDOW_UNIT is C.CALIB_EVAL_WINDOW_UNIT


# ================================================================ G-T2 辅 horizon


def test_g_t2_aux_horizons_are_exactly_one_and_twenty():
    """辅 horizon 恰好 `(1, 20)`, 顺序即呈现顺序。"""
    assert p25.CALIB_AUX_HORIZONS_TD == (1, 20)
    assert isinstance(p25.CALIB_AUX_HORIZONS_TD, tuple)


def test_g_t2_aux_horizons_have_no_duplicates_and_are_sorted():
    """去重且升序 —— 重复项会让「报了几个辅 horizon」这个计数失真。"""
    aux = p25.CALIB_AUX_HORIZONS_TD
    assert len(set(aux)) == len(aux)
    assert list(aux) == sorted(aux)


def test_g_t2_aux_horizons_are_mandatorily_reported():
    """两个辅 horizon **都必须报**, 不许只挑好看的那个。"""
    assert p25.P25_AUX_MANDATORY_REPORTED is True


# ================================================================ G-T3 成本


def test_g_t3_cost_is_the_same_object_as_calib_pin():
    """成本是**同一个对象**, 不是碰巧相等的两个数字。"""
    assert p25.P25_COST_BP_ROUNDTRIP is C.CALIB_COST_BP_ROUNDTRIP


def test_g_t3_cost_value_is_18_2_rounded_cost_no_cheating():
    """成本数值就是 18.2, 且被标为「引用既有钉」。

    这个断言看着是同义反复, 但它挡的是「有人把 `is` 改成 `== 18.2` 字面量、
    再把 `constants` 里的钉改掉」这一种 —— 那时 `is` 会红, 而这里还会再红一次。
    """
    assert p25.P25_COST_BP_ROUNDTRIP == 18.2
    assert p25.P25_COST_IS_CALIB_PIN is True


def test_g_t3_cost_is_roundtrip_not_one_way():
    """成本是**来回**口径: 文档与常量名里必须是 roundtrip。

    单边成本算 T+5 收益 = 每笔少算一倍成本 = 恒对「信号不行」有利。
    """
    note = p25.P25_LOCK_NOTE
    assert "18.2" in note
    assert "来回" in note


# ================================================================ G-T4 Wilson 判据


def test_g_t4_wilson_confidence_is_95_percent():
    assert p25.WILSON_CONFIDENCE == 0.95


def test_g_t4_wilson_lb_threshold_is_strictly_greater_than_half():
    """门槛是 **下界 > 0.5**, 严格大于。"""
    assert p25.WILSON_HIT_RATE_LB_MIN == 0.5
    assert p25.WILSON_POSITIVE_RULE == "wilson_95_lb_gt_0.5"


def test_g_t4_n30_requires_21_hits_20_is_not_enough():
    """n=30 时 **21 命中才过**, 20 命中不过。

    这一条是判据的实质: 点估计 20/30 = 0.667 看着「过六成六」, 但下界只有 0.488。
    用点估计当门的话 20/30 会过, 那就是用一次投硬币的结果当硬币不偏的证据。
    """
    assert p25.p25_positive_verdict(20, 30) is False
    assert p25.p25_positive_verdict(21, 30) is True
    assert p25.wilson_hit_rate_lower_bound(20, 30) < 0.5
    assert p25.wilson_hit_rate_lower_bound(21, 30) > 0.5


def test_g_t4_point_estimate_would_have_passed_more_cases_than_wilson():
    """**这条是判据的精神**: 存在一些 (hits, n) 使点估计过而 Wilson 下界不过。

    如果两者恒同, 那么用 Wilson 就是白写的。这里钉住差异确实存在。
    """
    divergent = [
        (k, n)
        for n in range(30, 61)
        for k in range(0, n + 1)
        if (k / n) > 0.5 and not p25.p25_positive_verdict(k, n)
    ]
    assert divergent, "Wilson 下界与点估计判据完全同构 —— 那这个判据没有意义"
    # 典型样本: 20/30
    assert (20, 30) in divergent


def test_g_t4_wilson_lb_is_monotone_in_hits_for_fixed_n():
    """n 固定时, 命中越多下界单调不降。判据不能因为多命中反而变严。"""
    lbs = [p25.wilson_hit_rate_lower_bound(k, 30) for k in range(0, 31)]
    assert lbs == sorted(lbs)


def test_g_t4_wilson_lb_never_exceeds_point_estimate():
    """下界恒 ≤ 点估计 —— 它是保守方向的一侧。"""
    for n in (30, 50, 100):
        for k in range(0, n + 1):
            assert p25.wilson_hit_rate_lower_bound(k, n) <= (k / n) + 1e-12


def test_g_t4_wilson_lb_closed_forms_at_extremes():
    """k=0 时下界为 0; k=n 时下界 < 1(小样本永远不到 1)。"""
    assert p25.wilson_hit_rate_lower_bound(0, 30) == pytest.approx(0.0)
    assert p25.wilson_hit_rate_lower_bound(30, 30) < 1.0
    assert p25.wilson_hit_rate_lower_bound(30, 30) > 0.85


def test_g_t4_wilson_matches_hand_computed_value_for_20_of_30():
    """与手算值对齐到 1e-9, 防止公式被改成「某种近似」。"""
    z = 1.959963984540054
    n, k = 30, 20
    p = k / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    expected = (center - margin) / denom
    assert p25.wilson_hit_rate_lower_bound(20, 30) == pytest.approx(expected, abs=1e-9)
    assert expected == pytest.approx(0.4878, abs=1e-3)


def test_g_t4_empty_sample_raises_instead_of_returning_zero():
    """空样本**抛错**, 不返回 0.0。

    返回 0.0 会被当成「算过了, 0 命中」; 实际是「没样本, 没有结论」。
    """
    with pytest.raises(ValueError):
        p25.wilson_hit_rate_lower_bound(0, 0)
    with pytest.raises(ValueError):
        p25.wilson_hit_rate_lower_bound(0, -1)


def test_g_t4_impossible_hit_counts_raise():
    """hits > n 或 hits < 0 抛错, 不静默夹取。"""
    with pytest.raises(ValueError):
        p25.wilson_hit_rate_lower_bound(31, 30)
    with pytest.raises(ValueError):
        p25.wilson_hit_rate_lower_bound(-1, 30)


def test_g_t4_other_confidence_levels_are_refused():
    """换置信水平直接抛错 —— 「Wilson 90%」不能悄悄顶替「Wilson 95%」。"""
    with pytest.raises(ValueError):
        p25.wilson_hit_rate_lower_bound(21, 30, confidence=0.90)


def test_g_t4_bool_is_not_accepted_as_int():
    """`True` 不是 `1` 命中。bool 是 int 子类, 不显式挡就会被混进去。"""
    with pytest.raises(ValueError):
        p25.wilson_hit_rate_lower_bound(True, 30)


# ================================================================ G-T5 样本下限


def test_g_t5_sample_min_is_the_same_object_as_calib_pin():
    """样本下限是 `CALIB_SAMPLE_N_MIN`, 不另写字面量 30。

    ## 为什么 `is` 在这里**不够**, 必须补源码级检查

    Python 对 `-5..256` 的小整数做驻留, 所以有人把这里改写成字面量 `30` 时
    `P25_SAMPLE_N_MIN is CALIB_SAMPLE_N_MIN` **仍然是 `True`** —— 光靠 `is`
    抓不住「抄了一遍数字」这件事(负控实测: 单 `is` 断言在改写后 60 条全绿)。

    所以补一条源码级断言: 赋值右侧必须是**标识符**, 不是数字字面量。
    这样「改成 30」会红, 「改成 `CALIB_SAMPLE_N_MIN`」不会。
    """
    assert p25.P25_SAMPLE_N_MIN is C.CALIB_SAMPLE_N_MIN
    assert p25.P25_SAMPLE_N_MIN == 30
    assert p25.P25_SAMPLE_IS_CALIB_PIN is True


def test_g_t5_sample_min_rhs_is_an_identifier_not_a_number():
    """赋值右侧必须是标识符(`CALIB_SAMPLE_N_MIN`), 不是数字字面量。

    同上: 小整数驻留让 `is` 在字面量下也成立, 只能从源码文本上区分。
    """
    src = _code_only(_MODULE_PATH)
    m = re.search(r"^P25_SAMPLE_N_MIN\s*:\s*Final\s*=\s*(\S+)\s*$", src, re.M)
    assert m, "找不到 P25_SAMPLE_N_MIN 的赋值"
    rhs = m.group(1)
    assert rhs == "CALIB_SAMPLE_N_MIN", f"必须是引用既有钉, 收到 {rhs!r}"
    assert not rhs.isdigit()


def test_g_t5_cost_rhs_is_an_identifier_not_a_number():
    """成本同理: 右侧必须是标识符, 不是 `18.2`(浮点此时幸而未被驻留, 仍一并钉住)。"""
    src = _code_only(_MODULE_PATH)
    m = re.search(r"^P25_COST_BP_ROUNDTRIP\s*:\s*Final\s*=\s*(\S+)\s*$", src, re.M)
    assert m, "找不到 P25_COST_BP_ROUNDTRIP 的赋值"
    assert m.group(1) == "CALIB_COST_BP_ROUNDTRIP"


# ================================================================ G-T6 三键对齐


def test_g_t6_align_key_fields_are_the_three_keys_in_order():
    assert p25.P25_ALIGN_KEY_FIELDS == (
        "signal_id",
        "definition_version",
        "freq_lock_version",
    )


def test_g_t6_align_key_fields_match_freq_lock_stamp_order():
    """与 `FreqLockStamp.align_key` 的**实际取值顺序**逐位一致。

    不是比对字段名列表, 而是造一条真记录拿它的 `align_key` 元组来对 ——
    这样 `FreqLockStamp.align_key` 若被改成 `(definition_version, signal_id, ...)`,
    这里立刻红。
    """
    stamp = FreqLockStamp(
        signal_id="bss.daily_quality_value",
        definition_version="v1",
        freq_lock_version="v1",
        cadence="daily_fundamental",
        asof="2026-10-28",
    )
    key = stamp.align_key
    assert len(key) == len(p25.P25_ALIGN_KEY_FIELDS)
    # 逐位: 第 i 个字段的值应当是 key[i]
    by_name = {
        "signal_id": stamp.signal_id,
        "definition_version": stamp.definition_version,
        "freq_lock_version": stamp.freq_lock_version,
    }
    for i, name in enumerate(p25.P25_ALIGN_KEY_FIELDS):
        assert key[i] == by_name[name], f"第 {i} 位应为 {name}"


def test_g_t6_align_key_fields_match_calib_record_order():
    """与 `CalibRecord.align_key` 同样逐位一致。"""
    rec = CalibRecord(
        signal_id="bss.daily_quality_value",
        definition_version="v1",
        freq_lock_version="v1",
        status="pending",
        eval_window=5,
        sample_n=30,
        cost_bp_roundtrip=C.CALIB_COST_BP_ROUNDTRIP,
        baseline_ref="baseline_p25_lock_probe",
    )
    key = rec.align_key
    by_name = {
        "signal_id": rec.signal_id,
        "definition_version": rec.definition_version,
        "freq_lock_version": rec.freq_lock_version,
    }
    for i, name in enumerate(p25.P25_ALIGN_KEY_FIELDS):
        assert key[i] == by_name[name], f"第 {i} 位应为 {name}"


def test_g_t6_calib_and_stamp_align_keys_agree_with_each_other():
    """`CalibRecord.align_key == FreqLockStamp.align_key`(同一套三键值)。"""
    stamp = FreqLockStamp(
        signal_id="s",
        definition_version="v1",
        freq_lock_version="fl1",
        cadence="daily_fundamental",
        asof="2026-10-28",
    )
    rec = CalibRecord(
        signal_id="s",
        definition_version="v1",
        freq_lock_version="fl1",
        status="pending",
        eval_window=5,
        sample_n=30,
        cost_bp_roundtrip=C.CALIB_COST_BP_ROUNDTRIP,
        baseline_ref="baseline_p25_lock_probe",
    )
    assert rec.align_key == stamp.align_key


# ================================================================ G-T7 跨版本禁合并


def test_g_t7_cross_version_merge_is_forbidden():
    assert p25.P25_CROSS_VERSION_MERGE_FORBIDDEN is True


def test_g_t7_merged_sample_returns_failed():
    """声称合并了样本 → 直接 `failed`, 哪怕样本数与命中数都够。"""
    verdict = p25.p25_gate_verdict(sample_n=60, hits=45, cross_version_merged=True)
    assert verdict == p25.P25_ANY_GATE_FAIL_STATUS
    assert verdict != C.CALIB_STATUS_PASSED


def test_g_t7_no_cross_version_merge_flag_defaults_to_false():
    """默认不合并 —— 「忘了传」不能等于「合并了」, 也不能等于「免检」。"""
    sig = inspect.signature(p25.p25_gate_verdict)
    assert sig.parameters["cross_version_merged"].default is False


def test_g_t7_same_numbers_without_merge_flag_can_pass():
    """同样的 60/45, 不合并就能过 —— 证明红的是那个标志, 不是数字。"""
    assert p25.p25_gate_verdict(sample_n=60, hits=45) == C.CALIB_STATUS_PASSED


# ================================================================ G-T8 Bonferroni / 禁 Holm-BH


def test_g_t8_bonferroni_triplet_is_the_same_objects_as_constants():
    """三件套全部 `is` 引用 `constants`, 不新造权威源。"""
    assert p25.P25_BONFERRONI_SCHEME_ID is C.BONFERRONI_SCHEME_ID
    assert p25.P25_BONFERRONI_M is C.BONFERRONI_M
    assert p25.P25_BONFERRONI_FAMILY_ALPHA is C.BONFERRONI_FAMILY_ALPHA


def test_g_t8_bonferroni_values_are_scheme_id_and_m6_and_alpha():
    assert p25.P25_BONFERRONI_SCHEME_ID == "independent_family_v1"
    assert p25.P25_BONFERRONI_M == 6
    assert p25.P25_BONFERRONI_FAMILY_ALPHA == pytest.approx(0.05 / 6)


def test_g_t8_holm_bh_substitute_is_forbidden():
    assert p25.P25_HOLM_BH_SUBSTITUTE_FORBIDDEN is True


def test_g_t8_substituted_test_returns_failed():
    """声明用了替代检验 → `failed`, 哪怕数字全达标。"""
    for name in ("holm", "benjamini_hochberg", "bh", "fdr_bh"):
        assert (
            p25.p25_gate_verdict(sample_n=30, hits=21, substituted_test=name)
            == p25.P25_ANY_GATE_FAIL_STATUS
        ), name


def test_g_t8_none_substituted_test_can_pass():
    """不替换(默认 `None`)就能过。"""
    assert p25.p25_gate_verdict(sample_n=30, hits=21, substituted_test=None) == (
        C.CALIB_STATUS_PASSED
    )


def test_g_t8_no_holm_bh_implementation_in_code():
    """**代码里**(剥掉字符串与注释后)不得出现 holm / benjamini / hochberg 的**实现**。

    ## 为什么不能简单子串搜

    本模块**必须**有一个叫 `P25_HOLM_BH_SUBSTITUTE_FORBIDDEN` 的常量 —— 那个名字里
    就含 `holm`。简单子串搜会把这个正确的禁止标记当成实现, 恒红。

    所以搜的是**实现与引入**的形态: `def xxx_holm`、`import ...holm`、
    `from ... import ...holm`。禁掉的是"真的算起来了", 不是"提到了"。
    """
    code = _code_only(_MODULE_PATH).lower()
    for needle in ("holm", "benjamini", "hochberg", "statsmodels"):
        assert not re.search(rf"\bdef\s+\w*{needle}", code), f"出现了 {needle} 的函数实现"
        assert not re.search(rf"\bimport\b[^\n]*{needle}", code), f"导入了 {needle}"
        assert not re.search(rf"\bfrom\b[^\n]*{needle}", code), f"从 {needle} 导入"


def test_g_t8_holm_ban_probe_is_not_vacuous():
    """自检: 上面那组正则**确实能**抓到实现(否则它只是在空转)。

    拿一段含 `def holm_adjust(...)` 的假源码检验正则本身。
    """
    fake = "\ndef holm_adjust(pvalues):\n    return pvalues\n"
    assert re.search(r"\bdef\s+\w*holm", fake.lower())
    fake2 = "\nfrom statsmodels.stats.multitest import multipletests\n"
    assert re.search(r"\bfrom\b[^\n]*statsmodels", fake2.lower())


# ================================================================ G-T9 失败状态


def test_g_t9_any_gate_fail_status_is_failed():
    assert p25.P25_ANY_GATE_FAIL_STATUS == "failed"


def test_g_t9_fail_status_is_in_closed_set_and_not_passed():
    """必须落在 `CALIB_STATUSES` 闭集内, 且**不是** `passed`。

    这条同时挡住两种错: 拼成 `"fail"`(不在闭集), 或写成了 `"passed"`。
    """
    assert p25.P25_ANY_GATE_FAIL_STATUS in C.CALIB_STATUSES
    assert p25.P25_ANY_GATE_FAIL_STATUS != C.CALIB_STATUS_PASSED


def test_g_t9_fail_status_is_not_pending():
    """**不是 `pending`** —— 有结论且没过, 不能装成「还没结论」。"""
    assert p25.P25_ANY_GATE_FAIL_STATUS != "pending"


def test_g_t9_insufficient_sample_returns_failed_not_pending():
    """样本 29 < 30 → `failed`, 不是 `pending`。"""
    assert p25.p25_gate_verdict(sample_n=29, hits=29) == "failed"


def test_g_t9_wilson_miss_returns_failed_not_pending():
    """样本够但 Wilson 下界没过 → `failed`, 不是 `pending`。"""
    assert p25.p25_gate_verdict(sample_n=30, hits=20) == "failed"


def test_g_t9_verdict_never_returns_pending_for_any_input():
    """穷举常见输入, 返回值**只有** `passed` / `failed` 两种, 从不 `pending`。"""
    seen = set()
    for n in (0, 1, 29, 30, 31, 60, 100):
        for k in range(0, n + 1):
            for merged in (False, True):
                for sub in (None, "holm"):
                    for swapped in (False, True):
                        seen.add(
                            p25.p25_gate_verdict(
                                sample_n=n,
                                hits=k,
                                cross_version_merged=merged,
                                substituted_test=sub,
                                primary_swapped=swapped,
                            )
                        )
    assert seen <= {C.CALIB_STATUS_PASSED, p25.P25_ANY_GATE_FAIL_STATUS}
    assert "pending" not in seen


def test_g_t9_flag_says_fail_status_is_calib_failed():
    assert p25.P25_ANY_GATE_FAIL_STATUS_IS_CALIB_FAILED is True


# ================================================================ G-T10 禁事后换主指标


def test_g_t10_primary_swap_is_forbidden():
    assert p25.P25_PRIMARY_SWAP_FORBIDDEN is True


def test_g_t10_swapped_primary_returns_failed():
    assert p25.p25_gate_verdict(sample_n=30, hits=21, primary_swapped=True) == (
        p25.P25_ANY_GATE_FAIL_STATUS
    )


def test_g_t10_swap_flag_defaults_to_false():
    sig = inspect.signature(p25.p25_gate_verdict)
    assert sig.parameters["primary_swapped"].default is False


def test_g_t10_swap_is_not_rescued_by_perfect_numbers():
    """100% 命中 + 合并禁用 + 无替代检验, 只要换了主指标就 `failed`。"""
    assert p25.p25_gate_verdict(sample_n=100, hits=100, primary_swapped=True) == "failed"


# ================================================================ G-T11 定序 P2.5 ≺ P2


def test_g_t11_p25_is_before_p2():
    assert p25.P25_BEFORE_P2 is True


def test_g_t11_sig_order_rule_is_documented_in_note():
    """一行摘要里必须写明定序, 否则贴到日志里就丢了。"""
    assert "P2.5" in p25.P25_LOCK_NOTE
    assert "P2" in p25.P25_LOCK_NOTE


# ================================================================ G-T12 口径锁 ≠ 可放量


def test_g_t12_lock_note_says_lock_is_not_deployable():
    """`P25_LOCK_NOTE` 必须带上「口径锁 != 可放量」这句话。

    这句话是这张锁面最容易被读错的地方: 常量齐备看起来像「做完了」,
    实际连样本都没凑。把否定句写进摘要字符串, 任何把它贴出去的地方都会带出去。
    """
    note = p25.P25_LOCK_NOTE
    assert "口径锁" in note
    assert "可放量" in note
    assert "!=" in note or "≠" in note


def test_g_t12_module_docstring_says_lock_is_not_deployable():
    doc = p25.__doc__ or ""
    assert "锁面 ≠ 可放量" in doc or "锁面 != 可放量" in doc
    assert "不" in doc


def test_g_t12_two_triggering_switches_remain_false():
    """两处 TRIGGERING 仍 `False` —— 锁面齐备**不**等于开了触发。"""
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False
    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False


def test_g_t12_module_imports_no_io_and_no_writers():
    """模块**不导入**任何写路径: 不碰 store / calib_store / hits_store / push。

    这是「锁面是常量, 不是评估器」的机械校验 —— 一旦有人往这里加 `upsert`,
    导入名会出现在源码里, 这条会红。
    """
    code = _code_only(_MODULE_PATH)
    for forbidden in (
        "calib_store",
        "hits_store",
        "CalibStore",
        "HitStore",
        "upsert",
        "manual_publish",
        "try_push",
        "open(",
        "Path(",
        "json.",
        "requests",
    ):
        assert forbidden not in code, f"锁面模块不得出现 {forbidden!r}"


# ================================================================ G-T13 零副作用 / 不开触发


def test_g_t13_module_source_has_no_triggering_flag_true():
    """源码里不得出现 `TRIGGERING_ENABLED = True` 之类的赋值或引用。

    搜的是**代码**(剥掉字符串与注释后): 本模块 docstring 里提到「`TRIGGERING_ENABLED`
    该开(本拍一个都没动)」是**说明**, 不是引用。剥完之后若还有, 才是真引用。
    """
    code = _code_only(_MODULE_PATH)
    assert "TRIGGERING" not in code, "锁面模块不得引用 TRIGGERING 开关"
    src = _MODULE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"TRIGGERING_ENABLED\s*[=:]\s*True", src)


def test_g_t13_module_source_has_no_env_reads():
    """不读 env —— 口径锁不是开关, 不能因为环境变量而变。"""
    code = _code_only(_MODULE_PATH)
    assert "os.environ" not in code
    assert "getenv" not in code
    assert "import os" not in code


def test_g_t13_full_input_domain_is_pure():
    """同一组输入调两次结果一致(纯函数)。"""
    for _ in range(3):
        assert p25.p25_gate_verdict(sample_n=30, hits=21) == C.CALIB_STATUS_PASSED
        assert p25.p25_gate_verdict(sample_n=30, hits=20) == "failed"
        assert p25.wilson_hit_rate_lower_bound(21, 30) == (
            p25.wilson_hit_rate_lower_bound(21, 30)
        )


def test_g_t13_all_exports_are_importable_and_named():
    """`__all__` 里每一项都真的存在。拼错的名字不能悄悄躺着。"""
    for name in p25.__all__:
        assert hasattr(p25, name), name


def test_g_t13_public_names_are_declared():
    """`P25_` / `WILSON_` / `CALIB_` 前缀的公共名**全部**进 `__all__`。

    `CALIB_*` 三个(从 `constants` 导入的既有钉)也算公共面: 它们能被
    `from calib_p25_lock import ...` 拿出去, 就该被列出来 —— 否则调用方只能靠猜。
    """
    public = {
        n
        for n in dir(p25)
        if not n.startswith("_") and n.startswith(("P25_", "WILSON_", "CALIB_"))
    }
    assert public <= set(p25.__all__), f"未声明: {sorted(public - set(p25.__all__))}"


def test_g_t13_probe_is_not_vacuous():
    """自检: `_code_only` 真的会剥东西(否则上面所有源码级断言都是空转)。

    对一个明知含字符串的文件跑一遍: docstring 里的中文不该剩, 真代码该还在。
    """
    probe = _code_only(_MODULE_PATH)
    assert "口径锁" not in probe  # 只出现在 docstring / 字符串里
    assert "p25_gate_verdict" in probe  # 真代码还在
