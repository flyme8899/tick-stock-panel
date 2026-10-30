"""T2 展示层字段契约锁面 + 契约文档（批次 20261030gt2 · P-bss-t2-tier-display · #-A-#12）。

## 这组测在测什么

T2 交付的是**字段契约**，所以坏法也只有两种，且都不报错：

1. **契约漂了** —— 四元组少一项、`no_tier` 与 `tier` 自相矛盾、六词禁止集里少一个词、
   价量仓单被允许进载荷。这类坏法**不会报错**，只会让页面某天开始显示一句
   「建议买入」，而没人知道它是从哪一版契约漏进来的。所以要对着**写死的期望值**断言。
2. **本拍越界** —— 文档写着"不接线 UI"，模块里却 import 了 `polars` / 推送；
   或者顺手把 T1 的阈值改了。这类坏法**没有运行时表现**，只能读源码/读 import 表钉。

## 为什么单独测"短词不误报"

六词长短语做子串匹配是安全的；短词（买/卖/加仓…）在本项目里**不能**做全文扫描 ——
「买卖信号」就是本仓的名字。这一条最容易在实现时被"顺手统一"成一种匹配方式，
所以专门写一条测例把它钉住：把「买卖信号」喂给六词检查器，必须**不炸**。
一个"看起来全覆盖、实际天天误报"的守卫，很快会被加 `# noqa` 关掉。

## 不测什么

不测算档结果 —— 本拍只有契约，没有算子。算档 / UI / 真推 / schema 进 hits 全部另口令。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

# ruff: noqa: N812, SIM300
# N812: `constants as C` / `tier_cut_lock as t1` 与既有锁面测例同风格。
# SIM300: 本文件满是 `assert CONST == 字面量`, ruff 把全大写标识符当"常量应在右",
#         在 assert 上系统性误报 —— 把字面量翻到左边才是真的不可读。

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

from app.buy_sell_signals import constants as C
from app.buy_sell_signals import tier_cut_lock as t1
from app.buy_sell_signals import tier_display_lock as d

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCK_PATH = (
    Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals" / "tier_display_lock.py"
)
_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-tier-display.md"
_SKELETON_PATH = _REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md"
_T1_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-tier-cut.md"

_SIX_PHRASES = (
    "可以买入",
    "建议买入",
    "强烈买入",
    "可以卖出",
    "建议卖出",
    "强烈卖出",
)


def _first_existing(*candidates: Path) -> Path:
    for path in candidates:
        if path.is_file():
            return path
    raise AssertionError(f"doc missing, tried: {[str(p) for p in candidates]}")


def _code_only(path: Path) -> str:
    """剥掉字符串与注释后的源码（docstring 里写满"不算行情"，直接搜会恒命中）。"""
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


def _imported_modules(path: Path) -> set[str]:
    """源码里所有**绝对** import 的顶层模块名（ast 解析，不受 docstring 干扰）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


# ================================================================ ① 常量 PRESENT + version


def test_tier_display_lock_constants_present_and_version_nonempty():
    """G-T3: 锁面常量齐备；version 非空且写死 `tier_display_v1`。"""
    assert d.TIER_DISPLAY_LOCK_VERSION
    assert d.TIER_DISPLAY_LOCK_VERSION == "tier_display_v1"

    # 四元组四项逐项在
    for key in ("track", "asof_grain", "mode", "tier"):
        assert key in d.TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS, key
    assert d.TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS == (
        "track",
        "asof_grain",
        "mode",
        "tier",
    )

    assert d.TIER_DISPLAY_NO_TIER_KEY == "no_tier"
    assert d.TIER_DISPLAY_NO_TIER_REASON_KEY == "no_tier_reason"

    # 六词一个都不能少
    for phrase in _SIX_PHRASES:
        assert phrase in d.TIER_DISPLAY_FORBIDDEN_ACTION_PHRASES, phrase
    assert d.TIER_DISPLAY_FORBIDDEN_ACTION_PHRASES == frozenset(_SIX_PHRASES)

    # 价量仓单（中英）
    for field in ("price", "quantity", "position", "order"):
        assert field in d.TIER_DISPLAY_FORBIDDEN_PAYLOAD_FIELDS, field
    for field in ("价格", "数量", "仓位", "订单"):
        assert field in d.TIER_DISPLAY_FORBIDDEN_PAYLOAD_FIELDS, field


