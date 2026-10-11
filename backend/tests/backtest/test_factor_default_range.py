"""因子回测默认区间回归测试。

覆盖 issue #202: `/api/backtest/factor/run` 与 `/api/backtest/factor/batch`
在省略 `start` 时错误使用了策略默认区间(3 年)而非因子默认区间(180 天)。

只关注「起始日解析」这一关注点。计算已改到 worker 子进程, 这里截住
run_worker_task, 从任务载荷里还原配置, 不做真实数据加载。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import ClassVar

import pytest

from app.api import backtest as bt
from app.backtest.factor import FactorBatchConfig, FactorConfig
from app.backtest.worker import _decode_dated_config


class _Capture:
    captured: ClassVar[list] = []


class _Req:
    """占位 request。起始日解析不读 app.state。"""


@pytest.fixture
def patched(monkeypatch, tmp_path):
    _Capture.captured = []

    def fake_run(task, *_args, **_kwargs):
        cls = FactorConfig if task["kind"] == "factor" else FactorBatchConfig
        _Capture.captured.append(_decode_dated_config(task["config"], cls))
        return {"ok": True}

    monkeypatch.setattr("app.backtest.worker.run_worker_task", fake_run)
    monkeypatch.setattr(bt.settings, "data_dir", tmp_path)
    # 隔离守卫, 单测「起始日解析」。
    monkeypatch.setattr(bt, "_guard_server_backtest_range", lambda start, end: None)
    return _Capture


def test_factor_run_omitted_start_uses_factor_default(patched):
    end = date(2026, 8, 24)
    bt.factor_run(bt.FactorBacktestRequest(factor_name="momentum_5d", end=end), _Req())
    cfg = patched.captured[-1]
    assert cfg.start == end - timedelta(days=bt.FACTOR_DEFAULT_DAYS)
    # 回归保护: 不得再退回策略默认的 3 年。
    assert cfg.start != end - timedelta(days=bt.STRATEGY_DEFAULT_DAYS)


def test_factor_batch_omitted_start_uses_factor_default(patched):
    end = date(2026, 8, 24)
    bt.factor_batch(bt.FactorBatchRequest(factor_names=["momentum_5d"], end=end), _Req())
    cfg = patched.captured[-1]
    assert cfg.start == end - timedelta(days=bt.FACTOR_DEFAULT_DAYS)
    assert cfg.start != end - timedelta(days=bt.STRATEGY_DEFAULT_DAYS)


def test_factor_run_explicit_null_start_means_all_history(patched):
    """显式传 start=null 语义不变: 代表全部历史。"""
    end = date(2026, 8, 24)
    bt.factor_run(bt.FactorBacktestRequest(factor_name="momentum_5d", start=None, end=end), _Req())
    assert patched.captured[-1].start == date(1900, 1, 1)


def test_factor_run_explicit_start_passthrough(patched):
    """显式日期原样透传。"""
    end = date(2026, 8, 24)
    bt.factor_run(
        bt.FactorBacktestRequest(factor_name="momentum_5d", start=date(2026, 1, 1), end=end),
        _Req(),
    )
    assert patched.captured[-1].start == date(2026, 1, 1)
