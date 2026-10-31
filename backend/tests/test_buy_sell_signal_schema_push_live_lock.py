"""hits schema 真推 live 开档锁面 + 旁注 + 契约文档（批次 20261031gschemalive · P-bss-schema-live · #-A-#9）。

## 这组测在测什么

开档层的坏法有四种，前三种都不报错：

1. **契约漂了** —— version 改名、旁注文案被单边删减（「研究档 · 未放量」→「研究档」）、
   `HITS_SCHEMA_VERSION` 被悄悄 bump。这类坏法**不会报错**，只会让文档某天开始描述
   一个不存在的世界。
2. **旁注悄悄消失** —— 有人重排 `lines`、有人传参把旁注置空、有人"顺手"删掉那行。
   模板照样返回 `(title, text)`，推送照发，谁也不会发现群里少了定性说明。
3. **门被悄悄推开** —— 某个 `*_TRIGGERING_ENABLED` 翻 True、COMPLETE 被翻、
   有人加了 `allow_shadow` 旁路。**在测试里完全不报错**，只会让下游某天开始把影子当
   live 发。
4. **本拍越界** —— 锁面 import 了调度器 / 写文件 / 网络；或者顺手改了 `try_push_hit`
   门序、动了 models 字段集。

## 为什么旁注一致性要用**交叉断言**而不是 import

锁面**不** import `hits_dingtalk`：锁面是契约层，不该依赖有网络/IO 的运行时模块。
代价是旁注文案存在两处（模板字面量 + 锁面常量）—— 那就必须由测试咬住：
`test_annotation_line_matches_lock_constant` 断言模板输出的那一行**逐字**等于锁面常量。
两处一旦分叉，这条当场红。

## 为什么锁面用**模块别名**而不是 `from x import CONST`

因为负控要能炸。`from x import CONST` 把值拷进锁面命名空间，
`monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", "2")` 改的是源模块，
锁面副本纹丝不动 —— 断言就成了**永远为真的守卫**。

`test_live_alias_not_snapshot_import_behavioural` 专门验这一点。

## 不测什么

不测真推、不测调度、不测行情 —— 本拍只有开档层，没有 live 写路径。

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
    _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "schema_push_live_lock.py"
)
_DINGTALK_PATH = _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "hits_dingtalk.py"
_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-schema-push-live.md"
_GATE_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-schema-push.md"
_SKELETON_PATH = _REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md"
_FOOTNOTE = "*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*"
_ANNOTATION = "研究档 · 未放量"


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


def _find_function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"函数 {name} 不存在于 {path}")


def _hit():
    """最小合法命中 —— 只用于 `build_hit_dingtalk_text`，该函数不看 mode / 可见性。"""
    from app.buy_sell_signals import hits_fixtures as fx

    return fx.calibrated_hit()


# ---------------------------------------------------------------- ① 常量 + version + 旁注


def test_schema_push_live_lock_version_present():
    """G-T3: version PRESENT、非空、等于 `schema_push_live_v1`。"""
    from app.buy_sell_signals import schema_push_live_lock as g

    assert g.SCHEMA_PUSH_LIVE_LOCK_VERSION
    assert g.SCHEMA_PUSH_LIVE_LOCK_VERSION == "schema_push_live_v1"
    assert isinstance(g.SCHEMA_PUSH_LIVE_LOCK_VERSION, str)
    assert g.SCHEMA_PUSH_LIVE_LOCK_NOTE
    assert "schema_push_live_v1" in g.SCHEMA_PUSH_LIVE_LOCK_NOTE


def test_annotation_constant_is_research_not_scaled():
    """G-T3: 旁注常量＝「研究档 · 未放量」，逐字。"""
    from app.buy_sell_signals import schema_push_live_lock as g

    assert g.SCHEMA_PUSH_LIVE_ANNOTATION == _ANNOTATION
    assert g.SCHEMA_PUSH_LIVE_ANNOTATION
    assert isinstance(g.SCHEMA_PUSH_LIVE_ANNOTATION, str)
    g.assert_annotation_present()


def test_annotation_half_deleted_still_fires(monkeypatch):
    """负控: 旁注被删成只剩「研究档」（仍然非空），断言必须发火。

    「非空即可」的守卫会放这条过去 —— 而它恰恰是最可能的坏法：
    有人觉得"未放量"多余就删掉，语义直接塌一半。
    """
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(g, "SCHEMA_PUSH_LIVE_ANNOTATION", "研究档")
    with pytest.raises(AssertionError, match="must be '研究档 · 未放量'"):
        g.assert_annotation_present()


def test_annotation_is_clean_of_price_qty_position_order():
    """G-T5: 旁注本身不含价量仓单、不含六词动作指令式、不含裸动作词。"""
    from app.buy_sell_signals import schema_push_live_lock as g

    g.assert_annotation_is_clean()
    annotation = g.SCHEMA_PUSH_LIVE_ANNOTATION
    for banned in ("price", "qty", "position", "order", "价格", "数量", "仓位", "订单"):
        assert banned not in annotation.lower(), f"旁注含禁词: {banned}"
    for word in ("买入", "卖出", "加仓", "减仓", "开多", "开空", "买", "卖"):
        assert word not in annotation, f"旁注含动作词: {word}"


def test_annotation_clean_guard_fires_on_dirty_annotation(monkeypatch):
    """负控: 旁注里塞价格，干净度断言必须发火。"""
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(g, "SCHEMA_PUSH_LIVE_ANNOTATION", "研究档 · 未放量 · price=10.5")
    with pytest.raises(AssertionError, match="价量仓单"):
        g.assert_annotation_is_clean()


def test_annotation_clean_guard_fires_on_action_phrase(monkeypatch):
    """负控: 旁注里塞「建议买入」，干净度断言必须发火。"""
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(g, "SCHEMA_PUSH_LIVE_ANNOTATION", "研究档 · 建议买入 · 未放量")
    with pytest.raises(AssertionError, match="动作指令式"):
        g.assert_annotation_is_clean()


def test_six_word_action_phrases_present():
    """禁六词集必须是**六**条，且逐条在。"""
    from app.buy_sell_signals import schema_push_live_lock as g

    assert len(g.SCHEMA_PUSH_LIVE_FORBIDDEN_ACTION_PHRASES) == 6
    for phrase in (
        "可以买入",
        "建议买入",
        "强烈买入",
        "可以卖出",
        "建议卖出",
        "强烈卖出",
    ):
        assert phrase in g.SCHEMA_PUSH_LIVE_FORBIDDEN_ACTION_PHRASES, phrase


# ---------------------------------------------------------------- ② 模板旁注（本拍核心）


def test_build_hit_dingtalk_text_contains_annotation():
    """G-T6 · 核心: `build_hit_dingtalk_text` 输出必须含旁注。"""
    from app.buy_sell_signals.hits_dingtalk import build_hit_dingtalk_text

    _, text = build_hit_dingtalk_text(_hit())
    assert _ANNOTATION in text
    lines = text.split("\n")
    assert _ANNOTATION in lines, f"旁注必须是独立一行, 实际行: {lines}"


def test_annotation_line_matches_lock_constant():
    """旁注一致性: 模板里那一行必须**逐字**等于锁面常量。

    锁面故意不 import `hits_dingtalk`（契约层不依赖运行时）。代价就是文案有两处，
    所以这条交叉断言是必需的 —— 两处一旦分叉，这里当场红。
    """
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals.hits_dingtalk import build_hit_dingtalk_text

    _, text = build_hit_dingtalk_text(_hit())
    lines = text.split("\n")
    annotation_lines = [ln for ln in lines if "研究档" in ln or "未放量" in ln]
    assert len(annotation_lines) == 1, f"旁注行数应为 1, 实际 {annotation_lines}"
    assert annotation_lines[0] == g.SCHEMA_PUSH_LIVE_ANNOTATION


def test_annotation_sits_before_disclaimer():
    """旁注在 disclaimer **之前**、且在空行之后 —— 建议位置的落地校验。"""
    from app.buy_sell_signals import disclaimer as D  # noqa: N812
    from app.buy_sell_signals.hits_dingtalk import build_hit_dingtalk_text

    _, text = build_hit_dingtalk_text(_hit())
    lines = text.split("\n")
    idx_annotation = lines.index(_ANNOTATION)
    idx_disclaimer = next(i for i, ln in enumerate(lines) if D.DISCLAIMER_TEXT in ln)
    assert idx_annotation < idx_disclaimer, "旁注应在 disclaimer 之前"
    assert lines[idx_annotation - 1] == "", "旁注前应有一个空行"
    assert idx_disclaimer == len(lines) - 1, "disclaimer 应是最后一行"


def test_text_line_structure_is_pinned():
    """G-T7 · 行结构钉死: 正文恰好 6 行，逐行形态固定。

    这是「旁注最小触碰」的行为级守卫 —— 有人多加一行、少加一行、改顺序，立刻红。
    比读 git diff 可靠：不依赖 CI 的浅克隆深度。
    """
    from app.buy_sell_signals import disclaimer as D  # noqa: N812
    from app.buy_sell_signals.hits_dingtalk import build_hit_dingtalk_text

    hit = _hit()
    _, text = build_hit_dingtalk_text(hit)
    lines = text.split("\n")
    assert len(lines) == 6, f"正文应为 6 行, 实际 {len(lines)}: {lines}"

    assert lines[0] == f"**命中** {hit.symbol} · {hit.direction_label}"
    assert lines[1].startswith("signal_id=")
    assert "definition_version=" in lines[1] and "asof=" in lines[1]
    assert lines[2].startswith("family=")
    assert "event_class=" in lines[2] and "hit_id=" in lines[2]
    assert lines[3] == ""
    assert lines[4] == _ANNOTATION
    assert lines[5] == f"_{D.DISCLAIMER_TEXT}_"


def test_title_semantics_unchanged():
    """G-T7: title 前缀与 64 字截断语义**未漂**（旁注只动正文）。"""
    from dataclasses import replace

    from app.buy_sell_signals.hits_dingtalk import build_hit_dingtalk_text

    hit = _hit()
    title, _ = build_hit_dingtalk_text(hit)
    assert title == f"买卖信号命中 · {hit.symbol}"[:64]
    assert title.startswith("买卖信号命中")

    long_hit = replace(hit, symbol="0" * 200)
    long_title, _ = build_hit_dingtalk_text(long_hit)
    assert len(long_title) <= 64


def test_text_keeps_no_price_quantity_position():
    """G-T5: 加了旁注之后，正文仍不含价量仓单禁词。"""
    from app.buy_sell_signals.hits_dingtalk import build_hit_dingtalk_text

    _, text = build_hit_dingtalk_text(_hit())
    for banned in (
        "price",
        "价格",
        "仓位",
        "数量",
        "订单",
        "买入价",
        "止损",
        "target",
        "position",
    ):
        assert banned not in text, f"命中外发正文出现禁词: {banned}"


def test_build_hit_dingtalk_text_body_shape_is_static():
    """结构守: `build_hit_dingtalk_text` 函数体**无控制流** —— 旁注无条件加。

    一旦有人把旁注包进 `if ...:`，它就变成了"某些情况不带"。
    这条查 AST 语句类型，不查文本。
    """
    fn = _find_function(_DINGTALK_PATH, "build_hit_dingtalk_text")
    for node in ast.walk(fn):
        assert not isinstance(
            node, (ast.If, ast.For, ast.While, ast.Try, ast.With)
        ), f"build_hit_dingtalk_text 出现控制流: {type(node).__name__}"


def test_annotation_literal_in_source_once():
    """模板源码里旁注字面量只出现一次（写在 `lines` 里那行）。"""
    code = _code_only(_DINGTALK_PATH)
    assert code.count(_ANNOTATION) == 1, (
        f"旁注字面量在模板源码中出现 {code.count(_ANNOTATION)} 次，应为 1"
    )


# ---------------------------------------------------------------- ③ schema + 两 TRIGGERING + COMPLETE


def test_hits_schema_version_still_v1():
    """G-T4 / G-T9: `HITS_SCHEMA_VERSION` **仍是字符串 `"1"`**，不是 int 1，更不是 "2"。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals.constants import HITS_SCHEMA_VERSION

    assert HITS_SCHEMA_VERSION == "1"
    assert isinstance(HITS_SCHEMA_VERSION, str)
    assert g.EXPECTED_HITS_SCHEMA_VERSION == "1"
    g.assert_hits_schema_still_v1()


