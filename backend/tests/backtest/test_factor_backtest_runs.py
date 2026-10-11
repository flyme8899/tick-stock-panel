"""因子回测历史：落盘、保留上限、异步提交与同步接口兼容。"""

from __future__ import annotations

import stat
import threading
import time
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import backtest as bt
from app.services import factor_backtest_runs as runs
from app.services.factor_backtest_runs import (
    FactorBacktestJobManager,
    FactorBacktestRunStore,
    align_owner,
    json_safe,
)
from app.services.heavy_job_limiter import HeavyJobLimiter


def _request(manager, username="alice"):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(factor_backtest_jobs=manager)),
        state=SimpleNamespace(auth_user=username),
    )


def _manager(tmp_path, runner):
    return FactorBacktestJobManager(tmp_path, worker_runner=runner)


def test_json_safe_turns_non_finite_into_null():
    assert json_safe({"ic": float("nan"), "nested": [float("inf"), 1]}) == {
        "ic": None,
        "nested": [None, 1],
    }


def test_store_roundtrip_prune_and_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(runs, "MAX_RETAINED_RUNS", 2)
    data = tmp_path / "data"
    data.mkdir()
    store = FactorBacktestRunStore(data)
    first = store.create("factor", {"factor_name": "momentum_5d", "start": "2026-01-01"}, "alice")
    store.transition(first["run_id"], "running")
    store.complete(
        first["run_id"], {"ic_mean": 0.1, "ic_series": [{"ic": float("nan")}]}, elapsed_ms=3
    )
    second = store.create("batch", {"factor_names": ["a", "b"]}, "bob")
    store.transition(second["run_id"], "running")
    store.complete(second["run_id"], {"results": [{}, {}], "n_symbols": 4}, elapsed_ms=4)
    third = store.create("factor", {"factor_name": "rsi_14"}, None)
    store.transition(third["run_id"], "running")
    store.complete(third["run_id"], {"ic_mean": 0.2}, elapsed_ms=5)

    listed = store.list_runs()
    assert [item["run_id"] for item in listed] == [third["run_id"], second["run_id"]]
    assert "result" not in listed[0]
    loaded = store.get(third["run_id"], include_result=True)
    assert loaded["username"] is None
    assert loaded["result"]["ic_mean"] == 0.2
    oldest = store.get(first["run_id"])
    assert oldest is None
    saved = store.get(second["run_id"], include_result=True)
    assert saved["result"]["n_symbols"] == 4
    assert saved["summary"]["n_factors"] == 2
    assert (
        data / "research" / "factor_backtest" / "runs" / third["run_id"] / "result.json"
    ).is_file()

    calls = []

    class _Stat:
        def __init__(self, uid, gid):
            self.st_uid = uid
            self.st_gid = gid
            self.st_mode = stat.S_IFDIR

    def fake_stat(self, *, follow_symlinks=True):
        if self == data:
            return _Stat(42, 43)
        return _Stat(1, 1)

    monkeypatch.setattr(runs.Path, "stat", fake_stat)
    monkeypatch.setattr(runs.os, "chown", lambda path, uid, gid: calls.append((uid, gid)))
    align_owner(data / "research", data)
    assert (42, 43) in calls


def test_delete_missing_invalid_and_busy(tmp_path):
    store = FactorBacktestRunStore(tmp_path)
    created = store.create("factor", {"factor_name": "momentum_5d"}, "alice")
    with pytest.raises(runs.FactorRunBusyError):
        store.delete(created["run_id"])
    with pytest.raises(KeyError):
        store.delete("a" * 32)
    with pytest.raises(runs.FactorRunValidationError):
        store.delete("../escape")
    store.transition(created["run_id"], "running")
    store.fail(created["run_id"], RuntimeError("boom"), elapsed_ms=1)
    store.delete(created["run_id"])
    assert store.get(created["run_id"]) is None


