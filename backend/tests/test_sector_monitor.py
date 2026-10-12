from __future__ import annotations

import time
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import sector_monitor
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField
from app.services.sector_monitor import SectorMonitorService
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


class _Repo:
    def __init__(self, data_dir, indices: pl.DataFrame | None = None):
        self.store = SimpleNamespace(data_dir=data_dir)
        self._indices = indices if indices is not None else pl.DataFrame()

    def get_index_instruments(self) -> pl.DataFrame:
        return self._indices


def _index_target(symbol: str, name: str) -> dict:
    return {
        "key": f"index:{symbol}",
        "kind": "index",
        "name": name,
        "symbol": symbol,
    }


def _sector_rule(targets: list[dict], **overrides) -> dict:
    rule = {
        "id": "r_sector",
        "name": "板块监控",
        "enabled": True,
        "type": "sector",
        "scope": "all",
        "sector_kind": targets[0]["kind"],
        "sector_targets": targets,
        "sector_trigger": "change_pct",
        "direction": "up",
        "threshold_pct": 1.0,
        "window_minutes": 5,
        "cooldown_seconds": 0,
        "severity": "info",
    }
    rule.update(overrides)
    return monitor_rules.normalize(rule)


def test_validate_accepts_sector_rule_and_rejects_mixed_target_kinds():
    rule = _sector_rule([_index_target("000001.SH", "上证指数")])
    monitor_rules.validate(rule)

    mixed = _sector_rule([
        _index_target("000001.SH", "上证指数"),
        {
            "key": "concept:test:field:人工智能",
            "kind": "concept",
            "name": "人工智能",
            "source_id": "test",
            "field": "field",
            "value": "人工智能",
        },
    ])
    try:
        monitor_rules.validate(mixed)
    except ValueError as exc:
        assert "类型" in str(exc)
    else:
        raise AssertionError("混合板块类型必须被拒绝")


def test_dimension_values_preserve_names_with_spaces_and_filter_nulls(tmp_path):
    service = SectorMonitorService(_Repo(tmp_path))

    assert service._dimension_values("中国AI 50;6G概念") == ["中国AI 50", "6G概念"]
    assert service._dimension_values("nan") == []
    assert service._dimension_values(float("nan")) == []
    assert service._industry_paths("电子-半导体-数字芯片设计")[-1] == (
        "电子-半导体-数字芯片设计", 3, "电子 / 半导体 / 数字芯片设计",
    )


def test_index_targets_are_evaluated_independently(tmp_path):
    repo = _Repo(tmp_path)
    service = SectorMonitorService(repo)
    engine = MonitorRuleEngine()
    engine.set_sector_monitor_service(service)
    sh = _index_target("000001.SH", "上证指数")
    cyb = _index_target("399006.SZ", "创业板指")
    engine.set_rules([_sector_rule([sh, cyb])])

    first = pl.DataFrame({
        "symbol": ["000001.SH", "399006.SZ"],
        "name": ["上证指数", "创业板指"],
        "close": [3000.0, 2000.0],
        "change_pct": [0.8, 0.9],
    })
    assert engine.evaluate_sectors(pl.DataFrame(), first, now=1000.0) == []

    second = first.with_columns(
        pl.Series("change_pct", [1.2, 0.95]),
    )
    events = engine.evaluate_sectors(pl.DataFrame(), second, now=1006.0)

    assert [event["sector_name"] for event in events] == ["上证指数"]
    assert events[0]["change_pct"] == 0.012


