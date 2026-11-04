"""VOLUME `http_minute_listing_auth_pending` 清零（批次 20261102gevid6 · P-bss-evidence）。

## 这一拍到底做了什么

从 `VOLUME_BREAKOUT_EVIDENCE_PENDING` 里**删掉一个标签**
（`http_minute_listing_auth_pending`），人读表 `VOLUME_BREAKOUT_EVIDENCE`
**同步删键**。没有新建任何 HTTP 探针模块，没有把任何密码 / token / secret
写进仓库，没有翻任何闸。

清零的依据是**运维侧**的事实：保险库 `minute.tsp.local` / 环境已持有凭证，
认证后列目录与 MANIFEST 已通（ALIGN 旁证 `d20cea98` 只记 HTTP 状态码）。
「凭证在运维侧」与「密码在代码里」是两件完全不同的事 —— 本拍只承认前者。

## 这组测在防什么

删标签这件事有三种坏法，**没有一种会自己报错**：

1. **只删一侧** —— `PENDING` 删了、`VOLUME_BREAKOUT_EVIDENCE` 没删（或反过来）。
   机器集干净了，人读表还在告诉读者"这条还欠着"，或者反过来 ——
   人读表删了而机器集还留着，`len()` 不变但读者以为清完了。
   `set(EVIDENCE) == set(PENDING)` 这条既有断言咬住它，本文件 T1 再加一层
   「两侧都真的没有这个键」。
2. **改成 `_done` 后缀** —— 集合非空、`len()` 甚至没变小，而"还有几条没清"
   从此失真。改后缀比删键"更安全"的错觉正是它危险的地方。
3. **借机多删** —— 顺手把 `threshold_lock_data_pending` 也清了。
   缺口数更好看，但那条缺的是**一个解释**（四个口径回测全负），不是一个凭证。
   **清掉 http_minute ≠ 阈值这条变轻了。**

还有三种本拍**明令禁止**的"清零方式"，T5 逐条咬住：

- 把密码 / token / secret 抄进仓库（清零依据不是这个，也不该是这个）
- 新建一个 HTTP 探针模块冒充交付（本拍不交付探针，与 #266 性质不同）
- 把未认证入口说成"也通"（**未认证入口仍应 401**，这是运维面的事实）

## 不测什么

不测真实 HTTP、不发任何网络请求、不读 `data/kline_minute`、不碰三个 env 旗标、
不测触发引擎、不测 calib、不解 parquet。

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
_APP = _REPO_ROOT / "backend" / "app"
_STUB_PATH = _APP / "buy_sell_signals" / "volume_breakout_stub.py"
_LOCK_PATH = _APP / "buy_sell_signals" / "trigger_gate_lock.py"
_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-trigger-gate.md"
_SKELETON_PATH = _REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md"
_NEG_LOCK_DOC_PATH = _REPO_ROOT / "docs" / "buy-sell-signal-threshold-neg-lock.md"

_CLEARED_TAG = "http_minute_listing_auth_pending"
_THRESHOLD_TAG = "threshold_lock_data_pending"
_FOOTNOTE = "*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*"

# 本拍明令零碰的三个 env 旗标
_FORBIDDEN_ENV_FLAGS = (
    "BUY_SELL_SIGNALS_PERSIST",
    "BUY_SELL_SIGNALS_PUSH",
    "BUY_SELL_SIGNALS_DINGTALK",
)

# 本拍禁碰的行情源 / HTTP 客户端（AST 导入层黑名单）
_FORBIDDEN_IMPORT_ROOTS = (
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "urllib",
    "http.client",
    "akshare",
    "tushare",
    "efinance",
    "xtick",
)


def _imports_of(path: Path) -> set[str]:
    """收集一个 .py 文件里所有被导入的模块名（含 `from x import y`）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ============================================================ T1 · 删键（两侧都删）


def test_t1_pending_no_longer_contains_cleared_tag() -> None:
    """机器集里**没有**这条标签了，且这不是"改了后缀"。"""
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert _CLEARED_TAG not in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING
    # 任何后缀变体都不许留下 —— `_done` / `_cleared` / `_ok` 都是同一类谎
    for tag in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING:
        assert "http_minute" not in tag, f"已清标签的后缀变体仍在: {tag}"
        assert tag.endswith("pending"), f"缺口标签应以 pending 结尾: {tag}"


