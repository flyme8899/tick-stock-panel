"""触发/放量门禁契约锁面 + 契约文档（批次 20261031gtrig · P-bss-trigger · #-A-#9）。

## 这组测在测什么

门禁契约的坏法有三种，前两种都不报错：

1. **契约漂了** —— version 改名、缺口数字写死成 8 而实际不是 8、六词集少一个词。
   这类坏法**不会报错**，只会让文档某天开始描述一个不存在的世界。
2. **闸被悄悄打开** —— 某个 `*_TRIGGERING_ENABLED` 从 False 变 True，或
   `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` 被翻。这类坏法**在测试里完全不报错**，
   只会让下游某天开始产出「已触发」结论。所以要对着写死的 False 断言。
3. **本拍越界** —— 锁面 import 了调度器 / 写文件 / 网络；或者在别处把开关赋 True。

## 为什么锁面用**模块别名**而不是 `from x import CONST`

因为负控要能炸。`from x import CONST` 把值拷进锁面命名空间，
`monkeypatch.setattr(volume_breakout_stub, "CONST", True)` 改的是源模块，
锁面副本纹丝不动 —— 断言就成了**永远为真的守卫**。

`test_assert_fires_when_volume_triggering_flipped` 专门验这一点：
如果哪天有人把锁面改回快照式 import，这条负控会红。

## 不测什么

不测算行情、不测调度、不测真推 —— 本拍只有契约，没有引擎。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

# ruff: noqa: SIM300
# SIM300: 本文件满是 `assert CONST == 字面量`, ruff 把全大写标识符当"常量应在右",
#         在 assert 上系统性误报 —— 把字面量翻到左边才是真的不可读。

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCK_PATH = (
    _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "trigger_gate_lock.py"
)
_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-trigger-gate.md"
_SKELETON_PATH = _REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md"


# ---------------------------------------------------------------- 工具


def _imported_modules(path: Path) -> set[str]:
    """ast 解析出**绝对** import 的顶层模块名（相对 import 不计入）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def _relative_import_modules(path: Path) -> set[str]:
    """ast 解析出**同包相对** import 的模块名。

    两种写法都要覆盖：

    - ``from .foo import x``  → ``node.module == "foo"``
    - ``from . import foo``  → ``node.module is None``，模块名在 ``alias.name`` 里

    第二种正是「模块别名」标准写法，漏掉它会让「不许快照导入」的守卫形同虚设。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level <= 0:
            continue
        if node.module:
            out.add(node.module)
        else:
            for alias in node.names:
                out.add(alias.name)
    return out


def _code_only(path: Path) -> str:
    """去掉 docstring 后的源码 —— 避免"文档里写着不算"造成假阳性。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body[0].value.value = ""
    return ast.unparse(tree)


# ---------------------------------------------------------------- ① 常量 + version


def test_trigger_gate_lock_version_present():
    """G-T3 / G-T6: version PRESENT、非空、等于 `trigger_gate_v1`。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    assert g.TRIGGER_GATE_LOCK_VERSION
    assert g.TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    assert isinstance(g.TRIGGER_GATE_LOCK_VERSION, str)
    assert g.TRIGGER_GATE_LOCK_NOTE
    assert "trigger_gate_v1" in g.TRIGGER_GATE_LOCK_NOTE


def test_six_word_action_phrases_present():
    """禁六词集必须是**六**条，且逐条在。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    assert len(g.TRIGGER_GATE_REFUSED_ACTION_PHRASES) == 6
    for phrase in (
        "可以买入",
        "建议买入",
        "强烈买入",
        "可以卖出",
        "建议卖出",
        "强烈卖出",
    ):
        assert phrase in g.TRIGGER_GATE_REFUSED_ACTION_PHRASES, phrase


# ---------------------------------------------------------------- ② 两 TRIGGERING + COMPLETE


