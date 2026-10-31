"""hits schema 真推门禁契约锁面 + 契约文档（批次 20261031gschema · P-bss-schema-push · #-A-#9）。

## 这组测在测什么

schema 真推门禁的坏法有三种，前两种都不报错：

1. **契约漂了** —— version 改名、`HITS_SCHEMA_VERSION` 被悄悄 bump 成 `"2"`、
   禁六词集少一个词。这类坏法**不会报错**，只会让文档某天开始描述一个不存在的世界。
2. **门被悄悄推开** —— 某个 `*_TRIGGERING_ENABLED` 从 False 变 True、
   `EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE` 被翻、或有人加了 `allow_shadow` 旁路。
   这类坏法**在测试里完全不报错**，只会让下游某天开始把影子当 live 发。
3. **本拍越界** —— 锁面 import 了调度器 / 写文件 / 网络；或者顺手改了 live 模板语义。

## 为什么锁面用**模块别名**而不是 `from x import CONST`

因为负控要能炸。`from x import CONST` 把值拷进锁面命名空间，
`monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", "2")` 改的是源模块，
锁面副本纹丝不动 —— 断言就成了**永远为真的守卫**。

`test_assert_fires_when_schema_bumped` 与 `test_lock_uses_module_alias_not_snapshot_import`
专门验这一点：如果哪天有人把锁面改回快照式 import，这两条会红。

## 不测什么

不测真推、不测调度、不测行情 —— 本拍只有契约，没有引擎，没有旁注落地代码。

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
_LOCK_PATH = _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "schema_push_lock.py"
_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-schema-push.md"
_SKELETON_PATH = _REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md"
_FOOTNOTE = "*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*"


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


def test_schema_push_lock_version_present():
    """G-T3 / G-T6: version PRESENT、非空、等于 `schema_push_v1`。"""
    from app.buy_sell_signals import schema_push_lock as g

    assert g.SCHEMA_PUSH_LOCK_VERSION
    assert g.SCHEMA_PUSH_LOCK_VERSION == "schema_push_v1"
    assert isinstance(g.SCHEMA_PUSH_LOCK_VERSION, str)
    assert g.SCHEMA_PUSH_LOCK_NOTE
    assert "schema_push_v1" in g.SCHEMA_PUSH_LOCK_NOTE


def test_lock_note_carries_the_big_letters():
    """备注里必须带门禁大字 —— 免得有人只读常量不读文档。"""
    from app.buy_sell_signals import schema_push_lock as g

    note = g.SCHEMA_PUSH_LOCK_NOTE
    for needle in (
        "schema 门禁契约!=授权真推live",
        "门禁契约!=授权翻闸",
        "合入!=部署",
        "路径已通!=calib!=可放量!=TRIGGERING",
    ):
        assert needle in note, f"备注缺大字: {needle}"


def test_six_word_action_phrases_present():
    """禁六词集必须是**六**条，且逐条在。"""
    from app.buy_sell_signals import schema_push_lock as g

    assert len(g.SCHEMA_PUSH_REFUSED_ACTION_PHRASES) == 6
    for phrase in (
        "可以买入",
        "建议买入",
        "强烈买入",
        "可以卖出",
        "建议卖出",
        "强烈卖出",
    ):
        assert phrase in g.SCHEMA_PUSH_REFUSED_ACTION_PHRASES, phrase


def test_refused_payload_fields_cover_price_qty_position_order():
    """禁写字段族须覆盖价 / 量 / 仓 / 单 四类。"""
    from app.buy_sell_signals import schema_push_lock as g

    fields = set(g.SCHEMA_PUSH_REFUSED_PAYLOAD_FIELDS)
    assert {"price", "qty", "position", "order"} <= fields
    assert all(isinstance(f, str) and f for f in fields)


# ---------------------------------------------------------------- ② schema + 两 TRIGGERING + COMPLETE


def test_hits_schema_version_still_v1():
    """G-T4 / G-T7: `HITS_SCHEMA_VERSION` **仍是字符串 `"1"`**，不是 int 1，更不是 "2"。"""
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals.constants import HITS_SCHEMA_VERSION

    assert HITS_SCHEMA_VERSION == "1"
    assert isinstance(HITS_SCHEMA_VERSION, str)
    assert g.EXPECTED_HITS_SCHEMA_VERSION == "1"
    g.assert_hits_schema_still_v1()


def test_both_triggering_still_false():
    """G-T4 / G-T6: 两处开关**仍是** `False`（`is` 而非 `==`）。"""
    from app.buy_sell_signals import schema_push_lock as g
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
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals.first_seen_audit import (
        EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE,
    )

    assert EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False
    assert type(EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE) is bool
    g.assert_complete_still_false()


def test_assert_fires_when_schema_bumped(monkeypatch):
    """负控: bump `HITS_SCHEMA_VERSION` 成 "2"，断言**必须发火**。

    这条同时验证锁面用的是**模块别名**（活引用）而不是快照式 import：
    快照式 import 时 monkeypatch 改不到副本，这条会假绿。
    """
    from app.buy_sell_signals import constants
    from app.buy_sell_signals import schema_push_lock as g

    monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", "2")
    with pytest.raises(AssertionError, match="HITS_SCHEMA_VERSION must stay '1'"):
        g.assert_hits_schema_still_v1()


def test_assert_fires_when_schema_is_int_one(monkeypatch):
    """负控: 把 `"1"` 换成 int `1` 也要发火 —— 契约钉的是字面量，不是"能解析成 1"。"""
    from app.buy_sell_signals import constants
    from app.buy_sell_signals import schema_push_lock as g

    monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", 1)
    with pytest.raises(AssertionError, match="HITS_SCHEMA_VERSION must stay '1'"):
        g.assert_hits_schema_still_v1()


def test_assert_fires_when_volume_triggering_flipped(monkeypatch):
    """负控: 把量能开关改成 True，断言**必须发火**。"""
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals import volume_breakout_stub

    monkeypatch.setattr(volume_breakout_stub, "VOLUME_BREAKOUT_TRIGGERING_ENABLED", True)
    with pytest.raises(AssertionError, match="VOLUME_BREAKOUT_TRIGGERING_ENABLED"):
        g.assert_triggering_still_false()


def test_assert_fires_when_auction_triggering_flipped(monkeypatch):
    """负控: 竞价开关改 True，断言必须发火。"""
    from app.buy_sell_signals import auction_abnormal_stub
    from app.buy_sell_signals import schema_push_lock as g

    monkeypatch.setattr(
        auction_abnormal_stub, "AUCTION_ABNORMAL_TRIGGERING_ENABLED", True
    )
    with pytest.raises(AssertionError, match="AUCTION_ABNORMAL_TRIGGERING_ENABLED"):
        g.assert_triggering_still_false()


def test_assert_fires_when_complete_flipped(monkeypatch):
    """负控: COMPLETE 翻 True，断言必须发火。"""
    from app.buy_sell_signals import first_seen_audit
    from app.buy_sell_signals import schema_push_lock as g

    monkeypatch.setattr(first_seen_audit, "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE", True)
    with pytest.raises(AssertionError, match="EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE"):
        g.assert_complete_still_false()


def test_truthy_substitute_still_fires(monkeypatch):
    """负控: 拿 `1` 顶替 False 也要发火 —— `not 1` 是 False，`1 is not False` 才是守卫。"""
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals import volume_breakout_stub

    monkeypatch.setattr(volume_breakout_stub, "VOLUME_BREAKOUT_TRIGGERING_ENABLED", 1)
    with pytest.raises(AssertionError, match="VOLUME_BREAKOUT_TRIGGERING_ENABLED"):
        g.assert_triggering_still_false()


# ---------------------------------------------------------------- ③ DIRECTION / Bonferroni


def test_direction_labels_still_three_values():
    """G-T5: 方向仍三值，`research_watch` **仍在**（不许删）。"""
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals.constants import DIRECTION_LABELS

    assert DIRECTION_LABELS == {"research_buy", "research_sell", "research_watch"}
    assert len(DIRECTION_LABELS) == 3
    assert "research_watch" in DIRECTION_LABELS
    g.assert_direction_labels_untouched()


def test_bonferroni_m_still_6():
    """G-T8: `BONFERRONI_M` 仍 6（门禁边不占 m，但也不许顺手改 m）。"""
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals.constants import BONFERRONI_M

    assert BONFERRONI_M == 6
    g.assert_bonferroni_untouched()


def test_assert_fires_when_direction_drifted(monkeypatch):
    """负控: DIRECTION 少一个值（比如删 `research_watch`），断言必须发火。"""
    from app.buy_sell_signals import constants
    from app.buy_sell_signals import schema_push_lock as g

    monkeypatch.setattr(
        constants, "DIRECTION_LABELS", frozenset({"research_buy", "research_sell"})
    )
    with pytest.raises(AssertionError, match="DIRECTION_LABELS mutated"):
        g.assert_direction_labels_untouched()


# ---------------------------------------------------------------- ④ 交叉 trigger_gate / shadow_obs / T1 / T2


def test_cross_trigger_gate_shadow_obs_t1_t2_versions():
    """G-T4: 四处只读交叉 version 一致，且锁面**不重定义**上游字面量。"""
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals import (
        shadow_obs_lock,
        tier_cut_lock,
        tier_display_lock,
        trigger_gate_lock,
    )

    assert trigger_gate_lock.TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    assert shadow_obs_lock.SHADOW_OBS_LOCK_VERSION == "shadow_obs_v1"
    assert tier_cut_lock.TIER_CUT_RULE_VERSION == "tier_cut_v1"
    assert tier_display_lock.TIER_DISPLAY_LOCK_VERSION == "tier_display_v1"
    assert g.EXPECTED_TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    assert g.EXPECTED_SHADOW_OBS_LOCK_VERSION == "shadow_obs_v1"
    assert g.EXPECTED_T1_RULE_VERSION == "tier_cut_v1"
    assert g.EXPECTED_T2_LOCK_VERSION == "tier_display_v1"
    g.assert_gate_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_on_trigger_gate_drift(monkeypatch):
    """负控: trigger_gate version 漂移，交叉断言必须发火。"""
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals import trigger_gate_lock

    monkeypatch.setattr(trigger_gate_lock, "TRIGGER_GATE_LOCK_VERSION", "trigger_gate_v2")
    with pytest.raises(AssertionError, match="trigger_gate version drift"):
        g.assert_gate_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_on_shadow_obs_drift(monkeypatch):
    """负控: shadow_obs version 漂移，交叉断言必须发火。"""
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals import shadow_obs_lock

    monkeypatch.setattr(shadow_obs_lock, "SHADOW_OBS_LOCK_VERSION", "shadow_obs_v2")
    with pytest.raises(AssertionError, match="shadow_obs version drift"):
        g.assert_gate_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_on_t1_drift(monkeypatch):
    """负控: T1 version 漂移，交叉断言必须发火。"""
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals import tier_cut_lock

    monkeypatch.setattr(tier_cut_lock, "TIER_CUT_RULE_VERSION", "tier_cut_v2")
    with pytest.raises(AssertionError, match="T1 version drift"):
        g.assert_gate_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_when_shadow_obs_default_enabled(monkeypatch):
    """负控: 影子观测口默认被打开 —— schema 侧取向必须一致（默认关）。"""
    from app.buy_sell_signals import schema_push_lock as g
    from app.buy_sell_signals import shadow_obs_lock

    monkeypatch.setattr(shadow_obs_lock, "SHADOW_OBS_DEFAULT_ENABLED", True)
    with pytest.raises(AssertionError, match="default-enabled drifted"):
        g.assert_gate_shadow_t1_t2_cross_lock()


# ---------------------------------------------------------------- ⑤ 无 allow_shadow


def test_no_allow_shadow_near_schema_push():
    """G-T4: 锁面与契约层命名空间无 `allow_shadow` 旁路。"""
    from app.buy_sell_signals import auction_abnormal_stub, constants, volume_breakout_stub
    from app.buy_sell_signals import schema_push_lock as g

    for module in (g, constants, volume_breakout_stub, auction_abnormal_stub):
        g.assert_no_allow_shadow(dir(module))
    # 守卫函数自己叫 `assert_no_allow_shadow` —— 名字里带它就把它判成旁路，
    # 会逼人给守卫改名。这里按**精确名**查，不按子串。
    banned = {"allow_shadow", "ALLOW_SHADOW", "AllowShadow"}
    assert not (banned & set(dir(g)))


def test_allow_shadow_guard_fires_on_synthetic_namespace():
    """负控: 塞一个 `allow_shadow` 进去，守卫必须发火。"""
    from app.buy_sell_signals import schema_push_lock as g

    with pytest.raises(AssertionError, match="allow_shadow forbidden"):
        g.assert_no_allow_shadow(["foo", "allow_shadow", "bar"])
    with pytest.raises(AssertionError, match="allow_shadow forbidden"):
        g.assert_no_allow_shadow(["ALLOW_SHADOW"])


# ---------------------------------------------------------------- ⑥ 文档 / skeleton


def test_schema_push_doc_exists_and_covers_contract_points():
    """G-T2 / G-T10: 文档 PRESENT 且契约要点逐条在正文里。"""
    assert _DOC_PATH.is_file(), f"schema 真推门禁契约文档缺失: {_DOC_PATH}"
    text = _DOC_PATH.read_text(encoding="utf-8")

    required = [
        # 1 产品语义
        "真推门禁",
        "**不是**立刻真推上线",
        "**不是**完整 schema 迁移",
        "**不是**授权真推 live",
        "**不是**授权翻闸",
        # 2 现状锚
        "HITS_SCHEMA_VERSION",
        'Final = "1"',
        "hits_models",
        "hits_dingtalk",
        "build_hit_dingtalk_text",
        "基础设施 PRESENT",
        # 3 真推另口令
        "真推另口令",
        "所有者明示口令",
        "真推 / 旁注落地须**所有者明示口令",
        # 4 前置条件表 + 表≠已可真推
        "前置条件表",
        "表 ≠ 已可真推",
        "schema 门禁契约 ≠ 授权真推 live",
        # 5 与 P3a / shadow / trigger_gate / live 模板
        "影子 ≠ live",
        "门禁契约 ≠ 授权翻闸",
        # 6 方向 / 禁词
        "DIRECTION_LABELS",
        "research_watch",
        "禁六词",
        "价量仓单",
        # 7 门禁大字
        "路径已通 **≠** calib **≠** 可放量 **≠** TRIGGERING",
        "K8 PASS **≠** 可放量 **≠** 可开 TRIGGERING",
        "讨论 **≠** 派单",
        "影子写入 **≠** 观测口 **≠** 放量",
        "合入 **≠** 部署",
        # 8 Bonferroni
        "m = 6",
        "**不是**永久豁免",
        # 9 挂点 / 另口令
        "另口令",
        "schema_push_lock.py",
        # 10 tip=prod
        "5569c8b50204cac8d656e85fb6be45f3cb869850",
        "tip = prod",
        "pre262_20261031_191850",
        # 11 脚注
        "仅供研究，不构成投资建议；执行须人工确认",
        "红涨绿跌",
        "18.2bp",
    ]
    missing = [needle for needle in required if needle not in text]
    assert not missing, f"文档缺少契约要点: {missing}"


def test_doc_states_this_is_not_push_or_migration():
    """文档必须**明写**「不是什么」——防止被读成"已经可以真推了"。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    for needle in (
        "**不是**立刻真推上线",
        "**不是**完整 schema 迁移",
        "**不是**授权真推 live",
        "**不是**授权翻闸",
    ):
        assert needle in text, f"文档缺「不是」声明: {needle}"