def test_both_triggering_still_false():
    """G-T4 / G-T9: 两处开关**仍是** `False`（`is` 而非 `==`）。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_TRIGGERING_ENABLED,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_TRIGGERING_ENABLED,
    )

    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False
    assert type(VOLUME_BREAKOUT_TRIGGERING_ENABLED) is bool
    assert type(AUCTION_ABNORMAL_TRIGGERING_ENABLED) is bool
    g.assert_triggering_still_false()


def test_complete_still_false():
    """G-T4 / G-T9: A2 完成门**仍关**。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals.first_seen_audit import (
        EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE,
    )

    assert EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False
    assert type(EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE) is bool
    g.assert_complete_still_false()


def test_assert_fires_when_schema_bumped(monkeypatch):
    """负控: bump `HITS_SCHEMA_VERSION` 成 "2"，断言**必须发火**（同时验活引用）。"""
    from app.buy_sell_signals import constants
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", "2")
    with pytest.raises(AssertionError, match="HITS_SCHEMA_VERSION must stay '1'"):
        g.assert_hits_schema_still_v1()


def test_assert_fires_when_schema_is_int_one(monkeypatch):
    """负控: 把 `"1"` 换成 int `1` 也要发火 —— 契约钉的是字面量。"""
    from app.buy_sell_signals import constants
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", 1)
    with pytest.raises(AssertionError, match="HITS_SCHEMA_VERSION must stay '1'"):
        g.assert_hits_schema_still_v1()