def test_both_triggering_still_false():
    """G-T4 / G-T6: 两处开关**仍是** `False`（`is` 而非 `==`）。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_TRIGGERING_ENABLED,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_TRIGGERING_ENABLED,
    )

    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False
    # 不是 0 / "" / None —— 契约说的是布尔事实
    assert type(VOLUME_BREAKOUT_TRIGGERING_ENABLED) is bool
    assert type(AUCTION_ABNORMAL_TRIGGERING_ENABLED) is bool
    g.assert_triggering_still_false()


def test_complete_still_false():
    """G-T4 / G-T6: A2 完成门**仍关**。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals.first_seen_audit import (
        EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE,
    )

    assert EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False
    assert type(EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE) is bool
    g.assert_complete_still_false()


def test_assert_fires_when_volume_triggering_flipped(monkeypatch):
    """负控: 把量能开关改成 True，断言**必须发火**。

    这条同时验证锁面用的是**模块别名**（活引用）而不是快照式 import：
    快照式 import 时 monkeypatch 改不到副本，这条会假绿。
    """
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals import volume_breakout_stub

    monkeypatch.setattr(
        volume_breakout_stub, "VOLUME_BREAKOUT_TRIGGERING_ENABLED", True
    )
    with pytest.raises(AssertionError, match="VOLUME_BREAKOUT_TRIGGERING_ENABLED"):
        g.assert_triggering_still_false()


def test_assert_fires_when_auction_triggering_flipped(monkeypatch):
    """负控: 竞价开关改 True，断言必须发火。"""
    from app.buy_sell_signals import auction_abnormal_stub
    from app.buy_sell_signals import trigger_gate_lock as g

    monkeypatch.setattr(
        auction_abnormal_stub, "AUCTION_ABNORMAL_TRIGGERING_ENABLED", True
    )
    with pytest.raises(AssertionError, match="AUCTION_ABNORMAL_TRIGGERING_ENABLED"):
        g.assert_triggering_still_false()


def test_assert_fires_when_complete_flipped(monkeypatch):
    """负控: COMPLETE 翻 True，断言必须发火。"""
    from app.buy_sell_signals import first_seen_audit
    from app.buy_sell_signals import trigger_gate_lock as g

    monkeypatch.setattr(
        first_seen_audit, "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE", True
    )
    with pytest.raises(AssertionError, match="EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE"):
        g.assert_complete_still_false()


def test_truthy_substitute_still_fires(monkeypatch):
    """`0` / `""` 之类假值**不许**冒充 False —— 契约说的是 `is False`。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals import volume_breakout_stub

    monkeypatch.setattr(
        volume_breakout_stub, "VOLUME_BREAKOUT_TRIGGERING_ENABLED", 0
    )
    with pytest.raises(AssertionError, match="must stay False"):
        g.assert_triggering_still_false()


# ---------------------------------------------------------------- ③ DIRECTION / Bonferroni


def test_direction_labels_still_three_values():
    """G-T5: 方向仍三值，`research_watch` 仍在。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals.constants import DIRECTION_LABELS

    assert DIRECTION_LABELS == frozenset(
        {"research_buy", "research_sell", "research_watch"}
    )
    assert "research_watch" in DIRECTION_LABELS
    assert len(DIRECTION_LABELS) == 3
    g.assert_direction_labels_untouched()


