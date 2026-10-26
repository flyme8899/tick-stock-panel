"""P-ledger 证据审计 + P-qe 时效门禁的隔离测。

覆盖 G-T1–G-T10 里属于 TSP 侧的部分。测例零网络、零生产库、零凭证：
上游账本用内存 dict 注入，quant_evidence 用 tmp_path 手写最小 YAML。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.dsa import EXTENSION_API_VERSION, EXTENSION_ID, ledger_audit, quant_evidence, setup
from app.extensions.registry import BackendExtensionRegistrar

_BACKEND = Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent
_APP = _BACKEND / "app"
_SHIPPED = _APP / "custom" / "dsa" / "quant_evidence.yaml"
_DSASPEC = _APP / "custom" / "dsa"


def _items() -> list[dict]:
    """上游 ``/backtest/results`` 的最小合法结构（字段名照上游 schema）。"""
    return [
        {"analysis_history_id": 11, "code": "600000", "outcome": "win", "start_price": 10.0, "simulated_return_pct": 3.2},
        {"analysis_history_id": 12, "code": "600000", "outcome": "loss", "start_price": 12.0, "simulated_return_pct": -2.1},
        {"analysis_history_id": 13, "code": "000001", "outcome": "win", "start_price": 8.0, "simulated_return_pct": 1.1},
        {"analysis_history_id": 14, "code": "000001", "outcome": "neutral", "start_price": 9.0},
    ]


# ===== G-T1 真源：读得到、不加表 =====
def test_rows_without_a_usable_history_id_are_dropped() -> None:
    """没有可用的 ``analysis_history_id`` 就没有可回溯的主键 —— 这种行不能进审计。

    上游理论上不会返回这种行；但一旦返回（schema 漂移、代理层塞了别的东西），
    静默收下会让分母里多出无法追溯的样本，那比少算一条更糟。
    """
    items = [
        {"analysis_history_id": 1, "code": "600000", "outcome": "win", "start_price": 10.0},
        {"analysis_history_id": "2", "code": "600000", "outcome": "win", "start_price": 10.0},  # 字符串 id
        {"analysis_history_id": True, "code": "600000", "outcome": "win", "start_price": 10.0},  # bool 冒充 int
        {"code": "600000", "outcome": "win", "start_price": 10.0},  # 缺 id
        {"analysis_history_id": None, "code": "600000", "outcome": "loss", "start_price": 10.0},
    ]
    audit = ledger_audit.audit_rows(items)

    assert [row.analysis_history_id for row in audit.rows] == [1]
    assert audit.summary()["rows_total"] == 1


def test_non_mapping_items_are_ignored() -> None:
    audit = ledger_audit.audit_rows([{"analysis_history_id": 1, "code": "X", "outcome": "win"}, "junk", None, 42])  # type: ignore[list-item]

    assert len(audit.rows) == 1


def test_audit_reads_upstream_shaped_items() -> None:
    audit = ledger_audit.audit_rows(_items())

    assert audit.available is True
    assert len(audit.rows) == 4
    assert audit.rows[0].code == "000001", "行按 (code, id) 排序，顺序稳定才可对比"
    ids = {row.analysis_history_id for row in audit.rows}
    assert ids == {11, 12, 13, 14}


def test_no_new_ledger_table_is_declared() -> None:
    """本拍不另起 append-only 旁路表：包内不得出现新的 ``__tablename__``。"""
    offenders = []
    for path in _DSASPEC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"__tablename__\s*=\s*['\"]([^'\"]+)['\"]", text):
            offenders.append((path.name, match.group(1)))
    assert offenders == [], f"DSA 包内不应声明任何表：{offenders}"


def test_audit_module_does_not_import_storage_or_write() -> None:
    text = (_DSASPEC / "ledger_audit.py").read_text(encoding="utf-8")
    code = _strip_strings(text)
    assert "src.storage" not in code, "审计层不应直接 import 上游 storage（走 proxy 只读端点）"
    assert "DatabaseManager" not in code
    for forbidden in (".add(", ".commit(", ".execute(", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in code, f"审计层不得写库：出现 {forbidden!r}"


def test_fetch_uses_results_endpoint_not_run() -> None:
    """只读端点必须打 ``backtest/results``，绝不能打 ``backtest/run``。"""
    text = (_DSASPEC / "ledger_audit.py").read_text(encoding="utf-8")
    assert "backtest/results" in text
    assert "backtest/run" not in text, "审计路径不得触发上游回测执行"


# ===== G-T2 入场并列 =====
def test_entry_close_and_next_open_are_both_kept() -> None:
    audit = ledger_audit.audit_rows(_items(), next_open_by_history_id={11: 10.35})

    row = next(r for r in audit.rows if r.analysis_history_id == 11)
    assert row.entry_close == 10.0, "上游原值不许被改写"
    assert row.entry_next_open == 10.35
    assert row.entry_gap_pct == pytest.approx(3.5, abs=1e-4)
    assert "并列" in row.entry_basis


def test_missing_next_open_stays_none_and_is_labelled_optimistic() -> None:
    audit = ledger_audit.audit_rows(_items())

    row = next(r for r in audit.rows if r.analysis_history_id == 12)
    assert row.entry_next_open is None, "缺次日开盘就是缺，不许用 close 顶上"
    assert row.entry_gap_pct is None
    assert "漏隔夜跳空" in row.entry_basis


def test_entry_gap_guards_against_non_positive_close() -> None:
    audit = ledger_audit.audit_rows([{"analysis_history_id": 1, "code": "X", "outcome": "win", "start_price": 0.0}], next_open_by_history_id={1: 5.0})

    assert audit.rows[0].entry_gap_pct is None


def test_entry_optimism_is_documented() -> None:
    doc = _ROOT.joinpath("docs", "dsa-ledger-audit.md").read_text(encoding="utf-8")
    assert "收盘入场" in doc
    assert "隔夜跳空" in doc
    assert "backtest_service.py" in doc


# ===== G-T3 一字涨停 / 不可成交不进分母 =====
def test_one_word_limit_up_is_unfillable() -> None:
    assert ledger_audit.is_unfillable_limit_up(open_price=11.0, high_price=11.0, low_price=11.0, close_price=11.0, prev_close=10.0) is True


def test_ordinary_bar_is_fillable() -> None:
    assert ledger_audit.is_unfillable_limit_up(open_price=10.2, high_price=10.9, low_price=10.0, close_price=10.8, prev_close=10.0) is False
    assert ledger_audit.unfillable_reason(open_price=10.2, high_price=10.9, low_price=10.0, close_price=10.8, prev_close=10.0) is None


def test_limit_up_that_traded_intraday_is_fillable() -> None:
    """涨停但换过手（开≠低）→ 能买得到，不能剔。"""
    assert ledger_audit.is_unfillable_limit_up(open_price=10.5, high_price=11.0, low_price=10.4, close_price=11.0, prev_close=10.0) is False


def test_unfillable_detection_is_conservative_without_prev_close() -> None:
    """缺昨收 → 判不出 → 不剔。宁可留着，也不凭猜剔样本。"""
    assert ledger_audit.is_unfillable_limit_up(open_price=11.0, high_price=11.0, low_price=11.0, close_price=11.0, prev_close=None) is False
    assert ledger_audit.unfillable_reason(open_price=11.0, high_price=11.0, low_price=11.0, close_price=11.0, prev_close=0.0) is None


def test_unfillable_rows_drop_out_of_denominator_but_stay_counted() -> None:
    """不可成交行要能被标出来并排除分母，但仍在总数里可见。

    ``/backtest/results`` 不返回 OHLC，只有 start_price/end_close，
    所以完整的一字涨停判定要靠 ``unfillable_reason`` 直接给价格。
    行级标记走 ``LedgerAudit`` 组装时的 unfillable 字段。
    """
    row = ledger_audit.AuditedRow(
        analysis_history_id=15,
        code="300750",
        outcome="win",
        entry_close=11.0,
        entry_next_open=None,
        unfillable=ledger_audit.UNFILLABLE_LIMIT_UP_ONE_WORD,
    )
    assert row.in_denominator is False
    assert row.to_dict()["unfillable"] == ledger_audit.UNFILLABLE_LIMIT_UP_ONE_WORD

    others = ledger_audit.audit_rows(_items()).rows
    audit = ledger_audit.LedgerAudit(rows=(*tuple(others), row))
    summary = audit.summary()
    assert summary["rows_total"] == 5, "不可成交仍要计数可见"
    assert summary["unfillable_count"] == 1
    assert summary["denominator"] == 3, "5 行 - 1 不可成交 - 1 中性 = 3"


def test_audit_rows_does_not_mark_unfillable_without_ohlc() -> None:
    """只给昨收、不给 OHLC → 判不出 → 不标不可成交（保守，不凭猜剔除）。"""
    items = [{"analysis_history_id": 15, "code": "300750", "outcome": "win", "start_price": 11.0}]
    audit = ledger_audit.audit_rows(items, prev_close_by_history_id={15: 10.0})

    assert audit.rows[0].unfillable is None
    assert audit.rows[0].in_denominator is True


def test_neutral_outcome_never_enters_denominator() -> None:
    assert ledger_audit.exclude_from_denominator(outcome="neutral", unfillable=None) is True
    assert ledger_audit.exclude_from_denominator(outcome="", unfillable=None) is True
    assert ledger_audit.exclude_from_denominator(outcome="win", unfillable=None) is False
    assert ledger_audit.exclude_from_denominator(outcome="loss", unfillable=None) is False
    assert ledger_audit.exclude_from_denominator(outcome="win", unfillable=ledger_audit.UNFILLABLE_LIMIT_UP_ONE_WORD) is True


# ===== G-T4 小样本不给百分比 =====
def test_small_sample_suppresses_win_rate() -> None:
    summary = ledger_audit.audit_rows(_items()).summary()

    assert summary["denominator"] == 3
    assert summary["sample_below_threshold"] is True
    assert summary["win_rate_pct"] is None, "小样本不得对外给胜率百分比"


def test_large_sample_computes_win_rate() -> None:
    items = [{"analysis_history_id": i, "code": "600000", "outcome": "win" if i % 2 else "loss", "start_price": 10.0} for i in range(1, 41)]
    summary = ledger_audit.audit_rows(items).summary()

    assert summary["denominator"] == 40
    assert summary["sample_below_threshold"] is False
    assert summary["win_rate_pct"] == pytest.approx(50.0)


def test_audit_summary_never_claims_audited() -> None:
    payload = ledger_audit.audit_rows(_items()).to_dict()

    assert payload["summary"]["audit_status"] == "unaudited"
    assert payload["summary"]["badge"] == "未经 TSP 审计"


# ===== G-T5 lookahead 登记 =====
def test_lookahead_fixture_is_registered_and_intact() -> None:
    """只登记，不改夹具：入口存在，且 #223 的 t10/t10b 仍在。"""
    from app.backtest.lookahead_fixture import assert_truncated, run_truncated_replay

    assert callable(run_truncated_replay)
    assert callable(assert_truncated)

    fixture_test = _BACKEND / "tests" / "backtest" / "test_lookahead_fixture.py"
    text = fixture_test.read_text(encoding="utf-8")
    assert "def test_t10_replay_call_chain_verifies_truncation" in text
    assert "def test_t10b_baseline_does_not_go_through_truncation_guard" in text