def test_index_targets_always_available(tmp_path):
    """指数标的恒可实时监控: quote_service 对核心四只 + 启用规则标的显式拉取,
    不再有按偏好池翻转 available 的行为。"""
    service = SectorMonitorService(_Repo(tmp_path))
    targets = {target["symbol"]: target for target in service.list_targets()["index"]}
    assert targets["000001.SH"]["available"] is True
    assert targets["399006.SZ"]["available"] is True

    first = targets["000001.SH"]
    initial_quote = pl.DataFrame({"symbol": ["000001.SH"], "change_pct": [0.2]})
    service.build_snapshots(pl.DataFrame(), initial_quote, [first], {5}, now=1000.0)

    changed_quote = pl.DataFrame({"symbol": ["000001.SH"], "change_pct": [1.3]})
    snapshot = service.build_snapshots(
        pl.DataFrame(), changed_quote, [first], {5}, now=1300.0,
    )
    assert snapshot["index:000001.SH"]["window_changes"][5] is not None


def test_concept_snapshot_uses_member_average_and_full_window(tmp_path):
    config = ExtConfig(
        id="concept_test",
        label="概念测试",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("concept", "string", "所属概念"),
        ],
    )
    ExtConfigStore(tmp_path).upsert(config)
    ext_dir = tmp_path / "ext_data" / config.id
    pl.DataFrame({
        "symbol": ["A", "B", "C", "D", "E"],
        "concept": ["人工智能", "人工智能", "人工智能", "人工智能", "人工智能"],
    }).write_parquet(ext_dir / "part.parquet")

    service = SectorMonitorService(_Repo(tmp_path))
    target = next(
        target for target in service.list_targets()["concept"]
        if target["name"] == "人工智能"
    )
    first = pl.DataFrame({
        "symbol": ["A", "B", "C", "D", "E"],
        "name": ["甲", "乙", "丙", "丁", "戊"],
        "close": [10.0] * 5,
        "change_pct": [0.01, 0.02, 0.03, -0.01, 0.0],
    })
    snapshots = service.build_snapshots(first, pl.DataFrame(), [target], {5}, now=1000.0)
    assert snapshots[target["key"]]["change_pct"] == pytest.approx(0.01)
    assert snapshots[target["key"]]["coverage_ratio"] == 1.0
    assert snapshots[target["key"]]["window_changes"][5] is None

    second = first.with_columns((pl.col("change_pct") + 0.01).alias("change_pct"))
    too_early = service.build_snapshots(second, pl.DataFrame(), [target], {5}, now=1240.0)
    assert too_early[target["key"]]["window_changes"][5] is None

    unrelated = ExtConfig(
        id="hot_test",
        label="热度测试",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("heat", "float", "市场热度"),
        ],
    )
    ExtConfigStore(tmp_path).upsert(unrelated)
    unrelated_dir = tmp_path / "ext_data" / unrelated.id
    pl.DataFrame({"symbol": ["A"], "heat": [1.0]}).write_parquet(unrelated_dir / "part.parquet")

    complete = service.build_snapshots(second, pl.DataFrame(), [target], {5}, now=1300.0)
    assert complete[target["key"]]["window_changes"][5] == pytest.approx(0.01)


def test_momentum_rule_triggers_after_complete_window(tmp_path):
    service = SectorMonitorService(_Repo(tmp_path))
    engine = MonitorRuleEngine()
    engine.set_sector_monitor_service(service)
    target = _index_target("000001.SH", "上证指数")
    engine.set_rules([_sector_rule(
        [target],
        sector_trigger="momentum",
        threshold_pct=1.0,
        window_minutes=5,
    )])
    start = pl.DataFrame({
        "symbol": ["000001.SH"],
        "name": ["上证指数"],
        "close": [3000.0],
        "change_pct": [0.2],
    })
    assert engine.evaluate_sectors(pl.DataFrame(), start, now=1000.0) == []

    early = start.with_columns(pl.lit(1.3).alias("change_pct"))
    assert engine.evaluate_sectors(pl.DataFrame(), early, now=1240.0) == []

    events = engine.evaluate_sectors(pl.DataFrame(), early, now=1300.0)
    assert len(events) == 1
    assert events[0]["type"] == "sector_momentum_up"
    assert events[0]["window_change_pct"] == pytest.approx(0.011)


