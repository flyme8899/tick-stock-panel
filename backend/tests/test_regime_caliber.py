"""regime 口径版本: 登记、比对、拦截、重算后写 stamp。

回归的是 NOTES.md §2.5 那条"上线必做"原来靠人记的纪律:

> 本 PR 部署后, 必须执行一次 POST /api/regime/recompute 全量重算。

漏做不会报错 —— `regime_history` 里老行老口径、新行新口径, `up_count` / `score` 序列
在那里断一截, 下游(回测 regime_filter、AI 助手、Regime 页)读到的是混合序列, 而
表里没有任何字段说明这件事。这里把纪律变成断言。

三条主线:
1. 指纹跟着常量走 —— 改任何一个口径常量, 指纹必变, 不需要有人记得 bump 版本号。
2. 不一致时**拦截增量**(不补算), 而不是混着算。
3. 只有**全量**重算成功才写 stamp; 区间重算不写(它只统一了一段, 写了就是撒谎)。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services import regime_builder, regime_caliber


@pytest.fixture(autouse=True)
def _isolate_regime_cache():
    """regime_builder 的 history/rows 缓存是模块级全局, 跨用例会串。

    每个用例前后清一次, 否则"上一条用例写过的表"会漏到这一条, 断言就变成
    在测缓存而不是在测口径逻辑。
    """
    regime_builder._REGIME_HISTORY_CACHE = None
    regime_builder._REGIME_HISTORY_SIG = None
    regime_caliber.clear_fingerprint_cache() if hasattr(regime_caliber, "clear_fingerprint_cache") else None
    regime_builder.clear_regime_rows_cache()
    yield
    regime_builder._REGIME_HISTORY_CACHE = None
    regime_builder._REGIME_HISTORY_SIG = None
    regime_builder.clear_regime_rows_cache()


class _FakeRepo:
    class store:
        def __init__(self, data_dir: Path) -> None:
            self.data_dir = data_dir

    def __init__(self, data_dir: Path) -> None:
        self.store = _FakeRepo.store(data_dir)

    def get_enriched_range(self, *args, **kwargs):
        """没有真实 enriched 缓存 —— run_regime_batch 会自己退到扫盘路径。"""
        return None


def _seed_history(data_dir: Path, days: int = 3) -> None:
    """写一张最小的 regime_history, 让"表里有没有行"可以被区分出来。"""
    regime_builder.upsert_regime_history(
        data_dir,
        pl.DataFrame({
            "date": [date(2026, 1, day) for day in range(1, days + 1)],
            "state": ["range"] * days,
            "score": [50] * days,
        }),
    )


def _seed_enriched(data_dir: Path, days: list[str]) -> None:
    enriched = data_dir / "kline_daily_enriched"
    for ds in days:
        d = enriched / f"date={ds}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "part.parquet").write_bytes(b"x")


# --------------------------------------------------------------------------- #
# stamp 的读写与指纹
# --------------------------------------------------------------------------- #

def test_stamp_missing_is_distinct_from_mismatch(tmp_path: Path) -> None:
    """「没登记过」和「口径变了」必须是两个结论。

    混成一个的话, 首次部署(或本特性上线前的历史数据)会被当成"口径变了"而反复
    自动重算 —— 每次启动扫一遍全 A enriched。
    """
    report = regime_caliber.check_caliber(tmp_path)
    assert report["match"] is False
    assert report["reason"] == "missing"
    assert report["stamped"] is None
    assert "未登记" in regime_caliber.describe_drift(report)


def test_write_then_check_matches(tmp_path: Path) -> None:
    """写完 stamp 再查应当一致, 并且带上人读的版本号与指纹。"""
    written = regime_caliber.write_stamp(tmp_path, reason="test")
    report = regime_caliber.check_caliber(tmp_path)
    assert report["match"] is True
    assert report["reason"] == "match"
    assert report["current"]["fingerprint"] == written["fingerprint"]
    assert report["stamped"]["reason"] == "test"
    assert report["changed"] == []


def test_stamp_file_lives_beside_the_parquet(tmp_path: Path) -> None:
    """stamp 跟表同目录 —— 拷数据目录一起搬, 不会出现"表走了、口径记录没走"。"""
    regime_caliber.write_stamp(tmp_path, reason="test")
    path = regime_caliber.stamp_path(tmp_path)
    assert path == tmp_path / "regime_history" / ".regime_caliber.json"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) >= {"version", "fingerprint", "constants", "written_at", "reason"}


def test_corrupt_stamp_reads_as_missing(tmp_path: Path) -> None:
    """半截/坏掉的 stamp 不能把启动搞挂, 读成 None 即可。"""
    path = regime_caliber.stamp_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert regime_caliber.read_stamp(tmp_path) is None
    assert regime_caliber.check_caliber(tmp_path)["reason"] == "missing"


def test_fingerprint_changes_when_any_caliber_constant_changes(monkeypatch) -> None:
    """改任一参与口径的常量, 指纹都要变 —— 不依赖谁记得 bump 版本号。"""
    baseline = regime_caliber.caliber_fingerprint()
    # 逐个常量改一下, 每个都必须让指纹变。
    for name in regime_caliber.CALIBER_CONSTANTS:
        original = getattr(regime_builder, name)
        if isinstance(original, dict):
            mutated = {**original, "__probe__": 1}
        elif isinstance(original, tuple):
            mutated = (original[0] + 0.001, *original[1:])
        elif isinstance(original, str):
            mutated = original + "-test"
        else:
            mutated = original + 1
        monkeypatch.setattr(regime_builder, name, mutated)
        changed = regime_caliber.caliber_fingerprint()
        assert changed != baseline, f"改了 {name} 指纹却没变"
        monkeypatch.undo()


def test_changed_constant_is_named_in_the_report(monkeypatch, tmp_path: Path) -> None:
    """不一致时要说清是**哪个**常量变了、从多少到多少 —— 排查靠这个。"""
    regime_caliber.write_stamp(tmp_path, reason="test")
    monkeypatch.setattr(regime_builder, "BREADTH_OUTLIER", 0.25 + 0.05)
    report = regime_caliber.check_caliber(tmp_path)
    assert report["match"] is False
    assert report["reason"] == "changed"
    assert "BREADTH_OUTLIER" in report["changed"]
    message = regime_caliber.describe_drift(report)
    assert "BREADTH_OUTLIER" in message and "0.3" in message


def test_threshold_values_themselves_are_not_touched() -> None:
    """本特性不该动口径数值本身 —— 冻结阈值有专门的守护测试, 这里再钉一次语义。

    任务书原话: 「**不许改 `BREADTH_OUTLIER` 数值本身**」。
    """
    assert regime_builder.BREADTH_OUTLIER == 0.25
    assert regime_builder.BREADTH_THRESHOLDS == (1.53761, 1.67424)
    assert regime_builder.BREADTH_WINDOW == 20


# --------------------------------------------------------------------------- #
# 增量入口: 口径漂移时拦截
# --------------------------------------------------------------------------- #

def test_incremental_refuses_to_run_when_caliber_changed(monkeypatch, tmp_path: Path) -> None:
    """口径变了而表没重算 → 增量必须**不补算**, 返回空表并留下能查的日志。

    不拦的后果: 新口径的行补进旧口径的表, 两种口径混在同一列里。20 日滚动窗口还会
    把它们平均在一起, 而 `score` 会在这里出现一个来源不明的台阶。
    """
    _seed_history(tmp_path)
    _seed_enriched(tmp_path, ["2026-01-04"])
    regime_caliber.write_stamp(tmp_path, reason="test")
    monkeypatch.setattr(regime_builder, "BREADTH_OUTLIER", 0.3)

    called = {"batch": 0}

    def _should_not_run(*_a, **_kw):
        called["batch"] += 1
        raise AssertionError("口径漂移时不该进入 run_regime_batch")

    monkeypatch.setattr(regime_builder, "run_regime_batch", _should_not_run)

    result = regime_builder.compute_regime_incremental(
        _FakeRepo(tmp_path), tmp_path, today=date(2026, 1, 5),
    )
    assert result.is_empty()
    assert called["batch"] == 0
    # 表没被动过: upsert 没发生, 行数还是 3。
    assert regime_builder.load_regime_history(tmp_path).height == 3


def test_incremental_still_runs_when_no_stamp(tmp_path: Path) -> None:
    """没登记过(首次部署 / 本特性上线前)不算漂移, 增量照常走。

    这里只关心"有没有被误拦"; run_regime_batch 在没有真实 enriched 数据时返回空,
    所以断言落在"进入了计算路径"上。
    """
    _seed_history(tmp_path)
    _seed_enriched(tmp_path, ["2026-01-04"])

    ran = {"count": 0}
    real = regime_builder.run_regime_batch

    def _spy(*args, **kwargs):
        ran["count"] += 1
        return real(*args, **kwargs)

    import app.services.regime_builder as rb

    original = rb.run_regime_batch
    rb.run_regime_batch = _spy
    try:
        regime_builder.compute_regime_incremental(
            _FakeRepo(tmp_path), tmp_path, today=date(2026, 1, 5),
        )
    finally:
        rb.run_regime_batch = original
    assert ran["count"] == 1


def test_incremental_runs_normally_when_caliber_matches(tmp_path: Path) -> None:
    """口径一致时不该被拦: 进入计算路径, 且不会误报。"""
    _seed_history(tmp_path)
    _seed_enriched(tmp_path, ["2026-01-04"])
    regime_caliber.write_stamp(tmp_path, reason="test")

    ran = {"count": 0}
    real = regime_builder.run_regime_batch

    def _spy(*args, **kwargs):
        ran["count"] += 1
        return real(*args, **kwargs)

    import app.services.regime_builder as rb

    original = rb.run_regime_batch
    rb.run_regime_batch = _spy
    try:
        regime_builder.compute_regime_incremental(
            _FakeRepo(tmp_path), tmp_path, today=date(2026, 1, 5),
        )
    finally:
        rb.run_regime_batch = original
    assert ran["count"] == 1


# --------------------------------------------------------------------------- #
# 启动自检
# --------------------------------------------------------------------------- #

def test_startup_check_does_not_recompute(tmp_path: Path, monkeypatch) -> None:
    """启动自检只做判定, 绝不触发重算 —— 启动路径里扫全 A 会把启动拖到分钟级。"""
    regime_caliber.write_stamp(tmp_path, reason="test")
    monkeypatch.setattr(regime_builder, "BREADTH_OUTLIER", 0.3)

    def _boom(*_a, **_kw):
        raise AssertionError("启动自检不该调用 run_regime_batch")

    monkeypatch.setattr(regime_builder, "run_regime_batch", _boom)
    report = regime_caliber.check_caliber(tmp_path)
    assert report["match"] is False
    assert report["reason"] == "changed"


# --------------------------------------------------------------------------- #
# recompute 接口: 成功后写 stamp, 区间重算不写
# --------------------------------------------------------------------------- #

class _StubRepo:
    def __init__(self, data_dir: Path, earliest: date | None) -> None:
        self.store = type("S", (), {"data_dir": data_dir})()
        self._earliest = earliest


def _client(data_dir: Path, earliest: date | None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import regime as regime_api

    app = FastAPI()
    app.include_router(regime_api.router)
    app.state.repo = _StubRepo(data_dir, earliest)
    return TestClient(app), app


@pytest.fixture
def recompute_stub(monkeypatch, tmp_path: Path):
    """把 recompute 链路上的重活全部打桩, 只留 stamp 那一段真逻辑。"""
    from app.services import market_mainline

    calls: list[tuple[date, date]] = []

    def _fake_batch(repo, start, end):
        calls.append((start, end))
        return pl.DataFrame()

    monkeypatch.setattr(regime_builder, "run_regime_batch", _fake_batch)
    monkeypatch.setattr(regime_builder, "refresh_phase_labels", lambda _d: 0)
    monkeypatch.setattr(
        market_mainline, "compute_mainline_range",
        lambda *a, **kw: pl.DataFrame(),
    )
    monkeypatch.setattr(market_mainline, "upsert_mainline_history", lambda *a, **kw: None)
    _seed_history(tmp_path)
    # earliest_enriched_date 是扫盘实现的, 全量重算要靠它定起点 —— 造一个分区出来。
    _seed_enriched(tmp_path, ["2025-12-01"])
    return tmp_path, calls


def test_full_recompute_writes_stamp(recompute_stub) -> None:
    """全量重算成功后写 stamp —— 这是"整张表已经统一口径"的唯一时刻。"""
    data_dir, calls = recompute_stub
    client, _app = _client(data_dir, date(2025, 12, 1))
    resp = client.post("/api/regime/recompute")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["caliber_stamped"] is True
    assert calls == [(date(2025, 12, 1), date.today())]
    report = regime_caliber.check_caliber(data_dir)
    assert report["match"] is True
    assert "recompute" in report["stamped"]["reason"]


def test_partial_recompute_does_not_write_stamp(recompute_stub) -> None:
    """区间重算**不写** stamp: 它只统一了一段, 表里仍是混合口径。

    写了的话下一次启动会把"口径一致"当成事实, 混算就此被永久掩盖 —— 这比不写更糟。
    """
    data_dir, _calls = recompute_stub
    # 先造一个"已变更"的现场
    regime_caliber.write_stamp(data_dir, reason="test")
    from app.api import regime as regime_api

    original = regime_builder.BREADTH_OUTLIER
    regime_builder.BREADTH_OUTLIER = 0.3
    try:
        client, _app = _client(data_dir, date(2025, 12, 1))
        resp = client.post("/api/regime/recompute?start=2026-01-02&end=2026-01-03")
        assert resp.status_code == 200, resp.text
        assert resp.json()["caliber_stamped"] is False
        # stamp 还停在旧口径, 仍然报"不一致"。
        assert regime_caliber.check_caliber(data_dir)["match"] is False
    finally:
        regime_builder.BREADTH_OUTLIER = original


def test_recompute_with_no_enriched_data_does_not_stamp(tmp_path: Path, monkeypatch) -> None:
    """没有 enriched 数据时什么都没算 —— 不能写 stamp 说"已统一口径"。"""
    client, _app = _client(tmp_path, None)
    resp = client.post("/api/regime/recompute")
    assert resp.status_code == 200
    assert resp.json()["caliber_stamped"] is False
    assert regime_caliber.read_stamp(tmp_path) is None


def test_caliber_endpoint_reports_status(recompute_stub) -> None:
    """`GET /api/regime/caliber` 给前端一个只读的判定结果。"""
    data_dir, _calls = recompute_stub
    client, _app = _client(data_dir, date(2025, 12, 1))

    missing = client.get("/api/regime/caliber")
    assert missing.status_code == 200
    assert missing.json()["match"] is False
    assert missing.json()["reason"] == "missing"

    client.post("/api/regime/recompute")
    ok = client.get("/api/regime/caliber").json()
    assert ok["match"] is True
    assert ok["reason"] == "match"
    assert ok["version"] == regime_caliber.REGIME_CALIBER_VERSION
    assert ok["stamped_at"]


# --------------------------------------------------------------------------- #
# NOTES.md 与实现保持一致
# --------------------------------------------------------------------------- #

def test_notes_section_25_points_at_the_automation(tmp_path: Path) -> None:
    """NOTES.md §2.5 的"上线必做"必须写明现在是自动检测的。

    文档还写着"靠人记得手动重算"的话, 下一个人会以为没有这层保护。
    """
    notes = Path(__file__).resolve().parents[2] / "NOTES.md"
    text = notes.read_text(encoding="utf-8")
    section = text.split("## 2.5", 1)[1].split("## 3.", 1)[0]
    assert "caliber" in section.lower() or "口径版本" in section
    assert "自动" in section
    assert "/api/regime/recompute" in section
