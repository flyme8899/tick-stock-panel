"""A1 · 轨②子定义命名 + Bonferroni 预算表(批次 20261030ga1)。

## 这组测例守什么

四件事, 分开测, 因为它们坏起来的样子不一样:

1. **D1 没被改名**(G-T2)。与 `REAL_DAILY_*` 做**同串断言**, 不是各自断言字面量 ——
   后者只能证明"我抄对了", 前者能证明"两边还指着同一个键"。
2. **D2a/D2b 键名与 event_class 正确**, 且 D2 不骑 D1(G-T3/G-T7)。
3. **预算账对得上**: 4 占用 / 2 留白 / M=6 / scheme 未 bump(G-T6)。
4. **第 7 格有守卫**(G-T7), 且守卫真会炸 —— 一条永不触发的守卫等于没写。

另测 G-T4(E1 仅占位)、G-T5(EVENT_CLASSES 扩而未新增 family)、G-T8(零碰)。

## 为什么有源码静态断言

有些约束**没有**运行时表现: 比如"这个模块不许自己实现评价器"。它只能靠读源码来钉。
静态断言直接用 `read_text` 会恒绿 —— docstring 里写着各种"不许做", 所以这里先剥掉
字符串与注释。

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

import pytest

from app.buy_sell_signals import constants as C
from app.buy_sell_signals import naming_budget_a1 as N
from app.buy_sell_signals.daily_fundamental_definition import (
    REAL_DAILY_DEFINITION_VERSION,
    REAL_DAILY_EVENT_CLASS,
    REAL_DAILY_FAMILY,
    REAL_DAILY_SIGNAL_ID,
)

_APP_DIR = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
_NAMING_PATH = _APP_DIR / "naming_budget_a1.py"
_CONSTANTS_PATH = _APP_DIR / "constants.py"
_D1_PATH = _APP_DIR / "daily_fundamental_definition.py"


def _code_only(path: Path) -> str:
    """剥掉字符串与注释后的源码。

    docstring 里写满了"不实现评价器""禁止改名", 注释里也写着各种键名。直接 `in src`
    搜会恒命中 —— 那种"绿"什么也证明不了。
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


# ================================================================ G-T2 D1 冻结


def test_g_t2_d1_identity_matches_the_real_definition():
    """G-T2: D1 四串与 `REAL_DAILY_*` **同串**, 不是各自对着字面量断言。

    写法上的这一点差别很要紧: 若两边一起被改成了同一个错名, 同串断言仍然绿 —— 但
    那种改动会同时在别处(calib 三键、已落样本路径)炸开。而如果**只**改了 `REAL_DAILY_*`,
    这里立刻红。这正是"改名"最可能的入口。
    """
    assert N.A1_D1_SIGNAL_ID == "bss.daily_quality_value"
    assert N.A1_D1_SIGNAL_ID == REAL_DAILY_SIGNAL_ID

    assert N.A1_D1_FAMILY == "daily_fundamental"
    assert N.A1_D1_FAMILY == REAL_DAILY_FAMILY

    assert N.A1_D1_EVENT_CLASS == "daily_quality_value"
    assert N.A1_D1_EVENT_CLASS == REAL_DAILY_EVENT_CLASS

    assert N.A1_D1_VERSION == "v1"
    assert N.A1_D1_VERSION == REAL_DAILY_DEFINITION_VERSION

    assert N.A1_D1_SLOT_STATUS == "occupied"


def test_g_t2b_d1_definition_file_is_not_touched():
    """G-T2: D1 定义文件本身没有改名痕迹 —— `REAL_DAILY_SIGNAL_ID` 仍是原串。"""
    text = _D1_PATH.read_text(encoding="utf-8")
    assert 'REAL_DAILY_SIGNAL_ID: Final = "bss.daily_quality_value"' in text


# ================================================================ G-T3 D2a / D2b


def test_g_t3_d2_keys_families_and_event_classes():
    """G-T3: D2a/D2b 的键、family、event_class 三者逐字对上。"""
    assert N.A1_D2A_SIGNAL_ID == "bss.day.macd_hist_cross"
    assert N.A1_D2A_FAMILY == "daily_fundamental"
    assert N.A1_D2A_EVENT_CLASS == "daily_macd"
    assert N.A1_D2A_VERSION == "v1"
    assert N.A1_D2A_SLOT_STATUS == "reserved"

    assert N.A1_D2B_SIGNAL_ID == "bss.day.kdj_gold_cross"
    assert N.A1_D2B_FAMILY == "daily_fundamental"
    assert N.A1_D2B_EVENT_CLASS == "daily_kdj"
    assert N.A1_D2B_VERSION == "v1"
    assert N.A1_D2B_SLOT_STATUS == "reserved"


