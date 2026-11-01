"""VOLUME 触发表清零一刀 + 契约回归（批次 20261101gevid · P-bss-evidence · #-A-#9）。

## 这组测在测什么

这批改动和前面几批不同：它不是「新增一份契约文档」，而是**真的从缺口集合里删了一个标签**。
删标签这件事有三种坏法，前两种都不报错：

1. **空删** —— 标签从 `frozenset` 里删掉了，但没有任何交付物顶上。缺口"消失"了，
   却没人说得清是哪件事让它消失的。这是最坏的一种：它让进度看起来前进了，实际没有。
2. **假删** —— 把标签改成 `trigger_evidence_table_data_done` 仍留在集合里。集合非空、
   断言照过，但"还有几条没清"从此失真。
3. **顺手多删** —— 借"清触发表"之名把 `threshold_lock_data_pending` 也删了。
   缺口数从 8 掉到 6，看起来更漂亮，但阈值一个都没标定。

所以这组测有三条主线：**表真的在**（T1）、**只删了一条且删得干净**（T2）、
**总数随之派生而变**（T3）。

## 为什么 T1 要真解析 yaml

「我只抄了原数」是一句自述，不是证据。把 yaml 读进来逐项比，才是证据。
表里的 `sample_size` 在 stub 的 Mapping 里根本没有（stub 只抄了 returns/matched），
所以那一项只能对 yaml 原文核 —— 否则有人把 78 改成 780 也没人发现。

## 为什么负控要篡改表行

`assert_rows_match_negative_evidence` 如果只是「表等于表」，它永远为真。
真正有价值的是：把表里某一行的 return 改掉，它必须**当场炸**。这条测就是在验
「交叉断言不是摆设」。

## 不测什么

不测真翻闸、不测触发引擎、不测算行情 —— 本拍只清一个标签。

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
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TABLE_PATH = (
    _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "volume_breakout_evidence_table.py"
)
_STUB_PATH = (
    _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "volume_breakout_stub.py"
)
_LOCK_PATH = (
    _REPO_ROOT / "backend" / "app" / "buy_sell_signals" / "trigger_gate_lock.py"
)
_YAML_PATH = _REPO_ROOT / "backend" / "app" / "custom" / "dsa" / "quant_evidence.yaml"
_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-trigger-gate.md"
_SKELETON_PATH = _REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md"

_CLEARED_TAG = "trigger_evidence_table_data_pending"
_FOOTNOTE = "*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*"


# ---------------------------------------------------------------- 工具


def _yaml_volume_breakout() -> dict:
    """读 yaml 里的 `volume_breakout` 原始条目 —— 交叉核对的**唯一真源**。"""
    data = yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8"))
    assert "volume_breakout" in data, "yaml 里没有 volume_breakout 条目"
    return data["volume_breakout"]


def _relative_import_modules(path: Path) -> set[str]:
    """ast 解析出**同包相对** import 的模块名（两种写法都覆盖）。"""
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


def _imported_modules(path: Path) -> set[str]:
    """ast 解析出**绝对** import 的顶层模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


def _code_only(path: Path) -> str:
    """去掉 docstring 后的源码。"""
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


# ---------------------------------------------------------------- T1 表 PRESENT 且与真源逐项相等


def test_t1_table_version_present():
    """G-T2: 表模块 PRESENT，version 非空且等于 `volume_breakout_evidence_table_v1`。"""
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    assert t.VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION
    assert t.VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION == (
        "volume_breakout_evidence_table_v1"
    )
    assert isinstance(t.VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION, str)


def test_t1_table_has_exactly_four_rows():
    """G-T2: 恰好四行，对应四个突破口径。"""
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    rows = list(t.iter_rows())
    assert len(rows) == 4
    assert len(t.VOLUME_BREAKOUT_EVIDENCE_TABLE_ROWS) == 4