def _all_a_rule(**overrides) -> dict:
    rule = {
        "id": "r_alla",
        "name": "全A",
        "enabled": True,
        "type": "sector",
        "sector_kind": "industry",
        "sector_scope": "all_a",
        "sector_targets": [],
        "sector_trigger": "momentum",
        "direction": "up",
        "threshold_pct": 1.0,
        "window_minutes": 5,
        "min_valid_count": 5,
        "min_coverage_pct": 80,
        "cooldown_seconds": 0,
        "severity": "info",
    }
    rule.update(overrides)
    return monitor_rules.normalize(rule)


def _write_membership(tmp_path, rows: dict[str, list]) -> None:
    config = ExtConfig(
        id="board_test",
        label="板块测试",
        mode="snapshot",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("concept", "string", "所属概念"),
            ExtField("industry", "string", "所属行业"),
        ],
    )
    ExtConfigStore(tmp_path).upsert(config)
    ext_dir = tmp_path / "ext_data" / config.id
    pl.DataFrame(rows).write_parquet(ext_dir / "part.parquet")


def _all_a_target(min_valid: int = 5, min_coverage: float = 0.8) -> dict:
    return {
        "key": SectorMonitorService.all_a_snapshot_key(min_valid, min_coverage),
        "kind": "all_a",
        "name": "全A",
        "min_valid_count": min_valid,
        "min_coverage": min_coverage,
    }


def test_validate_all_a_is_exclusive_and_normal_cap_stays():
    monitor_rules.validate(_all_a_rule())
    with pytest.raises(ValueError, match="互斥"):
        monitor_rules.validate(_all_a_rule(sector_targets=[
            {"key": "industry:x", "kind": "industry", "name": "电子"},
        ]))
    with pytest.raises(ValueError, match="全A"):
        monitor_rules.validate(_all_a_rule(sector_kind="index"))
    with pytest.raises(ValueError, match="有效成分"):
        monitor_rules.validate(_all_a_rule(min_valid_count=True))
    with pytest.raises(ValueError, match="覆盖"):
        monitor_rules.validate(_all_a_rule(min_coverage_pct=0))
    too_many = [_index_target(f"{i:06d}.SH", f"指数{i}") for i in range(21)]
    with pytest.raises(ValueError, match="20"):
        monitor_rules.validate(_sector_rule(too_many))


def test_all_a_equal_weight_dedupes_members_and_ignores_outside_quotes(tmp_path):
    _write_membership(tmp_path, {
        "symbol": ["A", "B", "C", "D", "E"],
        "concept": ["人工智能"] * 5,
        "industry": ["电子"] * 5,
    })
    service = SectorMonitorService(_Repo(tmp_path))
    assert service.all_a_member_count() == 5
    quotes = pl.DataFrame({
        "symbol": ["A", "A", "B", "C", "D", "E", "Z"],
        "name": ["甲", "甲2", "乙", "丙", "丁", "戊", "外人"],
        "change_pct": [0.01, 0.50, 0.02, 0.03, -0.01, 0.00, 0.90],
    })
    loose = _all_a_target(5, 0.8)
    strict = _all_a_target(6, 0.8)
    calls = {"n": 0}
    original = service._all_a_stats

    def _counted(frame):
        calls["n"] += 1
        return original(frame)

    service._all_a_stats = _counted
    snapshots = service.build_snapshots(quotes, pl.DataFrame(), [loose, strict], {5}, now=1000.0)
    assert calls["n"] == 1
    got = snapshots[loose["key"]]
    assert got["change_pct"] == pytest.approx(0.01)
    assert got["valid_count"] == 5
    assert got["total_count"] == 5
    assert got["coverage_ratio"] == pytest.approx(1.0)
    assert got["up_count"] == 3
    assert got["down_count"] == 1
    assert got["leader"]["symbol"] == "C"
    assert got["leader"]["name"] == "丙"
    assert got["valid"] is True
    assert snapshots[strict["key"]]["valid"] is False
    assert snapshots[strict["key"]]["change_pct"] == pytest.approx(0.01)