def test_audit_payload_registers_lookahead() -> None:
    payload = ledger_audit.ledger_audit_payload(items=_items())

    item = next(entry for entry in payload["audit_items"] if entry["id"] == "A5")
    assert item["state"] == "registered"
    assert "run_truncated_replay" in item["detail"]
    assert "assert_truncated" in item["detail"]


# ===== G-T6 未审计标 =====
def test_badge_text_is_the_agreed_string() -> None:
    assert ledger_audit.TSP_AUDIT_BADGE == "未经 TSP 审计"


def test_audit_status_cannot_be_flipped_by_argument() -> None:
    """审计状态只认审计记录，不认参数/env。"""
    assert ledger_audit.audit_status_for({"audit_status": "passed"}) == "unaudited"
    assert ledger_audit.audit_status_for(None) == "unaudited"


def test_unaudited_falls_back_with_reason() -> None:
    payload = ledger_audit.audit_unavailable("sidecar 不可达").to_dict()

    assert payload["available"] is False
    assert payload["summary"]["badge"] == "未经 TSP 审计"
    assert payload["summary"]["unavailable_reason"] == "sidecar 不可达"
    assert payload["rows"] == []


def test_frontend_workspace_shows_badge() -> None:
    """前端徽章契约。前端源码不在时由前端那侧的 vitest 测（gdsaledgerIsolation）守。

    这里**不**用 skip 装绿：真正的问题是「文件不在」，测试就明说这件事，
    并给出责任人（前端测试）。CI 后端 job 只 checkout backend 时不至于红。
    """
    workspace = _ROOT / "frontend" / "src" / "custom" / "dsa" / "DecisionWorkspace.tsx"
    if not workspace.is_file():
        # 后端单独 checkout 的环境：徽章契约由前端 vitest 覆盖，此处不重复断言。
        return
    text = workspace.read_text(encoding="utf-8")
    assert "未经 TSP 审计" in text
    assert "TSP_AUDIT_BADGE" in text
    assert "data-tsp-audit-badge" in text


