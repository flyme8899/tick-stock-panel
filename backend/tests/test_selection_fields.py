"""选股补充字段：解析、日 K 推导、缺数不删行、PE 断点、调度让路。"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.selection_fields.bars import derive_period_snapshot
from app.selection_fields.jobs import (
    advance_pe,
    fetch_northbound_quarter,
    sync_indexes,
    sync_northbound,
)
from app.selection_fields.parse import (
    ashare_symbol,
    combine_index_members,
    parse_holders,
    parse_index_members,
    parse_northbound,
    parse_pe_market,
    parse_pe_ttm,
    parse_unlock_schedule,
)
from app.selection_fields.query import annotate_result
from app.selection_fields.scheduler import SelectionWorker, due_tasks
from app.selection_fields.store import (
    commit,
    commit_index,
    read_frame,
    read_index_part,
    read_meta,
    save_checkpoint,
)

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
    quarter = parse_northbound([{
        "SECURITY_CODE": "600519",
        "FREE_SHARES_RATIO": 4.2967,
        "HOLD_DATE": "2026-06-30 00:00:00",
    }])
    assert quarter == [{
        "symbol": "600519.SH",
        "northbound_hold_pct": 4.2967,
        "trade_date": "2026-06-30",
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
    market = parse_pe_market([
        {"SECURITY_CODE": "000001", "PE_TTM": 5.17532828},
        {"SECURITY_CODE": "000002", "PE_TTM": -0.52903084},
        {"SECURITY_CODE": "000006", "PE_TTM": None},
        {"SECURITY_CODE": "399001", "PE_TTM": 9},
    ])
    by_symbol = {row["symbol"]: row["pe_ttm"] for row in market}
    assert by_symbol["000001.SZ"] == 5.17532828
    assert by_symbol["000002.SZ"] == -0.52903084
    assert by_symbol["000006.SZ"] is None
    assert "399001.SZ" not in by_symbol

    hs300 = parse_index_members([{"成分券代码": "600519"}], "沪深300")
    cyb = parse_index_members([{"样本代码": "300750"}], "创业板50")
    assert cyb == {"300750.SZ": "创业板50"}
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
    monkeypatch.setattr("app.fund_flow.scheduler.tickflow_sync_busy", lambda **_: True)
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


def test_pe_batch_fills_the_market_and_skips_a_missing_symbol_once(tmp_path) -> None:
    symbols = ["000001.SZ", "300750.SZ", "600519.SH"]
    directory = tmp_path / "instruments" / "stock"
    directory.mkdir(parents=True)
    pl.DataFrame({"symbol": symbols}).write_parquet(directory / "part.parquet")
    pages: list[int] = []
    singles: list[str] = []

    def ak(name, **kwargs):
        if name == "pe_market":
            pages.append(int(kwargs["page"]))
            if kwargs["page"] == 1:
                return {
                    "rows": [{"SECURITY_CODE": "000001", "PE_TTM": 5.17532828}],
                    "pages": 2,
                    "empty": False,
                }
            return {"rows": [{"SECURITY_CODE": "000002", "PE_TTM": -1.5}], "pages": 2, "empty": False}
        singles.append(kwargs["symbol"])
        if kwargs["symbol"] == "300750":
            raise RuntimeError("boom")
        return [{"数据日期": "2024-01-05", "PE(TTM)": 20}]

    hold = {"on": True}
    stopped = advance_pe(
        tmp_path,
        ak,
        "2024-01-05",
        flush_every=1,
        busy=lambda: hold["on"] and pages == [1],
    )
    hold["on"] = False
    assert stopped["complete"] is False
    assert pages == [1]
    assert singles == []
    assert read_frame(tmp_path, "pe_ttm").filter(pl.col("symbol") == "000001.SZ")["pe_ttm"][0] == 5.17532828

    finished = advance_pe(tmp_path, ak, "2024-01-05", flush_every=1, busy=lambda: False)
    assert pages == [1, 2]
    assert singles == ["300750", "600519"]
    assert finished["complete"] is True
    assert read_frame(tmp_path, "pe_ttm").filter(pl.col("symbol") == "600519.SH")["pe_ttm"][0] == 20
    assert read_frame(tmp_path, "pe_ttm").filter(pl.col("symbol") == "300750.SZ").height == 0

    save_checkpoint(tmp_path, {
        "trade_date": "2024-01-08",
        "done": ["600519.SH"],
        "skipped": [],
        "total": 3,
        "batch_done": True,
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


def test_northbound_uses_quarter_and_keeps_the_previous_snapshot(tmp_path, monkeypatch) -> None:
    seen: list[tuple] = []

    def fake_em_get(_url, *, params, headers=None, timeout=15.0):
        seen.append((params["reportName"], params.get("filter"), params.get("pageNumber")))
        if params["reportName"] == "RPT_MUTUAL_BOARD_HOLDRANKDATE":
            return _json_response({
                "success": True,
                "result": {"data": [
                    {"TRADE_DATE": "2026-09-30 00:00:00"},
                    {"TRADE_DATE": "2026-06-30 00:00:00"},
                ]},
            })
        if "2026-09-30" in str(params.get("filter")):
            return _json_response({"success": False, "code": 9201, "message": "返回数据为空"})
        if params.get("pageNumber") == "1":
            data = [{
                "SECURITY_CODE": "600519",
                "FREE_SHARES_RATIO": 4.2967,
                "HOLD_DATE": "2026-06-30 00:00:00",
            }]
        else:
            data = [{
                "SECURITY_CODE": "000001",
                "FREE_SHARES_RATIO": 2.35,
                "HOLD_DATE": "2026-06-30 00:00:00",
            }]
        return _json_response({"success": True, "result": {"pages": 2, "data": data}})

    monkeypatch.setattr("app.eastmoney.client.em_get", fake_em_get)
    fetched = parse_northbound(fetch_northbound_quarter())
    assert [row["northbound_hold_pct"] for row in fetched] == [4.2967, 2.35]
    assert fetched[0]["trade_date"] == "2026-06-30"
    assert [item[0] for item in seen] == [
        "RPT_MUTUAL_BOARD_HOLDRANKDATE",
        "RPT_MUTUAL_HOLDRANK_NEW",
        "RPT_MUTUAL_HOLDRANK_NEW",
        "RPT_MUTUAL_HOLDRANK_NEW",
    ]
    assert "2026-09-30" in str(seen[1][1])
    assert seen[2][2] == "1" and "2026-06-30" in str(seen[2][1])
    assert seen[3][2] == "2"

    def ak(name, **_kwargs):
        assert name == "northbound_quarter"
        return [
            {
                "SECURITY_CODE": "600519",
                "FREE_SHARES_RATIO": 4.2967,
                "HOLD_DATE": "2026-06-30 00:00:00",
            },
            {
                "SECURITY_CODE": "000001",
                "FREE_SHARES_RATIO": 2.35,
                "HOLD_DATE": "2026-06-30 00:00:00",
            },
        ]

    outcome = sync_northbound(ak, tmp_path)
    assert outcome["ok"] is True
    assert read_meta(tmp_path, "northbound")["note"] == "北向持股为季度口径，截至 2026-06-30"

    def broken(name, **_kwargs):
        assert name == "northbound_quarter"
        raise RuntimeError("down")

    failed = sync_northbound(broken, tmp_path)
    assert failed["ok"] is False
    assert read_frame(tmp_path, "northbound").to_dicts()[0]["northbound_hold_pct"] == 4.2967
    noted = annotate_result({"rows": [{"symbol": "600519.SH", "close": 1}]}, tmp_path)
    assert any("季度口径，截至 2026-06-30" in note for note in noted["field_notes"])
    assert "北向持股" not in noted["field_gaps"]


def test_one_index_failure_still_writes_the_others(tmp_path) -> None:
    commit_index(tmp_path, "000905", [{
        "symbol": "600519.SH",
        "index_membership": "中证500",
    }], ok=True, as_of="2024-01-07")
    calls: list[tuple[str, str]] = []

    def ak(name, **kwargs):
        symbol = kwargs["symbol"]
        calls.append((name, symbol))
        if symbol == "399673":
            assert name == "index_detail_cni"
            return [{"样本代码": "300750"}]
        assert name == "index_stock_cons_csindex"
        if symbol == "000905":
            raise RuntimeError("csindex down")
        return [{"成分券代码": "000001"}]

    outcome = sync_indexes(ak, tmp_path, day="2024-01-08")
    assert outcome["ok"] is False
    assert outcome["error"] == "指数成分未完成：中证500"
    assert ("index_detail_cni", "399673") in calls
    assert ("index_stock_cons_csindex", "399673") not in calls
    assert read_index_part(tmp_path, "000300").to_dicts()[0]["symbol"] == "000001.SZ"
    assert read_index_part(tmp_path, "000905").to_dicts()[0]["symbol"] == "600519.SH"
    assert read_index_part(tmp_path, "399673").to_dicts()[0]["symbol"] == "300750.SZ"

    again: list[str] = []

    def retry(name, **kwargs):
        again.append(kwargs["symbol"])
        if kwargs["symbol"] == "000905":
            raise RuntimeError("csindex down")
        return [{"成分券代码": "000001"}]

    sync_indexes(retry, tmp_path, day="2024-01-08")
    assert again == ["000905"]

    result = annotate_result({
        "as_of": "2024-01-08",
        "rows": [
            {"symbol": "600519.SH"},
            {"symbol": "000001.SZ"},
            {"symbol": "300750.SZ"},
        ],
    }, tmp_path)
    by_symbol = {row["symbol"]: row.get("index_membership") for row in result["rows"]}
    assert by_symbol["600519.SH"] == "中证500"
    assert by_symbol["000001.SZ"] == "沪深300、上证50、中证1000"
    assert by_symbol["300750.SZ"] == "创业板50"
    assert "指数成分" not in result["field_gaps"]
    assert any(note == "指数成分未完成：中证500" for note in result["field_notes"])


def test_pe_market_page_uses_ttm_field_and_request_timeout(monkeypatch) -> None:
    seen: list[dict] = []

    def fake(_url, *, params, headers=None, timeout=None):
        seen.append({"params": params, "timeout": timeout, "referer": (headers or {}).get("Referer")})
        if params.get("pageSize") == "1":
            return _json_response({
                "success": True,
                "result": {"pages": 1, "data": [{"TRADE_DATE": "2026-10-09 00:00:00"}]},
            })
        if "2024-01-05" in str(params.get("filter")):
            return _json_response({"success": False, "code": 9201, "message": "返回数据为空"})
        return _json_response({
            "success": True,
            "result": {
                "pages": 12,
                "data": [{"SECURITY_CODE": "000001", "TRADE_DATE": "2026-10-09", "PE_TTM": 5.17532828}],
            },
        })

    monkeypatch.setattr("app.eastmoney.client.em_get", fake)
    from app.selection_fields.jobs import fetch_pe_market_page

    empty = fetch_pe_market_page("2024-01-05", 1)
    assert empty["empty"] is True
    assert empty["latest"] == "2026-10-09"
    page = fetch_pe_market_page("2026-10-09", 1)
    assert page["pages"] == 12
    assert page["rows"][0]["PE_TTM"] == 5.17532828
    assert seen[0]["timeout"] == (10.0, 30.0)
    assert seen[0]["params"]["reportName"] == "RPT_VALUEANALYSIS_DET"
    assert "PE_TTM" in seen[0]["params"]["columns"]
    assert "f9" not in seen[0]["params"]["columns"]
    assert "gzfx" in seen[0]["referer"]


def test_timed_out_task_does_not_start_another_while_the_old_thread_runs(monkeypatch, tmp_path) -> None:
    for name in (
        "SELECTION_FIELDS_ENABLED",
        "SELECTION_NORTHBOUND",
        "SELECTION_HOLDERS",
        "SELECTION_INDEX_MEMBERS",
        "SELECTION_UNLOCK",
        "SELECTION_PE_TTM",
    ):
        monkeypatch.setenv(name, "true")
    monkeypatch.setattr("app.fund_flow.scheduler.tickflow_sync_busy", lambda **_: False)
    monkeypatch.setattr("app.selection_fields.timeouts.DEFAULT_TASK_TIMEOUT_S", 0.4)
    now = datetime(2024, 1, 6, 18, 0, tzinfo=CN_TZ)
    day = "2024-01-05"
    state_path = tmp_path / "selection_fields" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        '{"northbound_date": "2024-01-05", "holders_date": "2024-01-05"}',
        encoding="utf-8",
    )
    blocked = threading.Event()
    calls: list[str] = []

    def ak(name, **_kwargs):
        calls.append(name)
        if name in {"index_stock_cons_csindex", "index_detail_cni"}:
            blocked.wait(5)
            return [{"成分券代码": "600519"}]
        return []

    worker = SelectionWorker(tmp_path, ak=ak)
    assert worker.run_once(now) == "index"
    assert calls == ["index_stock_cons_csindex"]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved.get("index_date") != day
    assert saved.get("unlock_date") != day
    assert saved.get("last_error") == "指数成分任务超时"

    assert worker.run_once(now) is None
    assert calls == ["index_stock_cons_csindex"]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved.get("last_error") == "上一个补充字段任务仍在运行"

    blocked.set()
    inflight = worker._inflight
    assert inflight is not None
    inflight.join(2)
    assert not inflight.is_alive()
    assert calls == ["index_stock_cons_csindex"]

    assert worker.run_once(now) == "unlock"
    assert "stock_restricted_release_detail_em" in calls
    assert "stock_value_em" not in calls
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved.get("unlock_date") == day


def test_northbound_request_sets_connect_and_read_timeout(monkeypatch) -> None:
    seen: dict = {}

    def fake(_url, *, params, headers=None, timeout=None):
        seen["timeout"] = timeout
        return _json_response({"success": False, "code": 9201, "message": "返回数据为空"})

    monkeypatch.setattr("app.eastmoney.client.em_get", fake)
    with pytest.raises(RuntimeError, match="北向季度日期为空"):
        fetch_northbound_quarter()
    assert seen["timeout"] == (10.0, 30.0)


def _json_response(payload: dict):
    return type("Response", (), {"json": lambda self: payload})()


def test_pe_per_symbol_fill_skips_delisted_names(tmp_path) -> None:
    directory = tmp_path / "instruments"
    directory.mkdir()
    pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ", "000002.SZ", "300750.SZ"],
        "name": ["贵州茅台", "平安银行", "退市某某", "宁德时代"],
        "list_status": ["L", "D", "L", ""],
    }).write_parquet(directory / "instruments.parquet")
    singles: list[str] = []

    def ak(name, **kwargs):
        if name == "pe_market":
            return {
                "rows": [{"SECURITY_CODE": "600519", "PE_TTM": 20}],
                "pages": 1,
                "empty": False,
            }
        singles.append(kwargs["symbol"])
        return [{"数据日期": "2024-01-05", "PE(TTM)": 8}]

    advance_pe(tmp_path, ak, "2024-01-05", flush_every=1, busy=lambda: False)
    assert singles == ["300750"]


def test_instrument_flatten_keeps_list_status() -> None:
    import sys
    import types

    sys.modules.setdefault("tickflow", types.SimpleNamespace(AsyncTickFlow=object, TickFlow=object))
    from app.services.instrument_sync import _flatten_instruments

    rows = _flatten_instruments([{
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "code": "600519",
        "ext": {"list_status": "L", "listing_date": "2001-08-27"},
    }])
    assert rows[0]["list_status"] == "L"


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