def test_t1_every_row_is_negative():
    """G-T2: 每行 `polarity == "negative"`、`verdict == "strongly_negative"`。

    四行全负是这张表唯一的共同事实。若哪天有人混进一行正的，
    "全负"这个论断就地失效 —— 这条会红。
    """
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    rows = list(t.iter_rows())
    assert all(r["polarity"] == "negative" for r in rows)
    assert all(r["verdict"] == "strongly_negative" for r in rows)
    assert all(r["return"] < 0 for r in rows), "四行收益必须全为负"


def test_t1_rows_match_negative_evidence_mapping():
    """G-T2: 表行与 `VOLUME_BREAKOUT_NEGATIVE_EVIDENCE` 逐项相等（防 fabricate 漂移）。"""
    from app.buy_sell_signals import volume_breakout_evidence_table as t
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_NEGATIVE_EVIDENCE as neg,  # noqa: N811
    )

    rows = list(t.iter_rows())
    assert [r["matched"] for r in rows] == list(neg["matched"])
    assert [r["return"] for r in rows] == list(neg["returns"])
    assert all(r["source"] == neg["source"] for r in rows)
    assert all(r["key"] == neg["key"] for r in rows)
    assert all(r["verdict"] == neg["verdict"] for r in rows)
    # 交叉断言函数本身也要绿
    t.assert_rows_match_negative_evidence()


def test_t1_rows_match_yaml_original():
    """G-T2 · 关键: 表行与 `quant_evidence.yaml` **原文**逐项相等。

    这条是真源核对：stub 的 Mapping 里没有 `sample_sizes`，所以样本量那一列
    只能对 yaml 核。有人把 78 改成 780，只有这条能抓住。
    """
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    original = _yaml_volume_breakout()
    rows = list(t.iter_rows())

    assert [r["matched"] for r in rows] == original["matched"]
    assert [r["return"] for r in rows] == original["returns"]
    assert [r["sample_size"] for r in rows] == original["sample_sizes"]
    assert all(r["verdict"] == original["verdict"] for r in rows)
    assert t.VOLUME_BREAKOUT_EVIDENCE_TABLE_SOURCE == "app/custom/dsa/quant_evidence.yaml"
    assert t.VOLUME_BREAKOUT_EVIDENCE_TABLE_KEY == "volume_breakout"


def test_t1_assert_table_present_passes():
    """G-T2: `assert_table_present()` 通过。"""
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    t.assert_table_present()


def test_t1_iter_rows_yields_copies():
    """`iter_rows()` 每次给新 dict —— 调用方改了返回值污染不到契约数据。"""
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    first = next(iter(t.iter_rows()))
    first["return"] = 0.0
    second = next(iter(t.iter_rows()))
    assert second["return"] == -23.86, "返回值被改后污染了契约数据"


def test_t1_cross_assertion_fires_when_row_tampered(monkeypatch):
    """负控: 篡改一行 return，交叉断言必须**当场炸**。

    这条验的是「`assert_rows_match_negative_evidence` 不是摆设」——
    如果它只是"表等于表"，它就永远为真。
    """
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    tampered = tuple(
        {**row, "return": 99.9} if index == 0 else row
        for index, row in enumerate(t.VOLUME_BREAKOUT_EVIDENCE_TABLE_ROWS)
    )
    monkeypatch.setattr(t, "VOLUME_BREAKOUT_EVIDENCE_TABLE_ROWS", tampered)
    with pytest.raises(AssertionError, match="returns drifted"):
        t.assert_rows_match_negative_evidence()


def test_t1_cross_assertion_fires_when_matched_swapped(monkeypatch):
    """负控: 换掉某个口径名，交叉断言必须发火。"""
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    tampered = tuple(
        {**row, "matched": "编造口径"} if index == 1 else row
        for index, row in enumerate(t.VOLUME_BREAKOUT_EVIDENCE_TABLE_ROWS)
    )
    monkeypatch.setattr(t, "VOLUME_BREAKOUT_EVIDENCE_TABLE_ROWS", tampered)
    with pytest.raises(AssertionError, match="matched drifted"):
        t.assert_rows_match_negative_evidence()