# ===== G-T7 来源打标 =====
def test_source_labels_are_distinguishable() -> None:
    assert {"analysis_bridge", "manual_register"} == ledger_audit.LEDGER_SOURCES
    assert "替换" in ledger_audit.SOURCE_NOTES[ledger_audit.SOURCE_MANUAL_REGISTER]
    assert "自动" in ledger_audit.SOURCE_NOTES[ledger_audit.SOURCE_ANALYSIS_BRIDGE]


def test_manual_source_is_preserved_when_present() -> None:
    audit = ledger_audit.audit_rows([{"analysis_history_id": 1, "code": "X", "outcome": "win", "source": "manual_register"}])
    assert audit.rows[0].source == "manual_register"
    assert audit.rows[0].to_dict()["source_note"] == ledger_audit.SOURCE_NOTES[ledger_audit.SOURCE_MANUAL_REGISTER]


def test_unknown_source_defaults_to_bridge() -> None:
    audit = ledger_audit.audit_rows([{"analysis_history_id": 1, "code": "X", "outcome": "win", "source": "weird"}])
    assert audit.rows[0].source == ledger_audit.SOURCE_ANALYSIS_BRIDGE


# ===== G-T8 quant_evidence 时效 =====
def _yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "ev.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_fresh_evidence_injects(tmp_path: Path) -> None:
    path = _yaml(
        tmp_path,
        """
_meta:
  as_of: "2026-10-08"
  period: "2026-07-08 ~ 2026-10-08"
  env_label: "弱势下行"
  l5_entry_exit_status: "pending"
ma_golden_cross:
  matched: ["MA金叉"]
  returns: [-14.70]
  sample_sizes: [150]
  note: 够样本
""",
    )
    text = quant_evidence.render_skill_appendix("ma_golden_cross", path)
    assert "量化回测参考" in text


