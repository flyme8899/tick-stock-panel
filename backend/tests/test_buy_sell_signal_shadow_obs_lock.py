"""R2-3 独立影子观测口契约锁面 + 契约文档（批次 20261031gshadow · P-bss-shadow · #-A-#9）。

## 这组测在测什么

R2-3 交付的是**观测口契约**，坏法有三种，且前两种都不报错：

1. **契约漂了** —— 默认关变成默认开、空态枚举少一个、scope 明细禁集漏了键、
   三键改名。这类坏法**不会报错**，只会让观测口某天开始显示"一切正常"而实际没数据。
2. **空态撒谎** —— 空桶报 `ok`。这是本契约最要命的坏法：观测口**永远健康**，
   永远不暴露「根本没数据」。与 T1「无量＝无档、禁止 weak 顶缺失」同一个错误。
3. **本拍越界** —— 锁面 import 了 `hits_shadow_store`（存储实现）或 `hits_dingtalk`
   （推送实现），把契约层耦到实现层；或者顺手把 T1/T2 的 version 改了。
   这类坏法**没有运行时表现**，只能读源码 / 读 import 表钉。

## 为什么三键要在**测例**里交叉，而不是在锁面里 import

锁面是**纯契约层**，P3b 是**存储实现**。锁面若 import 它，`config` / `Path` /
`tempfile` 这些存储侧依赖就进了契约层，而且「实现改名但语义没变」与「语义被改了」
在 import 期无法区分。

所以：锁面**写死**三键字面量，**测例**去 import 真实 P3b 并比对。
漂移在这里红，而不是在锁面里假装交叉。

## 不测什么

不测算档结果、不测观测 UI、不测真推 —— 本拍只有契约，这些全部另口令。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

# ruff: noqa: SIM300
# SIM300: 本文件满是 `assert CONST == 字面量`, ruff 把全大写标识符当"常量应在右",
#         在 assert 上系统性误报 —— 把字面量翻到左边才是真的不可读。

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCK_PATH = (
    _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "shadow_obs_lock.py"
)
_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-shadow-obs.md"
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
    """ast 解析出**同包相对** import 的模块名（``from .x import y`` → ``x``）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module:
            out.add(node.module)
    return out


def _code_only(path: Path) -> str:
    """去掉 docstring 与注释后的源码 —— 避免"文档里写着不算"造成假阳性。"""
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


# ---------------------------------------------------------------- ① 常量 PRESENT + 默认关