def test_assert_fires_when_volume_triggering_flipped(monkeypatch):
    """负控: 把量能开关改成 True，断言**必须发火**。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals import volume_breakout_stub

    monkeypatch.setattr(volume_breakout_stub, "VOLUME_BREAKOUT_TRIGGERING_ENABLED", True)
    with pytest.raises(AssertionError, match="VOLUME_BREAKOUT_TRIGGERING_ENABLED"):
        g.assert_triggering_still_false()


def test_assert_fires_when_auction_triggering_flipped(monkeypatch):
    """负控: 竞价开关改 True，断言必须发火。"""
    from app.buy_sell_signals import auction_abnormal_stub
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(
        auction_abnormal_stub, "AUCTION_ABNORMAL_TRIGGERING_ENABLED", True
    )
    with pytest.raises(AssertionError, match="AUCTION_ABNORMAL_TRIGGERING_ENABLED"):
        g.assert_triggering_still_false()


def test_assert_fires_when_complete_flipped(monkeypatch):
    """负控: COMPLETE 翻 True，断言必须发火。"""
    from app.buy_sell_signals import first_seen_audit
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(first_seen_audit, "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE", True)
    with pytest.raises(AssertionError, match="EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE"):
        g.assert_complete_still_false()


def test_truthy_substitute_still_fires(monkeypatch):
    """负控: 拿 `1` 顶替 False 也要发火 —— `not 1` 是 False，`1 is not False` 才是守卫。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals import volume_breakout_stub

    monkeypatch.setattr(volume_breakout_stub, "VOLUME_BREAKOUT_TRIGGERING_ENABLED", 1)
    with pytest.raises(AssertionError, match="VOLUME_BREAKOUT_TRIGGERING_ENABLED"):
        g.assert_triggering_still_false()


