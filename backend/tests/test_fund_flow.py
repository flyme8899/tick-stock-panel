"""资金进出：单位、落盘、换源、调度和只读接口。不访问网络。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.fund_flow import config, factors, jobs, normalize, query, scheduler, store
from app.fund_flow.circuit import BACKOFF_SECONDS, CircuitBreaker, CircuitOpenError, call_with_retry
from app.fund_flow.jobs import Pace
from app.fund_flow.sources import (
    AK_ALLOWLIST,
    BLOCKED_AK_NAMES,
    SourceClients,
    eastmoney_secid,
    fetch_history_bill,
)
from app.fund_flow.units import BARE_YI, BARE_YUAN, money_to_yuan, percent_points_to_decimal
from app.services import dragon_tiger as dt

CN = timezone(timedelta(hours=8))


def _day(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 9, hour, minute, tzinfo=CN)  # 周五


def _flags(**overrides: bool) -> dict[str, bool]:
    base = {
        "enabled": True,
        "stock": True,
        "sector": True,
        "margin": True,
        "etf_shares": True,
        "southbound": True,
        "northbound_turnover": True,
        "lhb_backup": True,
    }
    base.update(overrides)
    return base


def test_flags_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FUND_FLOW_ENABLED", "FUND_FLOW_STOCK", "FUND_FLOW_SECTOR",
        "FUND_FLOW_MARGIN", "FUND_FLOW_ETF_SHARES", "FUND_FLOW_SOUTHBOUND",
        "FUND_FLOW_NORTHBOUND_TURNOVER", "FUND_FLOW_LHB_BACKUP",
    ):
        monkeypatch.delenv(name, raising=False)
    assert config.enabled() is False
    assert config.stock_enabled() is False
    monkeypatch.setenv("FUND_FLOW_STOCK", "true")
    assert config.stock_enabled() is False
    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    assert config.stock_enabled() is True


def test_money_units_are_explicit() -> None:
    assert money_to_yuan("1.5亿", bare=BARE_YUAN) == 1.5e8
    assert money_to_yuan("8000万", bare=BARE_YI) == 8e7
    assert money_to_yuan(12.5, bare=BARE_YI) == 12.5e8
    assert money_to_yuan(12.5, bare=BARE_YUAN) == 12.5
    assert percent_points_to_decimal("5.2%") == pytest.approx(0.052)


def test_northbound_keeps_turnover_only() -> None:
    rows = normalize.normalize_northbound_turnover([{
        "日期": "2026-10-09",
        "当日成交净买额": 40,
        "买入成交额": 10,
        "卖出成交额": 12,
    }])
    assert rows == [{
        "trade_date": "2026-10-09",
        "turnover": 22e8,
        "source": "akshare",
    }]
    assert "net_flow" not in rows[0]


def test_sector_rank_and_etf_share_units() -> None:
    ranked = normalize.normalize_sectors(
        [{"行业": "半导体", "净额": "2亿"}, {"行业": "银行", "净额": -0.5}],
        trade_date="2026-10-09",
        captured_at="2026-10-09T10:00:00",
        snapshot="intraday",
    )
    assert ranked[0]["name"] == "半导体"
    assert ranked[0]["rank"] == 1
    assert ranked[0]["net_inflow"] == 2e8
    assert ranked[1]["net_inflow"] == -0.5e8
    shares = normalize.normalize_etf_shares([{
        "基金代码": "510300",
        "基金简称": "沪深300ETF",
        "统计日期": "2026-10-09",
        "基金份额": 2.439e10,
    }])
    # akshare 1.19.1 已把万份乘 10000，510300 在 2026-10-09 是 243.9 亿份。
    assert shares[0]["code"] == "510300"
    assert shares[0]["shares"] == pytest.approx(2.439e10)
    tagged = normalize.normalize_etf_shares([{
        "基金代码": "510050",
        "基金简称": "50ETF",
        "统计日期": "2026-10-09",
        "基金份额": "553.371668万",
    }])
    assert tagged[0]["shares"] == pytest.approx(553.371668 * 1e4)
    # 深交所「基金规模(份)」裸数字已是份。159915.0 不能被收成别的代码。
    szse = normalize.normalize_etf_shares([{
        "基金代码": 159915.0,
        "基金简称": "创业板ETF",
        "日期": "2026-10-09",
        "基金规模(份)": 1.8e10,
    }])
    assert szse[0]["code"] == "159915"
    assert szse[0]["shares"] == pytest.approx(1.8e10)
    yi = normalize.normalize_etf_shares([{
        "基金代码": "159915",
        "基金简称": "创业板ETF",
        "日期": "20261009",
        "基金份额": "1.25亿",
    }])
    assert yi[0]["trade_date"] == "2026-10-09"
    assert yi[0]["shares"] == pytest.approx(1.25e8)


def test_partition_merge_is_atomic(tmp_path) -> None:
    store.write_rows(tmp_path, "stock", "2026-10-09", [{
        "symbol": "600519.SH", "code": "600519", "trade_date": "2026-10-09",
        "main_net": 1.0, "large_net": None, "super_net": None, "source": "efinance",
    }])
    store.write_rows(tmp_path, "stock", "2026-10-09", [{
        "symbol": "600519.SH", "code": "600519", "trade_date": "2026-10-09",
        "main_net": 9.0, "large_net": 2.0, "super_net": None, "source": "efinance",
    }])
    frame = store.read_partition(tmp_path, "stock", "2026-10-09")
    assert frame.height == 1
    assert frame["main_net"][0] == 9.0
    assert not list((tmp_path / "fund_flow" / "stock").rglob("*.tmp"))
    with pytest.raises(ValueError):
        store.write_rows(tmp_path, "../stock", "2026-10-09", [])


def test_retry_backoff_and_circuit() -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def fail():
        calls["n"] += 1
        raise RuntimeError("down")

    breaker = CircuitBreaker("efinance", clock=lambda: 0.0)
    with pytest.raises(RuntimeError):
        call_with_retry("efinance", fail, breaker, sleeper=sleeps.append)
    assert sleeps == list(BACKOFF_SECONDS)
    assert calls["n"] == 4

    clock = {"now": 100.0}
    limited = CircuitBreaker("akshare", clock=lambda: clock["now"], failure_limit=8, cooldown_s=600)
    for _ in range(8):
        limited.failure()
    assert limited.state == "open"
    with pytest.raises(CircuitOpenError):
        call_with_retry("akshare", fail, limited, sleeper=sleeps.append)
    clock["now"] = 699.0
    assert not limited.allow()
    clock["now"] = 700.0
    assert limited.allow()


def test_stock_fallback_backfill_and_pace(tmp_path) -> None:
    def bill(symbol: str):
        if symbol.startswith("000001"):
            raise RuntimeError("eastmoney down")
        return [
            {"日期": f"2026-01-{day:02d}", "主力净流入": day, "大单净流入": 1, "超大单净流入": 2}
            for day in range(1, 10)
        ] + [
            {"日期": f"2025-06-{day:02d}", "主力净流入": day, "大单净流入": 1, "超大单净流入": 2}
            for day in range(1, 29)
        ]

    mx_calls: list[str] = []

    def mx(symbol: str):
        mx_calls.append(symbol)
        return ([{"trade_date": "2026-10-09", "main_net": 5.0}], 0)

    clients = SourceClients(history_bill=bill, mx_query=mx, ak_call=lambda *a, **k: [])
    result = jobs.sync_stock(
        tmp_path,
        ["600519.SH", "000001.SZ"],
        clients,
        efinance=CircuitBreaker("efinance", clock=lambda: 0),
        mx=CircuitBreaker("mx", clock=lambda: 0),
        sleeper=lambda _seconds: None,
        mx_key="test-key",
        mx_max_calls=5,
    )
    assert mx_calls == ["000001.SZ"]
    assert result.wrote > 0
    saved = store.read_range(tmp_path, "stock")
    dates = saved.filter(pl.col("code") == "600519")["trade_date"].unique().to_list()
    assert len(dates) == jobs.BACKFILL_DATES or len(dates) == 9 + 28
    # 2026-01 的 9 天加 2025-06 的 28 天，不足 120，全部保留。
    assert len(dates) == 37
    assert "2025-06-01" in dates
    again = jobs.sync_stock(
        tmp_path,
        ["600519.SH"],
        clients,
        efinance=CircuitBreaker("efinance", clock=lambda: 0),
        mx=CircuitBreaker("mx", clock=lambda: 0),
        sleeper=lambda _seconds: None,
        mx_key="",
    )
    assert again.wrote >= 0
    # 已有最新分区的代码不再向 efinance 要数：再跑一次不会新增日期。
    assert store.read_range(tmp_path, "stock").filter(pl.col("code") == "600519").height == 37


def test_pace_drops_when_failure_rate_exceeds_five_percent() -> None:
    pace = Pace()
    for _ in range(18):
        pace.note(True)
    pace.note(False)
    pace.note(False)
    assert pace.requests_per_sec == jobs.SLOW_RPS
    assert pace.delay_s == pytest.approx(1.0)


def test_blocked_akshare_names_cannot_be_called() -> None:
    assert BLOCKED_AK_NAMES.isdisjoint(AK_ALLOWLIST)
    clients = SourceClients(ak_call=lambda name, **kwargs: [{"ok": name}])
    with pytest.raises(RuntimeError):
        clients.ak("stock_individual_fund_flow")
    with pytest.raises(RuntimeError):
        clients.ak("stock_main_fund_flow")


def test_sector_cache_and_margin_schedule(tmp_path) -> None:
    calls = {"n": 0}
    clock = {"now": 1000.0}

    def ak(name, **kwargs):
        calls["n"] += 1
        if name == "stock_fund_flow_industry":
            return [{"行业": "银行", "净额": 1}]
        return []

    clients = SourceClients(ak_call=ak)
    jobs.reset_sector_cache()
    jobs.fetch_sector_rows(clients, "industry", clock=lambda: clock["now"])
    jobs.fetch_sector_rows(clients, "industry", clock=lambda: clock["now"] + 30)
    assert calls["n"] == 1
    jobs.fetch_sector_rows(clients, "industry", clock=lambda: clock["now"] + 61)
    assert calls["n"] == 2

    assert jobs.previous_session(date(2026, 10, 12)) == date(2026, 10, 9)
    assert jobs.previous_session(date(2026, 10, 12), [date(2026, 9, 30)]) == date(2026, 9, 30)


def test_due_tasks_follow_shanghai_clock() -> None:
    state: dict = {"kinds": {}}
    assert "sector_intraday" in scheduler.due_tasks(
        _day(10, 0), trading=True, state=state, flags=_flags(), breakers_open=set(),
    )
    assert "stock" not in scheduler.due_tasks(
        _day(10, 0), trading=True, state=state, flags=_flags(), breakers_open=set(),
    )
    afternoon = scheduler.due_tasks(
        _day(16, 45), trading=True, state=state, flags=_flags(), breakers_open=set(),
    )
    assert "stock" in afternoon
    assert "southbound" in afternoon
    assert "sector_intraday" not in afternoon
    assert scheduler.due_tasks(
        _day(16, 45), trading=False, state=state, flags=_flags(), breakers_open=set(),
    ) == []
    assert scheduler.due_tasks(
        _day(16, 45), trading=True, state=state, flags=_flags(enabled=False), breakers_open=set(),
    ) == []
    evening = scheduler.due_tasks(
        _day(20, 10), trading=True, state=state, flags=_flags(), breakers_open=set(),
    )
    assert "margin_evening" in evening
    covered = {"kinds": {"margin": {"target_covered": "2026-10-08"}}}
    assert "margin_evening" not in scheduler.due_tasks(
        _day(20, 10), trading=True, state=covered, flags=_flags(), breakers_open=set(),
    )


def test_worker_defers_when_tickflow_is_busy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    monkeypatch.setenv("FUND_FLOW_STOCK", "true")
    called = {"n": 0}

    def bill(_symbol: str):
        called["n"] += 1
        return []

    worker = scheduler.FundFlowWorker()
    ran = worker.run_once(
        now=_day(17, 0),
        data_dir=tmp_path,
        clients=SourceClients(history_bill=bill),
        symbols=["600519.SH"],
        trading=True,
        busy=True,
    )
    assert ran == []
    assert called["n"] == 0
    assert worker.deferred_reason == "tickflow_sync"
    assert not (tmp_path / "fund_flow" / "stock").exists() or called["n"] == 0


def test_five_day_factor_does_not_use_future_dates(tmp_path) -> None:
    rows = []
    for offset, value in enumerate((1, 2, 3, 4, 5, 100), start=1):
        day = date(2026, 10, offset).isoformat()
        rows.append({
            "symbol": "600519.SH", "code": "600519", "trade_date": day,
            "main_net": float(value), "large_net": None, "super_net": None, "source": "efinance",
        })
        store.write_rows(tmp_path, "stock", day, [rows[-1]])
    frame = factors.main_net_frame(tmp_path).sort("trade_date")
    by_day = {row["trade_date"]: row["ff_main_net_5d"] for row in frame.to_dicts()}
    assert by_day["2026-10-04"] is None
    assert by_day["2026-10-05"] == 15
    assert by_day["2026-10-06"] == 114

    quote = pl.DataFrame({
        "symbol": ["600519.SH"],
        "date": [date(2026, 10, 5)],
        "close": [1.0],
    })
    attached = factors.attach_columns(quote, include_rank=False, data_dir=tmp_path)
    assert attached["ff_main_net_5d"][0] == 15


def test_hot_and_sector_rank(tmp_path) -> None:
    store.write_rows(tmp_path, "industry", "2026-10-09", normalize.normalize_sectors(
        [{"行业": "半导体", "净额": 3}, {"行业": "银行", "净额": 1}],
        trade_date="2026-10-09",
        captured_at="2026-10-09T15:00:00",
        snapshot="close",
    ))
    for offset in range(1, 6):
        day = date(2026, 10, offset).isoformat()
        store.write_rows(tmp_path, "stock", day, [{
            "symbol": "600519.SH", "code": "600519", "trade_date": day,
            "main_net": 1e8, "large_net": None, "super_net": None, "source": "efinance",
        }])
    annotated = factors.annotate_hot(
        [
            {"kind": "stock", "key": "600519.SH", "name": "贵州茅台"},
            {"kind": "sector", "key": "半导体", "name": "半导体"},
        ],
        tmp_path,
    )
    assert annotated[0]["fund_flow"]["main_net_5d"] == 5e8
    assert annotated[1]["fund_flow"]["sector_net_inflow_rank"] == 1


def test_read_api_and_etf_link(tmp_path, monkeypatch) -> None:
    store.write_rows(tmp_path, "stock", "2026-10-09", [{
        "symbol": "600519.SH", "code": "600519", "trade_date": "2026-10-09",
        "main_net": 3.0, "large_net": 1.0, "super_net": 2.0, "source": "efinance",
    }])
    store.write_rows(tmp_path, "margin", "2026-10-08", [{
        "market": "sse", "symbol": "", "name": "", "trade_date": "2026-10-08",
        "row_kind": "summary", "margin_balance": 10.0, "short_balance": 1.0,
        "margin_buy": 2.0, "source": "akshare",
    }])
    store.write_rows(tmp_path, "etf_shares", "2026-10-09", [{
        "symbol": "510050", "code": "510050", "name": "50ETF",
        "trade_date": "2026-10-09", "shares": 100.0, "source": "akshare",
    }])
    flow_dir = tmp_path / "etf_flow" / "date=2026-10-09"
    flow_dir.mkdir(parents=True)
    pl.DataFrame({"code": ["510050"], "trade_date": ["2026-10-09"], "net_flow": [7.0]}).write_parquet(
        flow_dir / "part.parquet",
    )
    monkeypatch.setattr(query, "_dir", lambda data_dir=None: tmp_path)
    app = FastAPI()
    from app.api.fund_flow import router
    app.include_router(router)
    client = TestClient(app)
    health = client.get("/api/fund-flow/health")
    assert health.status_code == 200
    assert health.json()["kinds"]["stock"]["rows"] == 1
    stock = client.get("/api/fund-flow/stock/600519.SH")
    assert stock.json()["items"][0]["main_net"] == 3.0
    margin = client.get("/api/fund-flow/margin")
    assert margin.json()["summary"][0]["margin_balance"] == 10.0
    etf = client.get("/api/fund-flow/etf-shares")
    assert etf.json()["etf_flow_linked"] is True
    assert etf.json()["items"][0]["flow_net"] == 7.0
    sectors = client.get("/api/fund-flow/sectors", params={"kind": "industry"})
    assert sectors.status_code == 200
    assert sectors.json()["items"] == []


def test_board_shows_existing_rows_and_skips_short_history(tmp_path) -> None:
    store.write_rows(tmp_path, "industry", "2026-10-09", normalize.normalize_sectors([{
        "行业": "半导体", "净额": 1.2,
    }], trade_date="2026-10-09", snapshot="close", captured_at="2026-10-09T15:00:00"))
    for offset in range(1, 5):
        day = date(2026, 10, offset).isoformat()
        store.write_rows(tmp_path, "stock", day, [{
            "symbol": "600519.SH", "code": "600519", "trade_date": day,
            "main_net": 1e8, "large_net": None, "super_net": None, "source": "efinance",
        }])
    store.write_rows(tmp_path, "stock", "2026-10-09", [{
        "symbol": "600519.SH", "code": "600519", "trade_date": "2026-10-09",
        "main_net": 2e8, "large_net": None, "super_net": None, "source": "efinance",
    }, {
        "symbol": "000001.SZ", "code": "000001", "trade_date": "2026-10-09",
        "main_net": -1e8, "large_net": None, "super_net": None, "source": "efinance",
    }])
    store.write_rows(tmp_path, "margin", "2026-10-08", [{
        "market": "sse", "symbol": "600519", "name": "贵州茅台", "trade_date": "2026-10-08",
        "row_kind": "detail", "margin_balance": 9.0, "short_balance": 1.0,
        "margin_buy": None, "source": "akshare",
    }, {
        "market": "sse", "symbol": "", "name": "", "trade_date": "2026-10-08",
        "row_kind": "summary", "margin_balance": 30e8, "short_balance": 1e8,
        "margin_buy": None, "source": "akshare",
    }])
    store.write_rows(tmp_path, "southbound", "2026-10-09", [{
        "trade_date": "2026-10-09", "net_flow": 4e8, "source": "akshare",
    }])
    store.write_rows(tmp_path, "etf_shares", "2026-10-08", [{
        "symbol": "510300", "code": "510300", "name": "沪深300ETF",
        "trade_date": "2026-10-08", "shares": 1000.0, "source": "akshare",
    }])
    store.write_rows(tmp_path, "etf_shares", "2026-10-09", [{
        "symbol": "510300", "code": "510300", "name": "沪深300ETF",
        "trade_date": "2026-10-09", "shares": 1100.0, "source": "akshare",
    }, {
        "symbol": "588000", "code": "588000", "name": "科创50ETF",
        "trade_date": "2026-10-09", "shares": 500.0, "source": "akshare",
    }])
    payload = query.board(tmp_path)
    assert payload["industry"]["items"][0]["name"] == "半导体"
    assert payload["concept"]["items"] == []
    # 两只股票对不上全市场门槛，不能排成榜。5 日因子仍按已有交易日计算。
    assert payload["stocks_today"]["partial"] is True
    assert payload["stocks_today"]["items"] == []
    assert payload["stocks_today"]["inflow"] == []
    assert payload["stocks_today"]["outflow"] == []
    assert "不作为" in (payload["stocks_today"]["note"] or "")
    assert payload["stocks_5d"]["partial"] is True
    assert payload["stocks_5d"]["items"] == []
    five = factors.main_net_frame(tmp_path).filter(pl.col("code") == "600519").sort("trade_date")
    assert five["ff_main_net_5d"][-1] == 6e8
    assert payload["margin"]["items"] == [{
        "trade_date": "2026-10-08", "market": "sse",
        "margin_balance": 30e8, "short_balance": 1e8,
    }]
    assert payload["margin"]["days"][0]["label"] == "仅沪市"
    assert payload["margin"]["days"][0]["margin_balance"] == 30e8
    assert payload["margin"]["days"][0]["szse_margin_balance"] is None
    assert payload["southbound"]["items"][0]["net_flow"] == 4e8
    broad = {item["code"]: item for item in payload["etf_shares"]["items"]}
    assert broad["510300"]["broad"] == "沪深300"
    assert broad["510300"]["share_change"] == 100.0
    assert broad["588000"]["broad"] == "科创50"
    assert broad["588000"]["share_change"] is None
    empty = query.board(tmp_path / "missing")
    assert empty["stocks_today"]["items"] == []
    assert empty["etf_shares"]["items"] == []


def test_lhb_backup_is_used_only_when_fuyao_missing(tmp_path, monkeypatch) -> None:
    (tmp_path / "kline_daily" / "date=2026-08-28").mkdir(parents=True)
    store.write_rows(tmp_path, "lhb", "2026-08-28", normalize.normalize_lhb([{
        "代码": "600519", "名称": "贵州茅台", "上榜日": "2026-08-28",
        "龙虎榜净买额": 1.5e8, "涨跌幅": 5.2,
    }]))
    monkeypatch.setattr(dt, "_provider", lambda: None)
    monkeypatch.setattr(dt, "cn_today", lambda: date(2026, 10, 10))
    payload = dt.get_dragon_tiger(tmp_path, date(2026, 8, 28))
    assert payload["source"] == "akshare_lhb"
    assert payload["all"]["stock_items"][0]["change"] == pytest.approx(0.052)
    assert payload["all"]["stock_items"][0]["net_value"] == 1.5e8

    class Provider:
        def dragon_tiger(self, board_type, day):
            return {
                "trade_date": "2026-08-28", "count": 0, "stock_count": 0,
                "stock_items": [{"name": "来自fuyao"}], "hot_money_items": [],
            }

    monkeypatch.setattr(dt, "_provider", lambda: Provider())
    primary = dt.get_dragon_tiger(tmp_path, date(2026, 8, 28))
    assert primary.get("source") != "akshare_lhb"
    assert primary["state"] == "ok"
    assert primary["all"]["stock_items"][0]["name"] == "来自fuyao"


def test_dsa_context_rows_skip_empty() -> None:
    from app.custom.dsa.fund_flow_bridge import context_rows, is_allowed_context_url

    assert context_rows({"items": []}) == []
    rows = context_rows({"items": [{
        "title": "资金进出 2026-10-09",
        "summary": "南向净流入 1.00亿元",
        "published_at": "2026-10-09T16:30:00+08:00",
    }]})
    assert rows[0]["source"] == "TSP资金进出"
    assert is_allowed_context_url("http://app:3018/api/fund-flow/dsa-context")
    assert not is_allowed_context_url("http://evil.example/api/fund-flow/dsa-context")


def test_worker_runs_stock_without_taking_run_slot(tmp_path, monkeypatch) -> None:
    from app.services import pipeline_jobs

    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    monkeypatch.setenv("FUND_FLOW_STOCK", "true")
    monkeypatch.setattr(pipeline_jobs, "try_acquire_run_slot", lambda owner="": (_ for _ in ()).throw(AssertionError("不应占用任务槽")))
    calls = {"n": 0}

    def bill(symbol: str):
        calls["n"] += 1
        return [{"日期": "2026-10-09", "主力净流入": 8, "大单净流入": 1, "超大单净流入": 2}]

    worker = scheduler.FundFlowWorker()
    ran = worker.run_once(
        now=_day(16, 40),
        data_dir=tmp_path,
        clients=SourceClients(history_bill=bill),
        symbols=["600519.SH"],
        trading=True,
        busy=False,
    )
    assert ran == ["stock"]
    assert calls["n"] == 1
    assert store.read_partition(tmp_path, "stock", "2026-10-09")["main_net"][0] == 8


def _raw_parquet(root, kind: str, trade_date: str, frame: pl.DataFrame) -> None:
    path = root / "fund_flow" / kind / f"date={trade_date}"
    path.mkdir(parents=True)
    frame.write_parquet(path / "part.parquet")


def test_board_normalizes_raw_etf_partition_and_skips_other_bad_kinds(tmp_path) -> None:
    _raw_parquet(tmp_path, "etf_shares", "2026-10-08", pl.DataFrame({
        "序号": [1],
        "基金代码": ["510300"],
        "基金简称": ["沪深300ETF"],
        "ETF类型": ["股票型"],
        "统计日期": ["2026-10-08"],
        "基金份额": [2.400e10],
        "stat_date": ["2026-10-08"],
    }))
    _raw_parquet(tmp_path, "etf_shares", "2026-10-09", pl.DataFrame({
        "序号": [1, 2],
        "基金代码": ["510300", "588000"],
        "基金简称": ["沪深300ETF", "科创50ETF"],
        "ETF类型": ["股票型", "股票型"],
        "统计日期": ["2026-10-09", "2026-10-09"],
        "基金份额": [2.439e10, 4.10e9],
        "stat_date": ["2026-10-09", "2026-10-09"],
    }))
    _raw_parquet(tmp_path, "stock", "2026-10-09", pl.DataFrame({"foo": [1]}))
    _raw_parquet(tmp_path, "margin", "2026-10-09", pl.DataFrame({"融资余额": [1.0]}))
    _raw_parquet(tmp_path, "southbound", "2026-10-09", pl.DataFrame({"当日成交净买额": [1.0]}))
    _raw_parquet(tmp_path, "industry", "2026-10-09", pl.DataFrame({"行业": ["半导体"]}))
    payload = query.board(tmp_path)
    broad = {item["code"]: item for item in payload["etf_shares"]["items"]}
    assert broad["510300"]["shares"] == pytest.approx(2.439e10)
    assert broad["510300"]["share_change"] == pytest.approx(0.039e10)
    assert broad["510300"]["broad"] == "沪深300"
    assert broad["588000"]["shares"] == pytest.approx(4.10e9)
    assert payload["stocks_today"]["items"] == []
    assert payload["margin"]["items"] == []
    assert payload["southbound"]["items"] == []
    assert payload["industry"]["items"] == []
    assert query.margin(data_dir=tmp_path)["summary"] == []
    assert query.etf_shares(data_dir=tmp_path)["items"][0]["code"] == "510300"


def test_write_raw_etf_normalizes_and_rejects_unknown_columns(tmp_path) -> None:
    wrote = store.write_rows(tmp_path, "etf_shares", "2026-10-09", [{
        "序号": 1,
        "基金代码": "510300",
        "基金简称": "沪深300ETF",
        "ETF类型": "股票型",
        "统计日期": "2026-10-09",
        "基金份额": 2.439e10,
        "stat_date": "2026-10-09",
    }])
    assert wrote == 1
    frame = pl.read_parquet(tmp_path / "fund_flow" / "etf_shares" / "date=2026-10-09" / "part.parquet")
    assert frame.columns == list(store._SCHEMAS["etf_shares"])
    assert frame["shares"][0] == pytest.approx(2.439e10)
    with pytest.raises(ValueError, match="未知列"):
        store.write_rows(tmp_path, "stock", "2026-10-09", [{
            "code": "600519",
            "trade_date": "2026-10-09",
            "主力净流入": 1,
        }])


def _iso_compact(compact: str) -> str:
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def _etf_ak(name, **kwargs):
    if name == "fund_etf_scale_sse":
        day = _iso_compact(kwargs["date"])
        return [{
            "序号": 1,
            "基金代码": "510300",
            "基金简称": "沪深300ETF",
            "ETF类型": "股票型",
            "统计日期": day,
            "基金份额": 2.439e10,
            "stat_date": day,
        }]
    if name == "fund_scale_daily_szse":
        assert kwargs["symbol"] == "ETF"
        start = date.fromisoformat(_iso_compact(kwargs["start_date"]))
        end = date.fromisoformat(_iso_compact(kwargs["end_date"]))
        assert (end - start).days < jobs.SZSE_WINDOW_DAYS
        rows = []
        cursor = start
        while cursor <= end:
            rows.append({
                "基金代码": "159915",
                "基金简称": "创业板ETF",
                "日期": cursor.isoformat(),
                "基金份额": 1.8e10,
            })
            cursor += timedelta(days=1)
        return rows
    raise AssertionError(name)


def test_etf_backfill_writes_normalized_rows(tmp_path) -> None:
    result = jobs.backfill_etf_shares(
        tmp_path,
        SourceClients(ak_call=_etf_ak),
        trade_dates=["2026-10-08", "2026-10-09"],
        breaker=CircuitBreaker("akshare", clock=lambda: 0),
        sleeper=lambda _seconds: None,
    )
    assert result.errors == []
    assert result.wrote == 4
    saved = store.read_range(tmp_path, "etf_shares").sort("trade_date", "code")
    assert saved["code"].to_list() == ["159915", "510300", "159915", "510300"]
    assert saved.filter(pl.col("code") == "510300")["shares"].to_list() == pytest.approx([2.439e10, 2.439e10])
    assert saved.filter(pl.col("code") == "159915")["shares"].to_list() == pytest.approx([1.8e10, 1.8e10])
    assert "基金份额" not in saved.columns


def test_szse_windows_cover_history_without_exceeding_six_months() -> None:
    days = jobs.etf_share_history_dates(start="2025-10-10", end="2026-10-09")
    assert days[0] == "2025-10-10"
    assert days[-1] == "2026-10-09"
    assert "2026-07-07" in days
    windows = jobs.szse_windows_for_dates(days)
    assert windows[0][0] == date(2025, 10, 10)
    assert len(windows) >= 3
    for start, end in windows:
        assert (end - start).days < jobs.SZSE_WINDOW_DAYS
    sparse = jobs.szse_windows_for_dates(["2025-10-10", "2026-10-09"])
    assert sparse == [
        (date(2025, 10, 10), date(2025, 10, 10)),
        (date(2026, 10, 9), date(2026, 10, 9)),
    ]


def test_sync_etf_shares_keeps_sse_when_szse_fails(tmp_path) -> None:
    def ak(name, **kwargs):
        if name == "fund_etf_scale_sse":
            return _etf_ak(name, **kwargs)
        raise RuntimeError("深交所超时")

    result = jobs.sync_etf_shares(
        tmp_path,
        SourceClients(ak_call=ak),
        trade_date="2026-10-09",
        breaker=CircuitBreaker("akshare", clock=lambda: 0),
        sleeper=lambda _seconds: None,
    )
    assert result.wrote == 1
    assert any("fund_scale_daily_szse" in item for item in result.errors)
    saved = store.read_partition(tmp_path, "etf_shares", "2026-10-09")
    assert saved["code"].to_list() == ["510300"]


def test_board_pins_szse_broad_etfs(tmp_path) -> None:
    store.write_rows(tmp_path, "etf_shares", "2026-10-09", [{
        "symbol": "159915", "code": "159915", "name": "创业板ETF易方达",
        "trade_date": "2026-10-09", "shares": 1.8e10, "source": "akshare",
    }, {
        "symbol": "159919", "code": "159919", "name": "沪深300ETF",
        "trade_date": "2026-10-09", "shares": 2e9, "source": "akshare",
    }, {
        "symbol": "159996", "code": "159996", "name": "创业板成长ETF",
        "trade_date": "2026-10-09", "shares": 3e9, "source": "akshare",
    }])
    items = {item["code"]: item for item in query.board(tmp_path)["etf_shares"]["items"]}
    assert items["159915"]["broad"] == "创业板"
    assert items["159915"]["share_change"] is None
    assert items["159919"]["broad"] == "沪深300"
    assert "159996" not in items


def test_eastmoney_secid_and_bill_request(monkeypatch: pytest.MonkeyPatch) -> None:
    assert eastmoney_secid("600519.SH") == "1.600519"
    assert eastmoney_secid("000001.SZ") == "0.000001"
    assert eastmoney_secid("830001.BJ") == "0.830001"
    assert eastmoney_secid("600519") == "1.600519"
    assert eastmoney_secid("000001") == "0.000001"
    assert eastmoney_secid("") == ""

    captured: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"klines": ["2026-10-09,100,1,2,40,50"]}}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("app.eastmoney.client.em_get", fake_get)
    rows = fetch_history_bill("600519.SH")
    assert captured["params"]["secid"] == "1.600519"
    assert captured["url"].startswith("https://push2his.eastmoney.com/")
    assert "eastmoney.com" in captured["headers"]["Referer"]
    assert rows[0]["日期"] == "2026-10-09"
    assert rows[0]["主力净流入"] == "100"
    assert rows[0]["大单净流入"] == "40"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("空 secid 不应出网")

    monkeypatch.setattr("app.eastmoney.client.em_get", forbidden)
    with pytest.raises(RuntimeError, match="secid"):
        fetch_history_bill("not-a-code")


def test_circuit_open_keeps_rows_and_ranks_the_rest(tmp_path) -> None:
    seen: list[str] = []

    def bill(symbol: str):
        seen.append(symbol)
        if symbol == "600519.SH":
            return [{"日期": "2026-10-09", "主力净流入": 100.0, "大单净流入": 1, "超大单净流入": 2}]
        raise RuntimeError("connection reset")

    mx_calls: list[str] = []

    def mx(symbol: str):
        mx_calls.append(symbol)
        return ([], None)

    ak_calls: list[str] = []

    def ak(name, **kwargs):
        ak_calls.append(name)
        if kwargs.get("symbol") == "即时":
            return [
                {"股票代码": "000001", "股票简称": "平安银行", "净额": 5e8},
                {"股票代码": "000002", "股票简称": "万科A", "净额": -3e8},
                {"股票代码": "600519", "股票简称": "贵州茅台", "净额": 9e8},
            ]
        if kwargs.get("symbol") == "5日排行":
            return [{"股票代码": "000001", "股票简称": "平安银行", "资金流入净额": 8e8}]
        raise AssertionError(name)

    result = jobs.sync_stock(
        tmp_path,
        ["600519.SH", "000001.SZ", "000002.SZ"],
        SourceClients(history_bill=bill, mx_query=mx, ak_call=ak),
        efinance=CircuitBreaker("efinance", clock=lambda: 0, failure_limit=1),
        mx=CircuitBreaker("mx", clock=lambda: 0),
        sleeper=lambda _seconds: None,
        mx_key="test-key",
        mx_max_calls=5,
        trade_date="2026-10-09",
    )
    assert seen == ["600519.SH", "000001.SZ"]
    assert mx_calls == ["000001.SZ"]
    assert ak_calls == ["stock_fund_flow_individual", "stock_fund_flow_individual"]
    assert result.wrote > 0
    frame = store.read_partition(tmp_path, "stock", "2026-10-09")
    by_code = {row["code"]: row for row in frame.to_dicts()}
    assert by_code["600519"]["main_net"] == 100.0
    assert by_code["600519"]["source"] == "efinance"
    assert by_code["000001"]["name"] == "平安银行"
    assert by_code["000001"]["main_net"] == 5e8
    assert by_code["000001"]["main_net_5d"] == 8e8
    assert by_code["000002"]["main_net"] == -3e8
    assert "000002" not in mx_calls


def test_rank_fallback_not_called_when_history_covers_the_day(tmp_path) -> None:
    def bill(symbol: str):
        return [{"日期": "2026-10-09", "主力净流入": 10, "大单净流入": 1, "超大单净流入": 1}]

    def ak(name, **kwargs):
        raise AssertionError(name)

    jobs.sync_stock(
        tmp_path,
        ["600519.SH", "000001.SZ"],
        SourceClients(history_bill=bill, ak_call=ak),
        efinance=CircuitBreaker("efinance", clock=lambda: 0),
        mx=CircuitBreaker("mx", clock=lambda: 0),
        sleeper=lambda _seconds: None,
        mx_key="",
        trade_date="2026-10-09",
    )
    assert store.read_partition(tmp_path, "stock", "2026-10-09").height == 2


def test_szse_empty_keeps_sse_and_tries_previous_day(tmp_path) -> None:
    def ak(name, **kwargs):
        if name == "stock_margin_sse":
            return [{
                "信用交易日期": "2026-10-09",
                "融资余额": 1_291_437_000_000,
                "融券余量金额": 1e8,
            }]
        if name == "stock_margin_szse":
            if kwargs.get("date") == "20261008":
                return [{"融资余额": 12380.27, "融券余额": 2.0}]
            raise ValueError(
                "Length mismatch: Expected axis has 0 elements, new values have 6 elements",
            )
        if name in {"stock_margin_detail_sse", "stock_margin_detail_szse"}:
            return []
        raise AssertionError(name)

    breaker = CircuitBreaker("akshare", clock=lambda: 0)
    result = jobs.sync_margin(
        tmp_path,
        SourceClients(ak_call=ak),
        trade_date="2026-10-09",
        breaker=breaker,
        sleeper=lambda _seconds: None,
    )
    assert breaker.allow()
    assert any("无数据，已跳过" in item for item in result.errors)
    current = store.read_partition(tmp_path, "margin", "2026-10-09")
    sse = current.filter(pl.col("market") == "sse")
    assert sse["margin_balance"][0] == 1_291_437_000_000
    assert current.filter(pl.col("market") == "szse").is_empty()
    previous = store.read_partition(tmp_path, "margin", "2026-10-08")
    assert previous["trade_date"][0] == "2026-10-08"
    assert previous["margin_balance"][0] == 1_238_027_000_000


def test_sse_margin_unit_stays_yuan() -> None:
    raw = 1_291_437_000_000
    rows = normalize.normalize_margin_summary(
        [{"信用交易日期": "2026-10-09", "融资余额": raw, "融券余量金额": 1e8}],
        market="sse",
    )
    assert rows[0]["margin_balance"] == raw
    assert rows[0]["margin_balance"] / 1e8 == pytest.approx(12914.37)
    assert rows[0]["margin_balance"] / 1e12 == pytest.approx(1.291437)


def _saturday(hour: int = 11, minute: int = 0) -> datetime:
    return datetime(2026, 10, 10, hour, minute, tzinfo=CN)


def test_weekend_catchup_fills_missing_stock_partition(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    monkeypatch.setenv("FUND_FLOW_STOCK", "true")
    calls: list[str] = []

    def bill(symbol: str):
        calls.append(symbol)
        return [{"日期": "2026-10-09", "主力净流入": 8, "大单净流入": 1, "超大单净流入": 2}]

    worker = scheduler.FundFlowWorker()
    ran = worker.run_once(
        now=_saturday(),
        data_dir=tmp_path,
        clients=SourceClients(history_bill=bill),
        symbols=["600519.SH"],
        trading=False,
        busy=False,
    )
    assert ran == ["stock"]
    assert calls == ["600519.SH"]
    assert store.read_partition(tmp_path, "stock", "2026-10-09")["main_net"][0] == 8


def test_catchup_skips_complete_partition_and_respects_retry_gap(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    monkeypatch.setenv("FUND_FLOW_STOCK", "true")
    # 覆盖判定现在会读 instruments 维表算各板块应有只数（深主板整段缺失才判不完整）。
    # 测试库的"全市场"就是这一只沪市主板股，维表必须同步写上。
    inst = tmp_path / "instruments"
    inst.mkdir()
    pl.DataFrame({"symbol": ["600519.SH"], "code": ["600519"]}).write_parquet(
        inst / "instruments.parquet"
    )
    store.write_rows(tmp_path, "stock", "2026-10-09", [{
        "symbol": "600519.SH", "code": "600519", "trade_date": "2026-10-09",
        "main_net": 8.0, "large_net": 1.0, "super_net": 2.0, "source": "efinance",
    }])

    def bill(symbol: str):
        raise AssertionError(symbol)

    worker = scheduler.FundFlowWorker()
    skipped = worker.run_once(
        now=_saturday(),
        data_dir=tmp_path,
        clients=SourceClients(history_bill=bill),
        symbols=["600519.SH"],
        trading=False,
        busy=False,
    )
    assert skipped == []

    missing = tmp_path / "gap"
    now = _saturday()
    store.update_kind_state(missing, "stock", last_attempt_at=now.isoformat())
    cooled = scheduler.FundFlowWorker().run_once(
        now=now,
        data_dir=missing,
        clients=SourceClients(history_bill=bill),
        symbols=["600519.SH"],
        trading=False,
        busy=False,
    )
    assert cooled == []
    later = scheduler.FundFlowWorker().run_once(
        now=now + timedelta(minutes=31),
        data_dir=missing,
        clients=SourceClients(history_bill=lambda symbol: [{
            "日期": "2026-10-09", "主力净流入": 3, "大单净流入": 1, "超大单净流入": 1,
        }]),
        symbols=["600519.SH"],
        trading=False,
        busy=False,
    )
    assert later == ["stock"]
    assert store.read_partition(missing, "stock", "2026-10-09")["main_net"][0] == 3


def _stock_row(code: str, symbol: str, main_net: float, *, name: str | None = None, five: float | None = None) -> dict:
    row = {
        "symbol": symbol,
        "code": code,
        "trade_date": "2026-10-09",
        "main_net": main_net,
        "large_net": None,
        "super_net": None,
        "source": "akshare_ths",
    }
    if name is not None:
        row["name"] = name
    if five is not None:
        row["main_net_5d"] = five
    return row


def test_board_hides_partial_ranking_and_ranks_full_sample(tmp_path) -> None:
    partial_rows = [
        _stock_row("600857", "600857.SH", 0.06e8, name="宁波中百"),
        _stock_row("603402", "603402.SH", 0.01e8),
        _stock_row("603098", "603098.SH", 0.01e8),
        _stock_row("603508", "603508.SH", -0.03e8),
        _stock_row("605056", "605056.SH", -0.06e8),
        _stock_row("601139", "601139.SH", -0.16e8),
    ]
    store.write_rows(tmp_path, "stock", "2026-10-09", partial_rows)
    hidden = query.board(tmp_path)
    assert hidden["stocks_today"]["partial"] is True
    assert hidden["stocks_today"]["items"] == []
    assert hidden["stocks_today"]["inflow"] == []
    assert hidden["stocks_today"]["outflow"] == []
    context = query.dsa_context(tmp_path)
    assert all("主力净流入居前" not in item["summary"] for item in context["items"])

    full = tmp_path / "full"
    instruments = full / "instruments"
    instruments.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ", "000002.SZ", "300001.SZ"],
        "code": ["600519", "000001", "000002", "300001"],
        "name": ["贵州茅台", "平安银行", "万科A", "特锐德"],
    }).write_parquet(instruments / "instruments.parquet")
    store.write_rows(full, "stock", "2026-10-09", [
        _stock_row("600519", "600519.SH", 2e8, five=9e8),
        _stock_row("000001", "000001.SZ", 5e8, name="平安银行", five=1e8),
        _stock_row("000002", "000002.SZ", -8e8, five=-2e8),
        _stock_row("300001", "300001.SZ", -1e8, five=4e8),
    ])
    ranked = query.board(full)
    assert ranked["stocks_today"]["partial"] is False
    assert [row["code"] for row in ranked["stocks_today"]["inflow"]] == ["000001", "600519"]
    assert ranked["stocks_today"]["inflow"][0]["name"] == "平安银行"
    assert ranked["stocks_today"]["inflow"][1]["name"] == "贵州茅台"
    assert ranked["stocks_today"]["inflow"][1]["code"] == "600519"
    assert [row["code"] for row in ranked["stocks_today"]["outflow"]] == ["000002", "300001"]
    assert ranked["stocks_today"]["outflow"][0]["name"] == "万科A"
    assert [row["code"] for row in ranked["stocks_5d"]["inflow"]] == ["600519", "300001", "000001"]
    assert ranked["stocks_5d"]["inflow"][0]["ff_main_net_5d"] == 9e8
    assert [row["code"] for row in ranked["stocks_5d"]["outflow"]] == ["000002"]
    summary = query.dsa_context(full)["items"][0]["summary"]
    assert "平安银行" in summary
    assert "600857.SH" not in summary


def test_margin_board_labels_single_exchange_and_sums_both(tmp_path) -> None:
    sse_only = 1_291_437_000_000
    store.write_rows(tmp_path, "margin", "2026-10-09", [{
        "market": "sse", "symbol": "", "name": "", "trade_date": "2026-10-09",
        "row_kind": "summary", "margin_balance": sse_only, "short_balance": 1e8,
        "margin_buy": None, "source": "akshare",
    }])
    one = query.board(tmp_path)["margin"]["days"][0]
    assert one["label"] == "仅沪市"
    assert one["coverage"] == "sse"
    assert one["margin_balance"] == sse_only
    assert one["margin_balance"] / 1e8 == pytest.approx(12914.37)
    assert one["szse_margin_balance"] is None

    both = tmp_path / "both"
    szse = 1_238_027_000_000
    store.write_rows(both, "margin", "2026-10-08", [{
        "market": "sse", "symbol": "", "name": "", "trade_date": "2026-10-08",
        "row_kind": "summary", "margin_balance": 1_298_438_000_000, "short_balance": 1e8,
        "margin_buy": None, "source": "akshare",
    }, {
        "market": "szse", "symbol": "", "name": "", "trade_date": "2026-10-08",
        "row_kind": "summary", "margin_balance": szse, "short_balance": 2e8,
        "margin_buy": None, "source": "akshare",
    }])
    day = query.board(both)["margin"]["days"][0]
    assert day["label"] == "沪深合计"
    assert day["coverage"] == "both"
    assert day["margin_balance"] == 1_298_438_000_000 + szse
    assert day["sse_margin_balance"] == 1_298_438_000_000
    assert day["szse_margin_balance"] == szse


def test_szse_margin_summary_converts_yi_and_repairs_old_partitions(tmp_path) -> None:
    """深市汇总按亿元入库前要换成元；已经是元的行、沪市和明细不能再乘。"""
    rows = normalize.normalize_margin_summary(
        [{"融资余额": 12380.27, "融券余额": 2.0, "融资买入额": 100.5}],
        market="szse",
        trade_date="2026-10-09",
    )
    assert rows[0]["margin_balance"] == pytest.approx(1_238_027_000_000)
    assert rows[0]["short_balance"] == pytest.approx(2e8)
    assert rows[0]["margin_buy"] == pytest.approx(100.5 * 1e8)

    sse = normalize.normalize_margin_summary(
        [{"信用交易日期": "2026-10-09", "融资余额": 1_291_437_000_000}],
        market="sse",
    )
    assert sse[0]["margin_balance"] == 1_291_437_000_000

    detail = normalize.normalize_margin_detail(
        [{"证券代码": "000001", "证券简称": "平安银行", "融资余额": 12380.27}],
        market="szse",
        trade_date="2026-10-09",
    )
    assert detail[0]["margin_balance"] == 12380.27
    assert detail[0]["row_kind"] == "detail"

    store.write_rows(tmp_path, "margin", "2026-10-09", [
        {
            "market": "szse", "symbol": "", "name": "", "trade_date": "2026-10-09",
            "row_kind": "summary", "margin_balance": 12380.27, "short_balance": 2.0,
            "margin_buy": 0, "source": "akshare",
        },
        {
            "market": "sse", "symbol": "", "name": "", "trade_date": "2026-10-09",
            "row_kind": "summary", "margin_balance": 1_291_437_000_000, "short_balance": 1e8,
            "margin_buy": None, "source": "akshare",
        },
        {
            "market": "szse", "symbol": "000001", "name": "平安银行", "trade_date": "2026-10-09",
            "row_kind": "detail", "margin_balance": 12380.27, "short_balance": None,
            "margin_buy": None, "source": "akshare",
        },
    ])
    assert store.repair_szse_margin_summaries(tmp_path) == 2
    assert store.repair_szse_margin_summaries(tmp_path) == 0
    frame = store.read_partition(tmp_path, "margin", "2026-10-09")
    summary = frame.filter(pl.col("row_kind") == "summary")
    szse = summary.filter(pl.col("market") == "szse").row(0, named=True)
    sse_row = summary.filter(pl.col("market") == "sse").row(0, named=True)
    assert szse["margin_balance"] == pytest.approx(1_238_027_000_000)
    assert szse["short_balance"] == pytest.approx(2e8)
    assert szse["margin_buy"] == 0
    assert sse_row["margin_balance"] == 1_291_437_000_000
    detail_row = frame.filter(pl.col("row_kind") == "detail").row(0, named=True)
    assert detail_row["margin_balance"] == pytest.approx(12380.27)

    day = query.board(tmp_path)["margin"]["days"][0]
    assert day["label"] == "沪深合计"
    assert day["szse_margin_balance"] == pytest.approx(1_238_027_000_000)
    assert day["margin_balance"] == pytest.approx(1_291_437_000_000 + 1_238_027_000_000)


def test_industry_by_code_uses_level2_to_match_fund_rank_names(tmp_path) -> None:
    """同花顺行业是三级串，资金榜 name 是二级名。取末段会 0 命中。"""
    ext = tmp_path / "ext_data" / "ext_hy_ths"
    ext.mkdir(parents=True)
    pl.DataFrame({
        "code": ["000001", "600519", "300835"],
        "所属同花顺行业": ["银行-银行-股份制银行", "食品饮料-白酒-白酒Ⅲ", "电子-半导体-半导体材料"],
    }).write_parquet(ext / "part.parquet")
    by_code = factors._industry_by_code(tmp_path)
    got = {row["code"]: row["industry"] for row in by_code.to_dicts()}
    assert got == {"000001": "银行", "600519": "白酒", "300835": "半导体"}
    # 二级名必须与资金榜 name 同口径（东财/同花顺行业榜用的就是二级）
    assert "银行" in got.values()
    assert not any("-" in name for name in got.values())


def test_ths_industry_level2_handles_short_and_empty() -> None:
    assert factors.ths_industry_level2("银行-银行-股份制银行") == "银行"
    assert factors.ths_industry_level2("电子-半导体-半导体材料") == "半导体"
    # 只有一级时原样返回，不要抛
    assert factors.ths_industry_level2("银行") == "银行"
    assert factors.ths_industry_level2("") is None
    assert factors.ths_industry_level2(None) is None


def test_sector_rank_attaches_when_industry_is_level2(tmp_path) -> None:
    """attach_columns 的 ff_sector_net_inflow_rank 必须能贴上（此前恒为 null）。"""
    d = tmp_path
    (d / "fund_flow").mkdir()
    ext = d / "ext_data" / "ext_hy_ths"
    ext.mkdir(parents=True)
    pl.DataFrame({
        "code": ["000001"],
        "所属同花顺行业": ["银行-银行-股份制银行"],
    }).write_parquet(ext / "part.parquet")
    store.write_rows(d, "industry", "2026-10-09", [
        {"name": "银行", "trade_date": "2026-10-09", "captured_at": "2026-10-09T15:05:00",
         "snapshot": "close", "net_inflow": 1.2e9, "rank": 7, "source": "akshare_ths"},
    ])
    frame = pl.DataFrame({"symbol": ["000001.SZ"], "date": ["2026-10-09"]})
    out = factors.attach_columns(frame, include_rank=True, data_dir=d)
    assert "ff_sector_net_inflow_rank" in out.columns
    assert out["ff_sector_net_inflow_rank"][0] == 7


def test_missing_boards_flags_absent_deep_main_board() -> None:
    """3723 只全是创业板/科创板/沪主板时，深主板整段缺失必须被识别。"""
    codes = (
        [f"300{i:03d}" for i in range(900)]      # 创业板 900
        + [f"688{i:03d}" for i in range(600)]    # 科创板 600
        + [f"600{i:03d}" for i in range(700)]    # 沪主板 700
    )
    frame = pl.DataFrame({"code": codes})
    gaps = store.missing_boards(frame)
    assert "sz_main" in gaps  # 000/001/002/003 一只都没有
    assert "sz_gem" not in gaps
    assert "sh_main" not in gaps
    assert "star" not in gaps


def test_partial_ranking_detects_board_gap_despite_large_count() -> None:
    """总数远超 FULL_MARKET_MIN，但缺深主板 → 仍判不完整，否则永不重拉。"""
    codes = [f"300{i:03d}" for i in range(900)] + [f"600{i:03d}" for i in range(700)]
    frame = pl.DataFrame({"code": codes})
    count = store.code_count(frame)
    assert count > store.FULL_MARKET_MIN
    assert store.is_partial_ranking(count, len(codes), frame) is True
    # 传 None 时退化成只看总数，保持旧行为
    assert store.is_partial_ranking(count, None) is False


def test_partial_ranking_respects_small_universe_from_instruments(tmp_path) -> None:
    """小库只有沪市主板时不该因为「缺深主板」被反复判不完整。"""
    inst = tmp_path / "instruments"
    inst.mkdir()
    pl.DataFrame({"symbol": ["600519.SH", "601398.SH"], "code": ["600519", "601398"]}).write_parquet(
        inst / "instruments.parquet"
    )
    expected, full = store.board_coverage(tmp_path)
    assert expected == {"sh_main"}
    frame = pl.DataFrame({"code": ["600519", "601398"]})
    assert store.is_partial_ranking(2, 2, frame, expected=expected, full=full) is False
    # 少了其中一只 → 板块覆盖不足 20% 应有只数 → 判不完整
    assert store.is_partial_ranking(1, 2, pl.DataFrame({"code": ["600519"]}),
                                    expected=expected, full=full) is True


def test_board_coverage_reports_each_board_size(tmp_path) -> None:
    inst = tmp_path / "instruments"
    inst.mkdir()
    pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ", "000002.SZ", "300001.SZ", "688981.SH"],
        "code": ["600519", "000001", "000002", "300001", "688981"],
    }).write_parquet(inst / "instruments.parquet")
    expected, full = store.board_coverage(tmp_path)
    assert expected == {"sh_main", "sz_main", "sz_gem", "star"}
    assert full == {"sh_main": 1, "sz_main": 2, "sz_gem": 1, "star": 1}


def test_listed_boards_returns_none_without_instruments(tmp_path) -> None:
    assert store.listed_boards(tmp_path) is None
    assert store.board_coverage(tmp_path) == (None, None)


def test_board_side_keeps_ranking_when_upstream_misses_a_board(tmp_path) -> None:
    """上游缺整段板块时，个股榜必须照常展示，不能整块变空（线上回归）。

    同花顺个股榜不覆盖深主板，分区 3723 只但 missing_boards=['sz_main']。
    若展示层把板块缺口也当成"样本不够"，个股主力净流入榜和 DSA
    「主力净流入居前」会永久消失 —— 板块缺口只该决定要不要重拉。
    """
    inst = tmp_path / "instruments"
    inst.mkdir()
    rows = ([f"300{i:03d}" for i in range(900)] + [f"600{i:03d}" for i in range(700)]
            + [f"000{i:03d}" for i in range(200)])  # 深主板应有 200 只，实际一只没有
    pl.DataFrame({
        "symbol": [f"{c}.SZ" if c[0] in "03" else f"{c}.SH" for c in rows],
        "code": rows,
    }).write_parquet(inst / "instruments.parquet")
    store.write_rows(tmp_path, "stock", "2026-10-09", [
        {"code": c, "trade_date": "2026-10-09", "main_net": float(i + 1), "source": "akshare_ths"}
        for i, c in enumerate(r for r in rows if not r.startswith("000"))
    ])

    today = query._stocks_today(tmp_path)
    assert today["partial"] is False          # 不能因板块缺口变 partial
    assert len(today["inflow"]) > 0           # 榜单必须有内容
    assert today["note"] and "深市主板" in today["note"]   # 缺口要以 note 提示
    context = query.dsa_context(tmp_path)
    assert any("主力净流入居前" in item["summary"] for item in context["items"])


def test_refresh_scoped_to_missing_board_only() -> None:
    """缺口重拉只碰缺失板块的代码，已拉到的照常 skip。"""
    existing = ["300001", "600519", "688981"]     # 已有：创业板/沪主板/科创板
    refresh_boards = {"sz_main"}                  # 只有深主板缺
    for code in existing:
        assert store.board_of(code) not in refresh_boards   # → keep skip
    for code in ("000001", "002415", "001979", "003816"):
        assert store.board_of(code) in refresh_boards       # → 才重拉


def test_board_gap_tries_are_capped_per_day() -> None:
    """同一交易日板块缺口补拉次数封顶，避免每 30 分钟全量重打东财。"""
    from app.fund_flow import scheduler as ff_scheduler

    assert ff_scheduler.BOARD_GAP_MAX_TRIES >= 1
    st = {"board_gap_date": "2026-10-09", "board_gap_tries": ff_scheduler.BOARD_GAP_MAX_TRIES}
    assert ff_scheduler._board_gap_exhausted(st, "2026-10-09") is True
    # 换一天重新计数
    assert ff_scheduler._board_gap_exhausted(st, "2026-10-10") is False
    # 没记过就是不限
    assert ff_scheduler._board_gap_exhausted({}, "2026-10-09") is False


def test_bj_board_includes_920_segment() -> None:
    """北交所新段 920xxx 必须归到 bj，不能算 unknown 被忽略。"""
    for code in ("920001", "920999", "830799", "430047"):
        assert store.board_of(code) == "bj"


def test_board_gap_note_labels_are_human_readable() -> None:
    assert query.board_gap_note([]) is None
    assert query.board_gap_note(["sz_main"]) == "上游个股资金榜未覆盖：深市主板"
    assert "深市主板" in query.board_gap_note(["sz_main", "bj"])


def test_etf_share_history_skips_holidays_without_tripping_breaker(tmp_path):
    calls: list[str] = []

    class _Clients:
        def ak(self, name, **kwargs):
            if name == "fund_etf_scale_sse":
                day = kwargs["date"]
                calls.append(day)
                if day in {"20260101", "20260102"}:
                    raise KeyError("None of [Index(['基金代码'], dtype='str')] are in the [columns]")
                iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
                return [{"基金代码": "510300", "基金简称": "300ETF", "统计日期": iso, "基金份额": 100.0}]
            return []

    breaker = CircuitBreaker("akshare", failure_limit=2)
    res = jobs.backfill_etf_shares(
        tmp_path, _Clients(), trade_dates=["2026-01-01", "2026-01-02", "2026-01-05"],
        breaker=breaker, sleeper=lambda _s: None,
    )
    assert breaker.allow()
    assert not any("fund_etf_scale_sse" in e for e in res.errors)
    assert "20260105" in calls


def test_etf_share_history_uses_local_calendar(tmp_path):
    for day in ("2025-12-31", "2026-01-05"):
        (tmp_path / "kline_daily" / f"date={day}").mkdir(parents=True)
    seen: list[str] = []
    orig = jobs.backfill_etf_shares
    try:
        jobs.backfill_etf_shares = (
            lambda *a, trade_dates, **k: seen.extend(trade_dates) or jobs.RunResult(kind="etf_shares")
        )
        jobs.backfill_etf_share_history(
            tmp_path, None, breaker=None, start="2025-12-31", end="2026-01-05",
        )
    finally:
        jobs.backfill_etf_shares = orig
    assert seen == ["2025-12-31", "2026-01-05"]


def test_etf_share_history_falls_back_to_weekdays_when_calendar_misses_start(tmp_path):
    """本地日 K 盖不住回补起点时，仍按工作日请求，节假日交给空表兜底。"""
    (tmp_path / "kline_daily" / "date=2026-01-05").mkdir(parents=True)
    seen: list[str] = []
    orig = jobs.backfill_etf_shares

    def _capture(*_args, trade_dates, **_kwargs):
        seen.extend(trade_dates)
        return jobs.RunResult(kind="etf_shares")

    try:
        jobs.backfill_etf_shares = _capture
        jobs.backfill_etf_share_history(
            tmp_path, None, breaker=None, start="2026-01-01", end="2026-01-05",
        )
    finally:
        jobs.backfill_etf_shares = orig
    assert seen == ["2026-01-01", "2026-01-02", "2026-01-05"]


def test_etf_share_sse_other_keyerror_still_opens_breaker(tmp_path):
    """不是休市空表的 KeyError 仍算源故障，该计入熔断。"""

    class _Clients:
        def ak(self, name, **kwargs):
            if name == "fund_etf_scale_sse":
                raise KeyError("基金代码")
            return []

    breaker = CircuitBreaker("akshare", failure_limit=2)
    res = jobs.backfill_etf_shares(
        tmp_path,
        _Clients(),
        trade_dates=["2026-01-05"],
        breaker=breaker,
        sleeper=lambda _s: None,
    )
    assert not breaker.allow()
    assert any("fund_etf_scale_sse" in item for item in res.errors)


def test_four_missing_star_names_are_not_an_entire_board() -> None:
    universe = [f"688{i:03d}" for i in range(100)] + [f"000{i:03d}" for i in range(1, 81)]
    assert jobs._entirely_missing_labels(universe[:4], universe) == []
    assert jobs._entirely_missing_labels([f"000{i:03d}" for i in range(1, 81)], universe) == ["深市主板"]


def test_sz_main_gap_uses_one_board_batch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "ranking_limit", lambda _universe: 1)
    monkeypatch.setattr(store, "missing_boards", lambda _frame, **_kwargs: ["sz_main"])
    store.write_rows(tmp_path, "stock", "2026-10-09", [{
        "symbol": "600519",
        "code": "600519",
        "name": "贵州茅台",
        "trade_date": "2026-10-09",
        "main_net": 10.0,
        "large_net": 1.0,
        "super_net": 2.0,
        "source": "efinance",
    }])
    bills: list[str] = []
    seen: list[dict] = []

    def bill(symbol: str):
        bills.append(symbol)
        raise AssertionError(symbol)

    def fake(url, *, params=None, headers=None, timeout=None):
        seen.append({"url": url, "timeout": timeout, "params": params or {}})
        assert "datacenter-web.eastmoney.com" in url
        assert params["reportName"] == "RPT_DMSK_TS_FUNDFLOW"
        assert params["filter"] == "(TRADE_DATE='2026-10-09')"
        assert "NET_INFLOW" in params["columns"]
        return type("Response", (), {"json": lambda self: {
            "success": True,
            "result": {"pages": 1, "data": [
                {
                    "SECURITY_CODE": "000001",
                    "SECURITY_NAME_ABBR": "平安银行",
                    "NET_INFLOW": -4647.8428,
                    "BIGDEAL_NET": -2866.7888,
                    "SUPERDEAL_NET": -1781.054,
                },
                {
                    "SECURITY_CODE": "600519",
                    "SECURITY_NAME_ABBR": "贵州茅台",
                    "NET_INFLOW": 1,
                    "BIGDEAL_NET": 1,
                    "SUPERDEAL_NET": 1,
                },
            ]},
        }})()

    monkeypatch.setattr("app.eastmoney.client.em_get", fake)
    jobs.sync_stock(
        tmp_path,
        ["600519.SH", "000001.SZ"],
        SourceClients(history_bill=bill, ak_call=lambda *args, **kwargs: []),
        efinance=CircuitBreaker("efinance", clock=lambda: 0),
        mx=CircuitBreaker("mx", clock=lambda: 0),
        sleeper=lambda _seconds: None,
        mx_key="",
        trade_date="2026-10-09",
    )
    assert bills == []
    assert seen and "datacenter-web.eastmoney.com" in seen[0]["url"]
    assert "push2" not in seen[0]["url"]
    assert seen[0]["timeout"] == (10.0, 30.0)
    frame = store.read_partition(tmp_path, "stock", "2026-10-09")
    row = {item["code"]: item for item in frame.to_dicts()}
    assert row["000001"]["main_net"] == pytest.approx(-46478428)
    assert row["000001"]["large_net"] == pytest.approx(-28667888)
    assert row["000001"]["super_net"] == pytest.approx(-17810540)
    assert row["000001"]["source"] == "eastmoney_sz_main"
    assert row["600519"]["source"] == "efinance"


def test_bj_gap_uses_code_range_pages_and_converts_wan_to_yuan(tmp_path, monkeypatch) -> None:
    """同花顺榜没有北交所。数据中心按代码段分页补，万元换成元，不打 push2，也不逐只回历史账单。"""
    monkeypatch.setattr(store, "ranking_limit", lambda _universe: 1)
    monkeypatch.setattr(store, "missing_boards", lambda _frame, **_kwargs: ["bj"])
    store.write_rows(tmp_path, "stock", "2026-10-09", [{
        "symbol": "600519",
        "code": "600519",
        "name": "贵州茅台",
        "trade_date": "2026-10-09",
        "main_net": 10.0,
        "source": "efinance",
    }])
    bills: list[str] = []
    seen: list[dict] = []

    def bill(symbol: str):
        bills.append(symbol)
        raise AssertionError(symbol)

    def fake(url, *, params=None, headers=None, timeout=None):
        params = params or {}
        seen.append({"url": url, "timeout": timeout, "params": params})
        assert "datacenter-web.eastmoney.com" in url
        assert params["reportName"] == "RPT_DMSK_TS_FUNDFLOW"
        assert "push2" not in url
        filt = params["filter"]
        assert filt.startswith("(TRADE_DATE='2026-10-09')")
        if '920000' in filt:
            rows = [
                {
                    "SECURITY_CODE": "920001",
                    "SECURITY_NAME_ABBR": "纬达光电",
                    "NET_INFLOW": 39.1608,
                    "BIGDEAL_NET": 10.5,
                    "SUPERDEAL_NET": 28.6608,
                },
                {
                    "SECURITY_CODE": "600519",
                    "SECURITY_NAME_ABBR": "贵州茅台",
                    "NET_INFLOW": 1,
                    "BIGDEAL_NET": 1,
                    "SUPERDEAL_NET": 1,
                },
            ]
            pages = 1
        elif '800000' in filt:
            rows = [{
                "SECURITY_CODE": "832566",
                "SECURITY_NAME_ABBR": "梓橦宫",
                "NET_INFLOW": -2.5,
                "BIGDEAL_NET": -1.5,
                "SUPERDEAL_NET": -1,
            }]
            pages = 1
        else:
            return type("Response", (), {"json": lambda self: {"success": False, "code": 9201, "message": "返回数据为空"}})()
        return type("Response", (), {"json": lambda self: {
            "success": True,
            "result": {"pages": pages, "data": rows},
        }})()

    monkeypatch.setattr("app.eastmoney.client.em_get", fake)
    jobs.sync_stock(
        tmp_path,
        ["600519.SH", "920001.BJ", "832566.BJ"],
        SourceClients(history_bill=bill, ak_call=lambda *args, **kwargs: []),
        efinance=CircuitBreaker("efinance", clock=lambda: 0),
        mx=CircuitBreaker("mx", clock=lambda: 0),
        sleeper=lambda _seconds: None,
        mx_key="",
        trade_date="2026-10-09",
    )
    assert bills == []
    filters = [item["params"]["filter"] for item in seen]
    assert any("920000" in item for item in filters)
    assert any("800000" in item for item in filters)
    assert any("430000" in item for item in filters)
    assert all(item["timeout"] == (10.0, 30.0) for item in seen)
    frame = store.read_partition(tmp_path, "stock", "2026-10-09")
    row = {item["code"]: item for item in frame.to_dicts()}
    assert row["920001"]["main_net"] == pytest.approx(391608)
    assert row["920001"]["large_net"] == pytest.approx(105000)
    assert row["920001"]["super_net"] == pytest.approx(286608)
    assert row["920001"]["source"] == "eastmoney_bj"
    assert row["832566"]["main_net"] == pytest.approx(-25000)
    assert row["832566"]["source"] == "eastmoney_bj"
    assert "600519" in row
    assert row["600519"]["source"] == "efinance"
    assert row["600519"]["main_net"] == pytest.approx(10.0)


def test_bj_batch_failure_does_not_fall_back_to_history_bill(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "ranking_limit", lambda _universe: 1)
    monkeypatch.setattr(store, "missing_boards", lambda _frame, **_kwargs: ["bj"])
    store.write_rows(tmp_path, "stock", "2026-10-09", [{
        "symbol": "600519",
        "code": "600519",
        "name": "贵州茅台",
        "trade_date": "2026-10-09",
        "main_net": 10.0,
        "source": "efinance",
    }])
    bills: list[str] = []

    def bill(symbol: str):
        bills.append(symbol)
        raise AssertionError(symbol)

    def fake(*_args, **_kwargs):
        raise RuntimeError("datacenter down")

    monkeypatch.setattr("app.eastmoney.client.em_get", fake)
    result = jobs.sync_stock(
        tmp_path,
        ["600519.SH", "920001.BJ"],
        SourceClients(history_bill=bill, ak_call=lambda *args, **kwargs: []),
        efinance=CircuitBreaker("efinance", clock=lambda: 0),
        mx=CircuitBreaker("mx", clock=lambda: 0),
        sleeper=lambda _seconds: None,
        mx_key="",
        trade_date="2026-10-09",
    )
    assert bills == []
    assert any("北交所" in item and "整批失败" in item for item in result.errors)
    assert store.read_partition(tmp_path, "stock", "2026-10-09").height == 1


def test_sz_main_batch_failure_does_not_fall_back_to_history_bill(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "ranking_limit", lambda _universe: 1)
    monkeypatch.setattr(store, "missing_boards", lambda _frame, **_kwargs: ["sz_main"])
    store.write_rows(tmp_path, "stock", "2026-10-09", [{
        "symbol": "600519",
        "code": "600519",
        "name": "贵州茅台",
        "trade_date": "2026-10-09",
        "main_net": 10.0,
        "source": "efinance",
    }])
    bills: list[str] = []

    def bill(symbol: str):
        bills.append(symbol)
        raise AssertionError(symbol)

    def fake(*_args, **_kwargs):
        raise RuntimeError("push2 down")

    monkeypatch.setattr("app.eastmoney.client.em_get", fake)
    result = jobs.sync_stock(
        tmp_path,
        ["600519.SH", "000001.SZ"],
        SourceClients(history_bill=bill, ak_call=lambda *args, **kwargs: []),
        efinance=CircuitBreaker("efinance", clock=lambda: 0),
        mx=CircuitBreaker("mx", clock=lambda: 0),
        sleeper=lambda _seconds: None,
        mx_key="",
        trade_date="2026-10-09",
    )
    assert bills == []
    assert any("整批失败" in item for item in result.errors)
    assert store.read_partition(tmp_path, "stock", "2026-10-09").height == 1