def test_recover_interrupted_marks_active_runs(tmp_path):
    store = FactorBacktestRunStore(tmp_path)
    queued = store.create("factor", {"factor_name": "momentum_5d"}, "alice")
    running = store.create("batch", {"factor_names": ["a"]}, "alice")
    store.transition(running["run_id"], "running")
    done = store.create("factor", {"factor_name": "rsi_14"}, "alice")
    store.transition(done["run_id"], "running")
    store.complete(done["run_id"], {"ic_mean": 0.1}, elapsed_ms=1)

    assert store.recover_interrupted() == 2
    assert store.get(queued["run_id"])["status"] == "interrupted"
    assert "重启" in store.get(running["run_id"])["error"]
    assert store.get(done["run_id"])["status"] == "succeeded"


def test_submit_returns_immediately_and_poll_sees_result(tmp_path, monkeypatch):
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.01)
    monkeypatch.setattr("app.services.heavy_job_limiter.shared_heavy_job_limiter", limiter)
    monkeypatch.setattr(bt, "_guard_server_backtest_range", lambda _start, _end: None)
    release = threading.Event()
    started = threading.Event()

    def fake_run(task):
        started.set()
        assert limiter.in_use == 2
        assert release.wait(2)
        return {
            "ic_mean": 0.08,
            "ir": 0.4,
            "ic_series": [{"date": "2026-01-02", "ic": float("nan")}],
            "group_stats": [{"group": 1, "label": "G1", "total_return": 0.1}],
            "group_nav": [{"date": "2026-01-02", "g1": 1.01}],
            "error": None,
            "elapsed_ms": 12,
            "n_symbols": 3,
            "n_dates": 4,
        }

    manager = _manager(tmp_path, fake_run)
    request = _request(manager)
    created = bt.factor_runs_submit(
        bt.FactorJobRequest(kind="factor", factor_name="momentum_5d", end=date(2026, 8, 24)),
        request,
    )
    assert created["status"] == "queued"
    assert created["username"] == "alice"
    assert created["run_id"]
    assert started.wait(2)
    current = bt.factor_run_get(created["run_id"], request)
    assert current["status"] == "running"
    assert current["result"] is None
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = bt.factor_run_get(created["run_id"], request)
        if current["status"] == "succeeded":
            break
        time.sleep(0.01)
    assert current["status"] == "succeeded"
    assert current["result"]["ic_mean"] == 0.08
    assert current["result"]["ic_series"][0]["ic"] is None
    assert current["result"]["group_stats"][0]["total_return"] == 0.1
    assert current["summary"]["ir"] == 0.4
    listed = bt.factor_runs_list(request)
    assert listed["items"][0]["run_id"] == created["run_id"]
    assert "result" not in listed["items"][0]


def test_second_submit_stays_queued_until_the_slot_frees(tmp_path, monkeypatch):
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.01)
    monkeypatch.setattr("app.services.heavy_job_limiter.shared_heavy_job_limiter", limiter)
    monkeypatch.setattr(bt, "_guard_server_backtest_range", lambda _start, _end: None)
    release = threading.Event()
    started = threading.Event()

    def fake_run(_task):
        if not started.is_set():
            started.set()
            assert release.wait(2)
        return {"results": [], "error": None, "n_symbols": 1, "n_dates": 1}

    manager = _manager(tmp_path, fake_run)
    request = _request(manager, username="carol")
    end = date(2026, 8, 24)
    first = bt.factor_runs_submit(
        bt.FactorJobRequest(kind="batch", factor_names=["momentum_5d"], end=end),
        request,
    )
    assert started.wait(2)
    second = bt.factor_runs_submit(
        bt.FactorJobRequest(kind="batch", factor_names=["turnover_rate"], end=end),
        request,
    )
    time.sleep(0.05)
    assert bt.factor_run_get(second["run_id"], request)["status"] == "queued"
    assert limiter.in_use == 2
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if bt.factor_run_get(second["run_id"], request)["status"] == "succeeded":
            break
        time.sleep(0.01)
    assert bt.factor_run_get(first["run_id"], request)["status"] == "succeeded"
    assert bt.factor_run_get(second["run_id"], request)["status"] == "succeeded"