def test_shadow_obs_lock_constants_present_default_off():
    """G-T3 / G-T6: 常量齐备，version 非空，**默认关**是 ``False`` 本身。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    assert s.SHADOW_OBS_LOCK_VERSION
    assert s.SHADOW_OBS_LOCK_VERSION == "shadow_obs_v1"
    # `is False` 而不是 `== False`：0 / "" / None 都不许冒充"关"
    assert s.SHADOW_OBS_DEFAULT_ENABLED is False
    assert s.SHADOW_OBS_STATE_MARKERS == frozenset({"closed", "empty", "stale", "ok"})
    for marker in ("closed", "empty", "stale", "ok"):
        assert marker in s.SHADOW_OBS_STATE_MARKERS, marker
    assert s.SHADOW_OBS_P3B_BUCKET_KEYS == (
        "signal_id",
        "definition_version",
        "freq_lock_version",
    )
    assert s.SHADOW_OBS_LOCK_NOTE
    assert "shadow_obs_v1" in s.SHADOW_OBS_LOCK_NOTE


def test_default_enabled_must_be_literal_false_not_truthy_substitute():
    """默认关不许被 ``0`` / ``""`` / ``None`` 顶替 —— 那些在布尔语境也"假"。

    用 ``is False`` 钉死类型：`SHADOW_OBS_DEFAULT_ENABLED = 0` 会让
    `if not ENABLED:` 仍然成立，但契约里写的是"默认关"这个**布尔事实**。
    """
    from app.buy_sell_signals import shadow_obs_lock as s

    assert s.SHADOW_OBS_DEFAULT_ENABLED is False
    assert type(s.SHADOW_OBS_DEFAULT_ENABLED) is bool


# ---------------------------------------------------------------- ② DIRECTION_LABELS 仍三值


def test_direction_labels_still_three_values():
    """G-T5: 方向标签未被本拍改动；``research_watch`` 仍在。"""
    from app.buy_sell_signals import shadow_obs_lock as s
    from app.buy_sell_signals.constants import DIRECTION_LABELS

    assert DIRECTION_LABELS == frozenset(
        {"research_buy", "research_sell", "research_watch"}
    )
    assert "research_watch" in DIRECTION_LABELS
    s.assert_direction_labels_untouched()


# ---------------------------------------------------------------- ③ T1/T2 交叉 + P3a 仍绿


def test_t1_t2_cross_and_p3a_still_green():
    """G-T4: 只读交叉 T1/T2 version；P3a ``NON_LIVE_MODE`` 仍在。"""
    from app.buy_sell_signals import shadow_obs_lock as s
    from app.buy_sell_signals import tier_cut_lock as t1
    from app.buy_sell_signals import tier_display_lock as t2
    from app.buy_sell_signals.hits_visibility import VisibilityReject

    s.assert_t1_t2_cross_lock()
    assert t1.TIER_CUT_RULE_VERSION == "tier_cut_v1"
    assert t2.TIER_DISPLAY_LOCK_VERSION == "tier_display_v1"
    s.assert_p3a_gate_still_green()
    assert hasattr(VisibilityReject, "NON_LIVE_MODE")
    # VisibilityReject 是普通类属性字符串容器（非 Enum）—— 用字符串直比
    assert VisibilityReject.NON_LIVE_MODE == "NON_LIVE_MODE"
    s.assert_shadow_obs_lock_invariants()


def test_no_allow_shadow_anywhere_near_the_gate():
    """G-T4: ``allow_shadow`` **没有**也不许将来加。

    检查三处命名空间：本锁面自己、P3b 存储、P3a 推送。
    推送那处才是「不许加」的本体 —— 影子命中与生产命中形状一致，
    有开关就有「打开的那天」。
    """
    from app.buy_sell_signals import hits_dingtalk, hits_shadow_store
    from app.buy_sell_signals import shadow_obs_lock as s

    s.assert_no_allow_shadow(dir(s))
    s.assert_no_allow_shadow(dir(hits_dingtalk))
    s.assert_no_allow_shadow(dir(hits_shadow_store))
    assert not hasattr(s, "allow_shadow")
    assert not hasattr(s, "ALLOW_SHADOW")


def test_cross_lock_actually_fires_on_t1_drift(monkeypatch):
    """负控: 交叉断言**真会炸** —— 别写出一个永远为真的守卫。"""
    from app.buy_sell_signals import shadow_obs_lock as s
    from app.buy_sell_signals import tier_cut_lock as t1
    from app.buy_sell_signals import tier_display_lock as t2

    monkeypatch.setattr(t1, "TIER_CUT_RULE_VERSION", "tier_cut_v2")
    with pytest.raises(AssertionError, match="T1 version drift"):
        s.assert_t1_t2_cross_lock()

    monkeypatch.setattr(t1, "TIER_CUT_RULE_VERSION", "tier_cut_v1")
    monkeypatch.setattr(t2, "TIER_DISPLAY_LOCK_VERSION", "tier_display_v2")
    with pytest.raises(AssertionError, match="T2 version drift"):
        s.assert_t1_t2_cross_lock()


def test_p3a_assert_fires_when_non_live_mode_renamed(monkeypatch):
    """负控: P3a 交叉断言真会炸 —— 改名必须被抓到。"""
    from app.buy_sell_signals import shadow_obs_lock as s
    from app.buy_sell_signals.hits_visibility import VisibilityReject

    monkeypatch.setattr(
        VisibilityReject, "NON_LIVE_MODE", "NON_LIVE_ONLY", raising=True
    )
    with pytest.raises(AssertionError, match="NON_LIVE_MODE mutated"):
        s.assert_p3a_gate_still_green()


def test_allow_shadow_guard_fires_on_synthetic_namespace():
    """负控: ``allow_shadow`` 守卫不是空转 —— 喂进一个带开关的命名空间必须炸。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    with pytest.raises(AssertionError, match="allow_shadow must not exist"):
        s.assert_no_allow_shadow(["foo", "allow_shadow", "bar"])
    with pytest.raises(AssertionError, match="allow_shadow must not exist"):
        s.assert_no_allow_shadow(["ALLOW_SHADOW"])


