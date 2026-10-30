"""A3 · 轨①独立 P2.5 口径锁面(批次 20261030ga3)。

## 这组测例守什么

五件事, 分开测, 因为它们坏起来的样子不一样:

1. **轨①主 horizon 不是日频 T+5**(G-T3)。这是本拍最核心的一条: 套用 T+5 不会
   报错, 只会让事件轨的评估量到"事件之后那五天里发生的别的事"。
2. **成本三档**: 权威 18.2 是**同一个对象** `is`, 30/50 只是分报档(G-T4)。
3. **A2 依赖与零碰**: COMPLETE 仍 False, 两处 TRIGGERING 仍 False(G-T7/G-T8)。
4. **日频锁面没被本拍动过**(G-T2)。
5. **口径锁不自相矛盾**: `assert_evt_lock_invariants` 真会拒。

## 为什么成本/样本用 `is` 而不是 `==`

`==` 只能证明"现在数值一样"。`is` 证明"还是同一个对象" —— 后者才排除了
"有人抄了一份字面量, 今天恰好相等, 明天 `constants` 一改就分叉"。

代价是: `is` 对小整数(`BONFERRONI_M = 6`)的成立依赖 CPython 的整数缓存, 那是
实现细节。所以对 `M` 这一项, 本文件**同时**断言"引用的是同一个模块级对象"与
"数值等于 6", 而不是把可靠性押在缓存上。

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。
成本来回 18.2bp(轨①敏感性 18.2 / 30 / 50bp 分报)。
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

from app.buy_sell_signals import calib_p25_evt_lock as evt
from app.buy_sell_signals import calib_p25_lock as daily
from app.buy_sell_signals import constants as C

_APP_DIR = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
_EVT_PATH = _APP_DIR / "calib_p25_evt_lock.py"
_DAILY_PATH = _APP_DIR / "calib_p25_lock.py"


def _code_only(path: Path) -> str:
    """剥掉字符串与注释后的源码。

    docstring 里写满了 "T+5" / "COMPLETE" / "不改" 这些词(为了说明"不做什么"),
    直接搜原文会恒绿。
    """
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


# ================================================================ G-T2 / G-T3 主 horizon


def test_g_t3_primary_horizon_is_t1_open_not_daily_t5():
    """G-T3: 轨①主＝`T+1_open`; 日频仍 T+5; 两者不等, 且类型都不同。"""
    assert evt.EVT_CALIB_PRIMARY_HORIZON == "T+1_open"
    assert isinstance(evt.EVT_CALIB_PRIMARY_HORIZON, str)

    # 日频主仍旧是 T+5 —— 本拍没动它。
    assert daily.CALIB_PRIMARY_HORIZON_TD == 5
    assert isinstance(daily.CALIB_PRIMARY_HORIZON_TD, int)

    # 三条各自挡住一种"套用"的写法。
    assert evt.EVT_CALIB_PRIMARY_HORIZON != daily.CALIB_PRIMARY_HORIZON_TD
    assert evt.EVT_CALIB_PRIMARY_HORIZON != 5
    assert evt.EVT_CALIB_PRIMARY_HORIZON != str(daily.CALIB_PRIMARY_HORIZON_TD)


def test_g_t3b_primary_is_a_t_plus_one_open_semantics_string():
    """G-T3: 串本身要说明"T+1 开盘" —— 不能只是某个不相干的字符串。"""
    primary = evt.EVT_CALIB_PRIMARY_HORIZON
    assert primary.startswith("T+") or primary.startswith("T1")
    assert "1" in primary
    assert "open" in primary.lower()

    # 辅窗只报不当门: 辅助列表里也不许出现日频那套裸整数。
    for aux in evt.EVT_CALIB_AUX_HORIZONS:
        assert isinstance(aux, str)
        assert aux != str(daily.CALIB_PRIMARY_HORIZON_TD)
    # 主不在辅里 —— 否则"主"与"辅"的区分就没了。
    assert evt.EVT_CALIB_PRIMARY_HORIZON not in evt.EVT_CALIB_AUX_HORIZONS


def test_g_t2_daily_p25_lock_primary_is_untouched():
    """G-T2: 日频锁面文件里主 horizon 仍是 5, 未被本拍改写。"""
    assert daily.CALIB_PRIMARY_HORIZON_TD == 5
    text = _DAILY_PATH.read_text(encoding="utf-8")
    assert "CALIB_PRIMARY_HORIZON_TD: Final = 5" in text


def test_g_t2b_evt_module_does_not_redefine_the_daily_constant():
    """G-T2: 轨①锁面**不**重新定义 `CALIB_PRIMARY_HORIZON_TD`。

    若有人为了"统一口径"在轨①模块里也写一个 `CALIB_PRIMARY_HORIZON_TD = 5`,
    两个模块就会各有一份"日频主 horizon", 而其中一份必然是死的。
    """
    code = _code_only(_EVT_PATH)
    assert "CALIB_PRIMARY_HORIZON_TD =" not in code
    # 只读引用都不该有 —— 轨①模块不需要知道日频 horizon 是多少。
    assert "CALIB_PRIMARY_HORIZON_TD" not in code


# ================================================================ G-T4 成本三档


def test_g_t4_cost_authority_is_the_same_object_and_sensitivity_is_present():
    """G-T4: 权威 18.2 `is` `CALIB_COST_BP_ROUNDTRIP`; 30/50 敏感性 PRESENT。"""
    assert evt.EVT_P25_COST_BP_ROUNDTRIP is C.CALIB_COST_BP_ROUNDTRIP
    assert evt.EVT_P25_COST_BP_ROUNDTRIP == 18.2
    assert evt.EVT_P25_COST_IS_CALIB_PIN is True

    assert evt.EVT_P25_COST_BP_SENSITIVITY == (30.0, 50.0)
    assert 30.0 in evt.EVT_P25_COST_BP_SENSITIVITY
    assert 50.0 in evt.EVT_P25_COST_BP_SENSITIVITY

    # 敏感性档不得包含权威门本身之外的"更松"的值来冒充通过。
    assert 18.2 not in evt.EVT_P25_COST_BP_SENSITIVITY
    # 三档严格递增: 18.2 < 30 < 50。
    tiers = (evt.EVT_P25_COST_BP_ROUNDTRIP, *evt.EVT_P25_COST_BP_SENSITIVITY)
    assert list(tiers) == sorted(tiers)
    assert len(set(tiers)) == 3


def test_g_t4b_cost_pin_really_catches_a_split():
    """G-T4: 成本的 `is` 断言**真会**在"脱钩"时炸 —— 否则它只是装饰。"""
    # 数值相等, 但对象不同 —— 正是"抄了一份字面量"的形状。
    split_cost = float("18.2")
    assert split_cost == C.CALIB_COST_BP_ROUNDTRIP
    assert split_cost is not C.CALIB_COST_BP_ROUNDTRIP
    with pytest.raises(AssertionError) as exc_cost:
        evt.assert_evt_lock_invariants(cost_pin=split_cost)
    assert "CALIB_COST_BP_ROUNDTRIP" in str(exc_cost.value)


def test_g_t4c_sample_pin_is_calib_pinned_but_is_check_cannot_catch_30():
    """G-T4: 样本钉**引用**了 `CALIB_SAMPLE_N_MIN`, 但 `is` 在 30 这个值上失效。

    这条测例测的是一个**已知的局限**, 不是缺陷修复。原因: `CALIB_SAMPLE_N_MIN = 30`
    落在 CPython 的小整数缓存(-5..256)里, 于是:

        int("30") is CALIB_SAMPLE_N_MIN   →  True
        (lambda: 30)() is 30              →  True

    也就是说, 在 30 这个值上, 无论抄几份字面量, `is` 都恒为 `True`。**`is` 在这里
    分辨不出"抄了一份"**。

    所以这条测例不假装它能分辨, 而是分开钉两件事:

    - **能钉的**: 模块确实是把 `CALIB_SAMPLE_N_MIN` **赋值**过来了(源码层面),
      而不是写了个 `30`
    - **钉不住的**: 运行期 `is`。它弱, 弱得很明确

    之所以不改成 `==`, 是因为 `==` 在成本这一项上更弱(18.2 不在缓存里, `is` 有效),
    两处用同一种写法更不容易被误读。弱点写在这里, 而不是藏起来。
    """
    # 运行期: 引用确实指向同一个对象。
    assert evt.EVT_P25_SAMPLE_N_MIN is C.CALIB_SAMPLE_N_MIN
    assert evt.EVT_P25_SAMPLE_IS_CALIB_PIN is True

    # 同时承认: 小整数缓存让 is 在这个值上挡不住冒充。
    fake = int("30")
    assert fake is C.CALIB_SAMPLE_N_MIN, (
        "若这条失败, 说明 CPython 整数缓存行为变了 —— 那本测例的说明需要重写"
    )

    # 真正的守卫在源码层面: 赋值右侧必须是常量名, 不是数字字面量。
    # 归一化空白后再比 —— tokenize 会把每个 token 用空格隔开, 于是实际文本长这样:
    #   "EVT_P25_SAMPLE_N_MIN : Final = CALIB_SAMPLE_N_MIN"
    # 用正则可以同时兼容"带类型标注"与"不带"两种写法。
    code = " ".join(_code_only(_EVT_PATH).split())
    assert re.search(
        r"EVT_P25_SAMPLE_N_MIN\s*(?::\s*Final)?\s*=\s*CALIB_SAMPLE_N_MIN\b", code
    ), "样本钉必须是 CALIB_SAMPLE_N_MIN 的引用, 不是抄来的 30"
    assert not re.search(r"EVT_P25_SAMPLE_N_MIN\s*(?::\s*Final)?\s*=\s*30\b", code), (
        "样本钉被写成了字面量 30 —— 那与 CALIB_SAMPLE_N_MIN 会各自演进"
    )


# ================================================================ G-T5 样本 / 三键 / Bonferroni


def test_g_t5_sample_pin_keys_and_bonferroni_references():
    """G-T5: 样本引用、三键顺序、Bonferroni 三件套引用, 全部对上。"""
    assert evt.EVT_P25_SAMPLE_N_MIN is C.CALIB_SAMPLE_N_MIN
    assert evt.EVT_P25_SAMPLE_N_MIN == 30
    assert evt.EVT_P25_SAMPLE_IS_CALIB_PIN is True

    # 三键顺序与日频/影子分桶同序 —— 换顺序不会报错, 只会让按位对齐全部错位一格。
    assert evt.EVT_P25_ALIGN_KEY_FIELDS == (
        "signal_id",
        "definition_version",
        "freq_lock_version",
    )
    assert evt.EVT_P25_ALIGN_KEY_FIELDS == C.SHADOW_BUCKET_KEY_FIELDS

    # Bonferroni: 引用而非抄写。
    assert evt.EVT_P25_BONFERRONI_M is C.BONFERRONI_M
    assert evt.EVT_P25_BONFERRONI_SCHEME_ID is C.BONFERRONI_SCHEME_ID
    assert evt.EVT_P25_BONFERRONI_FAMILY_ALPHA is C.BONFERRONI_FAMILY_ALPHA
    # 数值锚 + 方案锚, 与 `is` 分开断言(见模块 docstring 关于整数缓存的说明)。
    assert evt.EVT_P25_BONFERRONI_M == 6
    assert evt.EVT_P25_BONFERRONI_SCHEME_ID == "independent_family_v1"

    # 禁 Holm / BH, 禁"另选方案"。
    assert evt.EVT_P25_HOLM_BH_SUBSTITUTE_FORBIDDEN is True


# ================================================================ G-T6 禁换主 / failed / 定序


def test_g_t6_no_swap_and_fail_status_and_ordering():
    """G-T6: 禁事后换主; 任一门不过→`failed`; 定序 P2.5 ≺ P2。"""
    assert evt.EVT_P25_PRIMARY_SWAP_FORBIDDEN is True
    assert evt.EVT_P25_ANY_GATE_FAIL_STATUS == "failed"
    # 这个字面量必须真的是一个合法状态 —— 否则 `failed` 会指向一个不存在的取值。
    assert evt.EVT_P25_ANY_GATE_FAIL_STATUS in C.CALIB_STATUSES
    # 且不能是 `pending` / `passed`。
    assert evt.EVT_P25_ANY_GATE_FAIL_STATUS != "pending"
    assert evt.EVT_P25_ANY_GATE_FAIL_STATUS != C.CALIB_STATUS_PASSED

    assert evt.EVT_P25_BEFORE_P2 is True


def test_g_t6b_invariants_reject_a_swapped_primary():
    """G-T6: 主 horizon 被换成日频那套整数 → 自检必须炸。"""
    with pytest.raises(AssertionError) as exc:
        evt.assert_evt_lock_invariants(primary_horizon=5)
    assert "整数" in str(exc.value) or "字符串" in str(exc.value)

    with pytest.raises(AssertionError):
        evt.assert_evt_lock_invariants(primary_horizon="T+5_open")


# ================================================================ G-T7 A2 依赖


def test_g_t7_a2_dependencies_and_complete_still_false():
    """G-T7: 起点＝first_seen_at; 缺首见 fail-closed; COMPLETE 仍 False。"""
    from app.buy_sell_signals.first_seen_audit import (
        EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE,
    )

    assert EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False
    # 本模块 re-export 的是同一个对象, 不是抄的一份 False。
    assert evt.EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE

    assert evt.EVT_P25_HORIZON_STARTS_AT_FIRST_SEEN is True
    assert evt.EVT_P25_MISSING_FIRST_SEEN_FAIL_CLOSED is True
    assert evt.EVT_P25_COMPLETE_FALSE_BLOCKS_SHADOW_CALIB_CLAIM is True


def test_g_t7b_shadow_calib_claim_is_false_while_complete_is_false():
    """G-T7: COMPLETE 未翻之前, "可写影子 / 可 calib" 的声称一律为 False。"""
    assert evt.is_shadow_calib_claim_allowed() is False


def test_g_t7c_invariants_reject_a_flipped_complete():
    """G-T7: 若 COMPLETE 被翻 True, 自检必须炸 —— 而且报"本拍不该翻", 不顺手改回去。"""
    with pytest.raises(AssertionError) as exc:
        evt.assert_evt_lock_invariants(complete=True)
    assert "翻 True" in str(exc.value) or "另验收" in str(exc.value)


def test_g_t7d_evt_module_never_assigns_complete():
    """G-T7: 轨①锁面**不**给 COMPLETE 赋值(只 import)。"""
    code = _code_only(_EVT_PATH)
    assert "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE =" not in code
    # 也不许在 import 期 assert 硬炸 —— 那会把"翻旗"变成服务起不来。
    assert "assert EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE" not in code


# ================================================================ G-T8 零碰


def test_g_t8_triggering_flags_still_false():
    """G-T8: 两处 TRIGGERING 仍 False。"""
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_TRIGGERING_ENABLED,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_TRIGGERING_ENABLED,
    )

    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_g_t8b_bonferroni_values_unchanged():
    """G-T8: Bonferroni 数值与 scheme 仍原值。"""
    assert C.BONFERRONI_M == 6
    assert C.BONFERRONI_SCHEME_ID == "independent_family_v1"
    assert abs(C.BONFERRONI_FAMILY_ALPHA - 0.05 / 6) < 1e-12


def test_g_t8c_evt_module_implements_no_evaluator():
    """G-T9: 锁面模块里不许有评估器 / 回放 / 写入的痕迹。"""
    code = _code_only(_EVT_PATH)
    for forbidden in (
        "def evaluate",
        "def backtest",
        "def replay",
        "upsert_hit",
        "write_hits",
        "DiskHitStore",
        "pandas",
        "numpy",
        "open ",
    ):
        assert forbidden not in code, f"锁面模块越界: {forbidden}"


# ================================================================ 口径锁 note / 话术


def test_lock_note_states_lock_is_not_deployable():
    """G-T7/G-T10: 摘要必须写明"口径锁 != 可放量", 且点名未动的东西。"""
    note = evt.EVT_P25_LOCK_NOTE
    assert "可放量" in note
    assert "T+1_open" in note
    assert "18.2" in note
    assert "30/50" in note
    assert "first_seen_at" in note

    # 禁词: 不允许把"口径锁"说成通过的结论。
    for forbidden in ("calib 已过", "已验证", "已触发", "可放量已"):
        assert forbidden not in note, f"摘要出现禁词: {forbidden}"


def test_docstring_states_orthogonality_and_ordering():
    """G-T9: docstring 里写死正交、定序、A2 依赖与脚注。"""
    doc = evt.__doc__ or ""
    assert "T+5" in doc  # 必须提"不套 T+5"
    assert "正交" in doc
    assert "A2 ≺ A3" in doc or "A2≺A3" in doc
    assert "定序" in doc
    assert "口径锁" in doc
    assert "只报不当权威门" in doc
    assert "仅供研究" in doc


# ================================================================ 包导出


def test_symbols_exported_from_package_root():
    """E3: 关键符号可从包根导入, 且与模块内是同一个对象。"""
    from app.buy_sell_signals import (
        EVT_CALIB_PRIMARY_HORIZON as root_primary,
        assert_evt_lock_invariants as root_check,
        is_shadow_calib_claim_allowed as root_claim,
    )

    assert root_primary == evt.EVT_CALIB_PRIMARY_HORIZON
    assert root_check is evt.assert_evt_lock_invariants
    assert root_claim is evt.is_shadow_calib_claim_allowed
