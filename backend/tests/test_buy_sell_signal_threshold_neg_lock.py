"""阈值负锁语义契约（批次 20261101gthresh · P-bss-evidence · VOLUME `threshold_lock_data_pending`）。

## 这组测在测什么

这一拍交付的是**语义**，不是清零。所以测的不是"清得多干净"，而是**四种越界有没有被挡住**：

1. **标签被删** —— 有人觉得"语义都写清了，标签干脆清了吧"。这是把本拍改写成清零刀。
   T1 + T3 都咬这一处：标签必须在 pending 里，且 `assert_threshold_lock_still_pending`
   必须在标签缺失时炸。
2. **阈值被钉** —— 有人把"量比 2.5 / 突破 0.8% / 窗口 5 分钟"塞进来当"已锁定"。
   钉数＝fabricate。T3 逐个字段验 `raise`，并且**验 `0` 也炸**（否则漏出来的默认值
   会拿到通行证）。
3. **COUNT 被手抄** —— `TRIGGER_GATE_EVIDENCE_PENDING_COUNT` 必须仍是两族集合的
   求和，不得写成字面量 5。T2 用 `len(_vol) + len(_auc) == 常量` 咬，而不是只咬 `== 5`
   —— 只咬 6 的话，常量退化成手抄的 6 也照样绿。
4. **闸被顺手翻** —— 两 TRIGGERING / COMPLETE 被赋 True。T4 对写死的 False 断言。

## 为什么 T3 要既测 raise 又测"干净态不 raise"

只测"伪钉会炸"是不够的：一个**永远 raise** 的函数也能过那种测。所以要成对测 ——
干净态**必须**安静通过，伪钉态**必须**炸。这样"函数被写成永炸"和"函数被写成永过"
都会红。

## 为什么还咬文档

`docs/buy-sell-signal-trigger-gate.md` §4.1.1 是这一拍唯一给人读的交付物。
它最可能的坏法不是写错，是**被回退**（有人 rebase 冲掉、或者后来"优化"掉了）。
所以 T5 对 §4.1.1 的存在与四条大字做锚定 —— 文档不会自己喊，只能靠测。

## 不测什么

不测算行情、不测调度、不测真推、不测 calib —— 本模块没有 I/O，一行算子都没有。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

# ruff: noqa: SIM300
# SIM300: 本文件满是 `assert CONST == 字面量`, ruff 把全大写标识符当"常量应在右",
#         在 assert 上系统性误报 —— 把字面量翻到左边才是真的不可读。

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.buy_sell_signals import threshold_neg_lock as neg_lock
from app.buy_sell_signals.auction_abnormal_stub import (
    AUCTION_ABNORMAL_EVIDENCE,
    AUCTION_ABNORMAL_EVIDENCE_PENDING,
    AUCTION_ABNORMAL_STATUS,
    AUCTION_ABNORMAL_TRIGGERING_ENABLED,
)
from app.buy_sell_signals.first_seen_audit import (
    EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE,
)
from app.buy_sell_signals.threshold_neg_lock import (
    EXPECTED_EVIDENCE_TABLE_VERDICT_WHEN_UNLOCKED,
    THRESHOLD_LOCK_TAG_MUST_REMAIN,
    THRESHOLD_NEG_LOCK_NOTE,
    THRESHOLD_NEG_LOCK_VERSION,
    TRADEABLE_THRESHOLD_FIELDS,
    ThresholdTagRemovedError,
    TradeableThresholdNailError,
    assert_freq_lock_stamp_is_not_clear_reason,
    assert_no_tradeable_threshold_nail,
    assert_strongly_negative_does_not_lock_threshold,
    assert_threshold_lock_still_pending,
)
from app.buy_sell_signals.trigger_gate_lock import (
    TRIGGER_GATE_EVIDENCE_PENDING_COUNT,
    TRIGGER_GATE_LOCK_NOTE,
    TRIGGER_GATE_LOCK_VERSION,
    assert_evidence_pending_still_open,
    assert_trigger_gate_lock_invariants,
)
from app.buy_sell_signals.volume_breakout_evidence_table import (
    VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT,
)
from app.buy_sell_signals.volume_breakout_stub import (
    VOLUME_BREAKOUT_EVIDENCE,
    VOLUME_BREAKOUT_EVIDENCE_PENDING,
    VOLUME_BREAKOUT_STATUS,
    VOLUME_BREAKOUT_TRIGGERING_ENABLED,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = (
    _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "threshold_neg_lock.py"
)
_LOCK_PATH = _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "trigger_gate_lock.py"
_GATE_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-trigger-gate.md"
_NEG_LOCK_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-threshold-neg-lock.md"
_SKELETON_PATH = _REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md"


# ---------------------------------------------------------------- 工具


def _relative_import_modules(path: Path) -> set[str]:
    """ast 解析出**同包相对** import 的模块名（两种写法都覆盖）。

    用途只有一个：确认本模块**没有** import `freq_lock` / `volume_breakout_stub` /
    `volume_breakout_evidence_table`。理由见模块 docstring —— 交叉是语义层面的，
    为一条断言去 import 一整个模块，会把"零碰"变成"零碰但依赖"。
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
    """去掉 docstring 后的源码 —— 断言"代码里没有禁忌"时用它，避免文档自证。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body[0].value.value = ""
    return ast.unparse(tree)


# ================================================================ T1 · 标签仍在


def test_t1_threshold_tag_still_pending_in_volume_family() -> None:
    """`threshold_lock_data_pending` **必须仍在** VOLUME pending 里。

    这一条是本拍最容易失守的地方：模块建好了、语义写清了，
    "那干脆把标签清了吧" 是极自然的下一步 —— 而那是**另一刀**。
    """
    assert THRESHOLD_LOCK_TAG_MUST_REMAIN == "threshold_lock_data_pending"
    assert THRESHOLD_LOCK_TAG_MUST_REMAIN in VOLUME_BREAKOUT_EVIDENCE_PENDING
    # 本拍没有删掉任何一条：VOLUME 仍 2 条
    assert len(VOLUME_BREAKOUT_EVIDENCE_PENDING) == 2
    # 同族另一条（http 鉴权）也一条没动
    assert "http_minute_listing_auth_pending" in VOLUME_BREAKOUT_EVIDENCE_PENDING

    # 断言函数在真实集合上必须安静通过
    assert_threshold_lock_still_pending(VOLUME_BREAKOUT_EVIDENCE_PENDING)


def test_t1_human_read_table_covers_every_pending_key() -> None:
    """人读表的键集合与 pending 集合**完全相等**，且含 threshold 那条。

    两个集合不等就说明有一边漏了：pending 里有但人读表里没有 → 读者看不到这条缺口；
    人读表里有但 pending 里没有 → 表在描述一个已经不存在的缺口。
    """
    assert set(VOLUME_BREAKOUT_EVIDENCE) == set(VOLUME_BREAKOUT_EVIDENCE_PENDING)
    assert THRESHOLD_LOCK_TAG_MUST_REMAIN in VOLUME_BREAKOUT_EVIDENCE

    label, description = VOLUME_BREAKOUT_EVIDENCE[THRESHOLD_LOCK_TAG_MUST_REMAIN]
    assert label  # 有标题
    # 原文必须还在 —— 本拍只许"增补"，不许改写否定式
    assert "均未锁定" in description
    # 增补的负锁说明必须在
    assert "threshold_neg_lock" in description
    assert "负锁" in description
    assert "本拍≠清零刀" in description


def test_t1_assert_fires_when_tag_removed() -> None:
    """删标签必须被断言抓住（负控：干净集合改成缺标签的集合 → 必炸）。"""
    without_tag = VOLUME_BREAKOUT_EVIDENCE_PENDING - {THRESHOLD_LOCK_TAG_MUST_REMAIN}
    assert THRESHOLD_LOCK_TAG_MUST_REMAIN not in without_tag

    with pytest.raises(ThresholdTagRemovedError) as exc:
        assert_threshold_lock_still_pending(without_tag)
    # 文案要点：标签名 + "must remain"
    assert THRESHOLD_LOCK_TAG_MUST_REMAIN in str(exc.value)
    assert "must remain" in str(exc.value)
    # 自定义异常必须仍是 AssertionError 子类（调用方按断言失败理解）
    assert issubclass(ThresholdTagRemovedError, AssertionError)


def test_t1_auction_family_untouched() -> None:
    """AUCTION 族现役 3 条（含**同名** threshold），本拍零碰。

    批次 `20261101gauction` 从 AUCTION 清了 `auction_price_not_persisted`（4 → 3），
    但**同名的 `threshold_lock_data_pending` 必须还在** —— 本拍（gthresh）的钉子
    是「threshold 标签不许被删」，不是「AUCTION 恒 4 条」。
    """
    assert len(AUCTION_ABNORMAL_EVIDENCE_PENDING) == 3
    assert THRESHOLD_LOCK_TAG_MUST_REMAIN in AUCTION_ABNORMAL_EVIDENCE_PENDING
    assert set(AUCTION_ABNORMAL_EVIDENCE) == set(AUCTION_ABNORMAL_EVIDENCE_PENDING)


# ================================================================ T2 · COUNT 与状态


def test_t2_count_still_five_and_derived_not_copied() -> None:
    """COUNT == 5，且**必须是派生值**，不是手抄的字面量。

    批次 `20261101gauction`：AUCTION 4 → 3，总数 6 → 5。

    只咬 `== 6` 不够，只咬 `== len(vol) + len(auc)` 也不够 ——
    把常量写成 `Final = 5` 时，这两个等式**同时成立**（此刻 VOL+AUС 恰好就是 5），
    测全绿而常量已经是手抄的（负控 N04 验过）。所以还要看**源码**：
    `TRIGGER_GATE_EVIDENCE_PENDING_COUNT` 的右侧必须出现 `len(`，
    且不许是纯数字字面量。
    """
    assert TRIGGER_GATE_EVIDENCE_PENDING_COUNT == 5
    assert TRIGGER_GATE_EVIDENCE_PENDING_COUNT == (
        len(VOLUME_BREAKOUT_EVIDENCE_PENDING)
        + len(AUCTION_ABNORMAL_EVIDENCE_PENDING)
    )
    # 两族都没有被整族清空
    assert VOLUME_BREAKOUT_EVIDENCE_PENDING
    assert AUCTION_ABNORMAL_EVIDENCE_PENDING

    # 源码级派生检查：用 ast 找赋值右侧表达式
    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    rhs: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "TRIGGER_GATE_EVIDENCE_PENDING_COUNT"
        ):
            rhs.append(ast.unparse(node.value))
    assert len(rhs) == 1, rhs
    expr = rhs[0]
    assert "len(" in expr, expr
    # 不许是纯数字
    assert not expr.strip().isdigit(), expr
    # 不许出现 `or` / `if` 之类的短路兜底（`6 or len(...)` 这种写法会骗过静态检查）
    assert " or " not in expr, expr
    assert " if " not in expr, expr


def test_t2_gate_lock_assertions_still_pass() -> None:
    """门禁锁面的两条既有断言仍通过 —— 本拍没有把它们改松。"""
    assert_evidence_pending_still_open()
    assert_trigger_gate_lock_invariants()


def test_t2_statuses_still_data_pending() -> None:
    """两族评估态仍 `data_pending` —— 标签还开着，评估态就不该升格。"""
    assert VOLUME_BREAKOUT_STATUS == "data_pending"
    assert AUCTION_ABNORMAL_STATUS == "data_pending"


def test_t2_lock_version_explicitly_not_bumped() -> None:
    """本拍**显式不 bump** `trigger_gate_v1`（同 #265 / #266），并在 NOTE 里披露。

    不 bump 的理由：bump 会迫使 `schema_push_lock` / `schema_push_live_lock` 的
    `EXPECTED_TRIGGER_GATE_LOCK_VERSION` 同步改字面量，而那两个文件本拍零碰。
    **未 bump ≠ 未披露** —— 所以 NOTE 必须带 threshold 负锁旁注。
    """
    assert TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    assert "trigger_gate_v1" in TRIGGER_GATE_LOCK_NOTE
    assert "已清3/剩5" in TRIGGER_GATE_LOCK_NOTE
    assert "已清2/剩6" not in TRIGGER_GATE_LOCK_NOTE
    # 旁注必须在
    assert "threshold" in TRIGGER_GATE_LOCK_NOTE
    assert "负锁" in TRIGGER_GATE_LOCK_NOTE
    assert "threshold_neg_lock_v1" in TRIGGER_GATE_LOCK_NOTE
    assert "标签仍开" in TRIGGER_GATE_LOCK_NOTE


# ================================================================ T3 · 负锁语义 + 伪钉被拒


def test_t3_neg_lock_version_is_pinned() -> None:
    """version 非空且写死 —— 改判据必须 bump，而 bump 会让这条红。"""
    assert THRESHOLD_NEG_LOCK_VERSION == "threshold_neg_lock_v1"
    assert isinstance(THRESHOLD_NEG_LOCK_VERSION, str)
    assert THRESHOLD_NEG_LOCK_VERSION.strip() == THRESHOLD_NEG_LOCK_VERSION


def test_t3_honest_empty_state_passes() -> None:
    """**干净态必须安静通过** —— 否则"永远 raise"的函数也能过负控。"""
    assert_no_tradeable_threshold_nail()
    assert_no_tradeable_threshold_nail(None)
    assert_no_tradeable_threshold_nail({})
    assert_no_tradeable_threshold_nail(
        volume_ratio_locked=None,
        breakout_pct_locked=None,
        window_minutes_locked=None,
    )
    # 四种诚实空态都不炸
    assert_no_tradeable_threshold_nail(
        volume_ratio_locked=None,
        breakout_pct_locked=False,
        window_minutes_locked="",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("volume_ratio_locked", 2.5),
        ("volume_ratio_locked", "2.5"),
        ("volume_ratio_locked", 0),
        ("breakout_pct_locked", 1.0),
        ("breakout_pct_locked", 0),
        ("window_minutes_locked", 5),
        ("window_minutes_locked", "5"),
    ],
)
def test_t3_tradeable_nail_is_rejected_kwargs(field: str, value: object) -> None:
    """三种可交易阈值任一被钉值 → 必炸。`0` 也算钉（见模块 docstring）。"""
    with pytest.raises(TradeableThresholdNailError) as exc:
        assert_no_tradeable_threshold_nail(**{field: value})
    assert field in str(exc.value)
    assert "fabricate" in str(exc.value)
    assert issubclass(TradeableThresholdNailError, AssertionError)


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"volume_ratio_locked": 2.5}, "volume_ratio_locked"),
        ({"breakout_pct_locked": 1.0}, "breakout_pct_locked"),
        ({"window_minutes_locked": 5}, "window_minutes_locked"),
        (
            {"volume_ratio_locked": None, "breakout_pct_locked": 1.0},
            "breakout_pct_locked",
        ),
    ],
)
def test_t3_tradeable_nail_is_rejected_mapping(
    payload: dict[str, object], field: str
) -> None:
    """Mapping 形态也要炸 —— 两条调用路径不能只堵一条。"""
    with pytest.raises(TradeableThresholdNailError) as exc:
        assert_no_tradeable_threshold_nail(payload)
    assert field in str(exc.value)


def test_t3_tradeable_field_names_are_the_three_known_ones() -> None:
    """被守护的字段名是一个封闭三元组 —— 加字段必须动这里，动了就该被看见。"""
    assert TRADEABLE_THRESHOLD_FIELDS == (
        "volume_ratio_locked",
        "breakout_pct_locked",
        "window_minutes_locked",
    )


def test_t3_strongly_negative_is_not_threshold_locked() -> None:
    """触发表 verdict 是 `strongly_negative`，但它**不**等于阈值已锁。"""
    assert VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT == "strongly_negative"
    assert (
        EXPECTED_EVIDENCE_TABLE_VERDICT_WHEN_UNLOCKED
        == VOLUME_BREAKOUT_EVIDENCE_TABLE_VERDICT
    )
    # 正常出口＝什么都不发生
    assert_strongly_negative_does_not_lock_threshold("strongly_negative")
    # 其它 verdict 本拍不表态，也不 raise（另拍裁量）
    assert_strongly_negative_does_not_lock_threshold("weakly_negative")
    assert_strongly_negative_does_not_lock_threshold("")


def test_t3_freq_lock_stamp_is_not_clear_reason() -> None:
    """有 `FreqLockStamp` 不是清标签的理由 —— 正常出口仍是"什么都不发生"。"""
    assert_freq_lock_stamp_is_not_clear_reason(has_freq_lock_stamp=True)
    assert_freq_lock_stamp_is_not_clear_reason(has_freq_lock_stamp=False)


def test_t3_module_docstring_carries_the_big_words() -> None:
    """大字必须在模块 docstring 里 —— 这是给人读的唯一入口。"""
    doc = neg_lock.__doc__ or ""
    assert "负锁" in doc and "删标签" in doc
    assert "fabricate" in doc
    assert "本拍 ≠ 清零刀" in doc or "本拍 ≠ 清零刀" in doc.replace("**", "")
    assert "strongly_negative ≠ 阈值已锁" in doc
    assert "FreqLockStamp ≠ 可清标签" in doc
    assert "语义锁面 PRESENT ≠ 阈值已锁" in doc
    assert "清零一刀 ≠ 可翻闸" in doc
    assert "20261101gthresh" in doc
    assert "18.2bp" in doc
    assert "仅供研究，不构成投资建议；执行须人工确认" in doc


def test_t3_note_is_single_line_and_carries_invariants() -> None:
    """机器可读 NOTE 一行、含全部关键不变式 —— 供锁面 / 文档 / PR 复用同一份措辞。"""
    assert "\n" not in THRESHOLD_NEG_LOCK_NOTE
    for token in (
        "threshold_neg_lock_v1",
        "threshold_lock_data_pending",
        "COUNT仍6",
        "负锁!=删标签",
        "钉可交易数=fabricate",
        "本拍!=清零刀",
        "strongly_negative!=阈值已锁",
        "FreqLockStamp!=可清标签",
    ):
        assert token in THRESHOLD_NEG_LOCK_NOTE, token


# ================================================================ T4 · 闸位仍 False


def test_t4_triggers_and_complete_still_false() -> None:
    """两 TRIGGERING + COMPLETE 仍 `False` —— 本拍只读，不改取值。"""
    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False
    assert EVENT_TRACK_FIRST_SEEN_AUDIT_COMPLETE is False


def test_t4_module_has_no_io_and_no_cross_imports() -> None:
    """本模块无 I/O、不 import 交叉方 —— 两条都用 ast 咬。

    - I/O：源码里不许出现 `open(` / `Path(` / `requests` / `sqlite` 一类符号，
      **也不许 import `pathlib` / `os` / `io` / `subprocess` 等 I/O 模块** ——
      只查调用符号会漏掉"先 import 再懒调用"的写法（负控 N25 就是这么漏过去的）。
    - 交叉：相对 import 里不许出现 `freq_lock` / `volume_breakout_*`。
      交叉只读由**测例**咬（T3 里对比常量取值），不由 import 咬。
    """
    code = _code_only(_MODULE_PATH)
    for banned in (
        "open(",
        "Path(",
        "os.environ",
        "requests",
        "urllib",
        "sqlite3",
        "read_text",
        "write_text",
        "subprocess",
    ):
        assert banned not in code, banned

    # import 黑名单：任何 I/O 模块都不许进这个文件
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported_tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_tops.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_tops.add(node.module.split(".")[0])
    banned_modules = {
        "pathlib",
        "os",
        "io",
        "shutil",
        "subprocess",
        "sqlite3",
        "requests",
        "urllib",
        "httpx",
        "json",
        "tempfile",
    }
    assert imported_tops & banned_modules == set(), imported_tops & banned_modules

    imported = _relative_import_modules(_MODULE_PATH)
    assert imported == set(), imported


def test_t4_module_import_does_not_raise() -> None:
    """import 期不硬 raise —— 断言函数只在被调用时才可能抛。"""
    import importlib

    mod = importlib.import_module("app.buy_sell_signals.threshold_neg_lock")
    importlib.reload(mod)
    assert mod.THRESHOLD_NEG_LOCK_VERSION == "threshold_neg_lock_v1"


# ================================================================ T5 · 文档锚


def test_t5_gate_doc_has_neg_lock_section_and_path_list() -> None:
    """门禁文档 §4.1.1：披露句 + 四条大字 + 可钉证据路径清单 + 「仍 6」未被改成 5。

    文档最可能的坏法不是写错，是**被回退**。所以这里对关键句做锚定。

    标题用**行首精确前缀**比对，而不是 `"### 4.1.1" in doc` 这种子串匹配 ——
    把标题改成 `### 4.1.1-RENAMED` 时子串仍命中，那种测等于没测（负控 N19 验过）。
    """
    doc = _GATE_DOC_PATH.read_text(encoding="utf-8")
    headings = [
        ln for ln in doc.splitlines() if ln.startswith("### 4.1.1 ")
    ]
    assert len(headings) == 1, headings
    assert headings[0].startswith("### 4.1.1 `threshold_lock_data_pending`")

    assert "threshold_neg_lock_v1" in doc
    assert "可钉证据路径清单" in doc
    # 五条前置都在
    for marker in ("负号可解释", "研究档阈值标定", "真可交易标定", "诚实改写", "COUNT 派生"):
        assert marker in doc, marker
    # 四条大字
    assert "负锁 ≠ 删标签" in doc
    assert "钉数 ＝ fabricate" in doc
    assert "本拍 ≠ 清零刀" in doc
    assert "清零一刀 ≠ 可翻闸" in doc
    assert "语义锁面 PRESENT ≠ 阈值已锁 ≠ 可开 TRIGGERING" in doc
    # 计数与本拍一致：已清 3 / 剩 5（20261101gauction 清了 AUCTION 的 auction_price_not_persisted）
    assert "已清 3 / 剩 5" in doc
    assert "已清 2 / 剩 6" not in doc
    # 既有 threshold 缺口行仍在（本拍只增补，不删）
    assert "量比 / 突破幅度 / 窗口分钟数均未锁定" in doc


def test_t5_neg_lock_doc_exists_with_footnote() -> None:
    """短文档存在且**文末**带合规脚注。

    只查「全文含脚注」不够：脚注在开头也有一份，删掉结尾那份照样命中
    （负控 N23 验过）。所以这里取最后一个非空行做**尾部**比对。
    """
    doc = _NEG_LOCK_DOC_PATH.read_text(encoding="utf-8")
    assert "threshold_neg_lock_v1" in doc
    assert "负锁 ≠ 删标签" in doc
    assert "钉数 ＝ fabricate" in doc

    lines = [ln.strip() for ln in doc.strip().splitlines() if ln.strip()]
    tail = lines[-1]
    assert "仅供研究，不构成投资建议；执行须人工确认" in tail, tail
    assert "18.2bp" in tail, tail

    # 门禁文档与骨架文档的脚注同样要求在文末
    for path in (_GATE_DOC_PATH, _SKELETON_PATH):
        text = path.read_text(encoding="utf-8")
        last = [ln.strip() for ln in text.strip().splitlines() if ln.strip()][-1]
        assert "仅供研究，不构成投资建议；执行须人工确认" in last, (path.name, last)


def test_t5_skeleton_disclosure_synced() -> None:
    """骨架文档的相关披露句同步了负锁语义（仅相关句，不改无关业务节）。"""
    doc = _SKELETON_PATH.read_text(encoding="utf-8")
    assert "负锁语义 PRESENT ≠ 阈值已锁 ≠ 可开 TRIGGERING" in doc
    assert "负锁 ≠ 删标签" in doc
    assert "threshold_neg_lock" in doc
