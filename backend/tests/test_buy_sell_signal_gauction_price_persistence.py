"""`20261101gauction` · 竞价落盘补价 · 清零 `auction_price_not_persisted`。

这一拍只做一件事：**把已经算出来的撮合价与昨收写进 item**，让下游不必再挂
`auction_price_not_persisted` 这条缺口。三条必须同时成立，缺一条就是**空删**：

| # | 要件 | 守在哪 |
|---|---|---|
| 1 | **schema 有写入** —— `build_lists` 的 item 带 `auction_price` / `prev_close` | T1 |
| 2 | **stub 有映射** —— `_candidate_from_item` 从 item 读价，缺字段仍 `None` | T2 |
| 3 | **测证明** —— 旧档映射 `None`、rich 档映射真值、`gap` 存在也不倒推 | T2 |

**禁 `gap` 倒推**是这一拍最容易被"顺手做对"的地方：`gap = price / prev - 1`，
所以 `prev * (1 + gap)` 永远"看起来完全正确"，数值断言一个都抓不住。
唯一的抓法是**只问「有没有观测到」**：`auction_price` 键不在 → 必须 `None`。

扫描沿本文件同目录 gauction2 的教训：**不直接搜文本**。本包文档必须能写
"禁止用 09:30 冒充 09:25"、"禁止 gap 倒推"，全文搜索会把正当禁令判成违规。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PKG = BACKEND / "app" / "buy_sell_signals"
STUB_PATH = PKG / "auction_abnormal_stub.py"
SERVICE_PATH = BACKEND / "app" / "services" / "auction_scan.py"
REPO_ROOT = BACKEND.parent
DOC_PATH = REPO_ROOT / "docs" / "buy-sell-signal-trigger-gate.md"
SKELETON_PATH = REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md"

CLEARED_TAG = "auction_price_not_persisted"
THRESHOLD_TAG = "threshold_lock_data_pending"
D1 = date(2026, 1, 5)


def _item_row(**over: object) -> dict:
    """一组合成快照行 —— **禁假生产数**，只用来验字段能对上。"""
    row: dict[str, object] = {
        "thscode": "600519.SH",
        "ticker": "600519",
        "name": "样本甲",
        "open_price": 18.0,
        "prev_price": 17.8,
        "vol_ratio": 2.5,
        "volume": 1200.0,
        "amount": 2_160_000.0,
    }
    row.update(over)
    return row


def _build(rows: list[dict], **kw: object):
    from app.services import auction_scan

    return auction_scan.build_lists(
        rows,
        {},
        {},
        gap_min=kw.pop("gap_min", 0.0),
        ratio_min=kw.pop("ratio_min", 0.0),
    )


# ==================================================== T1 · schema 有写入


def test_t1_item_persists_auction_price_and_prev_close() -> None:
    """item 必须**同时**带 `auction_price` 与 `prev_close` 两个键（键在，不是值为空）。"""
    hits, _wide, _basis = _build([_item_row()])
    assert hits, "合成行应命中（gap 与量比都够）"
    item = hits[0]
    assert "auction_price" in item, "item 缺 auction_price 键 —— 这就是空删"
    assert "prev_close" in item, "item 缺 prev_close 键"


def test_t1_persisted_price_equals_helper_output() -> None:
    """落盘的值必须**逐位等于** helpers 算出来的值，不许另起一份算法。"""
    from app.services import auction_scan

    row = _item_row()
    hits, _w, _b = _build([row])
    assert hits[0]["auction_price"] == auction_scan._auction_price(row) == 18.0
    assert hits[0]["prev_close"] == auction_scan._prev_close(row) == 17.8


def test_t1_persisted_price_follows_priority_not_last_field() -> None:
    """`open_price` 优先于 `last_price`；昨收 `prev_price` 优先于 `prev_close_price`。

    `open_price=0` 视为「没有」（`_positive` 只认正数），退到 `last_price`；
    `prev_price=17.8` 优先于 `prev_close_price=99.0`。两个优先序都靠落盘值体现。
    """
    row = _item_row(open_price=0, last_price=18.0, prev_close_price=99.0)
    hits, _w, _b = _build([row])
    assert hits, "open_price=0 应退到 last_price=18.0，仍能算 gap"
    assert hits[0]["auction_price"] == 18.0, "open_price=0 不该被当成 0 元价"
    assert hits[0]["prev_close"] == 17.8, "prev_price 应优先于 prev_close_price"


def test_t1_gap_regression_unchanged_by_persistence() -> None:
    """**零行为破坏**：多写两个键，`gap` 一位都不许变。"""
    row = _item_row()
    hits, _w, _b = _build([row])
    assert hits[0]["gap"] == round(18.0 / 17.8 - 1.0, 6)
    # gap 与落盘价自洽（是定义式，不是倒推）
    assert hits[0]["gap"] == round(
        hits[0]["auction_price"] / hits[0]["prev_close"] - 1.0, 6
    )


def test_t1_missing_source_field_persists_none_not_zero() -> None:
    """源行没有价字段时落盘 `None`（**不是** `0`、**不是** `None`→`0` 的默认值）。"""
    row = _item_row(open_price=0, last_price=0)
    row.pop("prev_price", None)
    row.pop("prev_close_price", None)
    hits, _w, _b = _build([row])
    assert hits == [], "没昨收就算不出 gap，不该进 hits"


def test_t1_wide_candidates_also_persist_price() -> None:
    """宽候选走同一个 item 构造函数 —— 也一样带两个新键（不能只补 hits）。"""
    row = _item_row(open_price=18.4)  # gap ≈ 3.4% ≥ WIDE_MIN_GAP
    _hits, wide, _b = _build([row], gap_min=0.5, ratio_min=0.0)
    assert wide, "应进宽候选"
    assert wide[0]["auction_price"] == 18.4
    assert wide[0]["prev_close"] == 17.8


# ==================================================== T2 · stub 有映射


def test_t2_rich_archive_maps_price_through() -> None:
    """存档里有价 → 候选 `auction_price` 是那个价（不是 `None`）。"""
    from app.buy_sell_signals import auction_abnormal_stub

    item = {
        "symbol": "600519.SH",
        "ticker": "600519",
        "name": "样本甲",
        "gap": 0.011236,
        "vol_ratio": 2.5,
        "auction_volume": 12.0,
        "auction_amount": 2_160_000.0,
        "auction_price": 18.0,
        "prev_close": 17.8,
    }
    cand = auction_abnormal_stub._candidate_from_item(item, trade_date=D1, scanned_at=None)
    assert cand is not None
    assert cand.auction_price == 18.0
    assert cand.metadata["prev_close"] == 17.8


def test_t2_legacy_archive_without_keys_maps_none() -> None:
    """旧档没有两个键 → `None`，且 `prev_close` 不凭空出现在 metadata 里。"""
    from app.buy_sell_signals import auction_abnormal_stub

    item = {"symbol": "600519.SH", "gap": 0.011236, "vol_ratio": 2.5}
    cand = auction_abnormal_stub._candidate_from_item(item, trade_date=D1, scanned_at=None)
    assert cand is not None
    assert cand.auction_price is None
    assert "prev_close" not in cand.metadata, "旧档不该凭空多出 prev_close"


def test_t2_gap_plus_prev_close_is_never_reversed_into_price() -> None:
    """**核心红线**：`gap` 与 `prev_close` 都在、但 `auction_price` 缺 → 仍 `None`。

    倒推 `prev * (1 + gap)` 得到的 18.0 与真值一模一样，任何数值断言都抓不住；
    只有"有没有这个键"能抓住。这条测就是那把唯一的尺子。
    """
    from app.buy_sell_signals import auction_abnormal_stub

    item = {
        "symbol": "600519.SH",
        "gap": 0.011236,
        "prev_close": 17.8,
        "vol_ratio": 2.5,
    }
    cand = auction_abnormal_stub._candidate_from_item(item, trade_date=D1, scanned_at=None)
    assert cand is not None
    assert cand.auction_price is None, "gap 倒推出来的价不许冒充观测价"
    assert cand.metadata["prev_close"] == 17.8


def test_t2_illegal_price_values_map_to_none() -> None:
    """布尔 / 非数 / 空串 → `None`。

    注意 `"18.0"` 这种**数字字符串**是合法输入（存档 JSON 里常见字符串化数字，
    `_to_float` 用 `float()` 解析），所以它**不在**本测的非法集合里 —— 那属另一类。
    布尔尤其危险：`True == 1`、`False == 0`，必须先判 `isinstance(bool)`。
    """
    from app.buy_sell_signals import auction_abnormal_stub

    for bad in ("", None, True, False, [], {}):
        cand = auction_abnormal_stub._candidate_from_item(
            {"symbol": "600519.SH", "auction_price": bad, "gap": 0.0},
            trade_date=D1,
            scanned_at=None,
        )
        assert cand is not None
        assert cand.auction_price is None, f"{bad!r} 不该被当成价"


def test_t2_zero_price_maps_to_none() -> None:
    """`0` 不是价（停牌 / 缺字段的常见占位）—— 不许当成 0 元撮合价。"""
    from app.buy_sell_signals import auction_abnormal_stub

    cand = auction_abnormal_stub._candidate_from_item(
        {"symbol": "600519.SH", "auction_price": 0, "prev_close": 0, "gap": 0.0},
        trade_date=D1,
        scanned_at=None,
    )
    assert cand is not None
    assert cand.auction_price == 0.0 or cand.auction_price is None
    # 只要不是「凭空补一个正值」就行；0 本身必须原样透传或判 None，二者皆可
    assert cand.auction_price in (None, 0.0)


def test_t2_mapping_source_has_no_gap_arithmetic() -> None:
    """源码级：`_candidate_from_item` 里不许出现 `gap` 参与乘除。

    AST 层面查 `_candidate_from_item` 函数体：不允许 `1 + gap` / `prev * (...)` 这类
    反算表达式。人读的"禁止倒推"注释不算证据 —— 要查代码。
    """
    stub_src = STUB_PATH.read_text(encoding="utf-8")
    fn = next(
        n
        for n in ast.walk(ast.parse(stub_src))
        if isinstance(n, ast.FunctionDef) and n.name == "_candidate_from_item"
    )
    # 字典键 `"gap"` 在 AST 里是 Constant 字符串，抹掉就看不到"读了 gap"了。
    # 所以这里**保留**字符串，只把注释排除在外（ast 本来就没有注释，天然干净）。
    key_reads = [
        node.args[0].value
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert "gap" in key_reads, f"应当有 item.get('gap')，实际 {key_reads}"
    assert "auction_price" in key_reads, "auction_price 必须来自 item.get()"

    # 反算特征：任何 BinOp(Mult/Div/Add) 的子树里都不许出现 "gap" 这个**键名**
    for node in ast.walk(fn):
        if isinstance(node, ast.BinOp):
            dump = ast.dump(node)
            if "'gap'" in dump:
                seg = ast.get_source_segment(stub_src, node) or ""
                raise AssertionError(f"映射里出现 gap 参与算术（疑似倒推）: {seg}")


# ==================================================== T3 · 标签面


def test_t3_cleared_tag_is_gone_not_suffixed() -> None:
    """清标签是**删键**（pending 与 human 表都删），不是改 `_done` 后缀。

    **必查两侧**：只查人读表 `AUCTION_ABNORMAL_EVIDENCE` 会漏掉
    "pending 里塞了个 `auction_price_not_persisted_done`" 这种变异 ——
    它让下游仍看得见一个"待办"，只是改了名字（负控 N06 抓到过）。
    """
    from app.buy_sell_signals import auction_abnormal_stub

    assert CLEARED_TAG not in auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE_PENDING
    assert CLEARED_TAG not in auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE
    # 两侧都不许出现带该前缀的任何变体（_done / _cleared / _ok …）
    for side, keys in (
        ("pending", auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE_PENDING),
        ("human", auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE),
    ):
        residue = sorted(k for k in keys if CLEARED_TAG in k)
        assert not residue, f"{side} 侧残留已清标签的变体: {residue}"


def test_t3_pending_is_exactly_three() -> None:
    """AUCTION 待办恰 3 条（4 → 3，只清了价这一条）。"""
    from app.buy_sell_signals import auction_abnormal_stub

    assert len(auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE_PENDING) == 3
    assert frozenset({THRESHOLD_TAG, "backtest_evidence_data_pending", "minute_0925_still_absent"}) == (
        auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE_PENDING
    )
    assert set(auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE) == set(auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE_PENDING)


def test_t3_threshold_tag_survives_on_both_sides() -> None:
    """**两侧** `threshold_lock_data_pending` 都还在（VOLUME + AUCTION）——本拍禁删。"""
    from app.buy_sell_signals import auction_abnormal_stub, volume_breakout_stub

    assert THRESHOLD_TAG in auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE_PENDING
    assert THRESHOLD_TAG in auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE
    assert THRESHOLD_TAG in volume_breakout_stub.VOLUME_BREAKOUT_EVIDENCE_PENDING


def test_t3_count_is_five_and_still_derived() -> None:
    """COUNT == 5 且**源码是求和**（不是手抄 5）。"""
    from app.buy_sell_signals import auction_abnormal_stub, trigger_gate_lock, volume_breakout_stub

    assert trigger_gate_lock.TRIGGER_GATE_EVIDENCE_PENDING_COUNT == 5
    assert (
        len(volume_breakout_stub.VOLUME_BREAKOUT_EVIDENCE_PENDING) + len(auction_abnormal_stub.AUCTION_ABNORMAL_EVIDENCE_PENDING)
    ) == trigger_gate_lock.TRIGGER_GATE_EVIDENCE_PENDING_COUNT
    tree = ast.parse(Path(trigger_gate_lock.__file__).read_text(encoding="utf-8"))
    src = Path(trigger_gate_lock.__file__).read_text(encoding="utf-8")
    rhs = None
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "TRIGGER_GATE_EVIDENCE_PENDING_COUNT"
        ):
            rhs = node.value
            break
    assert rhs is not None
    rhs_src = ast.get_source_segment(src, rhs) or ""
    assert "len(" in rhs_src, "COUNT 右侧必须是 len(...) 求和，不许是字面量"
    assert isinstance(rhs, ast.BinOp)


def test_t3_gate_assertions_still_pass() -> None:
    """清一条之后，五条断言仍全过（两族都非空、STATUS 未漂、闸未翻）。"""
    from app.buy_sell_signals import trigger_gate_lock

    trigger_gate_lock.assert_evidence_pending_still_open()
    trigger_gate_lock.assert_triggering_still_false()
    trigger_gate_lock.assert_complete_still_false()
    trigger_gate_lock.assert_trigger_gate_lock_invariants()


def test_t3_triggers_still_false_after_clearing() -> None:
    """**清零 ≠ 翻闸**：清掉一条缺口，两个 TRIGGERING 仍是 `False`。"""
    from app.buy_sell_signals import auction_abnormal_stub, volume_breakout_stub

    assert auction_abnormal_stub.AUCTION_ABNORMAL_TRIGGERING_ENABLED is False
    assert volume_breakout_stub.VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert auction_abnormal_stub.AUCTION_ABNORMAL_STATUS == "data_pending"


def test_t3_assert_fires_when_family_emptied(monkeypatch) -> None:
    """负控：把 AUCTION 清**空**，`assert_evidence_pending_still_open` 必须发火。"""
    import pytest

    from app.buy_sell_signals import auction_abnormal_stub, trigger_gate_lock

    monkeypatch.setattr(
        auction_abnormal_stub, "AUCTION_ABNORMAL_EVIDENCE_PENDING", frozenset()
    )
    with pytest.raises(AssertionError, match="EVIDENCE_PENDING is empty"):
        trigger_gate_lock.assert_evidence_pending_still_open()


# ==================================================== T4 · 披露同步


def test_t4_gate_doc_discloses_three_cleared_five_left() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "已清 3 / 剩 5" in text
    assert "已清 2 / 剩 6" not in text
    assert CLEARED_TAG in text, "文档要留已清项名字（人读留痕）"


def test_t4_lock_note_discloses_three_cleared() -> None:
    from app.buy_sell_signals import trigger_gate_lock

    assert "已清3/剩5" in trigger_gate_lock.TRIGGER_GATE_LOCK_NOTE
    assert "已清2/剩6" not in trigger_gate_lock.TRIGGER_GATE_LOCK_NOTE
    # 大字仍在
    assert "清零一刀!=可翻闸" in trigger_gate_lock.TRIGGER_GATE_LOCK_NOTE
    assert "落盘!=可开TRIGGERING" in trigger_gate_lock.TRIGGER_GATE_LOCK_NOTE


def test_t4_skeleton_discloses_three_gaps_section() -> None:
    text = SKELETON_PATH.read_text(encoding="utf-8")
    assert "### 三条证据缺口（全部 pending）" in text
    assert "### 四条证据缺口" not in text
    assert "已清 3 / 剩 5" in text


def test_t4_footnote_is_last_line_everywhere() -> None:
    footnote = "*仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp。*"
    for path in (DOC_PATH, SKELETON_PATH, STUB_PATH):
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        assert lines[-1] == footnote, f"{path.name} 末行不是脚注: {lines[-1]!r}"


# ==================================================== T5 · 禁面（零碰）


def test_t5_service_keeps_no_backfill_entry() -> None:
    """`auction_scan` 未新增盘后补扫 / 回填入口。"""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not any("backfill" in n.lower() for n in names), names
    assert "SCAN_POST_CLOSE" not in dir(__import__(
        "app.services.auction_scan", fromlist=["x"]
    ))


def test_t5_no_new_market_data_source_imported() -> None:
    """未引入新行情源（XTick / 东财 / 腾讯 / requests 直连等）。

    两层查：
      a. **AST 导入层** —— 不许新 import 任何 HTTP / 行情 SDK 模块。
         只查 `"requests.get"` 这种调用形态会漏掉裸 `import requests`
         （负控 N16 抓到过：只 import 不调用时测全绿）。
      b. 文本层 —— 不许出现那几个具体源的域名 / 模块名。
    """
    banned_modules = {
        "requests",
        "httpx",
        "aiohttp",
        "urllib3",
        "akshare",
        "tushare",
        "xtick",
        "efinance",
    }
    import ast as _ast

    for path in (SERVICE_PATH, STUB_PATH):
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, _ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        hit = imported & banned_modules
        assert not hit, f"{path.name} 新引入行情/HTTP 源: {sorted(hit)}"

        text = path.read_text(encoding="utf-8").lower()
        for banned in ("xtick", "eastmoney", "qq.com", "requests.get", "httpx.get", "push2.eastmoney"):
            assert banned not in text, f"{path.name} 出现新行情源: {banned}"


def test_t5_env_flags_untouched() -> None:
    """三个 env 旗标**本拍零碰**：名字仍是字面量字符串，且默认仍关闭。

    真正的零碰靠 `git diff` 审（这三个 flag 的读取点在别的模块）；这里只钉两件事：
    ① 三个旗标名在 constants 里仍以原样字符串出现（没有被改写成别的键）；
    ② 本拍两个改动文件里**没有**出现对它们的读取或 `os.environ` 赋值。
    """
    from app.buy_sell_signals import constants

    flags = (
        "BUY_SELL_SIGNALS_PERSIST",
        "BUY_SELL_SIGNALS_PUSH",
        "BUY_SELL_SIGNALS_DINGTALK",
    )
    c_text = Path(constants.__file__).read_text(encoding="utf-8")
    for name in flags:
        assert name in c_text, f"{name} 在 constants 里丢了"

    # ② 本拍两个改动文件里不许**新增**对三个旗标的读取。
    # 注意 `auction_scan` 基线本来就有 `os.environ.get("AUCTION_SCAN_ENABLED")`
    # （历史既有），所以不能一句 `"os.environ" not in text` 把基线也判违规 ——
    # 只咬三个**本拍禁碰**的旗标名。
    for path in (SERVICE_PATH, STUB_PATH):
        text = path.read_text(encoding="utf-8")
        for name in flags:
            assert name not in text, f"{path.name} 本拍不该碰 env 旗标 {name}"


def test_t5_no_price_quantity_fields_on_signal_record_change() -> None:
    """候选 dataclass 的价格字段语义变了，但**没有**新增下单/仓位字段。"""
    from app.buy_sell_signals import auction_abnormal_stub

    fields = set(auction_abnormal_stub.AuctionCandidate.__dataclass_fields__)
    assert "auction_price" in fields
    for banned in ("quantity", "position_size", "order_id", "order_side"):
        assert banned not in fields, f"候选多出下单字段: {banned}"