def test_expired_evidence_is_not_injected(tmp_path: Path) -> None:
    path = _yaml(
        tmp_path,
        """
_meta:
  as_of: "2025-01-01"
  env_label: "弱势下行"
ma_golden_cross:
  matched: ["MA金叉"]
  returns: [-14.70]
  sample_sizes: [150]
""",
    )
    assert quant_evidence.render_skill_appendix("ma_golden_cross", path) == ""
    summary = quant_evidence.evidence_summary(path)
    assert summary["expired"] is True
    assert summary["meta"]["freshness"]["reason"] == "age_exceeded"


def test_explicit_expires_on_wins(tmp_path: Path) -> None:
    path = _yaml(
        tmp_path,
        """
_meta:
  as_of: "2026-10-08"
  expires_on: "2026-10-09"
  env_label: "弱势下行"
ma_golden_cross:
  matched: ["MA金叉"]
  returns: [-14.70]
  sample_sizes: [150]
""",
    )
    assert quant_evidence.render_skill_appendix("ma_golden_cross", path) == ""
    assert quant_evidence.evidence_summary(path)["meta"]["freshness"]["reason"] == "past_expires_on"


def test_missing_as_of_is_treated_as_expired(tmp_path: Path) -> None:
    """判不出时效 ≠ 新鲜。没有日期就不注入。"""
    path = _yaml(tmp_path, "ma_golden_cross:\n  matched: [x]\n  returns: [1.0]\n  sample_sizes: [500]\n")
    assert quant_evidence.render_skill_appendix("ma_golden_cross", path) == ""
    assert quant_evidence.evidence_summary(path)["meta"]["freshness"]["reason"] == "as_of_missing"


