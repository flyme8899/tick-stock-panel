"""月度再平衡快照测试(批次 20261025g2 ② B3)。

快照是**账面留痕**, 不是调度器: 不挂 cron、不写盘、不接 Store / main.py。
本文件守:

1. 计数自洽(scored_n ≤ universe_n, whitelist ≤ scored_n)
2. 计数与 `rank_within_industry` 的结果一致
3. `whitelist_percentile_locked` 恒 None; 试图填非空直接抛错
4. 不 import `t_rounds` / 提醒层; 不触碰磁盘
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest
from app.buy_sell_signals.constants import SIGNAL_DESIGN_PIN
from app.buy_sell_signals.quality_proxy import QualityProxy
from app.buy_sell_signals.quality_rank import rank_within_industry
from app.buy_sell_signals.rebalance_snapshot import (
    REBALANCE_ID_PREFIX,
    WHITELIST_COLUMN_TRIAL,
    RebalanceSnapshot,
    RebalanceSnapshotError,
    build_rebalance_snapshot,
    make_rebalance_id,
    score_version,
)

PKG_DIR = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"
ASOF = date(2026, 9, 30)


def _proxy(symbol: str, q_score: int | None, *, missing: bool = False) -> QualityProxy:
    return QualityProxy(symbol=symbol, asof=ASOF, q_score=q_score, q_missing_flag=missing or q_score is None)


def _ranked(scores: dict[str, int | None], missing: dict[str, bool] | None = None, industry: str = "801010"):
    missing = missing or {}
    proxies = [_proxy(sym, s, missing=missing.get(sym, False)) for sym, s in scores.items()]
    return rank_within_industry(proxies, {p.symbol: industry for p in proxies}, asof=ASOF)


# =========================================================== 基本形状


def test_snapshot_counts_match_rank_result():
    """4 只票, 1 只缺分 → scored 3, 其中 3 只的分位是 0 / .5 / 1, p40 过 2 只。"""
    res = _ranked({"A": 1, "B": 2, "C": 3, "D": None})
    snap = build_rebalance_snapshot(pit_asof=ASOF, universe=["A", "B", "C", "D"], rank_result=res)
    assert snap.universe_n == 4
    assert snap.scored_n == 3
    assert snap.whitelist_n_p40 == 2


def test_snapshot_id_and_asof():
    res = _ranked({"A": 1, "B": 2})
    snap = build_rebalance_snapshot(pit_asof=ASOF, universe=["A", "B"], rank_result=res)
    assert snap.rebalance_id == "rebal:2026-09-30"
    assert snap.pit_asof == ASOF


def test_make_rebalance_id_is_stable_per_date():
    """同一 asof 重复跑落到同一个 id, 便于对比两次结果; 不用自增号。"""
    assert make_rebalance_id(ASOF) == make_rebalance_id(ASOF) == f"{REBALANCE_ID_PREFIX}:2026-09-30"


def test_score_version_references_existing_pins():
    """score_version 引用已有口径, 不另造版本号, 也不宣称已验证。"""
    assert SIGNAL_DESIGN_PIN in score_version()
    assert "design_pending" in score_version()
    assert "已测通" not in score_version()


def test_snapshot_to_dict_marks_trial_not_locked():
    res = _ranked({"A": 1, "B": 2})
    payload = build_rebalance_snapshot(pit_asof=ASOF, universe=["A", "B"], rank_result=res).to_dict()
    assert payload[WHITELIST_COLUMN_TRIAL] == 1
    assert payload["whitelist_percentile_trial"] == "p40"
    assert payload["whitelist_percentile_locked"] is None
    assert payload["whitelist_threshold_locked"] is False


def test_whitelist_column_name_carries_trial_label():
    """字段名随试跑档位走 —— 换档之后新旧快照不会长得一模一样。"""
    assert WHITELIST_COLUMN_TRIAL == "whitelist_n_p40"


def test_universe_dedup():
    res = _ranked({"A": 1, "B": 2})
    snap = build_rebalance_snapshot(pit_asof=ASOF, universe=["A", "B", "A", " B "], rank_result=res)
    assert snap.universe_n == 2


def test_universe_subset_of_rank_result():
    """池子只取一部分时, 计数只按池子算, 但 scored 仍以池内条目为准。"""
    res = _ranked({"A": 1, "B": 2, "C": 3, "D": 4})
    snap = build_rebalance_snapshot(pit_asof=ASOF, universe=["C", "D"], rank_result=res)
    assert snap.universe_n == 2
    assert snap.scored_n == 2


# =========================================================== 校验


def test_universe_empty_rejected():
    with pytest.raises(RebalanceSnapshotError, match="universe"):
        build_rebalance_snapshot(pit_asof=ASOF, universe=[], rank_result=_ranked({"A": 1, "B": 2}))


def test_universe_with_unknown_symbol_rejected():
    """池子里有票没算过 → 两份输入不是同一批数据, 静默按"算过"处理会让计数虚高。"""
    with pytest.raises(RebalanceSnapshotError, match="不在 rank_result"):
        build_rebalance_snapshot(pit_asof=ASOF, universe=["A", "ZZZ"], rank_result=_ranked({"A": 1, "B": 2}))


def test_asof_mismatch_rejected():
    with pytest.raises(RebalanceSnapshotError, match="asof"):
        build_rebalance_snapshot(pit_asof=date(2026, 9, 29), universe=["A"], rank_result=_ranked({"A": 1, "B": 2}))


def test_scored_cannot_exceed_universe():
    with pytest.raises(RebalanceSnapshotError, match="scored_n"):
        RebalanceSnapshot(
            rebalance_id="rebal:x", pit_asof=ASOF, score_version="v",
            universe_n=3, scored_n=4, whitelist_n_p40=1,
        )


def test_whitelist_cannot_exceed_scored():
    with pytest.raises(RebalanceSnapshotError, match="whitelist_n_p40"):
        RebalanceSnapshot(
            rebalance_id="rebal:x", pit_asof=ASOF, score_version="v",
            universe_n=5, scored_n=2, whitelist_n_p40=3,
        )


def test_negative_counts_rejected():
    with pytest.raises(RebalanceSnapshotError, match="负"):
        RebalanceSnapshot(
            rebalance_id="rebal:x", pit_asof=ASOF, score_version="v",
            universe_n=-1, scored_n=0, whitelist_n_p40=0,
        )


def test_empty_rebalance_id_rejected():
    with pytest.raises(RebalanceSnapshotError, match="rebalance_id"):
        RebalanceSnapshot(
            rebalance_id="  ", pit_asof=ASOF, score_version="v",
            universe_n=1, scored_n=0, whitelist_n_p40=0,
        )


def test_locked_threshold_rejected_at_construction():
    """本批次不允许出现"已锁定"快照 —— 要锁定必须连着改这里, 那是一次显式决定。"""
    with pytest.raises(RebalanceSnapshotError, match="whitelist_percentile_locked"):
        RebalanceSnapshot(
            rebalance_id="rebal:x", pit_asof=ASOF, score_version="v",
            universe_n=5, scored_n=5, whitelist_n_p40=0,
            whitelist_percentile_locked="p40",
        )


def test_zero_scored_snapshot_is_valid():
    """全池缺分: 快照仍然成立(scored=0, whitelist=0), 只是没有可比较的票。"""
    res = _ranked({"A": None, "B": None})
    snap = build_rebalance_snapshot(pit_asof=ASOF, universe=["A", "B"], rank_result=res)
    assert (snap.universe_n, snap.scored_n, snap.whitelist_n_p40) == (2, 0, 0)


# =========================================================== 隔离


def _identifiers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
                if alias.asname:
                    names.add(alias.asname)
    return names


def test_snapshot_module_does_not_import_persistence_or_neighbours():
    """不碰 Store / main / t_rounds / 提醒层 —— 快照是纯内存结构。"""
    ids = _identifiers(PKG_DIR / "rebalance_snapshot.py")
    for forbidden in (
        "store", "InMemorySignalRegistryStore", "DiskSignalRegistryStore",
        "t_rounds", "trade_marks", "push", "news", "main", "cron", "schedule",
        "VWAP_BAND", "RANGE_BAND", "T_COOLDOWN_MIN",
    ):
        assert forbidden not in ids, f"rebalance_snapshot.py 里出现了 {forbidden!r}"


def test_snapshot_module_has_no_file_io():
    """纯函数工厂不写盘: 模块里不应出现 open / write_parquet / Path 写操作。"""
    text = (PKG_DIR / "rebalance_snapshot.py").read_text(encoding="utf-8")
    for token in ("write_parquet", "write_csv", "open(", "mkdir", "partition_writer"):
        assert token not in text, f"rebalance_snapshot.py 出现了写盘相关符号 {token!r}"