def test_g_t3b_d2_initial_status_is_not_a_pass_claim():
    """G-T3: D2 初态是 `candidate`, 且显示名层面不许出现「已验证/可放量/已触发」。"""
    assert N.A1_D2_INITIAL_CALIB_STATUS == "candidate"

    forbidden = ("可放量", "已触发", "已验证", "passed", "ready")
    source = _NAMING_PATH.read_text(encoding="utf-8")
    # 只在**赋值**层面查: docstring 里必须能说明"不许这么说", 所以不能全文搜。
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("A1_D2_INITIAL_CALIB_STATUS", "A1_D2A_SLOT_STATUS", "A1_D2B_SLOT_STATUS")):
            continue
        for word in forbidden:
            assert word not in stripped, f"状态常量出现禁词: {word} → {stripped}"


def test_g_t3c_prefix_rules_hold_for_every_key():
    """G-T3: 每个键都落在它该在的族里; 桩名与夹具前缀不进生产格。"""
    assert N.A1_D2A_SIGNAL_ID.startswith(N.A1_PREFIX_DAY)
    assert N.A1_D2B_SIGNAL_ID.startswith(N.A1_PREFIX_DAY)
    assert N.A1_E1_SIGNAL_ID.startswith(N.A1_PREFIX_EVT)

    # D1 是完整键, 不是 `bss.day.` 族 —— 这是故意的, 见模块 docstring。
    assert N.A1_D1_SIGNAL_ID == N.A1_PREFIX_D1_GRANDFATHER
    assert not N.A1_D1_SIGNAL_ID.startswith(N.A1_PREFIX_DAY)

    # 夹具前缀与桩名不得出现在任何预算格的 signal_id 里。
    for slot in N.A1_BUDGET_SLOTS:
        sid = slot["signal_id"]
        if not sid:
            continue
        assert not sid.startswith(N.A1_PREFIX_FIXTURE), f"夹具键占了生产格: {sid}"
        assert sid not in N.A1_STUB_NAMES, f"桩名占了预算格: {sid}"


# ================================================================ G-T4 E1 仅占位


def test_g_t4_e1_is_budget_only():
    """G-T4: E1 只占格 —— 无 event_class 常量、状态是 `reserved_budget_only`。"""
    assert N.A1_E1_SIGNAL_ID == "bss.evt.overnight_announce"
    assert N.A1_E1_FAMILY == "minute_event"
    assert N.A1_E1_SLOT_STATUS == "reserved_budget_only"
    assert N.A1_E1_HAS_EVENT_CLASS is False

    # 没有为 E1 新增任何 event_class: `overnight_announce` 不许出现在集合里。
    assert "overnight_announce" not in C.EVENT_CLASSES


def test_g_t4b_naming_module_implements_no_evaluator():
    """G-T4/G-T9: 命名模块里不许有评价器 / 回放 / hits 写入的痕迹。

    静态断言, 走 `_code_only`: docstring 写着"不实现评价器", 全文搜会恒命中。
    """
    code = _code_only(_NAMING_PATH)
    for forbidden in (
        "def evaluate",
        "def backtest",
        "def replay",
        "upsert_hit",
        "upsert_shadow_hit",
        "write_shadow_hits",
        "DiskHitStore",
        "hits_store",
        "import pandas",
        "import numpy",
    ):
        assert forbidden not in code, f"命名模块越界: {forbidden}"


# ================================================================ G-T5 EVENT_CLASSES


def test_g_t5_event_classes_extended_with_exactly_two_new_values():
    """G-T5: 扩了 `daily_macd` / `daily_kdj` 两项, 旧类一个不少, 也没顺手多加。"""
    expected = frozenset(
        {
            "auction",
            "intraday_volume",
            "limit_reopen",
            "daily_quality_value",
            "daily_macd",
            "daily_kdj",
            "other",
        }
    )
    assert C.EVENT_CLASSES == expected, (
        "EVENT_CLASSES 与期望集合不等 —— 本拍只许加 daily_macd / daily_kdj 两项"
    )

    # 形态属留白/另口令, 本拍不许提前加。
    assert "daily_pattern" not in C.EVENT_CLASSES

    # D2 的 event_class 必须真的在集合里, 否则命名位落不进字段校验。
    assert N.A1_D2A_EVENT_CLASS in C.EVENT_CLASSES
    assert N.A1_D2B_EVENT_CLASS in C.EVENT_CLASSES


def test_g_t5b_signal_families_unchanged():
    """G-T5: 不新增 family —— 集合内容零碰。"""
    assert C.SIGNAL_FAMILIES == frozenset(
        {"minute_event", "daily_fundamental", "composite"}
    )
    # D1/D2 用的 family 确实在这个闭集里。
    assert N.A1_D1_FAMILY in C.SIGNAL_FAMILIES
    assert N.A1_D2A_FAMILY in C.SIGNAL_FAMILIES
    assert N.A1_E1_FAMILY in C.SIGNAL_FAMILIES