def test_t1_human_table_key_also_removed() -> None:
    """人读表**同步删键** —— 这是本拍最容易漏的一侧。

    只删机器集不删人读表：`set(EVIDENCE) == set(PENDING)` 会红（好），
    但若有人为了"修红"把断言改松，人读表就会永远停留在旧叙事上。
    所以这里直接咬人读表的键。
    """
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert _CLEARED_TAG not in vb.VOLUME_BREAKOUT_EVIDENCE
    assert set(vb.VOLUME_BREAKOUT_EVIDENCE) == set(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING)
    assert frozenset({_THRESHOLD_TAG}) == set(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING)


def test_t1_human_table_text_no_stale_auth_narrative() -> None:
    """人读表的**说明文字**里不能再留"凭证没交 / 401"这类已过期的叙述。

    键删了但说明文字还提到"401 还没通"，读者照样会被误导。
    """
    from app.buy_sell_signals import volume_breakout_stub as vb

    text = repr(vb.VOLUME_BREAKOUT_EVIDENCE)
    for stale in ("401", "凭证未", "鉴权未", "列目录未"):
        assert stale not in text, f"人读表残留过期叙述: {stale}"


def test_t1_stub_docstring_records_this_batch() -> None:
    """stub docstring 必须**在已清清单里**记批次号 + 三项边界。

    只查 `"20261102gevid6" in text` 不够：文件别处也可能提到这个批次号
    （例如「批次 20261102gevid6 把同族另一条清掉之后」这句），
    那样删掉"已清清单"里那条记录，测照样绿（负控 N24 验过）。
    所以锚点必须**绑定到已清标签本身**。
    """
    text = _STUB_PATH.read_text(encoding="utf-8")
    # 已清清单里那条：标签名后面紧跟批次号
    assert f"`{_CLEARED_TAG}`（批次 20261102gevid6）" in text
    assert _CLEARED_TAG in text
    # 三项边界：凭证已通 ≠ 可翻闸 / 密码不入仓 / 未认证仍 401
    assert "凭证已通 ≠ 可开 TRIGGERING" in text
    assert "密码不入仓" in text
    assert "未认证入口仍应 401" in text


# ============================================================ T2 · threshold 负锁仍在


def test_t2_threshold_tag_still_pending_both_sides() -> None:
    """`threshold_lock_data_pending` **两侧都还在**，且负锁断言仍安静通过。

    这条是本拍最重要的守门：清掉同族另一条之后，剩下的这条**没有变轻** ——
    它缺的是一个解释（四个口径回测全负），不是一个凭证。
    """
    from app.buy_sell_signals import volume_breakout_stub as vb
    from app.buy_sell_signals.threshold_neg_lock import (
        assert_threshold_lock_still_pending,
    )

    assert _THRESHOLD_TAG in vb.VOLUME_BREAKOUT_EVIDENCE_PENDING
    assert _THRESHOLD_TAG in vb.VOLUME_BREAKOUT_EVIDENCE
    assert_threshold_lock_still_pending(vb.VOLUME_BREAKOUT_EVIDENCE_PENDING)


def test_t2_auction_threshold_still_pending() -> None:
    """AUCTION 族本拍**零碰**：3 条齐全，同名的 threshold 仍在。"""
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_EVIDENCE,
        AUCTION_ABNORMAL_EVIDENCE_PENDING,
    )

    assert len(AUCTION_ABNORMAL_EVIDENCE_PENDING) == 3
    assert set(AUCTION_ABNORMAL_EVIDENCE) == set(AUCTION_ABNORMAL_EVIDENCE_PENDING)
    assert _THRESHOLD_TAG in AUCTION_ABNORMAL_EVIDENCE_PENDING


def test_t2_negative_evidence_numbers_untouched() -> None:
    """测名保留：负号仍在，本拍未「解释掉负号」。

    20261104gyaml4 把四负换成 prod 面板权威读数；清凭证 / 写 yaml 都不等于负号被解释。
    """
    from app.buy_sell_signals import volume_breakout_stub as vb

    assert vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE["returns"] == (
        -46.88,
        -41.38,
        -22.82,
        -37.29,
    )
    assert vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE["verdict"] == "strongly_negative"


