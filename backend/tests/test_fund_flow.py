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
from app.fund_flow.sources import AK_ALLOWLIST, BLOCKED_AK_NAMES, SourceClients
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
        "基金代码": "510050",
        "基金简称": "50ETF",
        "统计日期": "2026-10-09",
        "基金份额": 5533716.68,
    }])
    assert shares[0]["shares"] == pytest.approx(5533716.68 * 1e4)


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