def test_tier_display_lock_exports_everything_in_dunder_all():
    """`__all__` 与实际符号对齐 —— 声明了却没定义，`from x import *` 会静默少东西。"""
    for name in d.__all__:
        assert hasattr(d, name), f"__all__ 声明了 {name!r}, 模块里没有"
    for name in (
        "TIER_DISPLAY_LOCK_VERSION",
        "TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS",
        "TIER_DISPLAY_FORBIDDEN_ACTION_PHRASES",
        "TIER_DISPLAY_FORBIDDEN_PAYLOAD_FIELDS",
        "TIER_DISPLAY_NO_TIER_KEY",
        "assert_t1_cross_lock",
        "assert_payload_quad_or_no_tier",
        "assert_tier_display_lock_invariants",
    ):
        assert name in d.__all__, f"{name!r} 未在 __all__ 中"


# ================================================================ ② DIRECTION 仍三值


def test_direction_labels_still_three_values():
    """G-T5: `DIRECTION_LABELS` 仍为原三值；档名与方向键不相交；保留 research_watch。"""
    assert C.DIRECTION_LABELS == frozenset(
        {"research_buy", "research_sell", "research_watch"}
    )
    d.assert_direction_labels_untouched()

    assert "research_watch" in C.DIRECTION_LABELS
    assert t1.TIER_LABELS.isdisjoint(C.DIRECTION_LABELS)


def test_tier_labels_are_neutral_no_action_directives():
    """G-T5: 档名与文案中性 —— 六词与短动作指令词都不得出现在档名里。"""
    for label in t1.TIER_LABELS:
        d.assert_tier_label_is_neutral(label)
    for zh in t1.TIER_LABEL_ZH.values():
        d.assert_tier_label_is_neutral(zh)


def test_short_action_words_are_label_only_and_do_not_misfire_on_project_name():
    """G-T5: 短词只查档名 —— 「买卖信号」不该被六词检查器误伤。

    这条钉的是一个**设计判断**，不是语法细节: 若把短词（买/卖/加仓…）也做成全文子串
    扫描，「买卖信号」这个本项目名会立刻炸。所以短词只在 `assert_tier_label_is_neutral`
    里对**封闭短串**（档名）检查。测例把这条边界变成可验事实。
    """
    # 项目名不该被六词检查器判违规
    d.assert_no_forbidden_action_phrases("买卖信号")
    d.assert_no_forbidden_action_phrases("研究档 · 未放量")

    # 但真的六词出现时必须炸
    with pytest.raises(AssertionError, match="forbidden action phrase"):
        d.assert_no_forbidden_action_phrases("今日建议买入 000001")
    with pytest.raises(AssertionError, match="forbidden action phrase"):
        d.assert_no_forbidden_action_phrases("强烈卖出")

    # 档名里出现短动作词 → 炸
    with pytest.raises(AssertionError, match="action word"):
        d.assert_tier_label_is_neutral("买入")
    with pytest.raises(AssertionError, match="action word"):
        d.assert_tier_label_is_neutral("加仓档")
    # 中性档名 → 不炸
    d.assert_tier_label_is_neutral("弱")
    d.assert_tier_label_is_neutral("mid")


# ================================================================ ③ 四元组 / 无档 / T1 交叉


def test_payload_quad_or_no_tier_and_t1_cross():
    """G-T4/G-T6: 四元组正路两种 + 无档显式 + 禁止 weak 顶缺失 + T1 交叉。"""
    d.assert_t1_cross_lock()
    assert t1.TIER_CUT_RULE_VERSION == "tier_cut_v1"
    assert d.EXPECTED_T1_RULE_VERSION == "tier_cut_v1"

    # 有档
    d.assert_payload_quad_or_no_tier(
        track="daily_fundamental",
        asof_grain="day",
        mode="shadow",
        tier="mid",
        no_tier=False,
    )
    # 显式无档
    d.assert_payload_quad_or_no_tier(
        track="minute_event",
        asof_grain="minute",
        mode="observation",
        tier=None,
        no_tier=True,
    )
    # 三个 mode 都通
    for mode in ("shadow", "observation", "live"):
        d.assert_payload_quad_or_no_tier(
            track="daily_fundamental", asof_grain="day", mode=mode, tier="strong", no_tier=False
        )

    d.assert_tier_display_lock_invariants()


