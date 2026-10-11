"""regime latest/history 按文件 mtime 返回现成行，命中后不再调用 polars。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.api import regime as regime_api
from app.services import regime_builder


def _request(data_dir):
    store = SimpleNamespace(data_dir=data_dir)
    repo = SimpleNamespace(store=store)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def _write(data_dir, rows: list[dict]) -> None:
    path = regime_builder.regime_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path)


def _reset() -> None:
    regime_builder.clear_regime_rows_cache()
    regime_builder._REGIME_HISTORY_CACHE = None
    regime_builder._REGIME_HISTORY_SIG = None


def test_missing_file_returns_empty_without_polars(monkeypatch, tmp_path):
    _reset()

    def boom(*_args, **_kwargs):
        raise AssertionError("should not touch polars")

    monkeypatch.setattr(regime_builder, "load_regime_history", boom)
    request = _request(tmp_path)
    assert regime_api.regime_latest(request) == {"row": None}
    assert regime_api.regime_history(request, start=None, end=None, limit=10) == {
        "rows": [],
        "total": 0,
    }


def test_latest_and_history_follow_mtime_and_skip_polars_on_hit(monkeypatch, tmp_path):
    _reset()
    _write(tmp_path, [
        {"date": date(2026, 1, 6), "state": "strong", "score": 80.0},
        {"date": date(2026, 1, 2), "state": "weak", "score": 10.0},
        {"date": date(2026, 1, 5), "state": "range", "score": 50.0},
    ])
    request = _request(tmp_path)
    calls = {"n": 0}
    real = regime_builder.load_regime_history

    def counted(data_dir):
        calls["n"] += 1
        return real(data_dir)

    monkeypatch.setattr(regime_builder, "load_regime_history", counted)

    latest = regime_api.regime_latest(request)
    assert latest["row"]["date"] == "2026-01-06"
    assert latest["row"]["state"] == "strong"
    history = regime_api.regime_history(request, start=None, end=None, limit=2)
    assert [row["date"] for row in history["rows"]] == ["2026-01-05", "2026-01-06"]
    assert history["total"] == 2
    ranged = regime_api.regime_history(
        request, start=date(2026, 1, 2), end=date(2026, 1, 5), limit=1,
    )
    assert [row["date"] for row in ranged["rows"]] == ["2026-01-02", "2026-01-05"]
    assert ranged["total"] == 2
    assert calls["n"] == 1

    regime_api.invalidate_regime_cache()
    assert regime_api.regime_latest(request)["row"]["date"] == "2026-01-06"
    assert calls["n"] == 2

    _write(tmp_path, [
        {"date": date(2026, 1, 8), "state": "lean_strong", "score": 60.0},
    ])
    assert regime_api.regime_latest(request)["row"]["date"] == "2026-01-08"
    assert calls["n"] == 3