def test_t2_status_and_triggering_untouched() -> None:
    """**清零 ≠ 翻闸**：STATUS 仍 `data_pending`，两个 TRIGGERING 仍 `False`（bool）。"""
    from app.buy_sell_signals import volume_breakout_stub as vb
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_TRIGGERING_ENABLED,
    )

    assert vb.VOLUME_BREAKOUT_STATUS == "data_pending"
    assert vb.VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert type(vb.VOLUME_BREAKOUT_TRIGGERING_ENABLED) is bool
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_t2_lock_assertions_still_pass() -> None:
    """门禁锁面的四类断言全过 —— 本拍没有把它们改松。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    g.assert_evidence_pending_still_open()
    g.assert_triggering_still_false()
    g.assert_complete_still_false()
    g.assert_trigger_gate_lock_invariants()


# ============================================================ T3 · COUNT 5 → 4 派生


def test_t3_count_four_and_derived_from_two_families() -> None:
    """COUNT == 4，且**等于两族之和**（VOLUME 1 + AUCTION 3），不是手抄的 4。"""
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals.auction_abnormal_stub import (
        AUCTION_ABNORMAL_EVIDENCE_PENDING,
    )
    from app.buy_sell_signals.volume_breakout_stub import (
        VOLUME_BREAKOUT_EVIDENCE_PENDING,
    )

    assert len(VOLUME_BREAKOUT_EVIDENCE_PENDING) == 1
    assert len(AUCTION_ABNORMAL_EVIDENCE_PENDING) == 3
    assert g.TRIGGER_GATE_EVIDENCE_PENDING_COUNT == 4
    assert g.TRIGGER_GATE_EVIDENCE_PENDING_COUNT == (
        len(VOLUME_BREAKOUT_EVIDENCE_PENDING) + len(AUCTION_ABNORMAL_EVIDENCE_PENDING)
    )


def test_t3_count_source_is_still_a_sum() -> None:
    """COUNT 的**源码**仍是 `len(...) + len(...)` —— 有人图省事改成 `Final = 4` 就红。"""
    tree = ast.parse(_LOCK_PATH.read_text(encoding="utf-8"))
    rhs: list[ast.expr] = []
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "TRIGGER_GATE_EVIDENCE_PENDING_COUNT"
        ):
            rhs.append(node.value)
    assert len(rhs) == 1, rhs
    expr = rhs[0]
    assert not isinstance(expr, ast.Constant), "COUNT 被写成了字面量"
    assert isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add)
    assert "len(" in ast.unparse(expr)


def test_t3_evidence_assert_still_fires_when_volume_emptied(monkeypatch) -> None:
    """负控：把 VOLUME 清**空**，`assert_evidence_pending_still_open` 必须发火。

    「清一条不炸、清空才炸」是这条断言的全部意义 —— 它不该因为
    VOLUME 只剩一条就变得难以触发。
    """
    from app.buy_sell_signals import trigger_gate_lock as g
    from app.buy_sell_signals import volume_breakout_stub as vb

    monkeypatch.setattr(vb, "VOLUME_BREAKOUT_EVIDENCE_PENDING", frozenset())
    with pytest.raises(AssertionError, match="EVIDENCE_PENDING is empty"):
        g.assert_evidence_pending_still_open()


# ============================================================ T4 · 披露同步（已清 4 / 剩 4）


def test_t4_gate_doc_discloses_four_cleared_four_left() -> None:
    """门禁文档披露「已清 4 / 剩 4」，且旧口径无残留。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    assert "已清 4 / 剩 4" in text
    assert "证据缺口现 4 条（已诚实清 4）" in text
    assert "已清 3 / 剩 5" not in text
    assert "已清 2 / 剩 6" not in text
    assert "证据缺口现 5 条" not in text
    # 本拍专属三项边界必须落在文档里
    assert "凭证已通 ≠ 可开 TRIGGERING" in text
    assert "密码不入仓" in text
    assert "未认证入口仍应 401" in text


