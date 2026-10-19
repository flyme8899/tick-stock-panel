"""回测基础过滤: 时点 ST 区间, 以及按交易日计的次新股剔除。"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    _populate_matrix_derived_arrays,
    build_basic_filter_mask,
    build_market_data_matrix,
)
from app.backtest.strategy import _basic_filter_dependencies
from app.indicators.pipeline import compute_limit_signals
from app.price_limits import (
    numpy_limit_pct_vectors,
    parse_listing_date,
    write_numpy_price_limit_matrix,
)
from app.services.st_history_sync import (
    attach_is_st_column,
    intervals_frame,
    intervals_from_flags,
    listing_dates_for_symbols,
    load_st_history,
    reset_missing_warning,
    spot_check_cninfo,
    st_history_path,
    sync_st_history,
)


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _panel(symbols: list[str], names: list[str], dates: list[date]) -> object:
    rows = []
    for day in dates:
        for symbol, name in zip(symbols, names, strict=True):
            rows.append({
                "symbol": symbol,
                "name": name,
                "date": day,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 1000.0,
            })
    return build_market_data_matrix(pl.DataFrame(rows))


def _write_intervals(data_dir, rows: list[dict]) -> None:
    path = st_history_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    intervals_frame(rows).write_parquet(path)


def _two_day(symbol: str, prev_close: float, today_close: float, trade_date: date) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol, symbol],
        "date": [trade_date - timedelta(days=1), trade_date],
        "raw_close": [prev_close, today_close],
        "close": [prev_close, today_close],
        "raw_high": [prev_close, today_close],
        "raw_low": [prev_close, today_close],
        "open": [prev_close, today_close],
        "high": [prev_close, today_close],
        "low": [prev_close, today_close],
        "change_pct": [0.0, today_close / prev_close - 1],
        "vol_ratio_5d": [1.0, 1.0],
    })


def test_removed_st_is_filtered_only_inside_the_interval(tmp_path):
    """曾 ST、后来摘帽: 只在 [start, end) 内剔除, 当前名称仍带 *ST 也不外扩。"""
    dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)]
    _write_intervals(tmp_path, [{
        "symbol": "600001.SH",
        "start_date": date(2024, 1, 3),
        "end_date": date(2024, 1, 5),
        "st_type": "ST",
        "source": "baostock",
    }])
    instruments = pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH"],
        "name": ["*ST已摘帽", "普通股"],
        "listing_date": ["2020-01-02", "2020-01-02"],
    })
    shape = (len(dates), 2)
    derived = _populate_matrix_derived_arrays(
        ["600001.SH", "600002.SH"],
        {"volume": np.ones(shape, dtype=np.float32), "close": np.full(shape, 10.0, dtype=np.float32)},
        {},
        frozenset(),
        instruments,
        np.ones(shape, dtype=bool),
        parquet_fields=[],
        vector_fields=[],
        trading_dates=dates,
        data_dir=tmp_path,
        trading_calendar=tuple(dates),
    )
    assert derived.st_point_in_time is True
    assert derived.is_st[:, 0].tolist() == [False, True, True, False]
    assert derived.is_st[:, 1].tolist() == [False, False, False, False]

    market = replace(
        _panel(["600001.SH", "600002.SH"], ["*ST已摘帽", "普通股"], dates),
        is_st=derived.is_st,
        st_point_in_time=True,
        listing_dates=derived.listing_dates,
        trading_calendar=tuple(dates),
    )
    mask = build_basic_filter_mask(market, {"enabled": True, "exclude_st": True})
    removed = market.symbols.index("600001.SH")
    peer = market.symbols.index("600002.SH")
    assert mask[:, removed].tolist() == [True, False, False, True]
    assert mask[:, peer].tolist() == [True, True, True, True]


def test_five_percent_limit_applies_only_inside_the_st_interval():
    """2026-07-06 前主板 ST 区间内 5%; 区间外以及新规当日即使仍是 ST 也是 10%。"""
    dates = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 6, 3), date(2026, 7, 6)]
    is_st = np.array([[False], [True], [False], [True]])
    target = np.empty((len(dates), 1), dtype=np.float32)
    write_numpy_price_limit_matrix(
        target,
        dates,
        ["600001.SH"],
        ["普通股"],
        is_st=is_st,
    )
    assert target[:, 0].tolist() == pytest.approx([0.10, 0.05, 0.10, 0.10])

    growth = np.empty((2, 1), dtype=np.float32)
    write_numpy_price_limit_matrix(
        growth,
        [date(2024, 1, 3), date(2024, 1, 4)],
        ["300001.SZ"],
        ["普通创业"],
        is_st=np.array([[True], [True]]),
    )
    assert growth[:, 0].tolist() == pytest.approx([0.20, 0.20])

    legacy, current = numpy_limit_pct_vectors(
        ["600001.SH", "600002.SH"],
        ["*ST名称", "普通"],
        np.array([False, True]),
    )
    assert legacy.tolist() == pytest.approx([0.10, 0.05])
    assert current.tolist() == pytest.approx([0.10, 0.10])


def test_limit_up_uses_interval_not_the_current_name():
    history = intervals_frame([{
        "symbol": "600001",
        "start_date": date(2024, 1, 3),
        "end_date": date(2024, 1, 4),
        "st_type": "ST",
        "source": "baostock",
    }])
    instruments = pl.DataFrame({"symbol": ["600001"], "name": ["普通股"]})
    inside = compute_limit_signals(
        _two_day("600001", 10.0, 10.5, date(2024, 1, 3)),
        instruments,
        st_history=history,
    ).sort("date")
    outside = compute_limit_signals(
        _two_day("600001", 10.0, 10.5, date(2024, 1, 8)),
        instruments,
        st_history=history,
    ).sort("date")
    assert inside["signal_limit_up"].to_list()[-1] is True
    assert outside["signal_limit_up"].to_list()[-1] is False

    flags = attach_is_st_column(
        pl.DataFrame({
            "symbol": ["600001", "600001"],
            "date": [date(2024, 1, 3), date(2024, 1, 8)],
            "name": ["*ST主板", "*ST主板"],
        }),
        pl.DataFrame({"symbol": ["600001"], "name": ["*ST主板"]}),
        history,
    )
    assert flags["_is_st"].to_list() == [True, False]


def test_new_stocks_are_excluded_for_the_configured_trading_days():
    calendar = _weekdays(date(2024, 1, 2), 40)
    dates = calendar
    symbols = ["600001.SH", "600002.SH", "600003.SH"]
    market = replace(
        _panel(symbols, ["次新", "老股", "未知上市日"], dates),
        listing_dates=(calendar[0], date(2020, 1, 2), None),
        trading_calendar=tuple(calendar),
    )
    mask = build_basic_filter_mask(market, {"enabled": True, "exclude_new_days": 30})
    fresh = market.symbols.index("600001.SH")
    seasoned = market.symbols.index("600002.SH")
    unknown = market.symbols.index("600003.SH")
    assert mask[:30, fresh].tolist() == [False] * 30
    assert mask[30:, fresh].tolist() == [True] * 10
    assert mask[:, seasoned].all()
    assert mask[:, unknown].all()


def test_list_date_column_counts_as_listing_date():
    assert parse_listing_date("20200102") == date(2020, 1, 2)
    assert parse_listing_date(20200102) == date(2020, 1, 2)
    assert parse_listing_date("2020-01-02") == date(2020, 1, 2)
    found = listing_dates_for_symbols(
        ["600001.SH"],
        pl.DataFrame({"symbol": ["600001"], "list_date": ["20200102"]}),
    )
    assert found == (date(2020, 1, 2),)


def test_missing_st_history_falls_back_to_current_name(tmp_path, caplog):
    reset_missing_warning()
    dates = [date(2024, 1, 2), date(2024, 1, 3)]
    with caplog.at_level(logging.WARNING):
        assert load_st_history(tmp_path) is None
        derived = _populate_matrix_derived_arrays(
            ["600001.SH"],
            {
                "volume": np.ones((2, 1), dtype=np.float32),
                "close": np.full((2, 1), 10.0, dtype=np.float32),
            },
            {},
            frozenset(),
            pl.DataFrame({"symbol": ["600001.SH"], "name": ["*ST历史"]}),
            np.ones((2, 1), dtype=bool),
            parquet_fields=[],
            vector_fields=[],
            trading_dates=dates,
            data_dir=tmp_path,
        )
    assert derived.st_point_in_time is False
    assert derived.is_st is None
    assert any("ST" in record.message for record in caplog.records)

    warned = _panel(["600001.SH"], ["*ST历史"], dates)
    kept = _panel(["600001.SH"], ["平安银行"], dates)
    config = {"enabled": True, "exclude_st": True}
    assert not build_basic_filter_mask(warned, config).any()
    assert build_basic_filter_mask(kept, config).all()

    legacy = np.empty((1, 1), dtype=np.float32)
    write_numpy_price_limit_matrix(legacy, [date(2024, 1, 3)], ["600001.SH"], ["*ST历史"])
    assert float(legacy[0, 0]) == pytest.approx(0.05)


def test_missing_listing_dates_skips_new_stock_filter_without_crashing(caplog):
    market = _panel(["600001.SH"], ["普通股"], [date(2024, 1, 2), date(2024, 1, 3)])
    import app.backtest.matrix as matrix_mod

    matrix_mod._MISSING_LISTING_WARNED = False
    with caplog.at_level(logging.WARNING):
        mask = build_basic_filter_mask(market, {"enabled": True, "exclude_new_days": 30})
    assert mask.all()
    assert any("exclude_new_days" in record.message for record in caplog.records)


def test_intervals_from_daily_isst_keep_suspension_gaps():
    rows = intervals_from_flags("sh.600001", [
        (date(2020, 1, 2), True),
        (date(2020, 1, 6), True),
        (date(2020, 1, 7), False),
        (date(2020, 1, 8), True),
    ])
    assert rows[0]["symbol"] == "600001.SH"
    assert rows[0]["start_date"] == date(2020, 1, 2)
    assert rows[0]["end_date"] == date(2020, 1, 7)
    assert rows[1]["end_date"] is None
    assert rows[0]["source"] == "baostock"
    assert rows[0]["st_type"] == "ST"


def test_sync_writes_intervals_from_injected_isst(tmp_path):
    instruments = tmp_path / "instruments"
    instruments.mkdir()
    pl.DataFrame({
        "symbol": ["600001.SH"],
        "name": ["示例"],
        "listing_date": ["2020-01-02"],
    }).write_parquet(instruments / "instruments.parquet")

    def fetch(code: str, start: date, end: date):
        assert code == "sh.600001"
        assert start == date(2020, 1, 2)
        assert end == date(2020, 1, 8)
        return [
            (date(2020, 1, 2), True),
            (date(2020, 1, 3), True),
            (date(2020, 1, 6), False),
            (date(2020, 1, 7), True),
            (date(2020, 1, 8), True),
        ]

    written = sync_st_history(
        tmp_path,
        fetch_flags=fetch,
        end=date(2020, 1, 8),
    )
    frame = pl.read_parquet(st_history_path(tmp_path))
    assert written == 2
    assert frame["end_date"].to_list() == [date(2020, 1, 6), None]
    assert not (tmp_path / "st_history" / "progress.json").exists()


def test_spot_check_reports_announcements_away_from_boundaries():
    history = intervals_frame([{
        "symbol": "600001.SH",
        "start_date": date(2020, 1, 2),
        "end_date": date(2020, 3, 1),
        "st_type": "ST",
        "source": "baostock",
    }])

    def far(_symbol: str, _start: date, _end: date):
        return [(date(2021, 6, 1), "关于实施风险警示的公告")]

    issues = spot_check_cninfo(
        history,
        limit=1,
        fetch_announcements=far,
        end=date(2021, 12, 31),
    )
    assert any("不靠近" in item for item in issues)

    def near(_symbol: str, _start: date, _end: date):
        return [(date(2020, 1, 2), "关于实施风险警示的公告")]

    assert spot_check_cninfo(
        history,
        limit=1,
        fetch_announcements=near,
        end=date(2020, 6, 1),
    ) == []


def test_matrix_filter_dependencies_do_not_require_current_name():
    deps = _basic_filter_dependencies({
        "enabled": True,
        "exclude_st": True,
        "exclude_new_days": 30,
    })
    assert "name" not in deps
    assert {"symbol", "close"} <= deps


def _write_three_instruments(tmp_path):
    instruments = tmp_path / "instruments"
    instruments.mkdir()
    pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "830001.BJ"],
        "name": ["甲", "乙", "ST丙"],
        "listing_date": ["2020-01-02", "2020-01-02", "2020-01-02"],
    }).write_parquet(instruments / "instruments.parquet")


def test_sync_retries_transient_errors_and_skips_beijing(tmp_path, monkeypatch):
    import app.services.st_history_sync as sync_mod

    monkeypatch.setattr(sync_mod, "_RETRY_BACKOFF_S", 0.0)
    _write_three_instruments(tmp_path)
    calls: dict[str, int] = {}

    def fetch(code: str, start: date, end: date):
        calls[code] = calls.get(code, 0) + 1
        if code == "sh.600001" and calls[code] == 1:
            raise RuntimeError("网络接收错误")
        return [(date(2020, 1, 2), True), (date(2020, 1, 3), False)]

    written = sync_st_history(tmp_path, fetch_flags=fetch, end=date(2020, 1, 8))
    assert written == 2
    assert calls == {"sh.600001": 2, "sh.600002": 1}
    assert not (tmp_path / "st_history" / "progress.json").exists()


def test_sync_flushes_when_the_last_symbol_fails(tmp_path, monkeypatch):
    import app.services.st_history_sync as sync_mod

    monkeypatch.setattr(sync_mod, "_RETRY_BACKOFF_S", 0.0)
    _write_three_instruments(tmp_path)

    def fetch(code: str, start: date, end: date):
        if code == "sh.600002":
            raise RuntimeError("down")
        return [(date(2020, 1, 2), True)]

    written = sync_st_history(tmp_path, fetch_flags=fetch, end=date(2020, 1, 8))
    frame = pl.read_parquet(st_history_path(tmp_path))
    assert written == 1
    assert frame["symbol"].to_list() == ["600001.SH"]
    progress = (tmp_path / "st_history" / "progress.json").read_text(encoding="utf-8")
    assert "600001.SH" in progress and "600002.SH" not in progress


def test_explicit_beijing_symbol_is_not_sent_to_baostock(tmp_path):
    calls: list[str] = []

    def fetch(code: str, start: date, end: date):
        calls.append(code)
        return []

    sync_st_history(
        tmp_path,
        symbols=["830001.BJ", "600001.SH"],
        fetch_flags=fetch,
        end=date(2020, 1, 8),
    )
    assert calls == ["sh.600001"]


def test_beijing_st_falls_back_to_current_name_when_history_exists(tmp_path):
    from app.backtest.matrix import _st_matrix_for_dates
    from app.services.st_history_sync import st_history_dir

    st_history_dir(tmp_path).mkdir(parents=True)
    intervals_frame([{
        "symbol": "600001.SH",
        "start_date": date(2020, 1, 3),
        "end_date": None,
        "st_type": "ST",
        "source": "baostock",
    }]).write_parquet(st_history_path(tmp_path))
    days = [date(2020, 1, 2), date(2020, 1, 3)]
    matrix, point = _st_matrix_for_dates(
        days,
        ["600001.SH", "830001.BJ", "830002.BJ", "830003.BJ"],
        tmp_path,
        names=["甲", "ST丙", "丁", "退市戊"],
    )
    assert point
    assert matrix.tolist() == [
        [False, True, False, True],
        [True, True, False, True],
    ]


def test_unchanged_intervals_do_not_force_enriched_rebuild(tmp_path, monkeypatch):
    import app.services.st_history_sync as sync_mod
    from app.indicators.pipeline import limit_formula_needs_rebuild, write_limit_formula_version

    monkeypatch.setattr(sync_mod, "_RETRY_BACKOFF_S", 0.0)
    _write_three_instruments(tmp_path)

    def fetch(code: str, start: date, end: date):
        return [(date(2020, 1, 2), True), (date(2020, 1, 3), False)]

    sync_st_history(tmp_path, fetch_flags=fetch, end=date(2020, 1, 8))
    write_limit_formula_version(tmp_path)
    stamped = st_history_path(tmp_path).stat().st_mtime_ns

    sync_st_history(tmp_path, fetch_flags=fetch, end=date(2020, 1, 8))
    assert st_history_path(tmp_path).stat().st_mtime_ns == stamped
    assert limit_formula_needs_rebuild(tmp_path) is False

    def changed(code: str, start: date, end: date):
        return [(date(2020, 1, 2), True)]

    sync_st_history(tmp_path, fetch_flags=changed, end=date(2020, 1, 8))
    assert st_history_path(tmp_path).stat().st_mtime_ns != stamped
    assert limit_formula_needs_rebuild(tmp_path) is True


def test_baostock_socket_timeout_is_restored(monkeypatch):
    import socket

    import app.services.st_history_sync as sync_mod

    monkeypatch.setattr(socket, "getdefaulttimeout", lambda: 7.5)
    restored: list[float | None] = []
    monkeypatch.setattr(socket, "setdefaulttimeout", restored.append)
    with sync_mod._baostock_socket_timeout(30.0):
        assert restored == [30.0]
    assert restored == [30.0, 7.5]


def test_baostock_network_error_is_retryable(monkeypatch):
    import baostock

    from app.services.st_history_sync import _fetch_baostock_flags

    class Result:
        error_code = "10002007"
        error_msg = "网络接收错误。"

    monkeypatch.setattr(baostock, "query_history_k_data_plus", lambda *args, **kwargs: Result())
    with pytest.raises(RuntimeError, match="网络接收错误"):
        _fetch_baostock_flags("sh.600001", date(2020, 1, 2), date(2020, 1, 3))