# ---------------------------------------------------------------- ④ DIRECTION / Bonferroni


def test_direction_labels_still_three_values():
    """G-T5: 方向仍三值，`research_watch` **仍在**（不许删）。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals.constants import DIRECTION_LABELS

    assert DIRECTION_LABELS == {"research_buy", "research_sell", "research_watch"}
    assert len(DIRECTION_LABELS) == 3
    assert "research_watch" in DIRECTION_LABELS
    g.assert_direction_labels_untouched()


def test_bonferroni_m_still_6():
    """G-T8: `BONFERRONI_M` 仍 6（开档边不占 m，但也不许顺手改 m）。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals.constants import BONFERRONI_M

    assert BONFERRONI_M == 6
    g.assert_bonferroni_untouched()


def test_assert_fires_when_direction_drifted(monkeypatch):
    """负控: DIRECTION 少一个值（比如删 `research_watch`），断言必须发火。"""
    from app.buy_sell_signals import constants
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(
        constants, "DIRECTION_LABELS", frozenset({"research_buy", "research_sell"})
    )
    with pytest.raises(AssertionError, match="DIRECTION_LABELS mutated"):
        g.assert_direction_labels_untouched()


# ---------------------------------------------------------------- ⑤ 交叉 schema_push / trigger_gate / shadow_obs / T1 / T2