# ---------------------------------------------------------------- ④ 空态诚实性（本契约核心）


def test_empty_bucket_must_not_report_ok():
    """**本契约最关键的一条**：空桶报 ``ok`` 必须炸。

    允许空桶报 ok，观测口就永远显示健康、永不暴露「根本没数据」——
    与 T1「无量＝无档、禁止 weak 顶缺失」是同一个错误的两种形态。
    """
    from app.buy_sell_signals import shadow_obs_lock as s

    # 空桶：closed / empty 合法
    s.assert_state_marker_honest(marker="closed", item_count=0)
    s.assert_state_marker_honest(marker="empty", item_count=0)
    # 空桶：ok / stale 非法 —— 没东西谈不上过期，也谈不上可读
    with pytest.raises(AssertionError, match="dishonest"):
        s.assert_state_marker_honest(marker="ok", item_count=0)
    with pytest.raises(AssertionError, match="dishonest"):
        s.assert_state_marker_honest(marker="stale", item_count=0)


def test_nonempty_bucket_must_not_report_empty():
    """反向也钉：有内容却报 ``empty`` 是撒谎。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    s.assert_state_marker_honest(marker="ok", item_count=3)
    s.assert_state_marker_honest(marker="stale", item_count=3)
    s.assert_state_marker_honest(marker="closed", item_count=3)
    with pytest.raises(AssertionError, match="dishonest"):
        s.assert_state_marker_honest(marker="empty", item_count=3)


def test_state_marker_rejects_unknown_and_negative():
    """未知标记 / 负条目数都要炸 —— 防止"新增一个近义词"悄悄放行。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    with pytest.raises(AssertionError, match="unknown state marker"):
        s.assert_state_marker_honest(marker="healthy", item_count=0)
    with pytest.raises(AssertionError, match="item_count must be >= 0"):
        s.assert_state_marker_honest(marker="ok", item_count=-1)


def test_zero_and_nonzero_marker_sets_are_sane():
    """两个标记集必须覆盖全集，且 ``empty`` 只在零条目侧、``ok/stale`` 只在对侧。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    assert (
        s.SHADOW_OBS_ZERO_ITEM_MARKERS | s.SHADOW_OBS_NONZERO_ITEM_MARKERS
        == s.SHADOW_OBS_STATE_MARKERS
    )
    assert "empty" in s.SHADOW_OBS_ZERO_ITEM_MARKERS
    assert "empty" not in s.SHADOW_OBS_NONZERO_ITEM_MARKERS
    assert {"ok", "stale"}.isdisjoint(s.SHADOW_OBS_ZERO_ITEM_MARKERS)
    # closed 两边都在：关着的时候，有没有内容都是 closed
    assert "closed" in s.SHADOW_OBS_ZERO_ITEM_MARKERS
    assert "closed" in s.SHADOW_OBS_NONZERO_ITEM_MARKERS


# ---------------------------------------------------------------- ⑤ 不并入 scope


def test_scope_with_only_counter_passes():
    """scope 只带 P3a 报警计数（常态 0）是**合法**的。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    s.assert_scope_carries_no_shadow_detail({"non_live_mode_rejected": 0})
    s.assert_scope_carries_no_shadow_detail({"non_live_mode_rejected": 2})