def test_t1_table_present_fires_when_version_drifted(monkeypatch):
    """负控: version 漂移，`assert_table_present()` 必须发火。"""
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    monkeypatch.setattr(t, "VOLUME_BREAKOUT_EVIDENCE_TABLE_VERSION", "v2")
    with pytest.raises(AssertionError, match="must be 'volume_breakout_evidence_table_v1'"):
        t.assert_table_present()


def test_t1_table_present_fires_when_row_count_wrong(monkeypatch):
    """负控: 行数不是 4（比如被删到 3 行），`assert_table_present()` 必须发火。"""
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    monkeypatch.setattr(t, "VOLUME_BREAKOUT_EVIDENCE_TABLE_ROWS", ())
    with pytest.raises(AssertionError, match="must have 4 rows"):
        t.assert_table_present()


# ---------------------------------------------------------------- T2 删标签（干净、不空删）


def test_t2_pending_exactly_three_without_cleared_tag():
    """G-T3: `VOLUME_BREAKOUT_EVIDENCE_PENDING` 恰 **3** 条且**不含**已清标签。"""
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert len(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING) == 3
    assert _CLEARED_TAG not in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING
    assert set(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING) == {
        "local_minute_lib_data_pending",
        "http_minute_listing_auth_pending",
        "threshold_lock_data_pending",
    }


def test_t2_no_done_suffix_left_behind():
    """G-T3: **不许**把标签改成 `_done` 留在集合里 —— 那是进度撒谎。

    两条都要：集合里没有任何 `_done`/`done` 后缀项；已清标签的任何后缀变体
    都不在集合里。
    """
    from app.buy_sell_signals import volume_breakout_stub as vb

    for tag in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING:
        assert tag.endswith("pending"), f"缺口标签应以 pending 结尾: {tag}"
        assert "_done" not in tag, f"出现 _done 后缀: {tag}"
    for variant in (
        "trigger_evidence_table_data_done",
        "trigger_evidence_table_done",
        "trigger_evidence_table_data_pending_done",
    ):
        assert variant not in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING


def test_t2_human_table_keys_match_pending():
    """G-T3: 人读表键集与机器集**相等**（删键，不是只删 pending）。"""
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert set(vb.VOLUME_BREAKOUT_EVIDENCE) == set(
        vb.VOLUME_BREAKOUT_EVIDENCE_PENDING
    )
    assert _CLEARED_TAG not in vb.VOLUME_BREAKOUT_EVIDENCE


def test_t2_status_and_triggering_unchanged():
    """G-T3 / G-T9: STATUS 仍 `data_pending`；TRIGGERING 仍 `False`。"""
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert vb.VOLUME_BREAKOUT_STATUS == "data_pending"
    assert vb.VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert type(vb.VOLUME_BREAKOUT_TRIGGERING_ENABLED) is bool


def test_t2_auction_family_untouched():
    """G-T3: AUCTION 族**一条都没动**（本拍不抢）。"""
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_EVIDENCE_PENDING,
    )

    assert len(AUCTION_ABNORMAL_EVIDENCE_PENDING) == 4
    assert set(AUCTION_ABNORMAL_EVIDENCE_PENDING) == {
        "auction_price_not_persisted",
        "threshold_lock_data_pending",
        "backtest_evidence_data_pending",
        "minute_0925_still_absent",
    }


def test_t2_threshold_lock_gap_still_open():
    """G-T3: **阈值锁那条必须还在** —— 本拍清的是"无触发表"，不是"阈值已标定"。"""
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert "threshold_lock_data_pending" in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING
    assert "local_minute_lib_data_pending" in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING
    assert "http_minute_listing_auth_pending" in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING


def test_t2_clear_is_not_empty_delete():
    """G-T2 / G-T3 · 「不许空删」的可执行版本。

    标签被删了，必须同时满足：表模块存在、四行在册、行数据与真源一致。
    三者缺一，就是空删 —— 缺口"消失"了但没有任何东西顶上。
    """
    from app.buy_sell_signals import volume_breakout_evidence_table as t
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert _CLEARED_TAG not in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING
    # 顶上来的东西必须真的在
    assert _TABLE_PATH.is_file(), "删了标签却没有触发表模块 = 空删"
    rows = list(t.iter_rows())
    assert len(rows) == 4, "触发表必须是四行（四个突破口径）"
    original = _yaml_volume_breakout()
    assert [r["return"] for r in rows] == original["returns"]


# ---------------------------------------------------------------- T3 COUNT 派生 = 7


def test_t3_count_is_seven_and_derived():
    """G-T4: COUNT == 7，且**等于两族之和**（不是手抄的 7）。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_EVIDENCE_PENDING,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_EVIDENCE_PENDING,
    )

    assert len(VOLUME_BREAKOUT_EVIDENCE_PENDING) == 3
    assert len(AUCTION_ABNORMAL_EVIDENCE_PENDING) == 4
    assert g.TRIGGER_GATE_EVIDENCE_PENDING_COUNT == 7
    assert g.TRIGGER_GATE_EVIDENCE_PENDING_COUNT == (
        len(VOLUME_BREAKOUT_EVIDENCE_PENDING)
        + len(AUCTION_ABNORMAL_EVIDENCE_PENDING)
    )


def test_t3_count_source_is_a_sum_not_a_literal():
    """G-T4: COUNT 的**源码**必须是求和，不许写成字面量 7。

    查 AST 不看文本：`len(...) + len(...)` 与 `7` 在 AST 里形状完全不同。
    """
    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    target = None
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "TRIGGER_GATE_EVIDENCE_PENDING_COUNT"
        ):
            target = node.value
            break
    assert target is not None, "找不到 TRIGGER_GATE_EVIDENCE_PENDING_COUNT 定义"
    assert not isinstance(target, ast.Constant), (
        "COUNT 被写成了字面量 —— 必须由两族 pending 只读求和派生"
    )
    assert isinstance(target, ast.BinOp) and isinstance(target.op, ast.Add), (
        "COUNT 应是两族 len 之和"
    )


def test_t3_lock_assertions_still_pass():
    """G-T4: 清 1 条后，`assert_evidence_pending_still_open` 与
    `assert_triggering_still_false` **仍应通过**（两族都还非空、STATUS 未漂）。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    g.assert_evidence_pending_still_open()
    g.assert_triggering_still_false()
    g.assert_complete_still_false()
    g.assert_trigger_gate_lock_invariants()


def test_t3_evidence_assert_still_fires_when_family_emptied(monkeypatch):
    """负控: 把 VOLUME 清**空**，`assert_evidence_pending_still_open` 必须发火。

    这是「清一条不炸、清空才炸」的分界线 —— 也正是它抓不住"只清一条"的原因。
    """
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals import volume_breakout_stub as vb

    monkeypatch.setattr(vb, "VOLUME_BREAKOUT_EVIDENCE_PENDING", frozenset())
    with pytest.raises(AssertionError, match="EVIDENCE_PENDING is empty"):
        g.assert_evidence_pending_still_open()


def test_t3_evidence_assert_still_fires_when_status_drifted(monkeypatch):
    """负控: STATUS 漂移，断言必须发火。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals import volume_breakout_stub as vb

    monkeypatch.setattr(vb, "VOLUME_BREAKOUT_STATUS", "publishable")
    with pytest.raises(AssertionError, match="status drifted"):
        g.assert_evidence_pending_still_open()


# ---------------------------------------------------------------- T4 文档 / 锁面披露


def test_t4_gate_doc_discloses_seven_left_one_cleared():
    """G-T4 / G-T10: 门禁文档披露「已清 1 / 剩 7」，且**没有**旧口径残留。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    assert "已清 1 / 剩 7" in text
    assert "仍 3 条" in text
    assert "仍 4 条" in text
    # 旧口径不得残留
    assert "0 / 8 清零" not in text, "文档仍有旧的「0 / 8 清零」"
    assert "证据缺口是 8 条" not in text
    # 已清标签只在「已清」语境里出现
    assert _CLEARED_TAG in text, "文档应记录已清项名称"
    assert "清零 ≠ 可翻闸" in text


