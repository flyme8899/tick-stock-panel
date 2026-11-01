"""gauction2 隔离扫（批次 20261026gauction2 · 竞价窄开）：守住"勿真开"与"不冒充"。

这一拍和 gvol 一样，**代码做的很少** —— 新建一个只读适配 stub，补身份常量与
四条 pending 标签，加导出与文档。真正的风险不是写错了什么，是**将来某一次"顺手"**：

- 顺手让 `evaluate_auction_abnormal()` 返回 `triggered=False`（"查过，没异动"——
  这是个我们并不知道的结论）
- 顺手用 09:30 的分钟 bar 顶替 09:25，把 `minute_0925_still_absent` 清掉
- 顺手给 `auction_price` 填个数，或者用 `gap` 倒推一个"看起来合理"的价
- 顺手把 `min_gap` / `min_vol_ratio` 当成已锁定的回测门禁写进常量
- 顺手在信号路径里调 `run_scan` / 重抓快照 / 盘后补扫
- 顺手把 `post_open_noise_bucket` 填进竞价记录（混桶）
- 顺手改 `volume_breakout_stub` 让两条腿"统一一下"
- 顺手写「竞价回测已通 / 信号可做 / 正式可推」的文案

这些都不会让既有测例变红 —— 所以这里逐条静态守住。

扫描方式沿用 gvol / gval / gwlist 的教训：**不能直接搜文本**。本包的文档里必须能写
"禁止用 09:30 冒充 09:25"，全文搜索会把正当的禁令判成违规。做法是把 AST 里所有
字符串字面量换成占位符再扫代码部分。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import date
from datetime import time as dt_time
from pathlib import Path

import pytest

from app.buy_sell_signals import auction_abnormal_stub as A
from app.buy_sell_signals import constants as C
from app.buy_sell_signals import models as M
from app.buy_sell_signals import quality_rank as qr
from app.buy_sell_signals import volume_breakout_stub as vb

BACKEND = Path(__file__).resolve().parents[1]
PKG = BACKEND / "app" / "buy_sell_signals"
STUB = PKG / "auction_abnormal_stub.py"

D1 = date(2026, 1, 5)
D2 = date(2026, 1, 6)


# ================================================================== 工具


def _sources() -> list[Path]:
    return sorted(PKG.glob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _code_without_strings(path: Path) -> str:
    """把源码里**所有**字符串字面量换成占位符后的 AST dump。

    本包的 docstring 必须能写"禁止用 09:30 冒充 09:25"，必须能写"不要填假价"。
    全文搜索会把这类正当禁令判成违规，所以只扫代码骨架。
    """
    tree = _tree(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = "<STR>"
    return ast.dump(tree)


def _string_constants(path: Path) -> list[str]:
    return [
        node.value
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _imported_modules(path: Path) -> set[str]:
    """文件里所有 `import x.y` / `from x.y import` 的**顶层模块名**。"""
    names: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


@pytest.fixture()
def scan_root(tmp_path: Path) -> Path:
    """一个**合成**的 data 根目录，里面放一日 scan 存档。

    **禁假生产数**：这里的 `gap` / `vol_ratio` / `auction_volume` 都是手写的
    小数值，只用来验证"字段能对上"，不对应任何真实标的或真实交易日。
    """
    root = tmp_path / "data"
    (root / "auction_scan").mkdir(parents=True)
    payload = {
        "trade_date": D1.isoformat(),
        "scanned_at": f"{D1.isoformat()}T09:25:12+08:00",
        "server_ts": 0,
        "scanned_count": 3,
        "volume_basis": "snapshot_auction_volume",
        "min_gap": 0.02,
        "min_vol_ratio": 2.0,
        "items": [
            {
                "symbol": "TEST01.SH",
                "ticker": "TEST01",
                "name": "合成一号",
                "gap": 0.051,
                "vol_ratio": 3.5,
                "auction_volume": 12.5,
                "auction_amount": 1000.0,
            },
            {
                "symbol": "TEST02.SZ",
                "ticker": None,
                "name": None,
                "gap": -0.031,
                "vol_ratio": None,
                "auction_volume": None,
                "auction_amount": None,
            },
        ],
    }
    (root / "auction_scan" / f"{D1.isoformat()}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return root


# ============================================================ ① 身份钉


def test_signal_id_is_auction_abnormal() -> None:
    assert A.AUCTION_ABNORMAL_SIGNAL_ID == "auction_abnormal"


def test_event_class_is_auction_and_known() -> None:
    assert A.AUCTION_ABNORMAL_EVENT_CLASS == "auction"
    assert A.AUCTION_ABNORMAL_EVENT_CLASS in C.EVENT_CLASSES


def test_event_class_is_not_the_volume_one() -> None:
    """竞价不是放量 —— 两个 event_class 必须不同。"""
    assert A.AUCTION_ABNORMAL_EVENT_CLASS != vb.VOLUME_BREAKOUT_EVENT_CLASS


def test_family_is_minute_event_and_known() -> None:
    assert A.AUCTION_ABNORMAL_FAMILY == "minute_event"
    assert A.AUCTION_ABNORMAL_FAMILY in C.SIGNAL_FAMILIES


def test_auction_stat_bucket_is_the_0925_constant() -> None:
    assert A.AUCTION_ABNORMAL_AUCTION_STAT_BUCKET == "auction_0925"
    assert A.AUCTION_ABNORMAL_AUCTION_STAT_BUCKET == C.AUCTION_STAT_BUCKET_0925


def test_auction_rule_id_is_none() -> None:
    """本拍不锁规则 —— 阈值未标定，任何 rule_id 都会暗示规则已定。"""
    assert A.AUCTION_ABNORMAL_AUCTION_RULE_ID is None
    assert A.AUCTION_ABNORMAL_AUCTION_RULE_ID is C.AUCTION_RULE_ID_NONE


def test_post_open_noise_flag_is_false_and_bucket_is_none() -> None:
    assert A.AUCTION_ABNORMAL_POST_OPEN_NOISE_FLAG is False
    assert A.AUCTION_ABNORMAL_POST_OPEN_NOISE_BUCKET is None


def test_signal_id_is_defined_in_exactly_one_module() -> None:
    """`AUCTION_ABNORMAL_SIGNAL_ID` 只能在 stub 里定义一次。

    判据是「谁把这个字符串**赋给** signal_id」，不是「谁提到了这个字符串」——
    `__init__.py` 的说明文字里出现它是正常的。
    """
    owners = [
        path.name
        for path in _sources()
        if re.search(
            r"^AUCTION_ABNORMAL_SIGNAL_ID\s*:\s*Final\s*=\s*[\"']"
            + re.escape(A.AUCTION_ABNORMAL_SIGNAL_ID),
            path.read_text(encoding="utf-8"),
            re.M,
        )
    ]
    assert owners == ["auction_abnormal_stub.py"], owners


# ======================================================= ② 记录构造


def test_record_identity_matches_the_constants() -> None:
    rec = A.build_auction_abnormal_record()

    assert rec.signal_id == A.AUCTION_ABNORMAL_SIGNAL_ID
    assert rec.event_class == "auction"
    assert rec.family == A.AUCTION_ABNORMAL_FAMILY
    assert rec.auction_stat_bucket == "auction_0925"
    assert rec.auction_rule_id is None
    assert rec.post_open_noise_flag is False
    assert rec.post_open_noise_bucket is None


def test_record_status_is_a_lifecycle_state_not_the_eval_state() -> None:
    """`data_pending` 是**评估**态，不能进 `SIGNAL_STATUSES`。"""
    rec = A.build_auction_abnormal_record()

    assert rec.status == A.AUCTION_ABNORMAL_RECORD_STATUS == "candidate"
    assert A.AUCTION_ABNORMAL_STATUS == "data_pending"
    assert A.AUCTION_ABNORMAL_STATUS not in M.SIGNAL_STATUSES


def test_record_carries_the_eval_state_in_metadata() -> None:
    rec = A.build_auction_abnormal_record()
    meta = rec.metadata

    assert meta["auction_abnormal_status"] == "data_pending"
    assert meta["auction_abnormal_triggering_enabled"] is False
    assert set(meta["auction_abnormal_evidence_pending"]) == A.AUCTION_ABNORMAL_EVIDENCE_PENDING


def test_record_is_not_auto_publishable() -> None:
    rec = A.build_auction_abnormal_record()

    assert rec.manual_publish_required is True
    assert rec.auto_promote_forbidden is True
    assert rec.publish_pointer is False
    assert rec.manual_published_at is None


# =============================================== ③ 混桶红线（双向）


def test_auction_record_with_post_open_bucket_is_rejected() -> None:
    """竞价带开盘后桶 → 拒。这是 `SignalRecord` 的既有校验，本拍只测下来。"""
    with pytest.raises(M.SignalValidationError) as ei:
        M.SignalRecord(
            signal_id="auction_abnormal_probe",
            display_name="混桶探针",
            family="minute_event",
            event_class="auction",
            spec_ref="test",
            auction_stat_bucket="auction_0925",
            post_open_noise_bucket="open_0930_1000",
        )
    assert "post_open_noise_bucket" in str(ei.value)
    assert "NOISE_MIX_FORBIDDEN" in str(ei.value) or "混" in str(ei.value)


def test_auction_record_with_unknown_bucket_is_rejected() -> None:
    """竞价桶只允许 `auction_0925` 一个取值。"""
    with pytest.raises(M.SignalValidationError) as ei:
        M.SignalRecord(
            signal_id="auction_abnormal_probe",
            display_name="错桶探针",
            family="minute_event",
            event_class="auction",
            spec_ref="test",
            auction_stat_bucket="auction_0930",
        )
    assert "auction_stat_bucket" in str(ei.value)


def test_auction_bucket_on_non_auction_class_is_rejected() -> None:
    """放量带竞价桶 → 拒（反方向同样不许混）。"""
    with pytest.raises(M.SignalValidationError) as ei:
        M.SignalRecord(
            signal_id="volume_probe",
            display_name="反方向探针",
            family="minute_event",
            event_class="intraday_volume",
            spec_ref="test",
            auction_stat_bucket="auction_0925",
        )
    assert "auction" in str(ei.value)


def test_volume_stub_still_pins_bucket_to_none() -> None:
    """#210 的钉住不变 —— 本拍只读它，不改它。"""
    assert vb.VOLUME_BREAKOUT_AUCTION_STAT_BUCKET is None


