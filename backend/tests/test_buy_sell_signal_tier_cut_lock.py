"""T1 切档规则锁面 + 规则文档（批次 20261030gt1 · P-bss-t1-tier-cut · #-A-#12）。

## 这组测在测什么

T1 只有两类东西要守，两类坏起来的样子完全不同：

1. **规则本身漂了** —— 阈值被改成 0.5、档名被改成动作指令、两族共用一个 measure id、
   无量时被填了个 `weak` 顶上。这类坏法**不会报错**，只会让页面上的"强"慢慢不再是
   当初那个"强"。所以要对着**写死的期望值**断言，不能自指（`assert X == X` 恒绿）。
2. **本拍越界** —— 规则文档写着"不接线 UI"，代码里却 import 了 `polars` / 推送模块。
   这类坏法**没有运行时表现**，只能读源码钉。而直接 `in src` 搜会恒命中 ——
   docstring 里写满了"不算行情""不写 hits"。所以先剥掉字符串与注释再搜。

## 为什么每条断言都分开

一条大红不如一条指名道姓的红：「DIRECTION_LABELS mutated」比「invariants failed」
有用得多。所以主断言助手 `assert_tier_cut_lock_invariants()` 之外，每个子项仍然
单独成测。

## 不测什么

不测算档结果 —— 本拍只有规则，没有算子。测例里那几行区间判定是**验证区间语义**
（⅓ 归弱、⅔ 归中、0 无档），不是生产实现；生产侧的算档接线属 T2 / 观测口另口令。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

# ruff: noqa: N812, SIM300
# N812: `constants as C` 与既有锁面测例(test_buy_sell_signal_naming_budget_a1)同风格;
# SIM300: 本文件满是 `assert CONST == 字面量`, ruff 把全大写标识符当"常量应在右",
#         在 assert 上系统性误报 —— 把字面量翻到左边才是真的不可读。
from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

from app.buy_sell_signals import constants as C
from app.buy_sell_signals import tier_cut_lock as t

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCK_PATH = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals" / "tier_cut_lock.py"
_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-tier-cut.md"
_SKELETON_PATH = _REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md"

_SIX_WORD_DIRECTIVES = ("买入", "卖出", "加仓", "减仓", "开多", "开空")


def _code_only(path: Path) -> str:
    """剥掉字符串与注释后的源码。

    锁面 docstring 里写满"不算行情""不写 hits""不接线 UI" —— 直接 `in src` 搜
    `hits` 会恒命中，那种"绿"什么也证明不了。
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