def test_no_tier_must_not_carry_weak_or_any_tier():
    """G-T6: `no_tier=True` 时带任何档（尤其 `weak`）都必须炸 —— 禁止用 weak 顶缺失。"""
    for bad_tier in ("weak", "mid", "strong"):
        with pytest.raises(AssertionError, match="must not carry a tier"):
            d.assert_payload_quad_or_no_tier(
                track="daily_fundamental",
                asof_grain="day",
                mode="live",
                tier=bad_tier,
                no_tier=True,
            )


def test_no_tier_false_requires_a_real_tier():
    """G-T6: `no_tier=False` 时 `tier` 必须在 `TIER_LABELS` 里；缺失/拼错都炸。"""
    for bad in (None, "", "midium", "extreme"):
        with pytest.raises(AssertionError, match="requires tier in TIER_LABELS"):
            d.assert_payload_quad_or_no_tier(
                track="daily_fundamental",
                asof_grain="day",
                mode="shadow",
                tier=bad,
                no_tier=False,
            )


@pytest.mark.parametrize(
    ("field", "bad_value", "pattern"),
    [
        ("track", "weekly_fundamental", "track out of domain"),
        ("track", None, "track out of domain"),
        ("asof_grain", "week", "asof_grain out of domain"),
        ("asof_grain", None, "asof_grain out of domain"),
        ("mode", "production", "mode out of domain"),
        ("mode", None, "mode out of domain"),
    ],
)
def test_quad_domain_violations_raise(field, bad_value, pattern):
    """G-T6: 三个取值域越界各自报错 —— 不是笼统一句 "invalid payload"。"""
    kwargs = {
        "track": "daily_fundamental",
        "asof_grain": "day",
        "mode": "shadow",
        "tier": "weak",
        "no_tier": False,
    }
    kwargs[field] = bad_value
    with pytest.raises(AssertionError, match=pattern):
        d.assert_payload_quad_or_no_tier(**kwargs)


def test_payload_keys_derive_from_t1_not_copied():
    """G-T4: 四元组前三项**派生**自 T1，不是抄字面量。

    抄一份的话，T1 哪天改了键名，T2 会静默地继续用旧名；派生 + 交叉断言会在那一刻红。
    """
    assert d.TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS[:-1] == t1.TIER_DISPLAY_REQUIRED_KEYS
    assert d.TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS[-1] == "tier"
    # 取值域直接引用 T1 集合（同一对象，不是复制成员）
    assert d.TIER_DISPLAY_PAYLOAD_REQUIRED_KEYS[0] in t1.TIER_DISPLAY_REQUIRED_KEYS


# ================================================================ ④ 价量仓单禁止 + 助手真会炸


def test_no_forbidden_payload_fields_accepts_mapping_keys_and_list():
    """G-T7: 载荷键名检查支持 dict / keys() / 字段名列表三种入参。"""
    clean = {
        "track": "daily_fundamental",
        "asof_grain": "day",
        "mode": "shadow",
        "tier": "mid",
        "no_tier": False,
    }
    d.assert_no_forbidden_payload_fields(clean)  # dict
    d.assert_no_forbidden_payload_fields(clean.keys())  # keys view
    d.assert_no_forbidden_payload_fields(list(clean))  # list

    # 中英各一个都要炸
    with pytest.raises(AssertionError, match="forbidden payload fields"):
        d.assert_no_forbidden_payload_fields({**clean, "price": 10.5})
    with pytest.raises(AssertionError, match="forbidden payload fields"):
        d.assert_no_forbidden_payload_fields([*clean, "订单"])

    # 值里出现 "price" 是文案自由，不该炸 —— 只查键名
    d.assert_no_forbidden_payload_fields({**clean, "note": "price is irrelevant here"})

    # 传单个字符串是调用方写错了
    with pytest.raises(TypeError, match="not a single string"):
        d.assert_no_forbidden_payload_fields("price")