def test_cross_schema_push_gate_shadow_t1_t2_versions():
    """G-T4: 五处只读交叉 version 一致。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals import (
        schema_push_lock,
        shadow_obs_lock,
        tier_cut_lock,
        tier_display_lock,
        trigger_gate_lock,
    )

    assert schema_push_lock.SCHEMA_PUSH_LOCK_VERSION == "schema_push_v1"
    assert trigger_gate_lock.TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    assert shadow_obs_lock.SHADOW_OBS_LOCK_VERSION == "shadow_obs_v1"
    assert tier_cut_lock.TIER_CUT_RULE_VERSION == "tier_cut_v1"
    assert tier_display_lock.TIER_DISPLAY_LOCK_VERSION == "tier_display_v1"
    assert g.EXPECTED_SCHEMA_PUSH_LOCK_VERSION == "schema_push_v1"
    assert g.EXPECTED_TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    assert g.EXPECTED_SHADOW_OBS_LOCK_VERSION == "shadow_obs_v1"
    assert g.EXPECTED_T1_RULE_VERSION == "tier_cut_v1"
    assert g.EXPECTED_T2_LOCK_VERSION == "tier_display_v1"
    g.assert_schema_push_gate_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_on_schema_push_drift(monkeypatch):
    """负控: schema_push version 漂移，交叉断言必须发火。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals import schema_push_lock

    monkeypatch.setattr(schema_push_lock, "SCHEMA_PUSH_LOCK_VERSION", "schema_push_v2")
    with pytest.raises(AssertionError, match="schema_push version drift"):
        g.assert_schema_push_gate_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_on_trigger_gate_drift(monkeypatch):
    """负控: trigger_gate version 漂移，交叉断言必须发火。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals import trigger_gate_lock

    monkeypatch.setattr(trigger_gate_lock, "TRIGGER_GATE_LOCK_VERSION", "trigger_gate_v2")
    with pytest.raises(AssertionError, match="trigger_gate version drift"):
        g.assert_schema_push_gate_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_on_shadow_obs_drift(monkeypatch):
    """负控: shadow_obs version 漂移，交叉断言必须发火。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals import shadow_obs_lock

    monkeypatch.setattr(shadow_obs_lock, "SHADOW_OBS_LOCK_VERSION", "shadow_obs_v2")
    with pytest.raises(AssertionError, match="shadow_obs version drift"):
        g.assert_schema_push_gate_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_on_t1_drift(monkeypatch):
    """负控: T1 version 漂移，交叉断言必须发火。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals import tier_cut_lock

    monkeypatch.setattr(tier_cut_lock, "TIER_CUT_RULE_VERSION", "tier_cut_v2")
    with pytest.raises(AssertionError, match="T1 version drift"):
        g.assert_schema_push_gate_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_on_t2_drift(monkeypatch):
    """负控: T2 version 漂移，交叉断言必须发火。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals import tier_display_lock

    monkeypatch.setattr(tier_display_lock, "TIER_DISPLAY_LOCK_VERSION", "tier_display_v2")
    with pytest.raises(AssertionError, match="T2 version drift"):
        g.assert_schema_push_gate_shadow_t1_t2_cross_lock()