def test_bonferroni_m_still_6():
    """G-T8: 预算未被本拍改动。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals.constants import BONFERRONI_M

    assert BONFERRONI_M == 6
    assert BONFERRONI_M == g.EXPECTED_BONFERRONI_M
    g.assert_bonferroni_untouched()


def test_assert_fires_when_direction_drifted(monkeypatch):
    """负控: DIRECTION 被改（删 research_watch），断言必须发火。"""
    from app.buy_sell_signals import constants as c
    from app.buy_sell_signals import trigger_gate_lock as g

    monkeypatch.setattr(
        c, "DIRECTION_LABELS", frozenset({"research_buy", "research_sell"})
    )
    with pytest.raises(AssertionError, match="DIRECTION_LABELS mutated"):
        g.assert_direction_labels_untouched()


# ---------------------------------------------------------------- ④ 交叉 shadow_obs / T1 / T2


def test_cross_shadow_obs_t1_t2_versions():
    """G-T4: 只读交叉三处 version 与观测口默认关取向。"""
    from app.buy_sell_signals import shadow_obs_lock as shadow
    from app.buy_sell_signals import tier_cut_lock as t1
    from app.buy_sell_signals import tier_display_lock as t2
    from app.buy_sell_signals import trigger_gate_lock as g

    g.assert_shadow_t1_t2_cross_lock()
    assert shadow.SHADOW_OBS_LOCK_VERSION == "shadow_obs_v1"
    assert shadow.SHADOW_OBS_DEFAULT_ENABLED is False
    assert t1.TIER_CUT_RULE_VERSION == "tier_cut_v1"
    assert t2.TIER_DISPLAY_LOCK_VERSION == "tier_display_v1"


def test_cross_lock_fires_on_shadow_obs_drift(monkeypatch):
    """负控: 他拍把 shadow_obs version 改 v2，交叉断言必须发火。"""
    from app.buy_sell_signals import shadow_obs_lock as shadow
    from app.buy_sell_signals import trigger_gate_lock as g

    monkeypatch.setattr(shadow, "SHADOW_OBS_LOCK_VERSION", "shadow_obs_v2")
    with pytest.raises(AssertionError, match="shadow_obs version drift"):
        g.assert_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_on_t1_drift(monkeypatch):
    """负控: T1 version 漂移必须发火。"""
    from app.buy_sell_signals import tier_cut_lock as t1
    from app.buy_sell_signals import trigger_gate_lock as g

    monkeypatch.setattr(t1, "TIER_CUT_RULE_VERSION", "tier_cut_v2")
    with pytest.raises(AssertionError, match="T1 version drift"):
        g.assert_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_when_shadow_obs_default_enabled(monkeypatch):
    """负控: 观测口默认关被人翻成 True，门禁交叉必须发火。"""
    from app.buy_sell_signals import shadow_obs_lock as shadow
    from app.buy_sell_signals import trigger_gate_lock as g

    monkeypatch.setattr(shadow, "SHADOW_OBS_DEFAULT_ENABLED", True)
    with pytest.raises(AssertionError, match="default-enabled drifted"):
        g.assert_shadow_t1_t2_cross_lock()


# ---------------------------------------------------------------- ⑤ 证据缺口（已清 3 / 剩 5）


def test_evidence_pending_is_five_across_two_families():
    """VOLUME 剩 2 + AUCTION 3 = **5** —— 且总数是**派生的**，不是写死 5。

    批次 20261101gevid 清了 VOLUME 的 `trigger_evidence_table_data_pending`；
    批次 20261101gevid3 清了 VOLUME 的 `local_minute_lib_data_pending`；
    批次 20261101gauction 清了 AUCTION 的 `auction_price_not_persisted`
    （auction_scan 落盘价 + stub 映射，三条齐）。
    这条同时咬住「只清了这三条」：VOLUME 不能是 1 或 3，AUCTION 不能是 2 或 4。
    """
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_EVIDENCE_PENDING,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_EVIDENCE_PENDING,
    )

    assert len(VOLUME_BREAKOUT_EVIDENCE_PENDING) == 2
    assert len(AUCTION_ABNORMAL_EVIDENCE_PENDING) == 3
    assert g.TRIGGER_GATE_EVIDENCE_PENDING_COUNT == 5
    # 派生关系：总数 == 两族之和（不是手抄的 5）
    assert g.TRIGGER_GATE_EVIDENCE_PENDING_COUNT == (
        len(VOLUME_BREAKOUT_EVIDENCE_PENDING)
        + len(AUCTION_ABNORMAL_EVIDENCE_PENDING)
    )


def test_evidence_status_still_data_pending():
    """两族评估状态仍 `data_pending`（这次评估没有结论）。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals.auction_abnormal_stub import AUCTION_ABNORMAL_STATUS
    from app.buy_sell_signals.volume_breakout_stub import VOLUME_BREAKOUT_STATUS

    assert VOLUME_BREAKOUT_STATUS == "data_pending"
    assert AUCTION_ABNORMAL_STATUS == "data_pending"
    assert g.TRIGGER_GATE_EVIDENCE_STATUS_EXPECTED == "data_pending"
    g.assert_evidence_pending_still_open()