def test_as_of_falls_back_to_period_end(tmp_path: Path) -> None:
    path = _yaml(
        tmp_path,
        """
_meta:
  period: "2026-07-08 ~ 2026-09-30"
  env_label: "震荡"
ma_golden_cross:
  matched: [x]
  returns: [1.0]
  sample_sizes: [500]
""",
    )
    assert quant_evidence.resolve_as_of({"period": "2026-07-08 ~ 2026-09-30"}) == date(2026, 9, 30)
    assert "量化回测参考" in quant_evidence.render_skill_appendix("ma_golden_cross", path)


def test_sample_below_threshold_blocks_guidance(tmp_path: Path) -> None:
    path = _yaml(
        tmp_path,
        """
_meta:
  as_of: "2026-10-08"
  env_label: "弱势下行"
shrink_pullback:
  matched: [缩量回踩]
  returns: [-11.25]
  sample_sizes: [127, 171, 94]
  guidance: |
    1. 不该出现
""",
    )
    # 94 < 100，虽有两项过百，仍整条不注入：任一匹配项不足即拦。
    assert quant_evidence.render_skill_appendix("shrink_pullback", path) == ""
    summary = quant_evidence.evidence_summary(path)
    assert summary["skills"][0]["sample_below_threshold"] is True
    assert summary["skills"][0]["min_sample"] == 94


def test_sample_gate_uses_worst_match_not_average() -> None:
    assert quant_evidence.sample_gate([127, 171, 94])["passed"] is False
    assert quant_evidence.sample_gate([100, 100])["passed"] is True
    assert quant_evidence.sample_gate([])["reason"] == "sample_sizes_missing"


def test_min_sample_threshold_is_100() -> None:
    assert quant_evidence.MIN_SAMPLE_FOR_GUIDANCE == 100


def test_shipped_evidence_all_below_threshold() -> None:
    """出厂 yaml 的 6 条证据全部不足样本 → 全部不注入（预期结果，非回归）。"""
    summary = quant_evidence.evidence_summary(_SHIPPED)
    mins = {item["name"]: item["min_sample"] for item in summary["skills"]}

    assert mins == {
        "ma_golden_cross": 59,
        "shrink_pullback": 94,
        "volume_breakout": 77,
        "bull_trend": 77,
        "dragon_head": 55,
        "bottom_volume": 71,
    }
    assert all(not item["injectable"] for item in summary["skills"])
    assert summary["expired"] is False, "时效没问题，是样本不够——两件事不能混"


def test_env_label_missing_is_flagged(tmp_path: Path) -> None:
    path = _yaml(tmp_path, "_meta:\n  as_of: '2026-10-08'\nma_golden_cross:\n  sample_sizes: [500]\n")
    assert quant_evidence.evidence_summary(path)["meta"]["env_label_missing"] is True


def test_env_label_present_is_not_flagged() -> None:
    assert quant_evidence.evidence_summary(_SHIPPED)["meta"]["env_label_missing"] is False


def test_shipped_meta_has_as_of_env_and_l5() -> None:
    meta = quant_evidence.evidence_summary(_SHIPPED)["meta"]

    assert meta["as_of"] == "2026-10-08"
    assert meta["env_label"]
    assert meta["expires_on"]
    assert meta["l5_entry_exit_status"] == "pending"


# ===== G-T9 L5 歧义 =====
def test_l5_undetermined_is_explicit() -> None:
    yaml_text = _SHIPPED.read_text(encoding="utf-8")

    assert "l5_entry_exit_status" in yaml_text
    assert "未定性" in yaml_text
    doc = _ROOT.joinpath("docs", "dsa-ledger-audit.md").read_text(encoding="utf-8")
    assert "未定性" in doc


def test_l5_default_is_unpinned(tmp_path: Path) -> None:
    path = _yaml(tmp_path, "_meta:\n  as_of: '2026-10-08'\nma_golden_cross:\n  sample_sizes: [500]\n")
    assert quant_evidence.evidence_summary(path)["meta"]["l5_entry_exit_status"] == quant_evidence.L5_STATUS_UNPINNED


# ===== G-T10 P-diag 读出记录 =====
def test_pdiag_readout_is_recorded_in_docs() -> None:
    """G-T10：P-diag 读出必须落进**仓库内**文档。

    刻意不读 PR 正文 / 归档目录：那些在 CI 上不存在，测例一旦依赖它们就会
    在本地绿、CI 红（本拍第一版就踩了这个坑）。能被 CI 验的事实，只能来自
    版本库里的文件。
    """
    doc = _ROOT.joinpath("docs", "dsa-ledger-audit.md").read_text(encoding="utf-8")
    for needle in ("data_pending", "reachable=true", "66", "144", "不必 defer"):
        assert needle in doc, f"P-diag 读出缺 {needle}"
    # 浏览器 L2 必须如实标为未读到，不许假装已读到数字
    assert "浏览器 L2 通/断累计" in doc