def test_cross_lock_fires_when_shadow_obs_default_enabled(monkeypatch):
    """负控: 影子观测口默认被打开 —— 开档层取向必须一致（默认关）。"""
    from app.buy_sell_signals import schema_push_live_lock as g
    from app.buy_sell_signals import shadow_obs_lock

    monkeypatch.setattr(shadow_obs_lock, "SHADOW_OBS_DEFAULT_ENABLED", True)
    with pytest.raises(AssertionError, match="default-enabled drifted"):
        g.assert_schema_push_gate_shadow_t1_t2_cross_lock()


# ---------------------------------------------------------------- ⑥ 无 allow_shadow


def test_no_allow_shadow_near_schema_push_live():
    """G-T4: 锁面与契约层命名空间无 `allow_shadow` 旁路。"""
    from app.buy_sell_signals import auction_abnormal_stub, constants, volume_breakout_stub
    from app.buy_sell_signals import schema_push_live_lock as g

    for module in (g, constants, volume_breakout_stub, auction_abnormal_stub):
        g.assert_no_allow_shadow(dir(module))
    # 守卫函数自己叫 `assert_no_allow_shadow` —— 按**精确名**查，不按子串。
    banned = {"allow_shadow", "ALLOW_SHADOW", "AllowShadow"}
    assert not (banned & set(dir(g)))


def test_allow_shadow_guard_fires_on_synthetic_namespace():
    """负控: 塞一个 `allow_shadow` 进去，守卫必须发火。"""
    from app.buy_sell_signals import schema_push_live_lock as g

    with pytest.raises(AssertionError, match="allow_shadow forbidden"):
        g.assert_no_allow_shadow(["foo", "allow_shadow", "bar"])
    with pytest.raises(AssertionError, match="allow_shadow forbidden"):
        g.assert_no_allow_shadow(["ALLOW_SHADOW"])


# ---------------------------------------------------------------- ⑦ 文档 / skeleton


def test_schema_push_live_doc_exists_and_covers_contract_points():
    """G-T2 / G-T10: 文档 PRESENT 且契约要点逐条在正文里。"""
    assert _DOC_PATH.is_file(), f"真推 live 开档契约文档缺失: {_DOC_PATH}"
    text = _DOC_PATH.read_text(encoding="utf-8")

    required = [
        # 1 产品语义
        "真推 live",
        "开档/落地层",
        "**不是** bump schema",
        "**不是** 新增 live 写入/调度",
        "**不是** 翻 TRIGGERING",
        "**不是** 可放量",
        "开档/旁注落地 ≠ 可放量 ≠ 翻闸 ≠ 已有 live 写入",
        # 2 现状锚
        "schema_push_v1",
        "HITS_SCHEMA_VERSION",
        'Final = "1"',
        "hits_models",
        "build_hit_dingtalk_text",
        "try_push_hit",
        "门禁 PRESENT ≠ 开档层已齐",
        # 3 前置核对清单
        "前置核对清单",
        "表齐 ≠ 可放量 ≠ 可翻闸 ≠ 可调度写 live",
        "否决清单",
        # 4 边界
        "零碰字段集",
        "仅追加",
        "零碰门序",
        "可配置的旁注等于没有旁注",
        # 5 方向 / 禁词
        "DIRECTION_LABELS",
        "research_watch",
        "禁六词",
        "价量仓单",
        # 6 门禁大字
        "路径已通 **≠** calib **≠** 可放量 **≠** TRIGGERING",
        "K8 PASS **≠** 可放量 **≠** 可开 TRIGGERING",
        "讨论 **≠** 派单",
        "影子写入 **≠** 观测口 **≠** 放量",
        "合入 **≠** 部署",
        "门禁契约 ≠ 授权翻闸",
        "schema 门禁契约 ≠ 授权真推 live",
        # 7 Bonferroni
        "m = 6",
        "**不是**永久豁免",
        # 8 挂点 / 另口令
        "另口令",
        "schema_push_live_lock.py",
        # 9 tip=prod
        "5ca4409802a0ee9078f2fb345f5b7821bad67964",
        "tip = prod",
        "pre263_20261031_220404",
        # 10 脚注
        "仅供研究，不构成投资建议；执行须人工确认",
        "红涨绿跌",
        "18.2bp",
    ]
    missing = [needle for needle in required if needle not in text]
    assert not missing, f"文档缺少契约要点: {missing}"