def test_noise_mix_forbidden_constant_is_true() -> None:
    assert C.NOISE_MIX_FORBIDDEN is True


def test_noise_bucket_closed_set_is_not_extended() -> None:
    """封闭集仍只有 tip 上的三个；本拍不扩集。"""
    assert frozenset(
        {"open_0930_1000", "intraday_1000_1430", "close_1430_1500"}
    ) == C.POST_OPEN_NOISE_BUCKETS


def test_auction_bucket_constant_itself_is_untouched() -> None:
    assert C.AUCTION_STAT_BUCKET_0925 == "auction_0925"


# ================================================= ④ 竞价时刻不冒充 09:25


def test_auction_ts_is_fixed_0925_cn() -> None:
    ts = A.auction_ts_for(D1)

    assert ts.hour == 9
    assert ts.minute == 25
    assert ts.second == 0
    assert ts.date() == D1
    assert ts.utcoffset() is not None
    assert ts.utcoffset().total_seconds() == 8 * 3600


def test_auction_ts_is_not_0930() -> None:
    """**永久硬禁**：09:30 或任意开盘后时刻冒充 09:25。"""
    ts = A.auction_ts_for(D1)

    assert (ts.hour, ts.minute) != (9, 30)
    assert (ts.hour, ts.minute) == (9, 25)