def test_doc_makes_clear_table_is_veto_list_not_pass():
    """前置条件表必须写明它是**否决清单**，不是通行证。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    assert "不是「本拍已满足可真推」的清单" in text
    assert "**否决**" in text
    assert "「已可真推」" in text


def test_skeleton_links_to_schema_push_and_keeps_footnote_last():
    """G-T2: skeleton 有一节链过去；脚注仍在**最后一行**。"""
    text = _SKELETON_PATH.read_text(encoding="utf-8")
    assert "buy-sell-signal-schema-push.md" in text
    assert "schema_push_lock.py" in text
    assert "schema_push_v1" in text

    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines, "skeleton 不该为空"
    assert lines[-1] == _FOOTNOTE, f"末行不是脚注: {lines[-1]!r}"


def test_skeleton_keeps_existing_sections_not_replaced():
    """skeleton 里既有链**未被替换**——本拍只追加，不重写。"""
    text = _SKELETON_PATH.read_text(encoding="utf-8")
    for needle in (
        "buy-sell-signal-tier-cut.md",
        "buy-sell-signal-tier-display.md",
        "buy-sell-signal-shadow-obs.md",
        "buy-sell-signal-trigger-gate.md",
    ):
        assert needle in text, f"既有链被动了: {needle}"


def test_side_links_appended_to_trigger_gate_and_shadow_obs():
    """旁链存在，且两文档自身 version 叙述仍在 —— 规则正文未被改。"""
    for name, own_version in (
        ("buy-sell-signal-trigger-gate.md", "trigger_gate_v1"),
        ("buy-sell-signal-shadow-obs.md", "shadow_obs_v1"),
    ):
        text = (_REPO_ROOT / "docs" / name).read_text(encoding="utf-8")
        assert "buy-sell-signal-schema-push.md" in text, f"{name} 缺旁链"
        assert own_version in text, f"{name} 自身 version 叙述被动了"
        assert "**不是**授权真推 live" in text, f"{name} 旁链缺边界声明"


# ---------------------------------------------------------------- ⑦ 结构守


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
    # `open(` 要用**标识符左边界**判 —— 若将来有 `..._still_open()`,
    # 裸子串会把函数名判成文件打开。这类误报的代价是逼人给真守卫加 noqa。
    for needle in (
        "write_text",
        "write_bytes",
        "requests.",
        "httpx.",
        "os.environ",
        "subprocess.",
    ):
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


def test_lock_module_has_no_schema_bump_or_triggering_true_assignment():
    """G-T7: 锁面**不得**给 schema version / TRIGGERING / COMPLETE 赋值（查目标，不查文本）。

    查文本会把 docstring 里「bump 须另口令」的说明判成违规，进而逼人删掉说明。
    """
    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    banned = {
        "HITS_SCHEMA_VERSION",
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
    assert not offenders, f"锁面不许给这些开关/版本赋值: {sorted(set(offenders))}"


def test_lock_module_does_not_touch_dingtalk_template():
    """G-T7: 锁面**零碰** live 模板 —— 不 import hits_dingtalk，不调 `build_hit_dingtalk_text`。

    真推旁注落地是另口令的事，不该从契约锁面溜进来。
    """
    relative = _relative_import_modules(_LOCK_PATH)
    assert "hits_dingtalk" not in relative
    assert "hits_models" not in relative
    code = _code_only(_LOCK_PATH)
    assert "build_hit_dingtalk_text" not in code
    assert "try_push_hit" not in code


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
        "TRIGGER_GATE_LOCK_VERSION",
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
    for module in (
        "constants",
        "volume_breakout_stub",
        "auction_abnormal_stub",
        "first_seen_audit",
        "trigger_gate_lock",
        "shadow_obs_lock",
        "tier_cut_lock",
        "tier_display_lock",
    ):
        assert module in relative, f"锁面缺模块别名 import: {module}"

    # 不许出现 `from .constants import HITS_SCHEMA_VERSION` 这种取值的写法
    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    snapshot_forbidden = {
        "constants",
        "volume_breakout_stub",
        "auction_abnormal_stub",
        "first_seen_audit",
        "trigger_gate_lock",
        "shadow_obs_lock",
        "tier_cut_lock",
        "tier_display_lock",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            mod = node.module or ""
            assert mod not in snapshot_forbidden, (
                f"不许 from .{mod} import 取值 —— 用模块别名（快照会让负控假绿）"
            )


def test_live_alias_not_snapshot_import_behavioural(monkeypatch):
    """活引用的**行为**证明：改源模块 → 锁面读到的就是新值。

    结构守（上一条）查的是源码形状；这条查的是运行时事实。
    万一有人用 `__getattr__` 之类巧妙办法绕过结构守，这条仍会红。
    """
    from app.buy_sell_signals import constants
    from app.buy_sell_signals import schema_push_lock as g

    monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", "1")  # 先钉回期望值
    g.assert_hits_schema_still_v1()  # 绿
    monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", "3")
    with pytest.raises(AssertionError):
        g.assert_hits_schema_still_v1()  # 同一函数对象，立刻看到新值


def test_lock_all_exports_are_real_and_sorted():
    """`__all__` 每个名字都真存在，且排序。"""
    from app.buy_sell_signals import schema_push_lock as g

    assert g.__all__
    missing = [name for name in g.__all__ if not hasattr(g, name)]
    assert not missing, f"__all__ 声明了不存在的符号: {missing}"
    assert len(set(g.__all__)) == len(g.__all__), "__all__ 有重复项"
    assert list(g.__all__) == sorted(g.__all__)


# ---------------------------------------------------------------- ⑧ 一把梭


def test_invariants_all_green():
    """全部不变量一次过 —— CI 里真正跑的那条门。"""
    from app.buy_sell_signals import schema_push_lock as g

    g.assert_schema_push_lock_invariants()
    g.assert_hits_schema_still_v1()
    g.assert_triggering_still_false()
    g.assert_complete_still_false()
    g.assert_gate_shadow_t1_t2_cross_lock()


def test_invariants_fires_when_version_drifted(monkeypatch):
    """负控: version 漂移，聚合断言必须发火。"""
    from app.buy_sell_signals import schema_push_lock as g

    monkeypatch.setattr(g, "SCHEMA_PUSH_LOCK_VERSION", "schema_push_v2")
    with pytest.raises(AssertionError, match="must be 'schema_push_v1'"):
        g.assert_schema_push_lock_invariants()


def test_invariants_fires_when_schema_bumped(monkeypatch):
    """负控: 聚合断言必须能看见 schema bump —— 不是只有单条断言看得到。"""
    from app.buy_sell_signals import constants
    from app.buy_sell_signals import schema_push_lock as g

    monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", "2")
    with pytest.raises(AssertionError, match="HITS_SCHEMA_VERSION"):
        g.assert_schema_push_lock_invariants()