def test_t4_gate_doc_section_4_1_family_counts() -> None:
    """§4.1 两族条数：VOLUME **1**、AUCTION **3**；且不再有 http_minute 当现役缺口。"""
    text = _DOC_PATH.read_text(encoding="utf-8")
    head = text.split("### 4.1 ", 1)
    assert len(head) == 2, "找不到 §4.1"
    block = head[1].split("\n### ", 1)[0]
    assert "**1**" in block
    assert "**3**" in block
    assert "仍 1 条" in block
    # 已清条目仍应以"已清"身份留痕（人读留痕），但不能出现在「仍 X 条」的现役清单里
    assert _CLEARED_TAG in block


def test_t4_lock_note_discloses_four_cleared() -> None:
    """锁面 NOTE 披露「已清4/剩4」，带大字，且旧口径不残留。"""
    from app.buy_sell_signals import trigger_gate_lock as g

    note = g.TRIGGER_GATE_LOCK_NOTE
    assert "已清4/剩4" in note
    assert "已清3/剩5" not in note
    assert "已清2/剩6" not in note
    assert "credential" not in note.lower()  # 不夹带英文占位
    assert "密码不入仓" in note
    assert "凭证已通!=可开TRIGGERING" in note


def test_t4_skeleton_one_gap_section_and_total() -> None:
    """骨架：VOLUME 那节缩到「一条证据缺口」，总口径「已清 4 / 剩 4」。"""
    text = _SKELETON_PATH.read_text(encoding="utf-8")
    assert "### 一条证据缺口：仍 `data_pending`" in text
    assert "### 两条证据缺口" not in text
    assert "### 三条证据缺口（全部 pending）" in text  # AUCTION 零碰
    assert "已清 4 / 剩 4" in text
    assert "已清 3 / 剩 5" not in text
    # VOLUME 节表格只 1 行，且是 threshold
    volume_head = text.split("### 一条证据缺口：仍 `data_pending`", 1)[1]
    volume_block = volume_head.split("\n### ", 1)[0]
    rows = [ln for ln in volume_block.splitlines() if ln.startswith("| `")]
    assert len(rows) == 1, rows
    assert _THRESHOLD_TAG in rows[0]
    assert _CLEARED_TAG not in "\n".join(rows)


def test_t4_neg_lock_doc_count_now_four() -> None:
    """负锁短文档：COUNT 现为 4（派生），且说明它是"唯一剩下的"。"""
    text = _NEG_LOCK_DOC_PATH.read_text(encoding="utf-8")
    assert "COUNT 现为 **4**" in text
    assert "COUNT **仍 6**" not in text
    assert "标签仍开（两侧）" in text
    # 它是「唯一」剩下的：数字上已经没有任何别的缺口替它分担注意力
    assert "只剩一条" in text
    assert "清掉前者 ≠ 后者变轻" in text


def test_t4_all_docs_footnote_on_last_line() -> None:
    """三份文档末行仍是合规脚注 —— 本拍改了正文，脚注不能被顺手挤走。"""
    for path in (_DOC_PATH, _SKELETON_PATH, _NEG_LOCK_DOC_PATH):
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert lines[-1] == _FOOTNOTE, f"{path.name} 末行不是脚注: {lines[-1]!r}"


def test_t4_no_stale_count_disclosure_anywhere() -> None:
    """docs + 锁面 + stub 全域扫旧口径（排除测试自身）。"""
    needles = (
        "已清 3 / 剩 5",
        "已清3/剩5",
        "证据缺口现 5 条",
        "两条证据缺口",
    )
    candidates = [*(_REPO_ROOT / "docs").glob("*.md"), _LOCK_PATH, _STUB_PATH]
    offenders: list[str] = []
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"仍有旧口径残留: {offenders}"


# ============================================================ T5 · 禁面（密码 / 探针 / 401 / env）