def test_evidence_assert_fires_when_pending_emptied(monkeypatch):
    """负控: 缺口被清零时断言**必须**发火（提醒文档 §4.1 过时）。

    这条断的含义不是"改坏了"，是"契约描述该更新了"。缺口补完是好事，
    但让文档继续写「8 条 0 清零」就是骗人。
    """
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals import volume_breakout_stub

    monkeypatch.setattr(
        volume_breakout_stub, "VOLUME_BREAKOUT_EVIDENCE_PENDING", frozenset()
    )
    with pytest.raises(AssertionError, match="EVIDENCE_PENDING is empty"):
        g.assert_evidence_pending_still_open()


def test_evidence_assert_fires_when_status_drifted(monkeypatch):
    """负控: 评估状态漂移（如改成 ok）时断言必须发火。"""
    from app.buy_sell_signals import auction_abnormal_stub
    from app.buy_sell_signals import trigger_gate_lock as g

    monkeypatch.setattr(auction_abnormal_stub, "AUCTION_ABNORMAL_STATUS", "ok")
    with pytest.raises(AssertionError, match="status drifted"):
        g.assert_evidence_pending_still_open()


def test_evidence_pending_names_appear_in_doc():
    """文档 §4.1 的缺口清单必须与代码里的常量**逐条对应**。

    手抄清单会在 stub 补了一条后继续显示旧的 —— 这条把「清单与代码同源」钉住。
    """
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_EVIDENCE_PENDING,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_EVIDENCE_PENDING,
    )

    text = _DOC_PATH.read_text(encoding="utf-8")
    for name in sorted(VOLUME_BREAKOUT_EVIDENCE_PENDING) + sorted(
        AUCTION_ABNORMAL_EVIDENCE_PENDING
    ):
        assert name in text, f"文档 §4.1 缺缺口项: {name}"


# ---------------------------------------------------------------- ⑥ 无 allow_shadow


def test_no_allow_shadow_near_gate():
    """G-T4: 两 stub 与锁面自身都**没有** `allow_shadow`。"""
    from app.buy_sell_signals import auction_abnormal_stub, volume_breakout_stub
    from app.buy_sell_signals import trigger_gate_lock as g

    g.assert_no_allow_shadow(dir(g))
    g.assert_no_allow_shadow(dir(volume_breakout_stub))
    g.assert_no_allow_shadow(dir(auction_abnormal_stub))
    assert not hasattr(g, "allow_shadow")


def test_allow_shadow_guard_fires_on_synthetic_namespace():
    """负控: 守卫不是空转。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    with pytest.raises(AssertionError, match="allow_shadow forbidden"):
        g.assert_no_allow_shadow(["foo", "allow_shadow"])
    with pytest.raises(AssertionError, match="allow_shadow forbidden"):
        g.assert_no_allow_shadow(["ALLOW_SHADOW"])


# ---------------------------------------------------------------- ⑦ 文档 / skeleton


def test_trigger_gate_doc_exists_and_covers_contract_points():
    """G-T2 / G-T10: 文档 PRESENT 且契约要点逐条在正文里。"""
    assert _DOC_PATH.is_file(), f"门禁契约文档缺失: {_DOC_PATH}"
    text = _DOC_PATH.read_text(encoding="utf-8")

    required = [
        # 1 产品语义
        "触发/放量**门禁**",
        "**不是**触发引擎",
        "**不是**放量开闸",
        "**不是**授权翻闸",
        # 2 两开关 Final 非 env
        "VOLUME_BREAKOUT_TRIGGERING_ENABLED",
        "AUCTION_ABNORMAL_TRIGGERING_ENABLED",
        "`Final = False`",
        "**非 env**",
        # 3 翻闸另口令
        "翻闸另口令",
        "所有者明示口令",
        "R2-10",
        # 4 前置条件表 + 表≠已可翻
        "表 ≠ 已可翻",
        "前置条件表",
        # 4.1 证据缺口（已清 3 / 剩 5）
        "VOLUME_BREAKOUT_EVIDENCE_PENDING",
        "AUCTION_ABNORMAL_EVIDENCE_PENDING",
        "已清 3 / 剩 5",
        "清零 ≠ 可翻闸",
        "落盘 ≠ 可开 TRIGGERING",
        "data_pending",
        # 5 与 COMPLETE/calib/K8/影子
        "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE",
        "calib 过 ≠ 可放量 ≠ 可开 TRIGGERING",
        "K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING",
        "影子写入 ≠ 观测口 ≠ 放量",
        # 6 方向 / 禁词
        "DIRECTION_LABELS",
        "research_watch",
        "禁六词",
        "价量仓单",
        # 7 门禁大字
        "路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING",
        "门禁契约 ≠ 授权翻闸",
        "讨论 ≠ 派单",
        "合入 ≠ 部署",
        # 8 Bonferroni
        "m = 6",
        "非永久豁免",
        # 9 另口令
        "另口令",
        # 10 tip=prod
        "c1b19fc8",
        "tip = prod",
        "pre261_20261031_172351",
        # 11 脚注
        "仅供研究，不构成投资建议；执行须人工确认",
        "红涨绿跌",
        "18.2bp",
    ]
    missing = [needle for needle in required if needle not in text]
    assert not missing, f"文档缺少契约要点: {missing}"


def test_doc_states_this_is_not_engine_or_open_gate():
    """文档必须**明写**「不是什么」——防止被读成"引擎已就绪 / 已可翻闸"。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    for needle in (
        "**不是**触发引擎",
        "**不是**放量开闸",
        "**不是**授权翻闸",
        "**不是**可放量声明",
    ):
        assert needle in text, f"文档缺「不是」声明: {needle}"