def test_auction_ts_only_takes_a_trade_date() -> None:
    """签名里**没有**分钟数据可传 —— 这是"不冒充"的机械保证。

    如果哪天有人给这个函数加一个 `frame` / `bar` / `minute` 参数，
    本测例就会红：那意味着"用真实分钟 bar 推竞价时刻"的口子开了。
    """
    import inspect

    params = list(inspect.signature(A.auction_ts_for).parameters)

    assert params == ["trade_date"], params


def test_auction_ts_rule_is_deterministic_across_dates() -> None:
    assert A.auction_ts_for(D1).isoformat() == f"{D1.isoformat()}T09:25:00+08:00"
    assert A.auction_ts_for(D2).isoformat() == f"{D2.isoformat()}T09:25:00+08:00"


# ============================================== ⑤ 只读适配 + fail-closed


def test_missing_scan_dir_fails_closed(tmp_path: Path) -> None:
    ev = A.evaluate_auction_abnormal(tmp_path / "no_such_dir", D1)

    assert ev.status == "data_pending"
    assert ev.reason == "no_data"
    assert ev.data_pending is True
    assert ev.has_scan is False
    assert ev.candidates == ()


def test_none_data_dir_fails_closed_without_guessing() -> None:
    """`data_dir=None` → `no_data`，**不猜**默认目录。

    猜错了会读到别的环境的存档，那种错比读不到更难发现。
    """
    ev = A.evaluate_auction_abnormal(None, D1)

    assert ev.reason == "no_data"
    assert ev.status == "data_pending"
    assert ev.candidates == ()


def test_missing_file_for_that_day_fails_closed(scan_root: Path) -> None:
    """目录在、但那天的文件不在 → 仍 `no_data`（不是空候选当触发）。"""
    ev = A.evaluate_auction_abnormal(scan_root, D2)

    assert ev.reason == "no_data"
    assert ev.candidates == ()
    assert ev.triggered is None


def test_existing_scan_maps_candidates(scan_root: Path) -> None:
    ev = A.evaluate_auction_abnormal(scan_root, D1)

    assert ev.has_scan is True
    assert ev.status == "data_pending"
    assert ev.reason == "evidence_pending"
    assert len(ev.candidates) == 2
    assert {c.symbol for c in ev.candidates} == {"TEST01.SH", "TEST02.SZ"}


def test_candidate_carries_the_mapped_fields(scan_root: Path) -> None:
    ev = A.evaluate_auction_abnormal(scan_root, D1)
    cand = next(c for c in ev.candidates if c.symbol == "TEST01.SH")

    assert cand.display_name == "合成一号"
    assert cand.ticker == "TEST01"
    assert cand.auction_volume == 12.5
    assert cand.metadata["gap"] == 0.051
    assert cand.metadata["vol_ratio"] == 3.5
    assert cand.metadata["auction_amount"] == 1000.0
    assert cand.metadata["scanned_at"].startswith(f"{D1.isoformat()}T09:25")


def test_legacy_archive_without_price_maps_to_none(scan_root: Path) -> None:
    """**旧档**（无 `auction_price` 键）→ 仍 `None`：诚实，不填 0、不用 gap 倒推。

    批次 20261101gauction 之前落盘的 JSON 都没有这个键。它们映射出来仍是 `None` ——
    这是**正确**的（那天确实没记下来），不是缺陷。标签清除的语义是
    **writer 契约 PRESENT**，不是「每一个历史文件都已回填」。
    """
    ev = A.evaluate_auction_abnormal(scan_root, D1)

    assert ev.candidates
    assert all(c.auction_price is None for c in ev.candidates)
    # 旧档里的 gap 有值（0.051 / -0.031），但**没有任何一条**被拿来倒推价格
    assert any(c.metadata["gap"] is not None for c in ev.candidates)
    assert all("prev_close" not in c.metadata for c in ev.candidates)