def test_no_forbidden_action_phrases_rejects_non_str():
    """类型错与内容错分开报 —— 一个 TypeError，一个 AssertionError。"""
    with pytest.raises(TypeError, match="text must be str"):
        d.assert_no_forbidden_action_phrases(123)  # type: ignore[arg-type]


def test_cross_lock_detects_t1_drift(monkeypatch):
    """G-T4: 交叉断言**真会炸** —— 把 T1 的 version 临时改掉，`assert_t1_cross_lock` 必须红。

    一条永不触发的守卫等于没写。这里用 monkeypatch 模拟"T1 被别人改了"，
    验的是本锁能不能发现，而不是真去改 T1。
    """
    monkeypatch.setattr(t1, "TIER_CUT_RULE_VERSION", "tier_cut_v2")
    with pytest.raises(AssertionError, match="T1 version drift"):
        d.assert_t1_cross_lock()

    monkeypatch.undo()
    monkeypatch.setattr(t1, "NO_MEASURE_NO_TIER", False)
    with pytest.raises(AssertionError, match="NO_MEASURE_NO_TIER must stay True"):
        d.assert_t1_cross_lock()

    monkeypatch.undo()
    monkeypatch.setattr(t1, "TIER_DISPLAY_REQUIRED_KEYS", ("track", "asof_grain"))
    with pytest.raises(AssertionError, match="T1 display keys mutated"):
        d.assert_t1_cross_lock()


# ================================================================ ⑤ 文档 + 挂链 + Bonferroni


def test_tier_display_doc_exists_and_bonferroni_m_still_6():
    """G-T8: 契约文档 PRESENT；`BONFERRONI_M` 仍 6、scheme 未 bump。"""
    doc = _first_existing(_DOC_PATH, Path("docs/buy-sell-signal-tier-display.md"))
    assert doc.is_file()

    assert C.BONFERRONI_M == 6
    assert C.BONFERRONI_SCHEME_ID == "independent_family_v1"
    assert d.EXPECTED_BONFERRONI_M == 6
    assert d.EXPECTED_BONFERRONI_SCHEME_ID == "independent_family_v1"


def test_tier_display_doc_covers_every_required_clause():
    """G-T2/G-T10: 契约文档 11 项要点逐条在文里，缺一条就红。

    每条挑的是该要点**不可省的那句表述** —— 少了「非永久豁免」，Bonferroni 那节
    就会被读成"永久不用管"；少了「禁止用 `weak` 顶」，无量＝无档就成了空话。
    """
    doc = _first_existing(_DOC_PATH, Path("docs/buy-sell-signal-tier-display.md"))
    text = doc.read_text(encoding="utf-8")

    required = {
        "锁面版本": "tier_display_v1",
        "四元组 track": "`track`",
        "四元组 asof_grain": "`asof_grain`",
        "四元组 mode": "`mode`",
        "四元组 tier": "`tier`",
        "无量＝无档": "无量＝无档",
        "禁 weak 顶缺失": "禁止用 `weak` 顶缺失",
        "六词之一": "建议买入",
        "六词之一(卖)": "强烈卖出",
        "短动作词": "加仓",
        "价量仓单": "价格",
        "门禁旁注": "研究档 · 未放量",
        "影子≠live": "影子 ≠ live 模板",
        "只读接线 T1": "只读消费",
        "T1≺T2": "T1 ≺ T2",
        "Bonferroni 不占": "不占 `BONFERRONI_M = 6`",
        "非永久豁免": "非永久豁免",
        "另口令": "另口令",
        "合规脚注": "仅供研究，不构成投资建议；执行须人工确认",
        "红涨绿跌": "红涨绿跌",
        "成本": "18.2bp",
    }
    missing = [label for label, needle in required.items() if needle not in text]
    assert not missing, f"文档缺要点: {missing}"

    # 越界表述：不该出现"已经可放量/已上线"这类结论
    for banned in ("已放量", "可以放量", "已上线真推", "已进 hits schema"):
        assert banned not in text, f"文档出现越界表述: {banned!r}"