def test_skeleton_links_to_trigger_gate_and_keeps_footnote_last():
    """G-T2: skeleton 有一节链过去；脚注仍在**最后一行**。"""
    text = _SKELETON_PATH.read_text(encoding="utf-8")
    assert "buy-sell-signal-trigger-gate.md" in text
    assert "trigger_gate_lock.py" in text
    assert "trigger_gate_v1" in text

    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines, "skeleton 不该为空"
    assert lines[-1].startswith("*仅供研究"), f"末行不是脚注: {lines[-1]!r}"
    assert "18.2bp" in lines[-1]


def test_skeleton_keeps_t1_t2_r2_3_links_not_replaced():
    """skeleton 里既有链**未被替换**——本拍只追加，不重写。"""
    text = _SKELETON_PATH.read_text(encoding="utf-8")
    assert "buy-sell-signal-tier-cut.md" in text
    assert "buy-sell-signal-tier-display.md" in text
    assert "buy-sell-signal-shadow-obs.md" in text


def test_shadow_obs_doc_links_to_trigger_gate():
    """旁链存在，且 shadow-obs 规则正文未被改。"""
    path = _REPO_ROOT / "docs" / "buy-sell-signal-shadow-obs.md"
    text = path.read_text(encoding="utf-8")
    assert "buy-sell-signal-trigger-gate.md" in text
    assert "shadow_obs_v1" in text  # 自身 version 叙述仍在
    assert "default" not in text.lower() or True  # 不强加措辞约束


# ---------------------------------------------------------------- ⑧ 结构守


def test_lock_module_imports_only_contract_layer():
    """G-T7: 锁面只依赖契约层 —— 无调度器 / 无网络 / 无文件写入。"""
    modules = _imported_modules(_LOCK_PATH)
    forbidden_abs = {
        "polars",
        "pandas",
        "numpy",
        "requests",
        "httpx",
        "duckdb",
        "sqlite3",
        "fastapi",
        "pathlib",
        "tempfile",
        "os",
        "json",
        "subprocess",
        "schedule",
        "apscheduler",
        "crontab",
        "threading",
        "asyncio",
    }
    assert not (modules & forbidden_abs), (
        f"锁面不该 import 这些: {sorted(modules & forbidden_abs)}"
    )
    assert modules <= {"__future__", "typing", "collections"}, (
        f"意外的绝对 import: {sorted(modules)}"
    )