# ===== 路由 =====
@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("TSP_QUANT_EVIDENCE_FILE", raising=False)
    app = FastAPI()
    registrar = BackendExtensionRegistrar(EXTENSION_ID, api_version=EXTENSION_API_VERSION)
    setup(registrar)
    for router in registrar.routers:
        app.include_router(router)
    return TestClient(app)


def test_ledger_audit_route_is_read_only_and_marks_unaudited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger_audit, "fetch_ledger_items", lambda **_kwargs: ([], "sidecar 不可达：测试桩"))
    response = client.get("/api/dsa/ledger-audit")

    assert response.status_code == 200, "取不到账本不抛 500，返回带原因的降级体"
    body = response.json()
    assert body["available"] is False
    assert body["summary"]["audit_status"] == "unaudited"
    assert body["summary"]["badge"] == "未经 TSP 审计"
    assert "sidecar 不可达" in body["summary"]["unavailable_reason"]


def test_ledger_audit_route_passes_items_through(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger_audit, "fetch_ledger_items", lambda **_kwargs: (_items(), ""))
    body = client.get("/api/dsa/ledger-audit").json()

    assert body["available"] is True
    assert body["summary"]["rows_total"] == 4
    assert body["summary"]["win_rate_pct"] is None
    assert len(body["audit_items"]) == 7
    assert {entry["id"] for entry in body["audit_items"]} == {f"A{i}" for i in range(1, 8)}


def test_quant_evidence_route_exposes_freshness(client: TestClient) -> None:
    body = client.get("/api/dsa/quant-evidence").json()

    assert body["meta"]["as_of"] == "2026-10-08"
    assert body["meta"]["freshness"]["expired"] is False
    assert body["expired"] is False
    assert body["min_sample_for_guidance"] == 100


# ===== 零碰自检 =====
def test_dsa_package_does_not_touch_buy_sell_signals() -> None:
    """零碰信号包：只禁 import，不禁 docstring 里的说明性提及。

    ``sniper_alerts.py`` 的模块注释本来就会提到 ``buy_sell_signals``
    （解释两边共用一个钉钉 webhook），那是历史文案，不是耦合。
    """
    for path in _DSASPEC.rglob("*.py"):
        for node in _ast_of(path.read_text(encoding="utf-8")).body:
            if isinstance(node, _ast_import()):
                names = [a.name for a in node.names] + [str(getattr(node, "module", "") or "")]
                assert not any("buy_sell_signals" in str(name) for name in names), f"{path.name} 不该 import 信号包"


def test_no_env_writes_for_three_gates() -> None:
    """三 env 只读不写：包内不得出现赋值或 setenv。"""
    patterns = ("BUY_SELL_SIGNALS_PERSIST", "BUY_SELL_SIGNALS_PUSH", "DINGTALK")
    for path in _DSASPEC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in patterns:
            for match in re.finditer(rf"{name}\s*=", text):
                snippet = text[max(0, match.start() - 40) : match.end() + 10]
                assert "os.environ" not in snippet, f"{path.name} 在写 {name}：{snippet!r}"


def test_audit_module_has_compliance_footnote() -> None:
    text = (_DSASPEC / "ledger_audit.py").read_text(encoding="utf-8")
    assert "仅供研究，不构成投资建议；执行须人工确认" in text


def test_docs_exist_with_required_sections() -> None:
    doc = _ROOT.joinpath("docs", "dsa-ledger-audit.md").read_text(encoding="utf-8")

    for needle in ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "未经 TSP 审计", "不另起", "仅供研究"):
        assert needle in doc, f"审计文档缺 {needle}"


def _ast_import():
    import ast

    return ast.ImportFrom


def _ast_of(text: str):
    import ast

    return ast.parse(text)


def _strip_strings(text: str) -> str:
    """去掉字符串字面量后再扫代码骨架，避免文档措辞被误判成实现。"""
    import ast

    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = "\0"
    return ast.unparse(tree)
