"""形态旁路只保留最近窗口，选股按交易日回看。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from fastapi import HTTPException

from app.indicators.candle_store import (
    KEEP_DAYS,
    ensure_candle_patterns,
    match_symbols,
    refresh_candle_patterns,
)
from app.services.fs_utils import atomic_write_parquet


def _write_day(root, day: date, rows: list[dict]) -> None:
    path = root / "kline_daily_enriched" / f"date={day.isoformat()}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(pl.DataFrame(rows), path)


def _bar(day: date, open_: float, high: float, low: float, close: float, symbol: str) -> dict:
    return {"symbol": symbol, "date": day, "open": open_, "high": high, "low": low, "close": close}


def test_refresh_keeps_recent_days_and_match_window(tmp_path, monkeypatch):
    monkeypatch.setenv("TSP_CANDLE_ENGINE", "builtin")
    start = date(2024, 1, 1)
    # 比保留窗口多几天，确认更早的分区不会留下。
    total = KEEP_DAYS + 3
    for offset in range(total):
        day = start + timedelta(days=offset)
        if offset == total - 1:
            rows = [
                _bar(day, 11.4, 11.5, 9.6, 9.7, "600000.SH"),
                _bar(day, 10.0, 10.0, 10.0, 10.0, "000001.SZ"),
            ]
        elif offset == total - 2:
            rows = [
                _bar(day, 10.0, 11.2, 9.9, 11.0, "600000.SH"),
                _bar(day, 10.0, 10.0, 10.0, 10.0, "000001.SZ"),
            ]
        elif offset == 0:
            rows = [
                _bar(day, 11.4, 11.5, 9.6, 9.7, "600000.SH"),
                _bar(day, 10.0, 10.0, 10.0, 10.0, "000001.SZ"),
            ]
        else:
            rows = [
                _bar(day, 10.0, 10.2, 9.8, 10.1, "600000.SH"),
                _bar(day, 10.0, 10.0, 10.0, 10.0, "000001.SZ"),
            ]
        _write_day(tmp_path, day, rows)

    first = refresh_candle_patterns(tmp_path, engine="builtin")
    assert first["skipped"] is False
    assert first["written_days"] == KEEP_DAYS
    store = tmp_path / "kline_candle_patterns"
    assert len(list(store.glob("date=*"))) == KEEP_DAYS
    assert not (store / f"date={start.isoformat()}").exists()

    latest = (start + timedelta(days=total - 1)).isoformat()
    hit = match_symbols(tmp_path, "engulfing", "bear", 1)
    assert hit["symbols"] == ["600000.SH"]
    assert hit["as_of"] == latest
    older = match_symbols(tmp_path, "engulfing", "bull", 1)
    assert older["symbols"] == []

    second = ensure_candle_patterns(tmp_path)
    assert second["skipped"] is True


def test_match_rejects_unavailable_pattern_and_bad_side(tmp_path, monkeypatch):
    monkeypatch.setenv("TSP_CANDLE_ENGINE", "builtin")
    day = date(2024, 6, 3)
    _write_day(tmp_path, day, [_bar(day, 10.0, 10.2, 9.8, 10.1, "600000.SH")])
    with pytest.raises(ValueError, match="TA-Lib"):
        match_symbols(tmp_path, "two_crows", "bear", 1)
    with pytest.raises(ValueError, match="方向"):
        match_symbols(tmp_path, "hammer", "bear", 1)

    from types import SimpleNamespace

    from app.api.screener import candle_pattern_match

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))),
    )
    with pytest.raises(HTTPException) as exc:
        candle_pattern_match(request, pattern="hammer", side="bear", within=1)
    assert exc.value.status_code == 400