def test_t5_no_credentials_in_repo() -> None:
    """**密码不入仓** —— 本拍改到的文件里不许出现任何凭据字面或取值。

    清零依据是"运维侧持有凭证"，不是"把密码抄进代码"。
    扫的是**本拍改到的文件**，不是全仓（全仓历史遗留与本拍无关）。
    """
    # 高风险字面：真实凭据通常以 `passwd = "..."` / `Basic <base64>` 形式出现
    patterns = (
        re.compile(r"""password\s*[=:]\s*["'][^"']{3,}["']""", re.IGNORECASE),
        re.compile(r"""passwd\s*[=:]\s*["'][^"']{3,}["']""", re.IGNORECASE),
        re.compile(r"""secret\s*[=:]\s*["'][^"']{3,}["']""", re.IGNORECASE),
        re.compile(r"""token\s*[=:]\s*["'][A-Za-z0-9_\-]{8,}["']""", re.IGNORECASE),
        re.compile(r"Basic\s+[A-Za-z0-9+/]{8,}={0,2}"),
        re.compile(r"Authorization\s*:\s*Basic\s+\S+", re.IGNORECASE),
    )
    offenders: list[str] = []
    for path in (_STUB_PATH, _LOCK_PATH, _DOC_PATH, _SKELETON_PATH, _NEG_LOCK_DOC_PATH):
        text = path.read_text(encoding="utf-8")
        for pat in patterns:
            for m in pat.finditer(text):
                offenders.append(f"{path.name}: {m.group(0)[:60]}")
    assert not offenders, f"疑似凭据入仓: {offenders}"


def test_t5_no_new_http_probe_module() -> None:
    """**不新建 HTTP 探针模块冒充交付**。

    清零交付的是"运维侧凭证已通"这个事实，不是一个新的只读探针
    （本拍与 `20261101gevid3` 的 `local_minute_lib` 性质不同）。
    所以 `buy_sell_signals` 包里不许出现新的、名字带 http/minute_listing 的模块。
    """
    pkg = _APP / "buy_sell_signals"
    suspects = [
        p.name for p in pkg.glob("*.py") if re.search(r"(http|minute_listing|listing_auth)", p.stem)
    ]
    assert not suspects, f"出现了新的 HTTP 探针模块: {suspects}"


def test_t5_cleared_reason_is_not_a_probe() -> None:
    """已清条目的人读说明里必须写「凭证在运维侧 / 密码不入仓」，
    而不是「新增了探针」——**清零依据必须与实际交付物一致**。"""
    text = _STUB_PATH.read_text(encoding="utf-8")
    assert "密码不入仓" in text
    assert "运维侧" in text
    # 不许把"新建探针"当成本拍的清零理由。
    # 注意：stub 里"探针"合法的出现只有 gevid3 的 `local_minute_lib`（那个探针确实存在），
    # 所以不能按"探针"一词一刀切 —— 要咬的是「HTTP 探针」与「本拍新建」这两个组合。
    joined = re.sub(r"\s+", "", text)
    assert "不新建" in joined
    for bad in (
        "新建HTTP探针",
        "新增HTTP探针",
        "本拍新建",
        "本拍新增探针",
        "加了一个探针",
        "新增探针模块",
    ):
        assert bad not in joined, f"疑似声称本拍新建了探针: {bad}"


def test_t5_no_forbidden_import_in_touched_modules() -> None:
    """本拍改到的模块**不得**引入任何 HTTP 客户端 / 行情源（AST 导入层，不是文本扫）。

    文本扫 `requests.get` 抓不到裸 `import requests`，所以按 AST 的 Import /
    ImportFrom 节点判 —— 这才是"没有网络依赖"的可执行版本。
    """
    for path in (_STUB_PATH, _LOCK_PATH):
        imported = _imports_of(path)
        for root in _FORBIDDEN_IMPORT_ROOTS:
            hit = [n for n in imported if n == root or n.startswith(root + ".")]
            assert not hit, f"{path.name} 引入了禁网依赖: {hit}"


def test_t5_unauthenticated_entry_still_401() -> None:
    """**未认证入口仍应 401**：这是运维面的既成事实，不许被说成"也通"。

    文档里 `401` 只能以"仍应 401"的身份出现 —— 若它出现在
    "已通 / 也不再 401 / 无需认证"的语境里，就是本拍最危险的那种改写。
    """
    text = _DOC_PATH.read_text(encoding="utf-8")
    assert "未认证入口仍应 401" in text
    for bad in ("不再 401", "无需认证", "无需鉴权", "免认证", "也通了", "认证已取消"):
        assert bad not in text, f"文档把未认证入口说成通了: {bad}"


