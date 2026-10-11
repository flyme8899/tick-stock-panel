"""因子回测走 worker 子进程，同一时间最多一个。"""
from __future__ import annotations

import threading
import time
from datetime import date

from app.api import backtest as bt
from app.backtest.factor import FactorBatchConfig, FactorConfig, FactorResult
from app.backtest.worker import _decode_dated_config, _execute_factor_task, make_worker_task
from app.services.heavy_job_limiter import HeavyJobLimiter


class _Req:
    pass


def test_factor_task_roundtrip_and_dispatch(monkeypatch, tmp_path):
    config = FactorConfig(
        factor_name="momentum_5d",
        symbols=["600000.SH"],
        start=date(2026, 1, 1),
        end=date(2026, 6, 1),
        n_groups=4,
        rebalance="weekly",
    )
    task = make_worker_task("factor", tmp_path, config)
    assert task["kind"] == "factor"
    assert _decode_dated_config(task["config"], FactorConfig) == config

    seen = {}

    class _Service:
        def __init__(self, engine):
            seen["repo"] = engine.repo

        def run(self, actual):
            seen["config"] = actual
            return FactorResult(run_id="run-1", config={"factor_name": actual.factor_name})

    monkeypatch.setattr("app.backtest.factor.FactorBacktestService", _Service)
    repo = object()
    result = _execute_factor_task("factor", task, repo)
    assert seen["repo"] is repo
    assert seen["config"] == config
    assert result["run_id"] == "run-1"
    assert result["config"]["factor_name"] == "momentum_5d"


def test_factor_batch_holds_exclusive_slot_for_one_job(monkeypatch):
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.01)
    monkeypatch.setattr("app.services.heavy_job_limiter.shared_heavy_job_limiter", limiter)
    monkeypatch.setattr(bt, "_guard_server_backtest_range", lambda _start, _end: None)

    entered = []
    release = threading.Event()
    started = threading.Event()

    def fake_run(task, *_args, **_kwargs):
        entered.append((task["kind"], limiter.in_use))
        if len(entered) == 1:
            started.set()
            assert release.wait(2)
        decoded = _decode_dated_config(task["config"], FactorBatchConfig)
        return {"factor_names": decoded.factor_names}

    monkeypatch.setattr("app.backtest.worker.run_worker_task", fake_run)

    errors: list[BaseException] = []

    def call():
        try:
            bt.factor_batch(
                bt.FactorBatchRequest(factor_names=["momentum_5d"], end=date(2026, 8, 24)),
                _Req(),
            )
        except BaseException as exc:  # noqa: BLE001 — 线程里的失败要带回主线程
            errors.append(exc)

    first = threading.Thread(target=call)
    second = threading.Thread(target=call)
    first.start()
    assert started.wait(2)
    second.start()
    time.sleep(0.05)
    assert entered == [("factor_batch", 2)]
    assert limiter.in_use == 2
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert entered == [("factor_batch", 2), ("factor_batch", 2)]