def _imported_modules(path: Path) -> set[str]:
    """源码里所有 import 的顶层模块名（ast 解析，不受 docstring 干扰）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


# ================================================================ ① 常量 PRESENT + version


def test_tier_cut_lock_constants_present_and_version_nonempty():
    """G-T3/G-T6: 锁面常量齐备，version 非空且写死 `tier_cut_v1`。"""
    assert t.TIER_CUT_RULE_VERSION
    assert t.TIER_CUT_RULE_VERSION == "tier_cut_v1"

    assert t.TIER_LABELS == frozenset({"weak", "mid", "strong"})
    assert t.TIER_LABEL_ZH["weak"] == "弱"
    assert t.TIER_LABEL_ZH["mid"] == "中"
    assert t.TIER_LABEL_ZH["strong"] == "强"

    assert t.TIER_CUT_WEAK_HI == 1.0 / 3.0
    assert t.TIER_CUT_MID_HI == 2.0 / 3.0

    assert t.NO_MEASURE_NO_TIER is True
    assert t.TIER_BEFORE_T2 is True
    assert t.TIER_CUT_BONFERRONI_DISPLAY_EDGE is True
    assert t.TIER_CUT_DOES_NOT_CONSUME_BONFERRONI_M is True
    assert "track" in t.TIER_DISPLAY_REQUIRED_KEYS


def test_tier_cut_lock_module_exports_everything_in_dunder_all():
    """`__all__` 与实际常量对齐 —— 声明了却没定义，`from x import *` 会静默少东西。"""
    for name in t.__all__:
        assert hasattr(t, name), f"__all__ 声明了 {name!r}, 模块里没有"
    for name in (
        "TIER_CUT_RULE_VERSION",
        "TIER_LABELS",
        "DAILY_TIER_MEASURE_ID",
        "EVT_TIER_MEASURE_ID",
        "TIER_CUT_WEAK_HI",
        "TIER_CUT_MID_HI",
        "NO_MEASURE_NO_TIER",
        "TIER_DISPLAY_REQUIRED_KEYS",
        "TIER_BEFORE_T2",
    ):
        assert name in t.__all__, f"{name!r} 未在 __all__ 中"


# ================================================================ ② DIRECTION 仍三值


def test_direction_labels_still_three_values():
    """G-T5: `DIRECTION_LABELS` 仍为原三值；档名与方向键不相交。"""
    assert C.DIRECTION_LABELS == frozenset(
        {"research_buy", "research_sell", "research_watch"}
    )
    t.assert_direction_labels_untouched()

    # 档名与方向键不相交：禁止把方向键塞进档
    assert t.TIER_LABELS.isdisjoint(C.DIRECTION_LABELS)


def test_research_watch_is_kept_and_no_six_word_directives_in_labels():
    """G-T5: 保留 `research_watch`；档名与文案里不出现六词动作指令式。"""
    assert "research_watch" in C.DIRECTION_LABELS
    for label in t.TIER_LABELS:
        for word in _SIX_WORD_DIRECTIVES:
            assert word not in label, f"档名 {label!r} 含动作指令词 {word!r}"
    for zh in t.TIER_LABEL_ZH.values():
        for word in _SIX_WORD_DIRECTIVES:
            assert word not in zh, f"档文案 {zh!r} 含动作指令词 {word!r}"


# ================================================================ ③ 无量＝无档 + 两 id 不同


def test_no_measure_no_tier_and_measure_ids_distinct():
    """G-T4/G-T6: 两族 measure id 不同；无量时档必须为 `None`，且助手真会炸。"""
    t.assert_measure_ids_distinct()
    assert t.DAILY_TIER_MEASURE_ID == "q_rank_within_ind"
    assert t.EVT_TIER_MEASURE_ID
    assert t.DAILY_TIER_MEASURE_ID != t.EVT_TIER_MEASURE_ID

    # 正路：无量 + 无档
    t.assert_no_measure_no_tier(has_measure=False, tier=None)
    # 有量时助手不拦（它只管"无量→无档"一个方向）
    t.assert_no_measure_no_tier(has_measure=True, tier="weak")

    with pytest.raises(AssertionError, match="no measure must yield no tier"):
        t.assert_no_measure_no_tier(has_measure=False, tier="weak")

    # 拼错的档名，有量时也要被抓到
    with pytest.raises(AssertionError, match="unknown tier label"):
        t.assert_no_measure_no_tier(has_measure=True, tier="midium")


def test_measure_ids_carry_their_defined_meaning():
    """两个 id 串不是随便起的：读法写死，改串等于改口径。"""
    # 日频：行业内分位 —— 串里必须出现 within_ind，否则读者不知道分母是"行业"
    assert "within_ind" in t.DAILY_TIER_MEASURE_ID
    # 事件：超额倍数 + 截面分位 —— 两个语义片段都必须在串里
    assert "excess" in t.EVT_TIER_MEASURE_ID
    assert "cross_section" in t.EVT_TIER_MEASURE_ID


# ================================================================ ④ 阈值语义（含 ⅓/⅔ 边界）


def _band(measure: float) -> str | None:
    """测例内的区间判定 —— 只验区间语义，不是生产算档实现。

    与文档 §5 / 锁面注释逐字对应：弱 (0,⅓] · 中 (⅓,⅔] · 强 (⅔,1]。
    """
    if 0.0 < measure <= t.TIER_CUT_WEAK_HI:
        return "weak"
    if t.TIER_CUT_WEAK_HI < measure <= t.TIER_CUT_MID_HI:
        return "mid"
    if t.TIER_CUT_MID_HI < measure <= 1.0:
        return "strong"
    return None


@pytest.mark.parametrize(
    ("measure", "expected"),
    [
        (1.0 / 3.0, "weak"),  # 恰落 ⅓：右闭 → 弱（N3 已拍板）
        (2.0 / 3.0, "mid"),  # 恰落 ⅔：右闭 → 中（N3 已拍板）
        (0.1, "weak"),
        (0.5, "mid"),
        (0.9, "strong"),
        (1.0, "strong"),  # 上界闭
        (0.0, None),  # 有量但无档：弱档下界是开的
    ],
)
def test_threshold_bands_are_right_closed_and_exclude_zero(measure, expected):
    """G-T6: 阈值边界归属写死；`measure == 0` 有量但无档（不是无量、更不是弱档）。"""
    assert _band(measure) == expected


def test_thresholds_ordered_and_not_silently_changed():
    """改阈值必须 bump 版本 —— 这里对着写死值断言，静默改会红。"""
    t.assert_thresholds_ordered()
    assert abs(t.TIER_CUT_WEAK_HI - 1.0 / 3.0) < 1e-12
    assert abs(t.TIER_CUT_MID_HI - 2.0 / 3.0) < 1e-12
    # 两段等宽，否则"中"会被挤成一条窄带而没人注意
    assert abs(t.TIER_CUT_WEAK_HI - (t.TIER_CUT_MID_HI - t.TIER_CUT_WEAK_HI)) < 1e-12


# ================================================================ ⑤ 展示三项必填


def test_display_required_keys_and_value_domains():
    """G-T2: 展示三项（轨 + asof 粒度 + mode）必填，取值域封闭。"""
    assert t.TIER_DISPLAY_REQUIRED_KEYS == ("track", "asof_grain", "mode")
    t.assert_display_keys_complete(t.TIER_DISPLAY_REQUIRED_KEYS)
    t.assert_display_keys_complete({"track", "asof_grain", "mode", "extra"})

    assert t.TIER_DISPLAY_TRACKS == frozenset({"daily_fundamental", "minute_event"})
    assert t.TIER_DISPLAY_GRAINS == frozenset({"day", "minute"})
    assert t.TIER_DISPLAY_MODES == frozenset({"shadow", "observation", "live"})

    # 轨取值域必须与既有 FREQ_LOCK_CADENCES 同集合（本拍零碰该集合）
    assert t.TIER_DISPLAY_TRACKS == C.FREQ_LOCK_CADENCES

    # 缺一项 → 真炸；缺两项也要报全，不是只报第一个
    with pytest.raises(AssertionError, match="missing required keys"):
        t.assert_display_keys_complete({"track", "mode"})
    with pytest.raises(AssertionError, match=r"asof_grain.*mode|mode.*asof_grain"):
        t.assert_display_keys_complete({"track"})

    # 传单个字符串是调用方写错了，不是"三项都缺"
    with pytest.raises(TypeError, match="not a single string"):
        t.assert_display_keys_complete("track")


# ================================================================ ⑥ 文档 + Bonferroni


def _first_existing(*candidates: Path) -> Path:
    for path in candidates:
        if path.is_file():
            return path
    raise AssertionError(f"tier-cut doc missing, tried: {[str(p) for p in candidates]}")


def test_tier_cut_doc_exists_and_bonferroni_m_still_6():
    """G-T8: 规则文档 PRESENT；`BONFERRONI_M` 仍为 6、scheme 未 bump。"""
    doc = _first_existing(_DOC_PATH, Path("docs/buy-sell-signal-tier-cut.md"))
    assert doc.is_file()

    assert C.BONFERRONI_M == 6
    assert C.BONFERRONI_SCHEME_ID == "independent_family_v1"
    assert t.EXPECTED_BONFERRONI_M == 6


def test_tier_cut_doc_covers_every_required_clause():
    """G-T2/G-T10: 文档 11 项要点逐条在文里，缺一条就红。

    这不是"搜关键词就算过"—— 每条挑的是**该要点不可省的那句表述**：
    少了「不占」或「非永久豁免」，Bonferroni 那节就会被读成"永久不用管"。
    """
    doc = _first_existing(_DOC_PATH, Path("docs/buy-sell-signal-tier-cut.md"))
    text = doc.read_text(encoding="utf-8")

    required = {
        "规则版本": "tier_cut_v1",
        "无量＝无档": "无量＝无档",
        "日频量": "q_rank_within_ind",
        "事件量": "evt_excess_vs_freq_cross_section_q",
        "弱档阈值": "⅓",
        "中档阈值": "⅔",
        "展示三项": "track",
        "Bonferroni 不占": "不占",
        "非永久豁免": "非永久豁免",
        "T1≺T2": "T1 ≺ T2",
        "合规脚注": "仅供研究，不构成投资建议；执行须人工确认",
        "红涨绿跌": "红涨绿跌",
        "成本": "18.2bp",
    }
    missing = [label for label, needle in required.items() if needle not in text]
    assert not missing, f"文档缺要点: {missing}"

    # 禁用话术：文档里不该出现"可放量""已放量"这类结论性表述（除否定句）
    for banned in ("已放量", "可以放量", "可放量：是"):
        assert banned not in text, f"文档出现越界表述: {banned!r}"


def test_skeleton_links_to_tier_cut_doc_and_stays_append_only():
    """G-T2: skeleton 里有链；且只追加了一节（脚注仍在文末，未被挪动）。"""
    skeleton = _first_existing(
        _SKELETON_PATH, Path("docs/buy-sell-signals-skeleton.md")
    )
    text = skeleton.read_text(encoding="utf-8")

    assert "buy-sell-signal-tier-cut.md" in text
    assert "T1 切档规则（批次 20261030gt1）" in text
    # 新节必须明确否定 T2 / schema 进 hits，否则读者会顺着"切档"读成"已经接线了"
    assert "不是** T2 展示字段接线" in text
    assert "不是** schema 进 hits" in text

    # 脚注仍是全文最后一行 —— 追加没有把原有尾部挤到中间
    last_line = text.rstrip("\n").splitlines()[-1]
    assert last_line == "*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*"


# ================================================================ ⑦ 结构守：锁面不许有 I/O


def test_lock_module_has_no_io_imports():
    """G-T7: 锁面**不算行情、不写 hits、不接线 UI / 钉钉** —— 用 import 表钉。

    读的是 ast 解析出的模块名，不是源码字符串：docstring 里写着"不读行情""不碰
    HitStore"，直接搜必然命中。
    """
    modules = _imported_modules(_LOCK_PATH)
    forbidden = {
        "polars",
        "pandas",
        "numpy",
        "requests",
        "httpx",
        "duckdb",
        "sqlite3",
        "fastapi",
        "app",
    }
    hit = modules & forbidden
    assert not hit, f"锁面不该 import 这些模块: {sorted(hit)}"
    # 只允许标准库 typing 与同包 constants
    assert modules <= {"__future__", "typing"}, f"意外的 import: {sorted(modules)}"


def test_lock_module_declares_no_side_effect_calls():
    """锁面不许有 open / write / requests / session 之类调用 —— 常量模块应无副作用。

    import 时**不硬 raise** 也在这一条里：模块顶层只该有赋值与 def。
    """
    code = _code_only(_LOCK_PATH)
    for needle in ("open(", "write_text", "write_bytes", "requests.", "httpx.", "session."):
        assert needle not in code, f"锁面出现副作用调用: {needle!r}"

    # 顶层语句只允许：import / 赋值 / 函数定义 / docstring（已剥）
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
    """G-T3: 常量模块 import 时不硬 raise —— 断言由测例调用。

    若在模块级 raise，一个常量写错会让整个 `app.buy_sell_signals` 包导不进来，
    报错还指向 import 而不是那一行。
    """
    import importlib

    module = importlib.import_module("app.buy_sell_signals.tier_cut_lock")
    assert module.TIER_CUT_RULE_VERSION == "tier_cut_v1"


# ================================================================ ⑧ 一把梭


def test_all_invariants_pass_on_tip():
    """G-T3–G-T6: 一把梭全部写死期望值 —— 任一漂移即红。"""
    t.assert_tier_cut_lock_invariants()

    # 单独再核一遍最容易"顺手改"的两处
    assert t.TIER_CUT_RULE_VERSION == "tier_cut_v1"
    assert C.BONFERRONI_M == 6
    assert C.BONFERRONI_SCHEME_ID == "independent_family_v1"
    assert C.DIRECTION_LABELS == frozenset(
        {"research_buy", "research_sell", "research_watch"}
    )