def test_scope_with_shadow_detail_fires():
    """G-T6: scope 带影子**明细**必须炸 —— 计数可以进，明细不可以进。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    with pytest.raises(AssertionError, match="must not carry shadow detail"):
        s.assert_scope_carries_no_shadow_detail(
            {"non_live_mode_rejected": 0, "shadow_hits": []}
        )
    with pytest.raises(AssertionError, match="must not carry shadow detail"):
        s.assert_scope_carries_no_shadow_detail(["shadow_obs_items", "other"])


def test_scope_with_unexpected_shadow_prefixed_key_fires():
    """没列进禁集、但以 ``shadow`` 开头的新键也要炸 —— 防"换个名字搬进去"。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    with pytest.raises(AssertionError, match="unexpected shadow-prefixed key"):
        s.assert_scope_carries_no_shadow_detail({"shadow_something_new": 1})


def test_scope_forbidden_set_is_nonempty_and_disjoint_from_allowed():
    """禁集非空，且与允许集不相交 —— 否则「允许」与「禁止」自相矛盾。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    assert s.SHADOW_OBS_SCOPE_MUST_NOT_CONTAIN
    assert s.SHADOW_OBS_SCOPE_MUST_NOT_CONTAIN.isdisjoint(
        s.SHADOW_OBS_SCOPE_ALLOWED_KEYS
    )
    assert s.SHADOW_OBS_SCOPE_ALLOWED_KEYS == frozenset({"non_live_mode_rejected"})


# ---------------------------------------------------------------- ⑥ P3b 交叉（测例侧）


def test_p3b_bucket_keys_match_shadow_store():
    """锁面三键必须与真实 P3b ``SHADOW_BUCKET_KEYS`` 一致。

    锁面**刻意不 import** 存储实现（详见模块 docstring「只读交叉的分层」），
    所以漂移在这里捕获 —— 这是三键唯一的交叉点。
    """
    from app.buy_sell_signals import shadow_obs_lock as s
    from app.buy_sell_signals.hits_shadow_store import SHADOW_BUCKET_KEYS

    assert tuple(SHADOW_BUCKET_KEYS) == s.EXPECTED_P3B_BUCKET_KEYS
    assert tuple(SHADOW_BUCKET_KEYS) == s.SHADOW_OBS_P3B_BUCKET_KEYS
    assert len(SHADOW_BUCKET_KEYS) == 3


def test_p3b_content_mode_matches_shadow_store():
    """影子内容的 ``mode`` 是 P3b ``SHADOW_MODE`` 同一个具体值。"""
    from app.buy_sell_signals import shadow_obs_lock as s
    from app.buy_sell_signals import tier_cut_lock as t1
    from app.buy_sell_signals.hits_shadow_store import SHADOW_MODE

    assert s.SHADOW_OBS_CONTENT_MODE == SHADOW_MODE == "shadow"
    # 且必须在 T1 展示值域内 —— 观测口载荷不能出现 T1 不认的 mode
    assert s.SHADOW_OBS_CONTENT_MODE in t1.TIER_DISPLAY_MODES


def test_p3a_reason_shadow_matches_dingtalk():
    """``shadow_not_pushable`` 拒因未变；这是"影子不真推"的代码事实。"""
    from app.buy_sell_signals import shadow_obs_lock as s
    from app.buy_sell_signals.hits_dingtalk import REASON_SHADOW

    assert REASON_SHADOW == s.EXPECTED_P3B_REASON_SHADOW == "shadow_not_pushable"


# ---------------------------------------------------------------- ⑦ 文档 / skeleton


def test_shadow_obs_doc_exists_and_covers_contract_points():
    """G-T2 / G-T8 / G-T10: 文档 PRESENT 且契约要点逐条在正文里。"""
    assert _DOC_PATH.is_file(), f"契约文档缺失: {_DOC_PATH}"
    text = _DOC_PATH.read_text(encoding="utf-8")

    required = [
        # 1 产品语义
        "独立只读影子观测口",
        "R2-3",
        # 2 默认关 / 显式开
        "默认关",
        "显式",
        "SHADOW_OBS_DEFAULT_ENABLED",
        # 3 空态 / 过期
        "closed",
        "empty",
        "stale",
        "ok",
        "空桶不得报",
        # 4 不并入 scope
        "/hits/scope",
        "non_live_mode_rejected",
        # 5 只读 P3b 三键
        "signal_id",
        "definition_version",
        "freq_lock_version",
        "list_shadow_hits",
        "核验钩子",
        # 6 兼容 T1/T2
        "四元组",
        "禁六词",
        "价量仓单",
        "research_watch",
        # 7 门禁大字
        "路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING",
        "影子写入 ≠ 观测口 ≠ 放量",
        "影子 ≠ live 模板",
        "字段契约 ≠ 可放量",
        # 8 Bonferroni 非永久豁免
        "非永久豁免",
        "m = 6",
        # 9 挂点 / 另口令
        "另口令",
        # 10 tip≠prod
        "51d36dc",
        "7b55d024",
        "tip ≠ prod",
        "#260 未部署",
        # 11 脚注
        "仅供研究，不构成投资建议；执行须人工确认",
        "红涨绿跌",
        "18.2bp",
    ]
    missing = [needle for needle in required if needle not in text]
    assert not missing, f"文档缺少契约要点: {missing}"


def test_doc_states_this_is_not_ui_or_volumetric_release():
    """文档必须**明写**「不是什么」——防止被读成"观测口已开通"。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    for needle in ("不是观测 UI 开通", "不是可放量", "不是真推落地", "不是部署 #260"):
        assert needle in text, f"文档缺「不是」声明: {needle}"