def test_all_a_momentum_uses_cached_quotes_and_same_window_gate(tmp_path, monkeypatch):
    _write_membership(tmp_path, {
        "symbol": ["A", "B", "C", "D", "E"],
        "concept": ["人工智能"] * 5,
        "industry": ["电子", "", "", "", ""],
    })
    service = SectorMonitorService(_Repo(tmp_path))
    service.list_targets()

    def _boom(*_args, **_kwargs):
        raise AssertionError("全A 评估不应重读成分 parquet")

    monkeypatch.setattr(sector_monitor.pl, "read_parquet", _boom)
    engine = MonitorRuleEngine()
    engine.set_sector_monitor_service(service)
    engine.set_rules([_all_a_rule()])

    def _frame(changes: list[float], symbols: list[str] | None = None) -> pl.DataFrame:
        names = symbols or ["A", "B", "C", "D", "E"]
        return pl.DataFrame({
            "symbol": names,
            "name": names,
            "close": [10.0] * len(names),
            "change_pct": changes,
        })

    # 覆盖率 3/5 不够 80%, 不能当作快速异动的基线
    sparse = _frame([0.20, 0.20, 0.20], ["A", "B", "C"])
    assert engine.evaluate_sectors(sparse, pl.DataFrame(), now=1000.0) == []
    # 第一次凑满覆盖只建立基线
    base = _frame([0.01, 0.01, 0.01, 0.01, 0.01])
    assert engine.evaluate_sectors(base, pl.DataFrame(), now=1300.0) == []
    moved = _frame([0.02, 0.02, 0.02, 0.02, 0.02])
    events = engine.evaluate_sectors(moved, pl.DataFrame(), now=1600.0)
    assert len(events) == 1
    assert events[0]["type"] == "sector_momentum_up"
    assert events[0]["sector_kind"] == "all_a"
    assert events[0]["sector_name"] == "全A"
    assert events[0]["window_change_pct"] == pytest.approx(0.01)
    assert events[0]["change_pct"] == pytest.approx(0.02)
    assert "全A" in events[0]["message"]
    assert "覆盖 100%" in events[0]["message"]


def test_all_a_respects_configured_coverage_and_does_not_rescan(tmp_path, monkeypatch):
    symbols = [f"{i:06d}.SZ" for i in range(5000)]
    _write_membership(tmp_path, {
        "symbol": symbols,
        "concept": ["全市场"] * len(symbols),
        "industry": ["综合"] * len(symbols),
    })
    service = SectorMonitorService(_Repo(tmp_path))
    assert service.all_a_member_count() == 5000

    def _boom(*_args, **_kwargs):
        raise AssertionError("全市场聚合不应每轮扫库")

    monkeypatch.setattr(sector_monitor.pl, "read_parquet", _boom)
    quotes = pl.DataFrame({
        "symbol": symbols,
        "change_pct": [0.01] * len(symbols),
    })
    loose = _all_a_target(5, 0.8)
    strict = _all_a_target(5, 0.99)
    # 只给 80% 行情时, 默认门槛通过, 99% 门槛不通过。两次门槛共用一次聚合。
    partial = quotes.head(4000)
    started = time.perf_counter()
    snapshots = service.build_snapshots(partial, pl.DataFrame(), [loose, strict], {1}, now=1000.0)
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0
    assert snapshots[loose["key"]]["valid"] is True
    assert snapshots[loose["key"]]["valid_count"] == 4000
    assert snapshots[loose["key"]]["coverage_ratio"] == pytest.approx(0.8)
    assert snapshots[loose["key"]]["change_pct"] == pytest.approx(0.01)
    assert snapshots[strict["key"]]["valid"] is False
    assert snapshots[strict["key"]]["window_changes"][1] is None