def test_t4_gate_doc_keeps_big_letters_and_footnote():
    """G-T10: 门禁大字与脚注仍在。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    for needle in (
        "路径已通 ≠ calib ≠ 可放量 ≠ TRIGGERING",
        "K8 PASS ≠ 可放量 ≠ 可开 TRIGGERING",
        "门禁契约 ≠ 授权翻闸",
        "合入 ≠ 部署",
        "仅供研究，不构成投资建议；执行须人工确认",
        "红涨绿跌",
        "18.2bp",
    ):
        assert needle in text, f"门禁文档缺: {needle}"

    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[-1] == _FOOTNOTE, f"末行不是脚注: {lines[-1]!r}"


def test_t4_lock_note_discloses_cleared_one():
    """G-T4: 锁面 NOTE 披露已清 1 / 剩 7，且带「清零一刀 != 可翻闸」。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    note = g.TRIGGER_GATE_LOCK_NOTE
    assert "证据缺口8条0清零" not in note, "NOTE 仍是旧的 8 条 0 清零"
    assert "已清1/剩7" in note
    assert "清零一刀!=可翻闸" in note


def test_t4_skeleton_discloses_current_state():
    """G-T4: skeleton 相关披露已同步（不再说「四条」/「8 条 / 0 清零」）。"""
    text = _SKELETON_PATH.read_text(encoding="utf-8")
    assert "### 三条证据缺口" in text
    assert "已清 1 / 剩 7" in text
    assert "证据缺口 **8 条 / 0 清零**" not in text

    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[-1] == _FOOTNOTE, "skeleton 脚注应仍在末行"


def test_t4_no_stale_zero_of_eight_anywhere():
    """G-T4: 仓库内（docs / 锁面 / 测）不得残留「0 / 8 清零」旧口径。

    注意排除本文件自己：测例里本来就要写旧字符串去断言"它不存在"。
    """
    needles = ("0 / 8 清零", "证据缺口8条0清零")
    offenders: list[str] = []
    candidates = [
        *(_REPO_ROOT / "docs").glob("*.md"),
        _LOCK_PATH,
        _REPO_ROOT / "backend" / "tests" / "test_buy_sell_signal_trigger_gate_lock.py",
    ]
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"仍有旧口径残留: {offenders}"


# ---------------------------------------------------------------- T5 不越界（结构守 + 零碰）


def test_t5_direction_labels_still_three_values():
    """G-T5: 方向仍三值，`research_watch` 仍在。"""
    from app.buy_sell_signals.constants import DIRECTION_LABELS

    assert DIRECTION_LABELS == {"research_buy", "research_sell", "research_watch"}
    assert len(DIRECTION_LABELS) == 3


def test_t5_bonferroni_still_six():
    """G-T8: `BONFERRONI_M` 仍 6。"""
    from app.buy_sell_signals.constants import BONFERRONI_M

    assert BONFERRONI_M == 6


def test_t5_hits_schema_still_one():
    """G-T7: `HITS_SCHEMA_VERSION` 仍 `"1"`（本拍不碰 schema）。"""
    from app.buy_sell_signals.constants import HITS_SCHEMA_VERSION

    assert HITS_SCHEMA_VERSION == "1"


def test_t5_no_allow_shadow():
    """G-T7: 表模块与锁面均无 `allow_shadow` 旁路。"""
    banned = {"allow_shadow", "ALLOW_SHADOW", "AllowShadow"}
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    for module in (t, g):
        assert not (banned & set(dir(module))), f"{module.__name__} 出现 allow_shadow"