def test_skeleton_links_to_shadow_obs_doc_and_keeps_footnote_last():
    """G-T2: skeleton 有一节链过去；脚注仍在**最后一行**（与 T1/T2 同体例）。"""
    text = _SKELETON_PATH.read_text(encoding="utf-8")
    assert "buy-sell-signal-shadow-obs.md" in text
    assert "R2-3" in text
    assert "shadow_obs_lock.py" in text
    assert "shadow_obs_v1" in text

    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines, "skeleton 不该为空"
    assert lines[-1].startswith("*仅供研究"), f"末行不是脚注: {lines[-1]!r}"
    assert "18.2bp" in lines[-1]


def test_skeleton_keeps_t1_and_t2_links_not_replaced():
    """skeleton 里 T1/T2 既有链**未被替换**——本拍只追加，不重写。"""
    text = _SKELETON_PATH.read_text(encoding="utf-8")
    assert "buy-sell-signal-tier-cut.md" in text
    assert "buy-sell-signal-tier-display.md" in text
    assert "tier_cut_v1" in text or "tier_cut_lock.py" in text
    assert "tier_display_v1" in text or "tier_display_lock.py" in text


def test_bonferroni_m_still_6():
    """G-T8: 预算未被本拍改动。"""
    from app.buy_sell_signals import shadow_obs_lock as s
    from app.buy_sell_signals.constants import BONFERRONI_M

    assert BONFERRONI_M == 6
    assert BONFERRONI_M == s.EXPECTED_BONFERRONI_M


# ---------------------------------------------------------------- ⑧ 结构守（本拍不许越界）


