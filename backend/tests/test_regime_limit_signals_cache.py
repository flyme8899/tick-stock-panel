"""regime 涨跌停数不得因内存缓存缺信号列而静默算成 0。

根因: KlineRepository._refresh_enriched_impl 直接读 self._instruments_cache,
独立进程 (或 clear_cache() 后惰性重建) 时它为 None → 传空维表 →
compute_limit_signals 被跳过 → 历史缓存无 signal_limit_up/down 列;
run_regime_batch 命中缓存后 _aggregate_daily 对缺列一律填 0。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.services import regime_builder
from app.tickflow.repository import DataStore, KlineRepository


def _write_enriched(tmp_path, days, rows_fn):
    for day in days:
        part = tmp_path / "kline_daily_enriched" / f"date={day.isoformat()}"
        part.mkdir(parents=True)
        rows_fn(day).write_parquet(part / "part.parquet")


def _day_frame(day: date, close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [day],
        "open": [close],
        "high": [close],
        "low": [close],
        "close": [close],
        "volume": [100.0],
        "amount": [1000.0],
        "raw_close": [close],
        "raw_high": [close],
        "raw_low": [close],
        "turnover_rate": [1.0],
        "consecutive_limit_ups": pl.Series([0], dtype=pl.UInt32),
        "consecutive_limit_downs": pl.Series([0], dtype=pl.UInt32),
    })


def test_refresh_history_cache_lazy_loads_instruments(tmp_path) -> None:
    d0 = date(2026, 6, 11)
    d1 = d0 + timedelta(days=1)
    _write_enriched(tmp_path, [d0, d1], lambda d: _day_frame(d, 10.0 if d == d0 else 11.0))
    inst = tmp_path / "instruments"
    inst.mkdir()
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "name": ["浦发银行"],
        "listing_date": ["1999-11-10"],
    }).write_parquet(inst / "instruments.parquet")

    repo = KlineRepository(DataStore(tmp_path))
    assert repo._instruments_cache is None  # 模拟独立进程 / clear_cache() 之后
    calls = []
    original = repo.get_instruments

    def _spy():
        calls.append(1)
        return original()

    repo.get_instruments = _spy  # type: ignore[method-assign]
    repo._refresh_enriched_impl()

    assert calls, "刷新历史缓存必须经 get_instruments() 懒加载维表"
    hist = repo._enriched_history_cache
    assert hist is not None and not hist.is_empty()
    assert "signal_limit_up" in hist.columns
    row = hist.filter(pl.col("date") == d1).row(0, named=True)
    assert row["signal_limit_up"] is True  # 10 → 11 主板 +10% 涨停


def test_run_regime_batch_falls_back_when_cache_lacks_signal_columns(tmp_path, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: False)
    monkeypatch.setattr(regime_builder, "_load_index_pct", lambda *a, **k: {})

    cache = pl.DataFrame({
        "date": [date(2026, 6, 12)] * 2,
        "symbol": ["A", "B"],
        "close": [11.0, 9.0],
        "change_pct": [0.1, -0.1],
        "amount": [1e8, 1e8],
        "ma20": [10.0, 10.0],
        "consecutive_limit_ups": [1, 0],
    })

    class _Store:
        data_dir = tmp_path

    class _FakeRepo:
        store = _Store

        def get_enriched_range(self, start, end):
            return cache

    sentinel = pl.DataFrame({"date": [date(2026, 6, 12)], "limit_up": [1]})
    called = {}

    def _fallback(repo, start, end, **kwargs):
        called["range"] = (start, end)
        return sentinel

    monkeypatch.setattr(regime_builder, "_scan_enriched_fallback", _fallback)
    out = regime_builder.run_regime_batch(_FakeRepo(), date(2026, 6, 12), date(2026, 6, 12))
    assert called["range"] == (date(2026, 6, 12), date(2026, 6, 12))
    assert out["limit_up"].to_list() == [1]


def test_run_regime_batch_uses_cache_when_signal_columns_present(tmp_path, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: False)
    monkeypatch.setattr(regime_builder, "_load_index_pct", lambda *a, **k: {})

    cache = pl.DataFrame({
        "date": [date(2026, 6, 12)] * 2,
        "symbol": ["A", "B"],
        "close": [11.0, 9.0],
        "change_pct": [0.1, -0.1],
        "amount": [1e8, 1e8],
        "ma20": [10.0, 10.0],
        "signal_limit_up": [True, False],
        "signal_limit_down": [False, True],
        "signal_broken_limit_up": [False, False],
        "consecutive_limit_ups": [1, 0],
    })

    class _Store:
        data_dir = tmp_path

    class _FakeRepo:
        store = _Store

        def get_enriched_range(self, start, end):
            return cache

    def _fallback(*a, **k):
        raise AssertionError("缓存列齐全时不应走慢路径")

    monkeypatch.setattr(regime_builder, "_scan_enriched_fallback", _fallback)
    out = regime_builder.run_regime_batch(_FakeRepo(), date(2026, 6, 12), date(2026, 6, 12))
    row = out.row(0, named=True)
    assert row["limit_up"] == 1 and row["limit_down"] == 1