def test_skeleton_and_t1_doc_link_to_display_doc():
    """G-T2: 两处挂链都在；且 skeleton 仍是仅追加（脚注仍在末行）。"""
    skeleton = _first_existing(_SKELETON_PATH, Path("docs/buy-sell-signals-skeleton.md"))
    sk_text = skeleton.read_text(encoding="utf-8")

    assert "buy-sell-signal-tier-display.md" in sk_text
    assert "T2 展示层字段契约（批次 20261030gt2）" in sk_text
    # 新节必须否定 schema / 真推 / 算档 —— 否则读者会顺着"字段契约"读成"已经接线了"
    assert "不是** schema 进 hits" in sk_text
    assert "不是**算档 / UI 真接线" in sk_text

    last_line = sk_text.rstrip("\n").splitlines()[-1]
    assert last_line == "*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*"

    # T1 文档 §9 的追加链
    t1_text = _first_existing(_T1_DOC_PATH, Path("docs/buy-sell-signal-tier-cut.md")).read_text(
        encoding="utf-8"
    )
    assert "buy-sell-signal-tier-display.md" in t1_text
    assert "T2 字段契约已开（批次 20261030gt2）" in t1_text
    # T1 规则正文关键句仍在（未被本拍改写）
    assert "T1 未锁，T2 无从接线" in t1_text
    assert 'TIER_CUT_RULE_VERSION: Final = "tier_cut_v1"' not in t1_text  # 文档里不是代码块写法
    assert "tier_cut_v1" in t1_text


# ================================================================ ⑥ 结构守：锁面不许有 I/O


def test_lock_module_has_no_io_imports():
    """G-T7: 锁面**不算行情、不写 hits、不接线 UI / 钉钉** —— 用 import 表钉。

    读的是 ast 解析出的**绝对** import 模块名，不是源码字符串：docstring 里写着
    "不读行情""不碰 HitStore"，直接搜必然命中。
    """
    modules = _imported_modules(_LOCK_PATH)
    forbidden = {"polars", "pandas", "numpy", "requests", "httpx", "duckdb", "sqlite3", "fastapi"}
    hit = modules & forbidden
    assert not hit, f"锁面不该 import 这些模块: {sorted(hit)}"
    # 只允许标准库这几个（同包相对 import 不计入）
    assert modules <= {"__future__", "typing", "collections"}, f"意外的 import: {sorted(modules)}"


def test_lock_module_declares_no_side_effect_calls():
    """锁面不许有 open / write / requests 之类调用；顶层只允许常量与 def。

    「import 时不硬 raise」也由这一条守着：模块顶层不该有裸的 assert / raise 语句。
    """
    code = _code_only(_LOCK_PATH)
    for needle in ("open(", "write_text", "write_bytes", "requests.", "httpx.", "session."):
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
        ), f"锁面顶层出现意外语句: {type(node).__name__}"


def test_lock_module_import_does_not_raise():
    """G-T3: 常量模块 import 时不硬 raise —— 断言由测例调用。"""
    import importlib

    module = importlib.import_module("app.buy_sell_signals.tier_display_lock")
    assert module.TIER_DISPLAY_LOCK_VERSION == "tier_display_v1"


def test_lock_note_states_non_deliverables():
    """`TIER_DISPLAY_LOCK_NOTE` 须含「不算行情 / 不写hits / 不接线」与「字段契约 != 可放量」。"""
    note = d.TIER_DISPLAY_LOCK_NOTE
    assert "tier_display_v1" in note
    assert "tier_cut_v1" in note
    assert "字段契约 != 可放量" in note
    for needle in ("不算行情", "不写hits", "不接线UI/钉钉"):
        assert needle in note, needle


# ================================================================ ⑦ 一把梭


def test_all_invariants_pass_on_tip():
    """G-T3–G-T8: 一把梭全部写死期望值 —— 任一漂移即红。"""
    d.assert_tier_display_lock_invariants()
    assert d.TIER_DISPLAY_LOCK_VERSION == "tier_display_v1"
    assert C.BONFERRONI_M == 6
    assert C.DIRECTION_LABELS == frozenset(
        {"research_buy", "research_sell", "research_watch"}
    )
    # T1 未被动过
    assert t1.TIER_CUT_RULE_VERSION == "tier_cut_v1"
    assert t1.TIER_LABELS == frozenset({"weak", "mid", "strong"})
    assert t1.NO_MEASURE_NO_TIER is True
    assert t1.TIER_BEFORE_T2 is True