def test_lock_module_imports_only_contract_layer():
    """G-T7: 锁面**只依赖契约层**，不依赖存储实现 / 推送实现。

    绝对 import 只允许 ``__future__`` / ``typing`` / ``collections``；
    同包相对 import 只允许锁面与契约常量 —— **不得**出现
    ``hits_shadow_store`` / ``hits_dingtalk`` / ``hits_models`` / ``config``。
    """
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
    }
    assert not (modules & forbidden_abs), (
        f"锁面不该 import 这些: {sorted(modules & forbidden_abs)}"
    )
    assert modules <= {"__future__", "typing", "collections"}, (
        f"意外的绝对 import: {sorted(modules)}"
    )

    relative = _relative_import_modules(_LOCK_PATH)
    forbidden_rel = {
        "hits_shadow_store",
        "hits_dingtalk",
        "hits_models",
        "hits_visibility_impl",
        "config",
        "repository",
        "push",
    }
    assert not (relative & forbidden_rel), (
        "锁面不该 import 存储/推送实现层: "
        f"{sorted(relative & forbidden_rel)} —— 三键交叉放在测例里做"
    )
    allowed_rel = {"tier_cut_lock", "tier_display_lock", "constants", "hits_visibility"}
    assert relative <= allowed_rel, f"意外的相对 import: {sorted(relative - allowed_rel)}"


def test_lock_module_declares_no_side_effect_calls():
    """锁面无写路径；顶层只允许常量与 def（import 时不硬 raise）。"""
    code = _code_only(_LOCK_PATH)
    for needle in (
        "open(",
        "write_text",
        "write_bytes",
        "requests.",
        "httpx.",
    ):
        assert needle not in code, f"锁面出现副作用调用: {needle!r}"

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
        # 顶层不许裸 assert / raise
        assert not isinstance(node, (ast.Assert, ast.Raise))


def test_lock_module_does_not_redefine_t1_t2_versions():
    """锁面只**读** T1/T2 version，不把它们再定义成自己的常量。"""
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
    # 允许 EXPECTED_T1_RULE_VERSION 这类「期望值」锚，但**不许**叫同名
    assert "TIER_CUT_RULE_VERSION" not in assigned, "锁面不许重新定义 T1 version"
    assert "TIER_DISPLAY_LOCK_VERSION" not in assigned, "锁面不许重新定义 T2 version"
    assert "DIRECTION_LABELS" not in assigned, "锁面不许重新定义方向标签"
    assert "BONFERRONI_M" not in assigned, "锁面不许重新定义预算"


def test_lock_module_has_no_triggering_or_complete_switch():
    """G-T9: 锁面里**不许**给 TRIGGERING / COMPLETE 赋值。

    用 ast 查**赋值目标**，不是查源码文本 —— docstring 里引用
    「两处 ``*_TRIGGERING_ENABLED = False`` 同一个取向」是在**说明**取向，
    不是在翻开关。查文本会把说明本身判成违规，进而逼人删掉说明。
    """
    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    banned = {"TRIGGERING_ENABLED", "EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE"}
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


def test_lock_all_exports_are_real_symbols():
    """``__all__`` 里每个名字都得真存在 —— 防止导出面写着却没有实现。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    assert s.__all__
    missing = [name for name in s.__all__ if not hasattr(s, name)]
    assert not missing, f"__all__ 声明了不存在的符号: {missing}"
    assert len(set(s.__all__)) == len(s.__all__), "__all__ 有重复项"


def test_lock_all_is_sorted_for_readability():
    """``__all__`` 排序（ruff RUF022 同口径）。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    assert list(s.__all__) == sorted(s.__all__)


# ---------------------------------------------------------------- ⑨ 一把梭


def test_shadow_obs_lock_invariants_all_green():
    """全部不变量一次过 —— 这是 CI 里真正跑的那条门。"""
    from app.buy_sell_signals import shadow_obs_lock as s

    s.assert_shadow_obs_lock_invariants()

    # 空态两端各抽一个合法态
    s.assert_state_marker_honest(marker="empty", item_count=0)
    s.assert_state_marker_honest(marker="ok", item_count=1)
    # scope 常态
    s.assert_scope_carries_no_shadow_detail({"non_live_mode_rejected": 0})