def test_sync_factor_run_still_returns_result_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(bt.settings, "data_dir", tmp_path)
    monkeypatch.setattr(bt, "_guard_server_backtest_range", lambda _start, _end: None)

    def fake_run(task):
        assert task["kind"] == "factor"
        return {"ic_mean": 0.03, "error": None, "config": {"factor_name": "momentum_5d"}}

    monkeypatch.setattr("app.backtest.worker.run_worker_task", fake_run)
    end = date(2026, 8, 24)
    request = SimpleNamespace(state=SimpleNamespace(auth_user="dave"))
    result = bt.factor_run(bt.FactorBacktestRequest(factor_name="momentum_5d", end=end), request)
    assert result["ic_mean"] == 0.03
    listed = FactorBacktestRunStore(tmp_path).list_runs()
    assert listed[0]["username"] == "dave"
    assert listed[0]["kind"] == "factor"
    assert listed[0]["status"] == "succeeded"
    loaded = FactorBacktestRunStore(tmp_path).get(listed[0]["run_id"], include_result=True)
    assert loaded["result"]["ic_mean"] == 0.03


def test_async_omitted_start_uses_factor_default_and_null_means_all_history(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "_guard_server_backtest_range", lambda _start, _end: None)
    seen = []

    def fake_run(task):
        seen.append(task["config"]["start"])
        return {"ic_mean": 0.01, "error": None}

    manager = _manager(tmp_path, fake_run)
    request = _request(manager)
    end = date(2026, 8, 24)
    created = bt.factor_runs_submit(
        bt.FactorJobRequest(kind="factor", factor_name="momentum_5d", end=end),
        request,
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not seen:
        time.sleep(0.01)
    assert seen == [(end - timedelta(days=bt.FACTOR_DEFAULT_DAYS)).isoformat()]
    assert bt.factor_run_get(created["run_id"], request)["request"]["start"] == seen[0]

    explicit = bt.factor_runs_submit(
        bt.FactorJobRequest(kind="factor", factor_name="momentum_5d", start=None, end=end),
        request,
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(seen) < 2:
        time.sleep(0.01)
    assert seen[1] == "1900-01-01"
    assert bt.factor_run_get(explicit["run_id"], request)["request"]["start"] == "1900-01-01"


def test_unknown_factor_does_not_create_a_run(tmp_path):
    manager = FactorBacktestJobManager(tmp_path, worker_runner=lambda _task: {})
    request = _request(manager)
    with pytest.raises(HTTPException) as exc_info:
        bt.factor_runs_submit(
            bt.FactorJobRequest(kind="factor", factor_name="not_a_factor"), request
        )
    assert exc_info.value.status_code == 400
    assert manager.store.list_runs() == []


def test_api_get_and_delete(tmp_path):
    manager = FactorBacktestJobManager(tmp_path, worker_runner=lambda _task: {"error": None})
    request = _request(manager)
    with pytest.raises(HTTPException) as missing:
        bt.factor_run_get("b" * 32, request)
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as invalid:
        bt.factor_run_delete("not-safe", request)
    assert invalid.value.status_code == 400

    created = manager.store.create("batch", {"factor_names": ["momentum_5d"]}, "alice")
    with pytest.raises(HTTPException) as busy:
        bt.factor_run_delete(created["run_id"], request)
    assert busy.value.status_code == 409
    manager.store.transition(created["run_id"], "running")
    manager.store.fail(created["run_id"], RuntimeError("worker down"), elapsed_ms=2)
    assert bt.factor_run_delete(created["run_id"], request) == {"ok": True}
    with pytest.raises(HTTPException):
        bt.factor_run_get(created["run_id"], request)


def test_worker_failure_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "_guard_server_backtest_range", lambda _start, _end: None)

    def fake_run(_task):
        raise RuntimeError("子进程退出")

    manager = _manager(tmp_path, fake_run)
    request = _request(manager)
    created = bt.factor_runs_submit(
        bt.FactorJobRequest(kind="batch", factor_names=["momentum_5d"], end=date(2026, 8, 24)),
        request,
    )
    deadline = time.monotonic() + 2
    current = None
    while time.monotonic() < deadline:
        current = bt.factor_run_get(created["run_id"], request)
        if current["status"] == "failed":
            break
        time.sleep(0.01)
    assert current["status"] == "failed"
    assert "子进程退出" in current["error"]
    assert current["result"] is None