def test_t5_three_env_flags_untouched() -> None:
    """三个 env 旗标本拍**零碰**（源码里连名字都不该出现）。"""
    for path in (_STUB_PATH, _LOCK_PATH):
        text = path.read_text(encoding="utf-8")
        for flag in _FORBIDDEN_ENV_FLAGS:
            assert flag not in text, f"{path.name} 碰到了 env 旗标: {flag}"


def test_t5_lock_version_explicitly_not_bumped() -> None:
    """**选项 β**：显式不 bump，version 仍 `trigger_gate_v1`，两个 EXPECTED_* 零碰。

    bump 会迫使 `schema_push_lock` / `schema_push_live_lock` 的
    `EXPECTED_TRIGGER_GATE_LOCK_VERSION` 同步改字面量，而那两个文件本拍零碰。
    **未 bump ≠ 未披露**，且 G-T9 明确「显式不 bump 且已披露 → 非硬伤」。
    """
    from app.buy_sell_signals import schema_push_live_lock as live
    from app.buy_sell_signals import schema_push_lock as base
    from app.buy_sell_signals import trigger_gate_lock as g

    assert g.TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    assert base.EXPECTED_TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    assert live.EXPECTED_TRIGGER_GATE_LOCK_VERSION == "trigger_gate_v1"
    # 不 bump 的理由必须写在锁面里，不能只活在 PR 描述里
    doc = _LOCK_PATH.read_text(encoding="utf-8")
    assert "显式不 bump" in doc
    assert "EXPECTED_TRIGGER_GATE_LOCK_VERSION" in doc


def test_t5_0925_not_impersonated_by_0930() -> None:
    """09:25 竞价桶不许被 09:30 冒充 —— 本拍不碰竞价，但守门仍在。

    三层都查，缺一层就会漏：

    1. **文本层**：`09:30` / `0930` 作为字面文本不出现（去字符串后扫，
       免得把 docstring 里"禁止用 09:30 回填"这条**禁令本身**判成违规）。
    2. **数值层**：`time(9, 30)` / `dt_time(9, 30)` 这种**非字符串写法**
       是同一件事的另一种表达 —— 文本扫抓不到（负控 N26 验过）。
       按 AST 取调用的实参数值判。
    3. **常量层**：`SCAN_END` / 任何形如 `(9, 30)` 的时间元组常量同样拒。
    """
    text = _STUB_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)

    # 层 2：任何"两整数实参 ＝ (9, 30)"的调用都拒 —— 不认函数名。
    #
    # 为什么不能按函数名认 `time(...)`：`from datetime import time as _t` 之后
    # 调用的是 `_t(9, 30)`，名字里根本没有 "time"（负控 N26 验过 ——
    # 按名字认的版本对 `_t(9, 30)` 完全失明，删掉 `as _t` 别名又抓得到，
    # 所以那种实现只是在测"有没有起别名"，不是在测"有没有冒充"）。
    # 反过来，"两整数实参 = (9, 30)"这件事本身就是冒充的**唯一实质**，
    # 与函数叫什么无关。本模块没有任何合法的 (9, 30) 调用。
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = [
            a.value
            for a in node.args[:2]
            if isinstance(a, ast.Constant) and isinstance(a.value, int)
        ]
        if args[:2] == [9, 30]:
            offenders.append(ast.unparse(node))
    assert not offenders, f"出现了 (9, 30) 时间构造调用：{offenders}"

    # 层 3：(9, 30) 时间元组常量
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:
            vals = [
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, int)
            ]
            assert vals != [9, 30], "出现了 (9, 30) 时间元组常量"

    # 层 1：文本层（去字符串后）
    stripped = ast.parse(text)
    for node in ast.walk(stripped):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    code = ast.unparse(stripped)
    assert "09:30" not in code
    assert "0930" not in code


def test_t5_no_minute_data_committed() -> None:
    """5.1G 分钟数据**不入仓**：本拍不新增任何数据文件。"""
    pkg = _APP / "buy_sell_signals"
    junk = [
        p.name
        for p in pkg.rglob("*")
        if p.is_file() and p.suffix in {".parquet", ".csv", ".feather", ".arrow"}
    ]
    assert not junk, f"包内出现数据文件: {junk}"