def test_t5_table_module_has_no_io_or_scheduler():
    """G-T7: 表模块无 I/O 写路径、无调度、无网络。"""
    modules = _imported_modules(_TABLE_PATH)
    forbidden = {
        "requests",
        "httpx",
        "os",
        "json",
        "subprocess",
        "pathlib",
        "tempfile",
        "schedule",
        "apscheduler",
        "crontab",
        "threading",
        "asyncio",
        "pandas",
        "numpy",
    }
    assert not (modules & forbidden), f"表模块不该 import: {sorted(modules & forbidden)}"
    assert modules <= {"__future__", "typing", "collections"}, (
        f"意外的绝对 import: {sorted(modules)}"
    )

    code = _code_only(_TABLE_PATH)
    for needle in ("write_text", "write_bytes", "requests.", "httpx.", "os.environ"):
        assert needle not in code, f"表模块出现副作用调用: {needle!r}"
    assert not re.search(r"(?<![A-Za-z0-9_])open\(", code)


def test_t5_table_uses_module_alias_not_snapshot_import():
    """表模块必须用**模块别名**读 stub（活引用），不许快照 import。

    快照会让 monkeypatch 改不到副本 —— 交叉断言退化成永远为真。
    """
    relative = _relative_import_modules(_TABLE_PATH)
    assert "volume_breakout_stub" in relative

    tree = ast.parse(_TABLE_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            assert (node.module or "") != "volume_breakout_stub", (
                "不许 from .volume_breakout_stub import 取值 —— 用模块别名"
            )


def test_t5_live_alias_is_behaviourally_live(monkeypatch):
    """活引用的**行为**证明：改 stub 的 Mapping，表模块的交叉断言立刻看到。"""
    from app.buy_sell_signals import volume_breakout_evidence_table as t
    from app.buy_sell_signals import volume_breakout_stub as vb

    monkeypatch.setattr(
        vb,
        "VOLUME_BREAKOUT_NEGATIVE_EVIDENCE",
        {**vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE, "returns": (1.0, 2.0, 3.0, 4.0)},
    )
    with pytest.raises(AssertionError, match="returns drifted"):
        t.assert_rows_match_negative_evidence()


def test_t5_table_module_declares_no_state_flip():
    """G-T7: 表模块**不得**给 TRIGGERING / COMPLETE / schema version 赋值。"""
    tree = ast.parse(_TABLE_PATH.read_text(encoding="utf-8"))
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
    assert not offenders, f"表模块不许给这些赋值: {sorted(set(offenders))}"


def test_t5_table_docstring_carries_big_letters_and_footnote():
    """G-T10: 表模块 docstring 须含大字与脚注。"""
    tree = ast.parse(_TABLE_PATH.read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree) or ""
    for needle in (
        "仅供研究，不构成投资建议；执行须人工确认",
        "红涨绿跌",
        "18.2bp",
        "表 PRESENT ≠ 可翻闸",
        "strongly_negative ≠ 阈值已锁",
        "清零一刀 ≠ 可翻闸",
        "负证据表 PRESENT ≠ 可开 TRIGGERING",
    ):
        assert needle in doc, f"表模块 docstring 缺: {needle}"


def test_t5_table_all_exports_real_and_sorted():
    """`__all__` 每个名字都真存在，且排序。"""
    from app.buy_sell_signals import volume_breakout_evidence_table as t

    assert t.__all__
    missing = [name for name in t.__all__ if not hasattr(t, name)]
    assert not missing, f"__all__ 声明了不存在的符号: {missing}"
    assert len(set(t.__all__)) == len(t.__all__)
    assert list(t.__all__) == sorted(t.__all__)


def test_t5_stub_keeps_negative_evidence_numbers():
    """G-T2: stub 的负证据数字**未被本拍改动**（禁改数字冒充"更新证据"）。"""
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_NEGATIVE_EVIDENCE as neg,  # noqa: N811
    )

    assert neg["returns"] == (-23.86, -32.44, -13.80, -19.81)
    assert neg["matched"] == ("放量创60日新高", "趋势突破", "平台整理突破", "布林突破")
    assert neg["verdict"] == "strongly_negative"
    assert neg["source"] == "app/custom/dsa/quant_evidence.yaml"
    assert neg["key"] == "volume_breakout"