def test_rich_archive_price_is_mapped(tmp_path: Path) -> None:
    """**新档**（有 `auction_price` / `prev_close`）→ 映射出来，且等于落盘值。

    这一条是 `test_legacy_archive_without_price_maps_to_none` 的对照：
    只测「旧档是 None」不够 —— 一个永远返回 `None` 的实现也能过那种测。
    """
    root = tmp_path / "data"
    (root / "auction_scan").mkdir(parents=True)
    payload = {
        "trade_date": D1.isoformat(),
        "scanned_at": f"{D1.isoformat()}T09:25:12+08:00",
        "server_ts": 0,
        "scanned_count": 1,
        "volume_basis": "snapshot_auction_volume",
        "min_gap": 0.02,
        "min_vol_ratio": 2.0,
        "items": [
            {
                "symbol": "TEST09.SH",
                "ticker": "TEST09",
                "name": "合成九号",
                "gap": 0.011236,
                "vol_ratio": 1.5,
                "auction_volume": 3.0,
                "auction_amount": 500.0,
                "auction_price": 18.0,
                "prev_close": 17.8,
            }
        ],
    }
    (root / "auction_scan" / f"{D1.isoformat()}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    ev = A.evaluate_auction_abnormal(root, D1)
    cand = next(c for c in ev.candidates if c.symbol == "TEST09.SH")

    assert cand.auction_price == 18.0
    assert cand.metadata["prev_close"] == 17.8
    # 与 gap 自洽（不是倒推出来的，是本来就自洽）
    # gap 在落盘时已 round 到 6 位，这里按 6 位容差核对自洽性
    assert abs(cand.auction_price / cand.metadata["prev_close"] - 1 - 0.011236) < 1e-6


def test_price_is_never_reversed_from_gap(tmp_path: Path) -> None:
    """**禁 gap 倒推**：`gap` 与 `prev_close` 都在、但 `auction_price` 缺 → 仍 `None`。

    这是本拍最核心的一条红线。倒推出来的数会「看起来完全正确」
    （`prev_close * (1 + gap)` 正好等于真价），所以它不会被任何数值断言抓到 ——
    只能靠「有没有填」来抓：没观测到，就必须是 `None`。
    """
    root = tmp_path / "data"
    (root / "auction_scan").mkdir(parents=True)
    payload = {
        "trade_date": D1.isoformat(),
        "scanned_at": f"{D1.isoformat()}T09:25:12+08:00",
        "scanned_count": 1,
        "items": [
            {
                "symbol": "TEST10.SH",
                "gap": 0.05,
                "vol_ratio": 2.0,
                # prev_close 有值，auction_price 故意缺
                "prev_close": 100.0,
            }
        ],
    }
    (root / "auction_scan" / f"{D1.isoformat()}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    ev = A.evaluate_auction_abnormal(root, D1)
    cand = next(c for c in ev.candidates if c.symbol == "TEST10.SH")

    # 100.0 * (1 + 0.05) == 105.0 —— 一个「看起来完全正确」的假价
    assert cand.auction_price is None
    assert cand.metadata["prev_close"] == 100.0


def test_candidate_missing_numbers_stay_none_not_zero(scan_root: Path) -> None:
    """缺字段是 `None`（"不知道"），**不是** 0（"量比是 0"）。"""
    ev = A.evaluate_auction_abnormal(scan_root, D1)
    cand = next(c for c in ev.candidates if c.symbol == "TEST02.SZ")

    assert cand.auction_volume is None
    assert cand.metadata["vol_ratio"] is None
    assert cand.metadata["auction_amount"] is None


def test_item_without_symbol_is_skipped(tmp_path: Path) -> None:
    """缺 `symbol` 的条目跳过 —— 不拿空串当标的。"""
    root = tmp_path / "data"
    (root / "auction_scan").mkdir(parents=True)
    (root / "auction_scan" / f"{D1.isoformat()}.json").write_text(
        json.dumps(
            {
                "trade_date": D1.isoformat(),
                "scanned_at": None,
                "items": [
                    {"symbol": "", "gap": 0.1},
                    {"gap": 0.1},
                    {"symbol": "TEST09.SH", "gap": 0.1},
                ],
            }
        ),
        encoding="utf-8",
    )

    ev = A.evaluate_auction_abnormal(root, D1)

    assert [c.symbol for c in ev.candidates] == ["TEST09.SH"]


def test_scan_with_empty_items_is_no_candidates_not_trigger(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "auction_scan").mkdir(parents=True)
    (root / "auction_scan" / f"{D1.isoformat()}.json").write_text(
        json.dumps({"trade_date": D1.isoformat(), "items": []}), encoding="utf-8"
    )

    ev = A.evaluate_auction_abnormal(root, D1)

    assert ev.has_scan is True
    assert ev.reason == "no_candidates"
    assert ev.candidates == ()
    assert ev.triggered is None


def test_trade_date_falls_back_to_target_when_payload_lacks_it(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "auction_scan").mkdir(parents=True)
    (root / "auction_scan" / f"{D1.isoformat()}.json").write_text(
        json.dumps({"items": [{"symbol": "TEST10.SH"}]}), encoding="utf-8"
    )

    ev = A.evaluate_auction_abnormal(root, D1)

    assert ev.trade_date == D1
    assert ev.auction_ts == A.auction_ts_for(D1)


def test_payload_trade_date_wins_over_target(tmp_path: Path) -> None:
    """存档里写的日期优先于调用方传的 `target` —— 记录"这份数据是哪天的"。"""
    root = tmp_path / "data"
    (root / "auction_scan").mkdir(parents=True)
    (root / "auction_scan" / f"{D1.isoformat()}.json").write_text(
        json.dumps({"trade_date": D2.isoformat(), "items": [{"symbol": "TEST11.SH"}]}),
        encoding="utf-8",
    )

    ev = A.evaluate_auction_abnormal(root, D1)

    assert ev.trade_date == D2


# ============================================ ⑥ 三标签（原四条，清一条）+ 恒不触发
#
# 批次 20261101gauction：`auction_scan.build_lists` 补落盘 `auction_price` /
# `prev_close`，本模块改为有则映射 → 缺口标签 `auction_price_not_persisted`
# 被消解并**删除**（删标签，不是改 `_done`）。原四条变三条。
# **清零一刀 ≠ 可翻闸**；**落盘 ≠ 可开 TRIGGERING**；**禁 gap 倒推**。


def test_evidence_pending_has_exactly_three_tags() -> None:
    assert len(A.AUCTION_ABNORMAL_EVIDENCE_PENDING) == 3


def test_evidence_pending_contains_the_three_required_tags() -> None:
    assert frozenset(
        {
            "threshold_lock_data_pending",
            "backtest_evidence_data_pending",
            "minute_0925_still_absent",
        }
    ) == A.AUCTION_ABNORMAL_EVIDENCE_PENDING


def test_cleared_auction_price_tag_is_gone_not_done() -> None:
    """清掉的那条是**删标签**，不是改 `_done` 后缀留存。"""
    assert "auction_price_not_persisted" not in A.AUCTION_ABNORMAL_EVIDENCE_PENDING
    assert "auction_price_not_persisted" not in A.AUCTION_ABNORMAL_EVIDENCE
    assert not any(
        tag.startswith("auction_price") for tag in A.AUCTION_ABNORMAL_EVIDENCE_PENDING
    )


def test_threshold_lock_tag_survives_in_auction_family() -> None:
    """本拍**勿删** `threshold_lock_data_pending`（两侧都要在）。"""
    assert "threshold_lock_data_pending" in A.AUCTION_ABNORMAL_EVIDENCE_PENDING


def test_evidence_human_table_keys_match() -> None:
    assert set(A.AUCTION_ABNORMAL_EVIDENCE) == set(A.AUCTION_ABNORMAL_EVIDENCE_PENDING)


def test_evidence_human_table_explains_each_gate() -> None:
    for tag, value in A.AUCTION_ABNORMAL_EVIDENCE.items():
        assert isinstance(value, tuple) and len(value) == 2, tag
        title, detail = value
        assert title and detail, tag


def test_evidence_tags_never_claim_done() -> None:
    """标签都是缺口；没有一条被改成 `_done`。"""
    assert not any(tag.endswith("_done") for tag in A.AUCTION_ABNORMAL_EVIDENCE_PENDING)


def test_minute_0925_tag_is_kept_not_cleared() -> None:
    """分钟 09:25 仍缺 —— 标签**保留**，永不因读到 scan 存档而清掉。"""
    assert "minute_0925_still_absent" in A.AUCTION_ABNORMAL_EVIDENCE_PENDING


def test_triggering_is_disabled_by_constant() -> None:
    assert A.AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_stub_never_reports_triggered(scan_root: Path) -> None:
    """有存档 / 无存档两条路径，`triggered` 都必须是 `None`。"""
    with_scan = A.evaluate_auction_abnormal(scan_root, D1)
    without_scan = A.evaluate_auction_abnormal(scan_root, D2)

    assert with_scan.triggered is None
    assert without_scan.triggered is None
    assert with_scan.passed is False
    assert without_scan.passed is False


def test_candidates_behind_pending_never_pass(scan_root: Path) -> None:
    ev = A.evaluate_auction_abnormal(scan_root, D1)

    assert ev.candidates
    assert all(c.triggered is None for c in ev.candidates)
    assert all(c.passed is False for c in ev.candidates)
    assert all(c.data_pending is True for c in ev.candidates)


def test_evaluation_and_candidates_carry_all_four_tags(scan_root: Path) -> None:
    ev = A.evaluate_auction_abnormal(scan_root, D1)

    assert set(ev.evidence_pending) == A.AUCTION_ABNORMAL_EVIDENCE_PENDING
    for cand in ev.candidates:
        assert set(cand.evidence_pending) == A.AUCTION_ABNORMAL_EVIDENCE_PENDING


def test_fail_closed_path_also_carries_all_four_tags(tmp_path: Path) -> None:
    ev = A.evaluate_auction_abnormal(tmp_path / "nope", D1)

    assert set(ev.evidence_pending) == A.AUCTION_ABNORMAL_EVIDENCE_PENDING


def test_no_threshold_is_pinned_anywhere() -> None:
    """本拍不钉任何阈值数值 —— UI 的 min_gap / min_vol_ratio 不是已锁门禁。

    扫 AST 里的**数值常量**：stub 里除了时间和 0 之外不应出现像阈值的浮点数。
    """
    # 只允许"时刻/时区"这几个数：09:25:00 与 +08:00。
    allowed = {0, 8, 9, 25}
    suspicious: list[str] = []
    for node in ast.walk(_tree(STUB)):
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            suspicious.append(f"float:{node.value}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int)
            and node.value not in allowed
        ):
            suspicious.append(f"int:{node.value}")
    assert suspicious == [], suspicious


def test_stub_has_no_min_gap_or_vol_ratio_threshold_constant() -> None:
    text = STUB.read_text(encoding="utf-8")
    assigned = re.findall(r"^([A-Z_]+)\s*:\s*Final\s*=", text, re.M)

    assert not [n for n in assigned if "MIN_GAP" in n], assigned
    assert not [n for n in assigned if "VOL_RATIO" in n], assigned


# ================================================= ⑦ 静态隔离 / 零碰


def test_stub_does_not_import_scan_execution_entrypoints() -> None:
    """信号侧**不得**引用扫描执行入口 / 调度注册。

    只读 `get_scan`（纯函数，读落盘文件）；`run_scan` 会真的去抓快照，
    `register_jobs` 会把任务挂进调度器 —— 两者都不该出现在信号路径里。
    """
    imported = _imported_modules(STUB)

    # 唯一允许碰到的服务模块就是 auction_scan（为了 get_scan）
    service_modules = {m for m in imported if m.startswith("app.")}
    assert service_modules == {
        "app.buy_sell_signals",
        "app.buy_sell_signals.models",
        "app.services.auction_scan",
    }, imported


def test_stub_imports_get_scan_lazily_from_the_service() -> None:
    """`get_scan` 在函数体内 import —— 模块级 import 会拖起 polars 等重依赖。"""
    text = STUB.read_text(encoding="utf-8")

    assert "from app.services.auction_scan import get_scan" in text
    # 模块级（顶格）不得出现该 import
    assert not re.search(r"^from app\.services\.auction_scan import", text, re.M)


def test_stub_code_never_calls_scan_execution() -> None:
    code = _code_without_strings(STUB)

    for forbidden in ("run_scan", "register_jobs", "run_scheduled", "SCAN_WINDOW"):
        assert forbidden not in code, forbidden


def test_stub_code_has_no_refetch_narrative() -> None:
    """代码骨架里不得出现"重抓 / 补扫 / 重新扫描"这类动作名。"""
    code = _code_without_strings(STUB)

    for forbidden in ("refetch", "rescan", "backfill", "backfill_after_close"):
        assert forbidden not in code, forbidden


def test_package_does_not_import_trade_marks_or_push_throttle() -> None:
    """包边界不变。

    `push.py` 在 **tip 上已经** 复用 `app.news.dingtalk.send_markdown` 这个
    **发送函数**（见包 docstring），所以不能一刀切禁掉 `app.news.*`。
    这里守的是真正的那条线：不 import `trade_marks`、不 import `app.news.push`
    这个**节流**模块（冷却参数不许借过来）。
    """
    offenders: list[str] = []
    for path in _sources():
        for mod in _imported_modules(path):
            if mod.startswith("app.services.trade_marks") or mod == "app.news.push":
                offenders.append(f"{path.name}:{mod}")
    assert offenders == [], offenders


def test_package_does_not_import_t_rounds() -> None:
    offenders = [
        f"{path.name}:{mod}"
        for path in _sources()
        for mod in _imported_modules(path)
        if mod.startswith("app.t_rounds")
    ]
    assert offenders == [], offenders


def test_volume_stub_keeps_all_pinned_values() -> None:
    """**零碰 #210 语义**：`volume_breakout_stub.py` 的所有钉死取值都不许动。

    ## 为什么不再用 git diff

    这条原先写的是 `git diff --quiet HEAD -- volume_breakout_stub.py` ——
    「文件相对 HEAD 一个字节都没改」。那个判据在 20261101gevid / 20261101gevid3
    两拍里连续失效：清证据缺口标签**必须**改这个文件（从 pending 集合里删标签、
    同步人读表），而清标签本身是任务书明确要求的动作。用「文件整体不许动」
    去守「取值不许动」，会在每次合法的清标签上误报——**误报和漏报一样坏**，
    它逼后来的人去忽略这条红灯。

    所以改成**逐值**钉：真正要守的从来不是"这个文件没被写过"，而是
    "身份 / 状态 / 闸开关 / 负证据这些取值没被改"。

    ## 仍然守得很紧

    下面每一项都是原值。谁想「顺手统一一下」，无论从哪个方向动，这里都会红。
    标签集合单独由 `test_evidence_pending_*` 系列咬住（清标签是记账变更，
    取值变更才是越界）。
    """
    assert vb.VOLUME_BREAKOUT_SIGNAL_ID == "volume_breakout"
    assert vb.VOLUME_BREAKOUT_EVENT_CLASS == "intraday_volume"
    assert vb.VOLUME_BREAKOUT_FAMILY == "minute_event"
    assert vb.VOLUME_BREAKOUT_AUCTION_STAT_BUCKET is None
    assert vb.VOLUME_BREAKOUT_AUCTION_RULE_ID is None
    # 评估态与生命周期态两个域都不许漂
    assert vb.VOLUME_BREAKOUT_STATUS == "data_pending"
    assert vb.VOLUME_BREAKOUT_RECORD_STATUS == "candidate"
    # 闸开关：本文件永远 Final=False
    assert vb.VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    # 负证据四个口径原样（不是本拍造的数，也不许本拍改）
    assert vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE["source"] == "app/custom/dsa/quant_evidence.yaml"
    assert vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE["key"] == "volume_breakout"
    assert vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE["verdict"] == "strongly_negative"
    assert vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE["returns"] == (
        -23.86,
        -32.44,
        -13.80,
        -19.81,
    )
    assert vb.VOLUME_BREAKOUT_NEGATIVE_EVIDENCE["matched"] == (
        "放量创60日新高",
        "趋势突破",
        "平台整理突破",
        "布林突破",
    )


def test_volume_stub_reports_triggered_none_still() -> None:
    """只读复核放量空桩语义未变（不改它，只断言）。"""
    stub = vb.evaluate_volume_breakout("TEST00.SH", D1)

    assert stub.triggered is None
    assert stub.auction_stat_bucket is None
    assert stub.status == "data_pending"


def test_auction_stub_code_does_not_mutate_volume_module() -> None:
    """stub 代码里不得对 `volume_breakout_stub` 做任何 setattr / 赋值。"""
    code = _code_without_strings(STUB)

    assert "setattr" not in code
    assert "volume_breakout_stub" not in code


def test_whitelist_lock_still_none() -> None:
    """#209 不回退：白名单仍试跑未锁。"""
    assert qr.WHITELIST_PERCENTILE_LOCKED is None


def test_three_env_names_are_unchanged() -> None:
    """三 env 名一个字符都没改。"""
    assert C.PERSIST_ENV_NAME == "BUY_SELL_SIGNALS_PERSIST"
    assert C.PUSH_ENV_NAME == "BUY_SELL_SIGNALS_PUSH"
    assert C.DINGTALK_ENV_NAME == "BUY_SELL_SIGNALS_DINGTALK"


def test_stub_does_not_touch_env_at_all() -> None:
    """stub 里没有任何 env 读取 —— 它不关心开关。"""
    code = _code_without_strings(STUB)

    assert "environ" not in code
    assert "getenv" not in code


def test_auction_scan_service_keeps_its_invariants() -> None:
    """`auction_scan.py` 的**行为不变式**逐项钉死。

    原本这里判的是「文件相对 HEAD 逐字节未改」。批次 20261101gauction 起，
    `build_lists` 的 item 多了 `auction_price` / `prev_close` 两个键 ——
    那个判据会**常红**，而一条长期红的测和一条不存在的测一样没用（误报和漏报
    一样坏）。所以改成**逐值钉**：真正要保的不是"这个文件没人碰过"，
    是"SCAN 窗、helpers 算法、筛选阈值、gap 语义一个都没变"。

    只要有人改了其中任何一项，下面总有一条会红。
    """
    from app.services import auction_scan as S

    # ① SCAN 窗：09:25:30 – 09:28:00，不改；窗外（含盘后）不补跑
    assert dt_time(9, 25, 30) == S.SCAN_START
    assert dt_time(9, 28, 0) == S.SCAN_END
    assert S.MISFIRE_GRACE_S == 60

    # ② 常量口径
    assert S.SESSION_MINUTES == 240
    assert S.SHARES_PER_HAND == 100
    assert S.WIDE_MIN_GAP == 0.03
    assert S.LOOKBACK_DAYS == 5

    # ③ helpers 算法：open_price 优先，否则 last_price；昨收同理；**只认正数**
    assert S._auction_price({"open_price": 18.0, "last_price": 17.5}) == 18.0
    assert S._auction_price({"last_price": 17.5}) == 17.5
    assert S._auction_price({"open_price": 0, "last_price": 17.5}) == 17.5
    assert S._auction_price({}) is None
    assert S._auction_price({"open_price": -1.0, "last_price": -2.0}) is None
    assert S._prev_close({"prev_price": 17.8, "prev_close_price": 17.0}) == 17.8
    assert S._prev_close({"prev_close_price": 17.0}) == 17.0
    assert S._prev_close({}) is None

    # ④ gap 语义不变：price / prev - 1，保留 6 位
    row = {
        "thscode": "600519.SH",
        "open_price": 18.0,
        "prev_price": 17.8,
        "vol_ratio": 2.0,   # 显式给量比 → basis=snapshot_volume_ratio，不依赖 avg_hands
        "volume": 1000.0,
    }
    hits, _wide, _basis = S.build_lists([row], avg_hands={}, gap_min=0.0, ratio_min=0.0)
    assert hits[0]["gap"] == round(18.0 / 17.8 - 1.0, 6)

    # ⑤ 筛选阈值语义不变：gap 低于 floor 不进 hits
    _h, wide, _b = S.build_lists([row], avg_hands={}, gap_min=0.5, ratio_min=0.0)
    assert wide == [] and _h == []

    # ⑥ 没有盘后补扫、没有 09:30 回填。
    # 注意：本拍在源码里**主动写了「禁止用 09:30 回填」这类说明性注释**，
    # 所以不能再用「源码里出现 09:30 就红」这种粗糙扫描（会把禁令本身当违规）。
    # 沿用本文件既有的教训（`_code_without_strings`）：**只扫代码骨架，不扫文本**。
    #   6a 不存在盘后补扫入口：代码骨架里不出现 backfill / 补扫 这些**标识符**；
    #   6b 所有 `time(h, m)` 字面量都不得晚于 SCAN_END（09:28），不许为「补到 09:30」加窗；
    #   6c run_once 在窗外**直接 return**，不进入抓取/落盘分支。
    assert "SCAN_POST_CLOSE" not in dir(S)

    service = BACKEND / "app" / "services" / "auction_scan.py"
    skeleton = _code_without_strings(service)
    assert '"backfill"' not in skeleton.lower()
    assert "backfill" not in skeleton.lower()
    assert "补扫" not in skeleton
    assert "SCAN_POST_CLOSE" not in skeleton

    src = service.read_text(encoding="utf-8")
    tree = _tree(service)

    # 6b：所有时间字面量都不得晚于 SCAN_END（09:28）。
    # 源码里是 `SCAN_START = dt_time(9, 25, 30)` —— 名字是 **id** 不是 attr，
    # 且末段不是 `time`，所以按「调用者名字里含 time」认（`dt_time` / `time` / `datetime.time`）。
    seen_times: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        else:
            callee = getattr(node.func, "id", "")
        if "time" not in callee.lower() or callee.endswith("timestamp"):
            continue
        if len(node.args) < 2:
            continue
        hour, minute = node.args[0], node.args[1]
        if isinstance(hour, ast.Constant) and isinstance(minute, ast.Constant):
            assert isinstance(hour.value, int) and isinstance(minute.value, int)
            seen_times.append((hour.value, minute.value))
    assert len(seen_times) >= 2, f"至少应有 SCAN_START / SCAN_END 两个时间字面量，实际 {seen_times}"
    for hour, minute in seen_times:
        assert dt_time(hour, minute) <= S.SCAN_END

    # 6c：run_scan 窗外**直接 return**（`outside_window`），不进入抓取/落盘分支
    run_scan = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_scan"
    )
    run_src = ast.get_source_segment(src, run_scan) or ""
    assert "in_scan_window" in run_src
    assert '"outside_window"' in run_src
    # _fetch_and_store（真正取数 + 落盘）只能在 run_scan 的窗口判断之后被调用
    lines = src.splitlines()
    gate_line = next(i for i, ln in enumerate(lines) if '"outside_window"' in ln)
    fetch_call = next(
        i for i, ln in enumerate(lines) if "_fetch_and_store(" in ln and "def " not in ln
    )
    assert fetch_call > gate_line, "取数/落盘必须在窗口判断之后"


# ========================================================== ⑧ 文案红线


def test_stub_makes_no_ready_or_locked_claims() -> None:
    """禁「竞价回测已通 / 信号可做 / 正式可推 / 阈值已锁定」这类肯定句。

    只扫**字符串常量与 docstring**；命中处必须同时带否定语（禁止 / 不得 / 未 / 不），
    否则算违规 —— 这样"禁止写'信号可做'"这种正当禁令不会被误判。
    """
    claims = ("回测已通", "信号可做", "正式可推", "阈值已锁定", "已生产验证")
    offenders: list[str] = []
    for path in [STUB, PKG / "__init__.py"]:
        for text in _string_constants(path):
            for claim in claims:
                if claim in text and not any(k in text for k in ("禁止", "不得", "未", "不")):
                    offenders.append(f"{path.name}:{claim}")
    assert offenders == [], offenders


def test_stub_docstring_has_disclaimer() -> None:
    assert "仅供研究，不构成投资建议；执行须人工确认" in STUB.read_text(encoding="utf-8")


def test_stub_docstring_states_the_0930_ban() -> None:
    text = STUB.read_text(encoding="utf-8")

    assert "09:30" in text
    assert "09:25" in text
    assert "永久禁止" in text


def test_stub_docstring_says_partial_unblock_is_not_ready() -> None:
    text = STUB.read_text(encoding="utf-8")

    assert "部分解阻" in text
    assert "B1" in text or "B2" in text


def test_package_init_mentions_gauction2() -> None:
    text = (PKG / "__init__.py").read_text(encoding="utf-8")

    assert "gauction2" in text
    assert "竞价" in text
    assert "不重做放量" in text


# ============================================================== ⑨ 导出


@pytest.mark.parametrize(
    "name",
    [
        "AUCTION_ABNORMAL_AUCTION_STAT_BUCKET",
        "AUCTION_ABNORMAL_EVENT_CLASS",
        "AUCTION_ABNORMAL_EVIDENCE",
        "AUCTION_ABNORMAL_EVIDENCE_PENDING",
        "AUCTION_ABNORMAL_SIGNAL_ID",
        "AUCTION_ABNORMAL_STATUS",
        "AUCTION_ABNORMAL_TRIGGERING_ENABLED",
        "AuctionCandidate",
        "AuctionEvaluation",
        "auction_ts_for",
        "build_auction_abnormal_record",
        "evaluate_auction_abnormal",
    ],
)
def test_public_symbol_is_exported(name: str) -> None:
    import app.buy_sell_signals as pkg

    assert name in pkg.__all__
    assert hasattr(pkg, name)


def test_stub_all_matches_its_definitions() -> None:
    """`__all__` 里的名字都得真的存在（拼错了不会报错，只会 import 失败得更晚）。"""
    for name in A.__all__:
        assert hasattr(A, name), name


# ============================================================ ⑩ 渲染


def test_evaluation_to_dict_is_json_serialisable(scan_root: Path) -> None:
    ev = A.evaluate_auction_abnormal(scan_root, D1)
    payload = ev.to_dict()

    json.dumps(payload, ensure_ascii=False, allow_nan=False)

    assert payload["signal_id"] == "auction_abnormal"
    assert payload["event_class"] == "auction"
    assert payload["auction_stat_bucket"] == "auction_0925"
    assert payload["auction_rule_id"] is None
    assert payload["post_open_noise_flag"] is False
    assert payload["post_open_noise_bucket"] is None
    assert payload["noise_mix_forbidden"] is True
    assert payload["triggered"] is None
    assert payload["triggering_enabled"] is False
    assert payload["auction_ts"] == f"{D1.isoformat()}T09:25:00+08:00"


def test_candidate_to_dict_keeps_price_none_and_bucket_fixed(scan_root: Path) -> None:
    cand = A.evaluate_auction_abnormal(scan_root, D1).candidates[0]
    payload = cand.to_dict()

    json.dumps(payload, ensure_ascii=False, allow_nan=False)

    assert payload["auction_price"] is None
    assert payload["auction_stat_bucket"] == "auction_0925"
    assert payload["post_open_noise_bucket"] is None
    assert payload["auction_ts"] == f"{D1.isoformat()}T09:25:00+08:00"
    assert payload["triggered"] is None


def test_no_data_to_dict_is_json_serialisable(tmp_path: Path) -> None:
    payload = A.evaluate_auction_abnormal(tmp_path / "nope", D1).to_dict()

    json.dumps(payload, ensure_ascii=False, allow_nan=False)

    assert payload["reason"] == "no_data"
    assert payload["candidates"] == []
    assert payload["has_scan"] is False


def test_stub_source_is_ascii_safe_for_json_dumps() -> None:
    """源码能正常 UTF-8 解码（中文注释不破坏读盘）。"""
    raw = STUB.read_bytes()

    assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(raw).hexdigest()
    assert raw.decode("utf-8")


# ========================================================== ⑪ docs 落地


def _docs_text() -> str:
    return (BACKEND.parent / "docs" / "buy-sell-signals-skeleton.md").read_text(encoding="utf-8")


def test_docs_have_a_gauction2_section() -> None:
    text = _docs_text()

    assert "gauction2" in text
    assert "auction_0925" in text


def test_docs_state_b1_partial_and_b2_still_blocked() -> None:
    """文档必须区分 B1（部分解阻）与 B2（仍 blocked），不能一笔带过。"""
    text = _docs_text()

    assert "B1" in text
    assert "B2" in text
    assert "部分解阻" in text
    assert "仍 blocked" in text or "仍然 blocked" in text


def test_docs_ban_0930_impersonation() -> None:
    text = _docs_text()

    assert "09:30" in text
    assert "09:25" in text
    assert "永久禁止" in text


def test_docs_list_all_four_pending_tags() -> None:
    text = _docs_text()

    for tag in A.AUCTION_ABNORMAL_EVIDENCE_PENDING:
        assert tag in text, tag


def test_docs_state_volume_orthogonality() -> None:
    text = _docs_text()

    assert "intraday_volume" in text
    assert "正交" in text


def test_docs_carry_the_disclaimer() -> None:
    assert "仅供研究，不构成投资建议；执行须人工确认" in _docs_text()


def test_docs_mention_red_up_green_down() -> None:
    """红涨绿跌口径。"""
    assert "红涨绿跌" in _docs_text()


def test_docs_say_read_only_and_no_refetch() -> None:
    text = _docs_text()

    assert "只读" in text
    assert "SCAN_WINDOW" in text
    assert "不重抓" in text