# ================================================================ G-T6 预算不变量


def test_g_t6_budget_is_four_filled_two_empty_of_six():
    """G-T6: 占用+预留=4、留白=2、格数=6, 且不变量检查通过。"""
    assert C.BONFERRONI_M == 6
    assert C.BONFERRONI_SCHEME_ID == "independent_family_v1"

    assert N.A1_BUDGET_SLOT_COUNT == 6
    assert N.A1_BUDGET_OCCUPIED_OR_RESERVED == 4
    assert N.A1_BUDGET_EMPTY == 2
    assert len(N.A1_BUDGET_SLOTS) == 6
    assert N.A1_BUDGET_OCCUPIED_OR_RESERVED + N.A1_BUDGET_EMPTY == N.A1_BUDGET_SLOT_COUNT

    N.assert_budget_invariants(
        bonferroni_m=C.BONFERRONI_M,
        scheme_id=C.BONFERRONI_SCHEME_ID,
    )


def test_g_t6b_budget_invariants_reject_a_changed_m_or_bumped_scheme():
    """G-T6: 不变量检查**真会**拒绝 —— 换个 m 或 bump scheme 都得炸。

    一条永远通过的检查等于没写。这里两头各试一次。
    """
    with pytest.raises(AssertionError) as exc_m:
        N.assert_budget_invariants(bonferroni_m=7, scheme_id="independent_family_v1")
    assert "禁止改 m" in str(exc_m.value)

    with pytest.raises(AssertionError) as exc_s:
        N.assert_budget_invariants(bonferroni_m=6, scheme_id="independent_family_v2")
    assert "禁止 bump" in str(exc_s.value)


def test_g_t6c_slot_roles_are_exactly_d1_d2a_d2b_e1_then_empty():
    """G-T6: 六格的角色顺序写死, 且每格 signal_id 与对应常量同值。"""
    roles = [slot["role"] for slot in N.A1_BUDGET_SLOTS]
    assert roles == ["D1", "D2a", "D2b", "E1", "EMPTY", "EMPTY"]

    assert N.A1_BUDGET_SLOTS[0]["signal_id"] == N.A1_D1_SIGNAL_ID
    assert N.A1_BUDGET_SLOTS[1]["signal_id"] == N.A1_D2A_SIGNAL_ID
    assert N.A1_BUDGET_SLOTS[2]["signal_id"] == N.A1_D2B_SIGNAL_ID
    assert N.A1_BUDGET_SLOTS[3]["signal_id"] == N.A1_E1_SIGNAL_ID
    assert N.A1_BUDGET_SLOTS[4]["signal_id"] == ""
    assert N.A1_BUDGET_SLOTS[5]["signal_id"] == ""


def test_g_t6d_budget_table_returns_a_copy():
    """G-T6: `budget_table()` 给副本 —— 改它不该污染权威表。

    `Final` 只挡重新赋值, 挡不住就地改 dict。一份能被就地篡改的"权威表", 被篡改之后
    仍然会通过所有基于它的检查。
    """
    copy = N.budget_table()
    copy[0]["status"] = "empty"
    copy[0]["signal_id"] = "hacked"
    assert N.A1_BUDGET_SLOTS[0]["status"] == "occupied"
    assert N.A1_BUDGET_SLOTS[0]["signal_id"] == N.A1_D1_SIGNAL_ID

    # 篡改副本之后, 权威表仍过不变量。
    N.assert_budget_invariants()


# ================================================================ G-T7 第 7 守卫 + D2 不骑 D1


def test_g_t7_seventh_slot_requires_scheme_bump():
    """G-T7: 当前 4 格不触发; 模拟 7 格且未 bump → 炸, 且 token 可指认。"""
    assert N.A1_SEVENTH_REQUIRES_SCHEME_BUMP is True

    # 现状: 4 格, 不触发。
    N.assert_no_seventh_without_bump(4, C.BONFERRONI_SCHEME_ID)
    N.assert_no_seventh_without_bump(6, C.BONFERRONI_SCHEME_ID)  # 满 6 也不触发

    # 第 7 格且未 bump → 炸。
    with pytest.raises(AssertionError) as exc:
        N.assert_no_seventh_without_bump(7, "independent_family_v1")
    assert N.A1_SEVENTH_BUMP_TOKEN in str(exc.value)
    assert "bonferroni_seventh_requires_scheme_bump" in str(exc.value)

    # bump 之后放行 —— 守卫挡的是"静默", 不是"扩容"。
    N.assert_no_seventh_without_bump(7, "independent_family_v2")