def test_doc_states_this_is_not_bump_or_live_write():
    """文档必须**明写**「不是什么」——防止被读成"已经可以真推/已写 live"。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    for needle in (
        "**不是** bump schema",
        "**不是** 新增 live 写入/调度",
        "**不是** 翻 TRIGGERING",
        "**不是** 可放量",
    ):
        assert needle in text, f"文档缺「不是」声明: {needle}"


def test_doc_says_table_is_veto_list_not_pass():
    """前置清单必须写明它是**否决清单**，不是通行证。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    assert "它是否决清单，不是通行证" in text
    assert "因为本拍没有那个状态" in text


def test_skeleton_links_to_schema_push_live_and_keeps_footnote_last():
    """G-T2: skeleton 有一节链过去；脚注仍在**最后一行**。"""
    text = _SKELETON_PATH.read_text(encoding="utf-8")
    assert "buy-sell-signal-schema-push-live.md" in text
    assert "schema_push_live_lock.py" in text
    assert "schema_push_live_v1" in text

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
        "buy-sell-signal-schema-push.md",
    ):
        assert needle in text, f"既有链被动了: {needle}"


def test_gate_doc_keeps_body_and_only_appends_side_link():
    """旁链只追加: 门禁文档正文语义仍在，且旁链指向开档文档。"""
    text = _GATE_DOC_PATH.read_text(encoding="utf-8")
    assert "buy-sell-signal-schema-push-live.md" in text
    # 门禁文档自身的关键语义仍在（未被旁链冲掉）
    for needle in ("schema_push_v1", "真推另口令", "schema 门禁契约 ≠ 授权真推 live"):
        assert needle in text, f"门禁文档正文被动了: {needle}"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[-1] == _FOOTNOTE, f"门禁文档末行不是脚注: {lines[-1]!r}"


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


def test_lock_module_does_not_import_runtime_push_layer():
    """锁面**不** import `hits_dingtalk` / `hits_models` —— 契约层不依赖运行时。

    旁注一致性靠测例交叉，不靠 import 图。锁面若 import 了推送层，
    契约断言就会与网络/IO 生命周期耦合。

    查的是 AST 里的 **import 与调用/引用节点**，不查源码文本 ——
    本模块的说明文字里必然提到 `build_hit_dingtalk_text`（那是在解释为什么要交叉断言），
    文本查会把这段解释判成违规，进而逼人删掉解释。
    """
    relative = _relative_import_modules(_LOCK_PATH)
    assert "hits_dingtalk" not in relative
    assert "hits_models" not in relative

    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    banned_names = {"build_hit_dingtalk_text", "try_push_hit", "hits_dingtalk", "hits_models"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in banned_names:
                offenders.append(str(name))
        elif isinstance(node, ast.Name) and node.id in banned_names:
            offenders.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in banned_names:
            offenders.append(node.attr)
    assert not offenders, f"锁面不该引用运行时推送层: {sorted(set(offenders))}"


def test_lock_module_declares_no_side_effect_calls():
    """锁面无写路径；顶层只允许常量与 def（import 时不硬 raise）。"""
    code = _code_only(_LOCK_PATH)
    # `open(` 用**标识符左边界**判 —— 本锁面有 `assert_annotation_present`,
    # 但若将来出现 `..._still_open()`, 裸子串会把函数名判成文件打开。
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
    """G-T7: 锁面**不得**给 schema version / TRIGGERING / COMPLETE 赋值。

    查 AST 赋值目标，不查文本 —— 查文本会把 docstring 里「不 bump」的说明判成违规，
    进而逼人删掉说明。
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
        "SCHEMA_PUSH_LOCK_VERSION",
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
        "schema_push_lock",
        "trigger_gate_lock",
        "shadow_obs_lock",
        "tier_cut_lock",
        "tier_display_lock",
    ):
        assert module in relative, f"锁面缺模块别名 import: {module}"

    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    snapshot_forbidden = {
        "constants",
        "volume_breakout_stub",
        "auction_abnormal_stub",
        "first_seen_audit",
        "schema_push_lock",
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

    结构守查源码形状；这条查运行时事实。万一有人用 `__getattr__` 之类巧妙办法
    绕过结构守，这条仍会红。
    """
    from app.buy_sell_signals import constants
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", "1")  # 先钉回期望值
    g.assert_hits_schema_still_v1()  # 绿
    monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", "3")
    with pytest.raises(AssertionError):
        g.assert_hits_schema_still_v1()  # 同一函数对象，立刻看到新值


def test_lock_all_exports_are_real_and_sorted():
    """`__all__` 每个名字都真存在，且排序。"""
    from app.buy_sell_signals import schema_push_live_lock as g

    assert g.__all__
    missing = [name for name in g.__all__ if not hasattr(g, name)]
    assert not missing, f"__all__ 声明了不存在的符号: {missing}"
    assert len(set(g.__all__)) == len(g.__all__), "__all__ 有重复项"
    assert list(g.__all__) == sorted(g.__all__)


def test_lock_module_docstring_carries_footnote_and_big_letters():
    """G-T10: 模块 docstring 须含脚注与门禁大字。"""
    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree) or ""
    for needle in (
        "仅供研究，不构成投资建议；执行须人工确认",
        "红涨绿跌",
        "18.2bp",
        "开档/旁注落地 ≠ 可放量 ≠ 翻闸 ≠ 已有 live 写入",
        "schema 门禁契约 ≠ 授权真推 live",
        "门禁契约 ≠ 授权翻闸",
    ):
        assert needle in doc, f"锁面 docstring 缺: {needle}"