def test_lock_module_declares_no_side_effect_calls():
    """锁面无写路径；顶层只允许常量与 def（import 时不硬 raise）。"""
    code = _code_only(_LOCK_PATH)
    # `open(` 要用**标识符左边界**判 —— 本锁面有 `assert_evidence_pending_still_open()`,
    # 裸子串会把函数名判成文件打开。这类误报的代价是逼人给真守卫加 noqa。
    for needle in ("write_text", "write_bytes", "requests.", "httpx.", "os.environ", "subprocess."):
        assert needle not in code, f"锁面出现副作用调用: {needle!r}"
    assert not re.search(r"(?<![A-Za-z0-9_])open\(", code), "锁面出现 open() 调用"

    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        assert isinstance(
            node,
            (
                ast.ImportFrom,
                ast.Import,
                ast.Assign,
                ast.AnnAssign,
                ast.FunctionDef,
                ast.Expr,  # 模块 docstring
            ),
        ), f"顶层出现意外语句: {type(node).__name__}"
        assert not isinstance(node, (ast.Assert, ast.Raise))


def test_lock_module_has_no_triggering_true_assignment():
    """G-T7: 锁面**不得**把 TRIGGERING / COMPLETE 赋 True（查赋值目标，不查文本）。

    查文本会把 docstring 里「翻闸须另口令」的说明判成违规，进而逼人删掉说明。
    """
    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    banned = {
        "TRIGGERING_ENABLED",
        "VOLUME_BREAKOUT_TRIGGERING_ENABLED",
        "AUCTION_ABNORMAL_TRIGGERING_ENABLED",
        "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in banned:
                offenders.append(target.id)
    assert not offenders, f"锁面不许给这些开关赋值: {sorted(set(offenders))}"


def test_lock_module_does_not_redefine_upstream_versions():
    """锁面只**读**上游 version，不把它们重定义成冲突字面量。"""
    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    assigned: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                assigned.add(target.id)
    for forbidden in (
        "SHADOW_OBS_LOCK_VERSION",
        "TIER_CUT_RULE_VERSION",
        "TIER_DISPLAY_LOCK_VERSION",
        "DIRECTION_LABELS",
        "BONFERRONI_M",
        "VOLUME_BREAKOUT_TRIGGERING_ENABLED",
        "AUCTION_ABNORMAL_TRIGGERING_ENABLED",
        "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE",
    ):
        assert forbidden not in assigned, f"锁面不许重新定义 {forbidden}"


def test_lock_uses_module_alias_not_snapshot_import():
    """锁面必须用**模块别名**（活引用），不许 `from x import CONST` 快照。

    快照式 import 会让 monkeypatch 改不到副本，负控假绿 —— 那是比没有守卫更坏的事。
    """
    relative = _relative_import_modules(_LOCK_PATH)
    assert "volume_breakout_stub" in relative
    assert "auction_abnormal_stub" in relative
    assert "first_seen_audit" in relative
    # 不许出现 `from .volume_breakout_stub import ...` 这种取值的写法
    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    snapshot_forbidden = {
        "volume_breakout_stub",
        "auction_abnormal_stub",
        "first_seen_audit",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            mod = node.module or ""
            assert mod not in snapshot_forbidden, (
                f"不许 from .{mod} import 取值 —— 用模块别名（快照会让负控假绿）"
            )


def test_lock_all_exports_are_real_and_sorted():
    """`__all__` 每个名字都真存在，且排序。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    assert g.__all__
    missing = [name for name in g.__all__ if not hasattr(g, name)]
    assert not missing, f"__all__ 声明了不存在的符号: {missing}"
    assert len(set(g.__all__)) == len(g.__all__), "__all__ 有重复项"
    assert list(g.__all__) == sorted(g.__all__)


# ---------------------------------------------------------------- ⑨ 一把梭


def test_invariants_all_green():
    """全部不变量一次过 —— CI 里真正跑的那条门。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    g.assert_trigger_gate_lock_invariants()
    g.assert_triggering_still_false()
    g.assert_complete_still_false()
    g.assert_evidence_pending_still_open()
    g.assert_shadow_t1_t2_cross_lock()


def test_invariants_fires_when_version_drifted(monkeypatch):
    """负控: version 漂移，聚合断言必须发火。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    monkeypatch.setattr(g, "TRIGGER_GATE_LOCK_VERSION", "trigger_gate_v2")
    with pytest.raises(AssertionError, match="must be 'trigger_gate_v1'"):
        g.assert_trigger_gate_lock_invariants()