def test_g_t7b_d2_does_not_ride_d1():
    """G-T7: 正常 D2 通过; 骑 D1 键 / family 不对 / 前缀不对 三种错法都炸。"""
    assert N.A1_D2_MUST_NOT_INHERIT_D1_CALIB_PASSED is True

    N.assert_d2_not_riding_d1(
        d2_signal_id=N.A1_D2A_SIGNAL_ID, d2_family=N.A1_D2A_FAMILY
    )
    N.assert_d2_not_riding_d1(
        d2_signal_id=N.A1_D2B_SIGNAL_ID, d2_family=N.A1_D2B_FAMILY
    )

    # 骑 D1 键
    with pytest.raises(AssertionError) as exc_id:
        N.assert_d2_not_riding_d1(
            d2_signal_id=N.A1_D1_SIGNAL_ID, d2_family=N.A1_D1_FAMILY
        )
    assert "骑 D1" in str(exc_id.value)

    # family 不对
    with pytest.raises(AssertionError) as exc_fam:
        N.assert_d2_not_riding_d1(
            d2_signal_id=N.A1_D2A_SIGNAL_ID, d2_family="minute_event"
        )
    assert "daily_fundamental" in str(exc_fam.value)

    # 前缀不对
    with pytest.raises(AssertionError) as exc_pfx:
        N.assert_d2_not_riding_d1(
            d2_signal_id="bss.evt.macd_hist_cross", d2_family="daily_fundamental"
        )
    assert "bss.day." in str(exc_pfx.value)


def test_g_t7c_docstring_states_both_rules():
    """G-T7: 第 7 bump 与 D2 不骑 D1 必须写在**人读得到**的地方。"""
    doc = N.__doc__ or ""
    assert "D2 不骑 D1" in doc
    assert "第 7" in doc and "bump" in doc.lower()
    assert "占用" in doc and "留白" in doc


# ================================================================ G-T8 零碰


def test_g_t8_bonferroni_numbers_and_triggering_untouched():
    """G-T8: Bonferroni 数值 / scheme; 两处 TRIGGERING 仍 False。"""
    assert C.BONFERRONI_M == 6
    assert C.BONFERRONI_SCHEME_ID == "independent_family_v1"
    # α_family = 0.05 / 6, 浮点比较用容差。
    assert abs(C.BONFERRONI_FAMILY_ALPHA - 0.05 / 6) < 1e-12

    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_TRIGGERING_ENABLED,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_TRIGGERING_ENABLED,
    )

    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_g_t8b_a2_complete_still_false_and_untouched():
    """G-T8: A2 的 COMPLETE 仍 False —— 本拍零碰 #254。"""
    from app.buy_sell_signals.first_seen_audit import (
        EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE,
    )

    assert EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False


def test_g_t8c_constants_diff_only_touches_event_classes():
    """G-T8: `constants.py` 里被改的只有 `EVENT_CLASSES` 那一块。

    做法: 用 git 取基线版本, 逐行比较, 断言所有变化行都落在 EVENT_CLASSES 区间内。
    这比"rg BONFERRONI 无命中"更严 —— 后者挡不住"改了 BONFERRONI 附近的注释"或
    "顺手动了别处一个字"。
    """
    import subprocess

    repo = Path(__file__).resolve().parents[2]
    try:
        diff = subprocess.run(
            ["git", "diff", "-U0", "d1e295c41e9e60af6c3ce158917eca50ae184216", "--",
             str(_CONSTANTS_PATH.relative_to(repo))],
            cwd=str(repo), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git 不可用, 跳过 diff 范围检查")

    if diff.returncode != 0:
        pytest.skip("基线对象不在本地, 跳过 diff 范围检查")

    changed = [
        line for line in diff.stdout.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith(("+++", "---"))
    ]
    for line in changed:
        body = line[1:]
        assert "BONFERRONI" not in body, f"constants.py 动了 Bonferroni: {line}"
        assert "SIGNAL_FAMILIES" not in body, f"constants.py 动了 SIGNAL_FAMILIES: {line}"
        assert (
            "daily_macd" in body
            or "daily_kdj" in body
            or "20261030ga1" in body
            or body.strip() == ""
        ), f"constants.py 出现预期外的改动行: {line}"


# ================================================================ 包导出


def test_symbols_are_exported_from_package_root():
    """E4: 公共符号可从包根导入, 且与模块内是同一个对象。"""
    from app.buy_sell_signals import (
        A1_BUDGET_SLOTS as exported_slots,
        A1_D1_SIGNAL_ID as exported_d1,
        assert_budget_invariants as exported_check,
    )

    assert exported_d1 == N.A1_D1_SIGNAL_ID
    assert exported_slots is N.A1_BUDGET_SLOTS
    assert exported_check is N.assert_budget_invariants