# ---------------------------------------------------------------- ⑨ 一把梭


def test_invariants_all_green():
    """全部不变量一次过 —— CI 里真正跑的那条门。"""
    from app.buy_sell_signals import schema_push_live_lock as g

    g.assert_schema_push_live_lock_invariants()
    g.assert_annotation_present()
    g.assert_annotation_is_clean()
    g.assert_hits_schema_still_v1()
    g.assert_triggering_still_false()
    g.assert_complete_still_false()
    g.assert_schema_push_gate_shadow_t1_t2_cross_lock()


def test_invariants_fires_when_version_drifted(monkeypatch):
    """负控: version 漂移，聚合断言必须发火。"""
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(g, "SCHEMA_PUSH_LIVE_LOCK_VERSION", "schema_push_live_v2")
    with pytest.raises(AssertionError, match="must be 'schema_push_live_v1'"):
        g.assert_schema_push_live_lock_invariants()


def test_invariants_fires_when_schema_bumped(monkeypatch):
    """负控: 聚合断言必须能看见 schema bump —— 不是只有单条断言看得到。"""
    from app.buy_sell_signals import constants
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(constants, "HITS_SCHEMA_VERSION", "2")
    with pytest.raises(AssertionError, match="HITS_SCHEMA_VERSION"):
        g.assert_schema_push_live_lock_invariants()


def test_invariants_fires_when_annotation_dirtied(monkeypatch):
    """负控: 聚合断言必须能看见旁注被弄脏。

    注意这里**不**限定错误消息：聚合断言第一步是 `assert_annotation_present()`，
    它钉死逐字文案，所以"脏旁注"在那一关就先炸了 —— 这比走到干净度检查更早、
    更强。限定成 `价量仓单` 反而会误判成失败（#263 踩过的同类坑）。
    """
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(g, "SCHEMA_PUSH_LIVE_ANNOTATION", "研究档 · 未放量 仓位50%")
    with pytest.raises(AssertionError):
        g.assert_schema_push_live_lock_invariants()


def test_annotation_clean_guard_fires_independently(monkeypatch):
    """负控: 绕过"逐字文案"直接调干净度断言，它自己也要发火。

    这条证明 `assert_annotation_is_clean` 不是摆设：即使有一天文案常量被改成
    另一版（并同步改了 present 断言），禁词检查仍然独立成立。
    """
    from app.buy_sell_signals import schema_push_live_lock as g

    monkeypatch.setattr(g, "SCHEMA_PUSH_LIVE_ANNOTATION", "研究档 · 未放量 · position=1")
    with pytest.raises(AssertionError, match="价量仓单"):
        g.assert_annotation_is_clean()
