"""选股补充字段：解析、日 K 推导、缺数不删行、PE 断点、调度让路。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.selection_fields.bars import derive_period_snapshot
from app.selection_fields.jobs import advance_pe, sync_indexes
from app.selection_fields.parse import (
    ashare_symbol,
    combine_index_members,
    parse_holders,
    parse_index_members,
    parse_northbound,
    parse_pe_ttm,
    parse_unlock_schedule,
)
from app.selection_fields.query import annotate_result
from app.selection_fields.scheduler import SelectionWorker, due_tasks
from app.selection_fields.store import commit, read_frame, read_meta, save_checkpoint

FLAGS = {
    "enabled": True,
    "northbound": True,
    "holders": True,
    "index": True,
    "unlock": True,
    "pe": True,
}


def test_northbound_percent_is_not_rescaled() -> None:
    rows = parse_northbound([
        {
            "代码": "600519",
            "今日持股-占流通股比": 2.35,
            "今日增持估计-占流通股比": 5,
            "日期": "2024-01-05",
        },
        {"代码": "000001.SH", "今日持股-占流通股比": 1},
        {"代码": "399001", "今日持股-占流通股比": 1},
    ])
    assert rows == [{
        "symbol": "600519.SH",
        "northbound_hold_pct": 2.35,
        "trade_date": "2024-01-05",
    }]
    assert ashare_symbol("000001.SZ") == "000001.SZ"
    assert ashare_symbol("000001.SH") == ""


def test_holders_unlock_pe_and_index_labels() -> None:
    holders = parse_holders([{
        "代码": "000001",
        "股东户数-本次": "1234567",
        "股东户数-增减比例": -1.2,
        "股东户数统计截止日-本次": "2024-09-30",
    }])
    assert holders[0]["holder_num"] == 1234567
    assert holders[0]["holder_change_pct"] == -1.2

    unlock = parse_unlock_schedule(
        [
            {"股票代码": "600519", "解禁时间": "2024-01-01", "占解禁前流通市值比例": 9},
            {"股票代码": "600519", "解禁时间": "2024-03-01", "占解禁前流通市值比例": 4.5},
            {"股票代码": "600519", "解禁时间": "2024-06-01", "占解禁前流通市值比例": 8},
        ],
        today=date(2024, 2, 1),
    )
    assert unlock == [{
        "symbol": "600519.SH",
        "next_unlock_date": "2024-03-01",
        "unlock_float_ratio": 4.5,
    }]

    pe = parse_pe_ttm([
        {"数据日期": "2024-01-02", "PE(TTM)": 10},
        {"数据日期": "2024-01-08", "PE(TTM)": 12.5},
        {"数据日期": "2024-01-03", "PE(TTM)": None},
    ])
    assert pe == 12.5

    hs300 = parse_index_members([{"成分券代码": "600519"}], "沪深300")
    zz500 = parse_index_members([{"成分券代码": "600519"}, {"成分券代码": "000001"}], "中证500")
    combined = combine_index_members([("沪深300", hs300), ("中证500", zz500)])
    by_symbol = {row["symbol"]: row["index_membership"] for row in combined}
    assert by_symbol["600519.SH"] == "沪深300、中证500"
    assert by_symbol["000001.SZ"] == "中证500"


def test_daily_bars_derive_week_month_and_streak() -> None:
    streak = pl.DataFrame({
        "symbol": ["600519.SH"] * 6,
        "date": [date(2024, 1, day) for day in (2, 3, 4, 5, 8, 9)],
        "open": [10.0, 11, 12, 11, 10, 11],
        "high": [10.0, 11, 12, 11, 10, 11],
        "low": [10.0, 11, 12, 11, 10, 11],
        "close": [10.0, 11, 12, 11, 10, 11],
    })
    periods = pl.DataFrame({
        "symbol": ["000001.SZ"] * 3,
        "date": [date(2024, 1, 2), date(2024, 1, 8), date(2024, 2, 1)],
        "open": [10.0, 11.0, 12.0],
        "high": [10.0, 11.0, 12.0],
        "low": [10.0, 11.0, 12.0],
        "close": [10.0, 11.0, 12.0],
    })
    single = pl.DataFrame({
        "symbol": ["300750.SZ"],
        "date": [date(2024, 1, 2)],
        "open": [9.0],
        "high": [9.0],
        "low": [9.0],
        "close": [9.0],
    })
    long_days = _weekdays(date(2023, 6, 1), 80)
    long = pl.DataFrame({
        "symbol": ["601318.SH"] * len(long_days),
        "date": long_days,
        "open": [10.0 + index for index in range(len(long_days))],
        "high": [11.0 + index for index in range(len(long_days))],
        "low": [9.0 + index for index in range(len(long_days))],
        "close": [10.5 + index for index in range(len(long_days))],
    })
    out = derive_period_snapshot(pl.concat([streak, periods, single, long], how="diagonal"))
    by_symbol = {row["symbol"]: row for row in out.to_dicts()}

    assert by_symbol["600519.SH"]["up_streak"] == 1
    assert by_symbol["600519.SH"]["down_streak"] == 0
    assert by_symbol["000001.SZ"]["weekly_change_pct"] == pytest.approx(12 / 11 - 1)
    assert by_symbol["000001.SZ"]["monthly_change_pct"] == pytest.approx(12 / 11 - 1)
    assert by_symbol["300750.SZ"]["weekly_change_pct"] is None
    assert by_symbol["300750.SZ"]["monthly_change_pct"] is None
    assert by_symbol["300750.SZ"]["up_streak"] == 0
    assert by_symbol["300750.SZ"]["down_streak"] == 0

    grown = by_symbol["601318.SH"]
    assert grown["macd_hist_w"] == pytest.approx((grown["macd_dif_w"] - grown["macd_dea_w"]) * 2)
    assert grown["macd_hist_m"] == pytest.approx((grown["macd_dif_m"] - grown["macd_dea_m"]) * 2)
    assert grown["kdj_k_w"] is not None
    assert grown["kdj_j_w"] == pytest.approx(3 * grown["kdj_k_w"] - 2 * grown["kdj_d_w"])
    assert derive_period_snapshot(pl.DataFrame()).is_empty()


def test_partial_snapshot_keeps_every_row_and_names_the_gap(tmp_path) -> None:
    commit(tmp_path, "northbound", [{
        "symbol": "600519.SH",
        "northbound_hold_pct": 2.35,
        "trade_date": "2024-01-05",
    }], ok=True)
    _write_daily(tmp_path, "600519.SH")
    result = annotate_result({
        "as_of": "2024-01-03",
        "rows": [
            {"symbol": "600519.SH", "close": 10},
            {"symbol": "000001.SZ", "close": 11},
            {"symbol": "300750.SZ", "close": 12},
        ],
    }, tmp_path)
    assert [row["symbol"] for row in result["rows"]] == ["600519.SH", "000001.SZ", "300750.SZ"]
    assert [row["close"] for row in result["rows"]] == [10, 11, 12]
    assert result["rows"][0]["northbound_hold_pct"] == 2.35
    assert result["rows"][1].get("northbound_hold_pct") is None
    assert "股东户数" in result["field_gaps"]
    assert "指数成分" in result["field_gaps"]
    assert "北向持股" not in result["field_gaps"]
    assert "周线、月线、连涨天数" not in result["field_gaps"]
    assert "northbound_hold_pct" not in result["gap_keys"]
    assert "holder_num" in result["gap_keys"]
    assert len(result["rows"]) == 3


def test_failed_refresh_keeps_the_previous_parquet(tmp_path) -> None:
    commit(tmp_path, "holders", [{
        "symbol": "600519.SH",
        "holder_num": 100,
        "holder_change_pct": 1.5,
        "report_date": "2024-09-30",
    }], ok=True)
    commit(tmp_path, "holders", [], ok=False, error="eastmoney down")
    frame = read_frame(tmp_path, "holders")
    assert frame.to_dicts()[0]["holder_num"] == 100
    assert read_meta(tmp_path, "holders")["status"] == "error"

    commit(tmp_path, "index_members", [{
        "symbol": "600519.SH",
        "index_membership": "沪深300",
    }], ok=True)

    def ak(name, **kwargs):
        if kwargs.get("symbol") == "000905":
            raise RuntimeError("csindex down")
        return [{"成分券代码": "000001"}]

    outcome = sync_indexes(ak, tmp_path)
    assert outcome["ok"] is False
    assert read_frame(tmp_path, "index_members").to_dicts()[0]["index_membership"] == "沪深300"


def test_scheduler_skips_when_the_heavy_slot_is_busy(monkeypatch, tmp_path) -> None:
    for name in (
        "SELECTION_FIELDS_ENABLED",
        "SELECTION_NORTHBOUND",
        "SELECTION_HOLDERS",
        "SELECTION_INDEX_MEMBERS",
        "SELECTION_UNLOCK",
        "SELECTION_PE_TTM",
    ):
        monkeypatch.setenv(name, "true")
    monkeypatch.setattr("app.fund_flow.scheduler.tickflow_sync_busy", lambda: True)
    calls: list[str] = []

    def ak(name, **_kwargs):
        calls.append(name)
        return []

    worker = SelectionWorker(tmp_path, ak=ak)
    now = datetime(2024, 1, 6, 18, 0, tzinfo=CN_TZ)
    assert worker.run_once(now) is None
    assert calls == []


def test_due_window_is_after_close_and_on_weekends() -> None:
    saturday = datetime(2024, 1, 6, 11, 0, tzinfo=CN_TZ)
    monday_morning = datetime(2024, 1, 8, 10, 0, tzinfo=CN_TZ)
    monday_after = datetime(2024, 1, 8, 16, 25, tzinfo=CN_TZ)
    assert due_tasks(saturday, trading=False, state={}, flags=FLAGS)[0] == "northbound"
    assert due_tasks(monday_morning, trading=True, state={}, flags=FLAGS) == []
    assert due_tasks(monday_after, trading=True, state={}, flags=FLAGS)[0] == "northbound"


def test_pe_checkpoint_resumes_and_skips_a_symbol_after_two_failures(tmp_path) -> None:
    symbols = ["000001.SZ", "300750.SZ", "600519.SH"]
    directory = tmp_path / "instruments" / "stock"
    directory.mkdir(parents=True)
    pl.DataFrame({"symbol": symbols}).write_parquet(directory / "part.parquet")
    seen: list[str] = []

    def ak(_name, **kwargs):
        code = kwargs["symbol"]
        seen.append(code)
        if code == "300750":
            raise RuntimeError("boom")
        if code == "000001":
            return [{"数据日期": "2024-01-05", "PE(TTM)": 18}]
        return [{"数据日期": "2024-01-05", "PE(TTM)": 20}]

    first = advance_pe(
        tmp_path,
        ak,
        "2024-01-05",
        flush_every=1,
        busy=lambda: seen == ["000001"],
    )
    assert first["complete"] is False
    assert seen == ["000001"]

    advance_pe(tmp_path, ak, "2024-01-05", flush_every=1, busy=lambda: False)
    advance_pe(tmp_path, ak, "2024-01-05", flush_every=1, busy=lambda: False)
    fourth = advance_pe(tmp_path, ak, "2024-01-05", flush_every=1, busy=lambda: False)
    assert seen == ["000001", "300750", "300750", "600519"]
    assert fourth["complete"] is True
    assert read_frame(tmp_path, "pe_ttm").filter(pl.col("symbol") == "600519.SH")["pe_ttm"][0] == 20
    assert read_frame(tmp_path, "pe_ttm").filter(pl.col("symbol") == "000001.SZ")["pe_ttm"][0] == 18

    save_checkpoint(tmp_path, {
        "trade_date": "2024-01-08",
        "done": ["600519.SH"],
        "skipped": [],
        "attempts": {},
        "total": 3,
    })
    result = annotate_result({
        "as_of": "2024-01-08",
        "rows": [
            {"symbol": "600519.SH", "close": 1, "pe_ttm": None},
            {"symbol": "000001.SZ", "close": 2},
        ],
    }, tmp_path)
    assert "PE(TTM)" not in result["field_gaps"]
    assert "pe_ttm" in result["gap_keys"]
    assert any("1/3" in note for note in result["field_notes"])
    assert result["rows"][0]["pe_ttm"] == 20
    assert result["rows"][0]["close"] == 1
    assert len(result["rows"]) == 2


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _write_daily(data_dir, symbol: str) -> None:
    path = data_dir / "kline_daily_enriched" / "date=2024-01-03" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [symbol, symbol],
        "date": [date(2024, 1, 2), date(2024, 1, 3)],
        "open": [10.0, 11.0],
        "high": [10.0, 11.0],
        "low": [10.0, 11.0],
        "close": [10.0, 11.0],
        "volume": [1.0, 1.0],
        "amount": [1.0, 1.0],
        "raw_close": [10.0, 11.0],
        "raw_high": [10.0, 11.0],
        "raw_low": [10.0, 11.0],
        "turnover_rate": [1.0, 1.0],
        "consecutive_limit_ups": [0, 0],
        "consecutive_limit_downs": [0, 0],
        "quote_ts": [0, 0],
    }).write_parquet(path)
