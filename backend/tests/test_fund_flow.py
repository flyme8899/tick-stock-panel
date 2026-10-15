"""资金进出：单位、落盘、换源、调度和只读接口。不访问网络。"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime, timedelta, timezone

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
        "stock_intraday": True,
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
        "FUND_FLOW_ENABLED", "FUND_FLOW_STOCK", "FUND_FLOW_STOCK_INTRADAY", "FUND_FLOW_SECTOR",
        "FUND_FLOW_MARGIN", "FUND_FLOW_ETF_SHARES", "FUND_FLOW_SOUTHBOUND",
        "FUND_FLOW_NORTHBOUND_TURNOVER", "FUND_FLOW_LHB_BACKUP", "FUND_FLOW_INTRADAY_PRIMARY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert config.enabled() is False
    assert config.intraday_primary() == "eastmoney"
    assert config.stock_enabled() is False
    monkeypatch.setenv("FUND_FLOW_STOCK", "true")
    assert config.stock_enabled() is False
    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    assert config.stock_enabled() is True
    assert config.stock_intraday_enabled() is True
    monkeypatch.setenv("FUND_FLOW_STOCK_INTRADAY", "false")
    assert config.stock_intraday_enabled() is False
    monkeypatch.setenv("FUND_FLOW_STOCK_INTRADAY", "true")
    assert config.stock_intraday_enabled() is True


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
    assert "stock_intraday" in scheduler.due_tasks(
        _day(10, 0), trading=True, state=state, flags=_flags(), breakers_open=set(),
    )
    assert "stock" not in scheduler.due_tasks(
        _day(10, 0), trading=True, state=state, flags=_flags(), breakers_open=set(),
    )
    lunch = scheduler.due_tasks(
        _day(12, 0), trading=True, state=state, flags=_flags(), breakers_open=set(),
    )
    assert "stock_intraday" not in lunch
    assert "sector_intraday" in lunch
    afternoon = scheduler.due_tasks(
        _day(16, 45), trading=True, state=state, flags=_flags(), breakers_open=set(),
    )
    assert "stock" in afternoon
    assert "southbound" in afternoon
    assert "sector_intraday" not in afternoon
    assert "stock_intraday" not in afternoon
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
    # 让路也要刷心跳 —— 否则运维看到的心跳会停住，误判成调度器挂了。
    hb = store.load_state(tmp_path)["kinds"]["_scheduler"]
    assert hb["deferred"] == "tickflow_sync"
    assert hb["updated_at"]


def test_defer_cap_forces_a_round(tmp_path, monkeypatch) -> None:
    """让路有上限：连续让路到阈值就强行放行，不能让循环无限空转。

    ``tickflow_sync_busy`` 一旦恒真，没有上限的实现会一轮轮静默空转、整天不落盘。
    """
    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    monkeypatch.setenv("FUND_FLOW_STOCK", "true")
    worker = scheduler.FundFlowWorker()

    def run_once(busy: bool):
        return worker.run_once(
            now=_day(17, 0),
            data_dir=tmp_path,
            clients=SourceClients(history_bill=lambda _s: []),
            symbols=["600519.SH"],
            trading=True,
            busy=busy,
        )

    # 阈值内一直让路。
    for _ in range(scheduler.DEFER_MAX_STREAK):
        assert run_once(True) == []
    assert worker._defer_streak == scheduler.DEFER_MAX_STREAK
    # 再一轮越过阈值 → 即使上游仍忙也放行。
    worker.run_once(
        now=_day(17, 0),
        data_dir=tmp_path,
        clients=SourceClients(history_bill=lambda _s: []),
        symbols=["600519.SH"],
        trading=True,
        busy=True,
    )
    assert worker._defer_streak == 0, "放行后计数要归零，避免下一轮又立刻放行"
    # 上游恢复后计数器同样复位。
    run_once(False)
    assert worker._defer_streak == 0
    assert worker.deferred_reason is None


def test_heartbeat_never_goes_backwards(tmp_path) -> None:
    """心跳只前进不后退 —— 陈旧快照写回不能把它拽回去。

    线上现象：文件 mtime 是 17:36（确实在写），``_scheduler.updated_at`` 却停在
    15:42。并发的读改写把旧值复活了，停摆信号失真。``touch_heartbeat`` 拒绝更旧的值。
    """
    later = datetime(2026, 10, 13, 17, 36, 0)
    earlier = datetime(2026, 10, 13, 15, 42, 46)
    store.set_heartbeat_clock(lambda: later)
    try:
        store.touch_heartbeat(tmp_path, deferred=None)
        assert store.load_state(tmp_path)["kinds"]["_scheduler"]["updated_at"] == "2026-10-13T17:36:00"
        # 换成一个更旧的时钟（模拟陈旧快照写回），不该倒退。
        store.set_heartbeat_clock(lambda: earlier)
        store.touch_heartbeat(tmp_path, deferred=None)
        assert store.load_state(tmp_path)["kinds"]["_scheduler"]["updated_at"] == "2026-10-13T17:36:00"
        assert store.load_state(tmp_path)["kinds"]["_scheduler"]["updated_at"] != earlier.isoformat()
    finally:
        store.set_heartbeat_clock(None)


def test_stale_intraday_thread_is_restarted(tmp_path, monkeypatch) -> None:
    """盘中旁路线程卡死超时后，不再挡住下一轮 —— 否则当天剩余时段再也不出快照。

    线上曾停在 13:44：``_intraday_running()`` 只看 ``is_alive()``，线程卡在网络读上
    时恒为真，后续每轮都只打「仍在拉取」就 continue。
    """
    worker = scheduler.FundFlowWorker()

    class FakeThread:
        def __init__(self, alive: bool) -> None:
            self._alive = alive

        def is_alive(self) -> bool:
            return self._alive

    # 刚启动的线程：视为在跑，不重复启动。
    worker._intraday_thread = FakeThread(True)
    worker._intraday_started_at = scheduler.time_time()
    assert worker._intraday_running() is True

    # 已跑超过超时阈值：判定卡死，允许重启。
    worker._intraday_started_at = scheduler.time_time() - scheduler.INTRADAY_THREAD_TIMEOUT_S - 1
    assert worker._intraday_running() is False

    # 线程结束：照旧视为没在跑。
    worker._intraday_thread = FakeThread(False)
    worker._intraday_started_at = scheduler.time_time()
    assert worker._intraday_running() is False


def test_stale_intraday_result_is_discarded(tmp_path, monkeypatch) -> None:
    """卡死重启后，旧代线程迟到的结果必须丢弃，不能盖过新代。

    评审指出：Python 线程杀不掉，超时重启只是叠加一个新线程。旧线程若之后解卡，
    会和新线程同时写盘、同时标成功，可能用一份过时快照掩盖新代的失败。

    复审进一步指出：光在 ``sync_stock_intraday`` **返回之后**查世代是不够的 ——
    写盘发生在该函数**内部**。所以这里**走真实的 ``sync_stock_intraday``**，
    并断言旧代**没有留下分区**，而不只是「没标成功」。
    """
    monkeypatch.delenv("FUND_FLOW_INTRADAY_PRIMARY", raising=False)
    day = "2026-10-13"

    def ak(name, **kwargs):
        assert name == "stock_individual_fund_flow_rank"
        return _eastmoney_rank_rows()

    clients = SourceClients(ak_call=ak)

    # 旧代迟到的这一次：回调说「你已经不是当前代了」。
    stale = jobs.sync_stock_intraday(
        tmp_path,
        clients,
        trade_date=day,
        captured_at=f"{day}T13:44:50",
        breaker=_breaker(),
        is_current=lambda: False,
    )
    assert stale.errors == [jobs.STALE_GENERATION]
    assert stale.wrote == 0
    # 关键断言：旧代没有留下任何分区，状态也没被碰过。
    assert store.read_partition(tmp_path, "stock_intraday", day).is_empty()
    assert not (tmp_path / "stock_intraday").exists()

    # 当前代同样数据 → 正常落盘。
    fresh = jobs.sync_stock_intraday(
        tmp_path,
        clients,
        trade_date=day,
        captured_at=f"{day}T13:45:00",
        breaker=_breaker(),
        is_current=lambda: True,
    )
    assert fresh.errors == []
    assert fresh.wrote == store.FULL_MARKET_MIN
    assert store.read_partition(tmp_path, "stock_intraday", day).height == store.FULL_MARKET_MIN


def test_stale_generation_also_guards_ths_fallback(tmp_path, monkeypatch) -> None:
    """同花顺兜底那条路也要拦 —— 它走的是 ``_write_intraday``，另一个落盘点。

    东财行数不够（或直接报错）时改走同花顺即时榜，落盘发生在 ``_write_intraday``
    里。只堵东财那一处等于没堵，旧代照样能从兜底路径写进去。
    """
    monkeypatch.setenv("FUND_FLOW_INTRADAY_PRIMARY", "eastmoney")
    day = "2026-10-13"

    def ak(name, **kwargs):
        if name == "stock_individual_fund_flow_rank":
            raise TimeoutError("read timed out")
        return [{"股票代码": "600519", "股票简称": "贵州茅台", "净额": "1.5亿"}]

    stale = jobs.sync_stock_intraday(
        tmp_path,
        SourceClients(ak_call=ak),
        trade_date=day,
        captured_at=f"{day}T13:44:50",
        breaker=_breaker(),
        is_current=lambda: False,
    )
    assert stale.errors == [jobs.STALE_GENERATION]
    assert stale.wrote == 0
    assert not (tmp_path / "stock_intraday").exists(), "旧代不该从兜底路径写进分区"


def test_stale_generation_skips_breaker_persist(tmp_path, monkeypatch) -> None:
    """过期代连熔断状态都不回写 —— 那是新一代的账。

    复审 2 的后半条：``finally`` 里无条件 ``_persist_breakers`` 会让旧代把
    「解卡后迟到的成功」写进熔断状态，把新代的失败盖掉。
    """
    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    worker = scheduler.FundFlowWorker()
    persisted: list[int] = []
    monkeypatch.setattr(
        worker, "_persist_breakers", lambda data_dir: persisted.append(1)
    )

    def fake_sync(data_dir, clients, *, trade_date, captured_at, breaker, is_current=None):
        # 真实实现里这里会落盘；本用例只关心熔断状态回写，用不着真数据。
        return scheduler.jobs.RunResult(kind="stock_intraday", wrote=1)

    monkeypatch.setattr(scheduler.jobs, "sync_stock_intraday", fake_sync)

    # 旧代：_spawn 之后立刻把世代推进，模拟「新代已经接手」。
    worker._spawn_stock_intraday(
        tmp_path,
        datetime(2026, 10, 13, 13, 44, 50),
        SourceClients(),
        trading=True,
        known_days=[],
    )
    worker._intraday_generation += 1
    worker.wait_intraday(timeout=5)
    assert persisted == [], "过期代不该写回熔断状态"

    # 当前代：正常回写。
    worker._spawn_stock_intraday(
        tmp_path,
        datetime(2026, 10, 13, 13, 46, 0),
        SourceClients(),
        trading=True,
        known_days=[],
    )
    worker.wait_intraday(timeout=5)
    assert persisted == [1], "当前代应该回写一次"


def test_etf_baseline_uses_record_count_not_trading_days(tmp_path) -> None:
    """ETF 基准按「前 N 份落盘」算；中间缺天时不会假装是 N 个交易日。

    评审 4：``etf_shares`` 中间缺天（线上 10-13 就没落），按落盘日推的「前 1 份」
    实际是 2 个交易日之前。文案与语义统一按「份」表述。
    """
    # 10-09 后直接跳到 10-13（缺 10-10、10-12 两个交易日）。
    for day, shares in (
        ("2026-10-09", 100.0),
        ("2026-10-13", 130.0),
    ):
        store.write_rows(tmp_path, "etf_shares", day, [
            {"symbol": "510300", "code": "510300", "name": "沪深300ETF",
             "trade_date": day, "shares": shares},
        ])
    payload = query._etf_changes(tmp_path)
    assert payload["trade_date"] == "2026-10-13"
    # 前 1 份落盘是 10-09（不是「前 1 个交易日」的 10-10）。
    assert payload["baselines"]["1"] == "2026-10-09"
    assert payload["baselines"]["5"] is None
    assert payload["items"][0]["change_1d"] == pytest.approx(30.0)


def test_stock_marked_success_when_fallback_fills_partition(tmp_path, monkeypatch) -> None:
    """熔断时靠整批兜底把样本补齐，也要标成功 —— 否则收盘分区永远不落盘。

    线上 ``stock`` 一直报「efinance 熔断冷却中」，``_mark_success`` 因
    ``result.errors`` 非空而从不调用，导致 30 分钟一轮的无限重试且分区缺失。
    """
    universe = store.FULL_MARKET_MIN + 10
    # 无维表时按固定门槛判「板块拉到过」：每个板块至少 20 只。这里造齐五大板块。
    prefixes = ("600", "688", "000", "300", "920")
    rows = []
    for index in range(universe):
        prefix = prefixes[index % len(prefixes)]
        code = f"{prefix}{index:03d}"
        rows.append({
            "symbol": f"{code}.SH", "code": code, "name": f"股{index}",
            "trade_date": "2026-10-13", "main_net": float(index),
            "large_net": None, "super_net": None, "source": "akshare_ths",
        })
    # 分区还没落盘 → 不完整。
    assert scheduler._stock_day_complete(tmp_path, "2026-10-13", universe) is False

    store.write_rows(tmp_path, "stock", "2026-10-13", rows)
    # 样本达到全市场门槛 → 足够当排名，判成功（这正是修好的落盘判据）。
    assert scheduler._stock_day_complete(tmp_path, "2026-10-13", universe) is True

    # 分区存在但样本远低于门槛 → 仍不完整。
    thin = tmp_path / "thin"
    store.write_rows(thin, "stock", "2026-10-13", rows[:10])
    assert scheduler._stock_day_complete(thin, "2026-10-13", universe) is False


def test_etf_changes_expose_1_5_15_day_windows(tmp_path) -> None:
    """ETF 份额变化给 1 / 5 / 15 日三个窗口，基准按落盘交易日往前推。"""
    # 造 20 个交易日的份额：510300 每日 +1e8，510050 每日 -2e8。
    days = [date(2026, 9, d).isoformat() for d in range(1, 21)]
    for index, day in enumerate(days):
        store.write_rows(tmp_path, "etf_shares", day, [
            {"symbol": "510300", "code": "510300", "name": "沪深300ETF",
             "trade_date": day, "shares": 1e10 + index * 1e8, "source": "akshare"},
            {"symbol": "510050", "code": "510050", "name": "50ETF",
             "trade_date": day, "shares": 5e9 - index * 2e8, "source": "akshare"},
        ])
    payload = query.board(tmp_path)["etf_shares"]
    assert payload["trade_date"] == days[-1]
    assert payload["windows"] == [1, 5, 15]
    # 基准日：最新 20 号，往前 1/5/15 个交易日。
    assert payload["baselines"] == {
        "1": days[-2], "5": days[-6], "15": days[-16],
    }
    assert payload["prev_trade_date"] == days[-2]
    by_code = {row["code"]: row for row in payload["items"]}
    hs300 = by_code["510300"]
    assert hs300["change_1d"] == pytest.approx(1e8)
    assert hs300["change_5d"] == pytest.approx(5e8)
    assert hs300["change_15d"] == pytest.approx(15e8)
    # 老字段与新字段同源，保持向后兼容。
    assert hs300["share_change"] == hs300["change_1d"]
    sz50 = by_code["510050"]
    assert sz50["change_1d"] == pytest.approx(-2e8)
    assert sz50["change_15d"] == pytest.approx(-30e8)


def test_etf_changes_window_baseline_is_none_when_history_short(tmp_path) -> None:
    """历史不足 N 个交易日时，该窗口留空，不拿现有最早一天凑数。"""
    days = [date(2026, 9, d).isoformat() for d in range(1, 4)]  # 只有 3 天
    for index, day in enumerate(days):
        store.write_rows(tmp_path, "etf_shares", day, [
            {"symbol": "510300", "code": "510300", "name": "沪深300ETF",
             "trade_date": day, "shares": 1e10 + index * 1e8, "source": "akshare"},
        ])
    payload = query.board(tmp_path)["etf_shares"]
    row = payload["items"][0]
    assert row["change_1d"] == pytest.approx(1e8)      # 有前一交易日
    assert row["change_5d"] is None                    # 不足 5 日
    assert row["change_15d"] is None                   # 不足 15 日
    assert payload["baselines"]["5"] is None
    assert payload["baselines"]["15"] is None


def test_etf_changes_empty_when_no_partition(tmp_path) -> None:
    payload = query.board(tmp_path)["etf_shares"]
    assert payload["trade_date"] is None
    assert payload["items"] == []
    assert payload["windows"] == [1, 5, 15]
    assert payload["baselines"] == {}


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


def test_board_etf_shares_sorted_by_shares_desc(tmp_path) -> None:
    """宽基和其他组都按份额降序。落盘没有市值，份额是规模代理。"""
    day = "2026-10-09"
    prev = "2026-10-08"
    broad_rows = [
        ("159901", "创业板50ETF-A", 0.65e8),
        ("159902", "创业板50ETF-B", 165.48e8),
        ("159903", "创业板50ETF-C", 2.20e8),
        ("510300", "沪深300ETF", 200e8),
        ("588000", "科创50ETF", None),
    ]
    # 份额最大的非宽基变化为 0；其余 12 只份额小但变化额大。按变化额排序会把它截掉。
    other_rows = [("510880", "红利ETF", 90e8, 90e8)]
    for index in range(12):
        shares = (index + 1) * 1e6
        other_rows.append((f"512{index:03d}", f"主题ETF{index}", shares, shares - 50e8))

    def row(code: str, name: str, trade_date: str, shares: float | None) -> dict:
        return {
            "symbol": code, "code": code, "name": name,
            "trade_date": trade_date, "shares": shares, "source": "akshare",
        }

    store.write_rows(tmp_path, "etf_shares", prev, [
        row(code, name, prev, shares if shares is not None else 1.0)
        for code, name, shares in broad_rows
    ] + [
        row(code, name, prev, previous)
        for code, name, _shares, previous in other_rows
    ])
    store.write_rows(tmp_path, "etf_shares", day, [
        row(code, name, day, shares) for code, name, shares in broad_rows
    ] + [
        row(code, name, day, shares) for code, name, shares, _previous in other_rows
    ])

    items = query.board(tmp_path)["etf_shares"]["items"]
    broad = [item for item in items if item["broad"]]
    others = [item for item in items if not item["broad"]]
    assert [item["code"] for item in broad] == ["510300", "159902", "159903", "159901", "588000"]
    assert [item["shares"] for item in broad[:4]] == pytest.approx([200e8, 165.48e8, 2.20e8, 0.65e8])
    assert broad[-1]["shares"] is None
    assert [item["code"] for item in others] == ["510880"] + [f"512{index:03d}" for index in range(11, 0, -1)]
    assert others[0]["share_change"] == 0
    assert all(item["share_change"] == pytest.approx(50e8) for item in others[1:])
    assert "512000" not in {item["code"] for item in others}
    assert items.index(others[0]) > items.index(broad[-1])


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


def _two_name_universe(root) -> None:
    inst = root / "instruments"
    inst.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"],
        "code": ["600519", "000001"],
        "name": ["贵州茅台", "平安银行"],
    }).write_parquet(inst / "instruments.parquet")


def _close_pair(root, trade_date: str, *, maotai: float, pingan: float, five: float = 3e8) -> None:
    store.write_rows(root, "stock", trade_date, [
        {
            "symbol": "600519.SH", "code": "600519", "name": "贵州茅台",
            "trade_date": trade_date, "main_net": maotai, "main_net_5d": five, "source": "efinance",
        },
        {
            "symbol": "000001.SZ", "code": "000001", "name": "平安银行",
            "trade_date": trade_date, "main_net": pingan, "main_net_5d": -five, "source": "efinance",
        },
    ])


def test_stocks_today_prefers_today_intraday_snapshot(tmp_path, monkeypatch) -> None:
    """当天 stock_intraday 有完整快照、且仍在交易时段内时，今日榜用最近一次盘中。"""
    monkeypatch.setattr(query, "cn_today", lambda: date(2026, 10, 13))
    monkeypatch.setattr(query, "cn_now", lambda: datetime(2026, 10, 13, 10, 35))
    _two_name_universe(tmp_path)
    _close_pair(tmp_path, "2026-10-12", maotai=9e8, pingan=-9e8)
    # 后写的是更早的切片。不先按 captured_at 过滤时，unique(keep=last) 会排到这一份。
    store.write_rows(tmp_path, "stock_intraday", "2026-10-13", [
        {
            "symbol": "000001.SZ", "code": "000001", "name": "平安银行",
            "trade_date": "2026-10-13", "captured_at": "2026-10-13T10:30:00",
            "main_net": 8e8, "source": "akshare_em_rank",
        },
        {
            "symbol": "600519.SH", "code": "600519", "name": "贵州茅台",
            "trade_date": "2026-10-13", "captured_at": "2026-10-13T10:30:00",
            "main_net": -1e8, "source": "akshare_em_rank",
        },
    ])
    store.write_rows(tmp_path, "stock_intraday", "2026-10-13", [
        {
            "symbol": "600519.SH", "code": "600519", "name": "贵州茅台",
            "trade_date": "2026-10-13", "captured_at": "2026-10-13T10:00:00",
            "main_net": 5e8, "source": "akshare_em_rank",
        },
        {
            "symbol": "000001.SZ", "code": "000001", "name": "平安银行",
            "trade_date": "2026-10-13", "captured_at": "2026-10-13T10:00:00",
            "main_net": -3e8, "source": "akshare_ths",
        },
    ])
    stock_before = store.read_partition(tmp_path, "stock", "2026-10-12").height
    payload = query.board(tmp_path)
    today = payload["stocks_today"]
    assert store.list_dates(tmp_path, "stock") == ["2026-10-12"]
    assert store.read_partition(tmp_path, "stock", "2026-10-12").height == stock_before
    assert today["trade_date"] == "2026-10-13"
    assert today["snapshot"] == "intraday"
    assert today["captured_at"] == "2026-10-13T10:30:00"
    assert today["source"] == "akshare_em_rank"
    assert today["partial"] is False
    assert [row["code"] for row in today["inflow"]] == ["000001"]
    assert today["inflow"][0]["main_net"] == 8e8
    assert [row["code"] for row in today["outflow"]] == ["600519"]
    assert today["outflow"][0]["main_net"] == -1e8
    assert today["items"] == today["inflow"]
    five = payload["stocks_5d"]
    assert five["trade_date"] == "2026-10-12"
    assert "snapshot" not in five
    assert [row["code"] for row in five["inflow"]] == ["600519"]
    assert five["inflow"][0]["ff_main_net_5d"] == 3e8


def test_stocks_today_falls_back_to_stock_close(tmp_path, monkeypatch) -> None:
    """没有可用的今日盘中时，今日榜回到 stock 收盘。昨日盘中和残缺快照都不能顶上。"""
    monkeypatch.setattr(query, "cn_today", lambda: date(2026, 10, 13))
    _two_name_universe(tmp_path)
    _close_pair(tmp_path, "2026-10-12", maotai=-2e8, pingan=4e8)
    store.write_rows(tmp_path, "stock_intraday", "2026-10-12", [
        {
            "symbol": "600519.SH", "code": "600519", "name": "贵州茅台",
            "trade_date": "2026-10-12", "captured_at": "2026-10-12T14:30:00",
            "main_net": 7e8, "source": "akshare_em_rank",
        },
        {
            "symbol": "000001.SZ", "code": "000001", "name": "平安银行",
            "trade_date": "2026-10-12", "captured_at": "2026-10-12T14:30:00",
            "main_net": -7e8, "source": "akshare_em_rank",
        },
    ])
    closed = query.board(tmp_path)["stocks_today"]
    assert closed["snapshot"] == "close"
    assert closed["trade_date"] == "2026-10-12"
    assert closed["captured_at"] is None
    assert closed["source"] is None
    assert closed["partial"] is False
    assert [row["code"] for row in closed["inflow"]] == ["000001"]
    assert closed["inflow"][0]["main_net"] == 4e8

    partial = tmp_path / "partial"
    _two_name_universe(partial)
    _close_pair(partial, "2026-10-12", maotai=-2e8, pingan=4e8)
    store.write_rows(partial, "stock_intraday", "2026-10-13", [{
        "symbol": "600519.SH", "code": "600519", "name": "贵州茅台",
        "trade_date": "2026-10-13", "captured_at": "2026-10-13T10:30:00",
        "main_net": 8e8, "source": "akshare_em_rank",
    }])
    again = query.board(partial)["stocks_today"]
    assert again["snapshot"] == "close"
    assert again["trade_date"] == "2026-10-12"
    assert again["partial"] is False
    assert [row["code"] for row in again["inflow"]] == ["000001"]

    missing = query.board(tmp_path / "none")
    assert missing["stocks_today"] == {
        "trade_date": None,
        "snapshot": None,
        "captured_at": None,
        "source": None,
        "partial": False,
        "note": None,
        "items": [],
        "inflow": [],
        "outflow": [],
    }
    assert missing["stocks_5d"]["snapshot"] is None
    assert missing["stocks_5d"]["captured_at"] is None
    assert missing["stocks_5d"]["source"] is None


def test_intraday_fresh_line_is_close_clock() -> None:
    """新鲜度只看快照自身的 captured_at，不看墙上时钟。

    评审指出的回归：只看时钟时，15:00 一过就不认当天盘中，而收盘分区要到
    16:30 才就绪，中间会退回上一交易日的旧榜单。改成按取样时刻判：
    14:45 及以后的当天快照收盘后仍可用（视作收盘前快照），更早的判陈旧。
    """
    # 盘中：当天任意快照都新鲜。
    assert query._intraday_still_fresh("2026-10-13T13:44:50", datetime(2026, 10, 13, 14, 59)) is True
    assert query._intraday_still_fresh("2026-10-13T11:25:32", datetime(2026, 10, 13, 14, 0)) is True
    # 收盘后：14:45 及以后仍可用（收盘前快照），更早的判陈旧。
    assert query._intraday_still_fresh("2026-10-13T14:52:00", datetime(2026, 10, 13, 17, 36)) is True
    assert query._intraday_still_fresh("2026-10-13T14:45:00", datetime(2026, 10, 13, 15, 30)) is True
    assert query._intraday_still_fresh("2026-10-13T13:44:50", datetime(2026, 10, 13, 17, 36)) is False
    assert query._intraday_still_fresh("2026-10-13T13:44:50", datetime(2026, 10, 13, 15, 0)) is False
    # 没有快照 / 快照日期不是今天 → 一律不新鲜。
    assert query._intraday_still_fresh(None, datetime(2026, 10, 13, 10, 0)) is False
    assert query._intraday_still_fresh("2026-10-12T14:55:00", datetime(2026, 10, 13, 10, 0)) is False
    assert query._intraday_still_fresh("坏时间", datetime(2026, 10, 13, 10, 0)) is False


def test_intraday_freshness_converts_timezone_not_just_drops_it() -> None:
    """非东八区的 aware 时间要先换算到东八区再比，不能只丢掉时区标记。

    复审 3：原来的 ``replace(tzinfo=None)`` 只是把时区标记抹掉，数值原样留下。
    传入 UTC 的时间会当成东八区去比，差 8 小时：东八区的 14:45 实际是 UTC 06:45，
    被抹标后落回 06:45，收盘后就误判成「午盘快照」而丢弃。
    """
    utc = UTC
    west5 = timezone(timedelta(hours=-5))
    # 东八区 17:00 对应 UTC 09:00。用它当「收盘后」的墙上时钟。
    close = datetime(2026, 10, 13, 9, 0, tzinfo=utc)
    # UTC 06:45 == 东八区 14:45，正卡在收盘前快照的下界上 → 收盘后仍算新鲜。
    assert query._intraday_still_fresh("2026-10-13T06:45:00+00:00", close) is True
    # UTC 05:44 == 东八区 13:44，定格在午盘 → 收盘后判陈旧。
    assert query._intraday_still_fresh("2026-10-13T05:44:00+00:00", close) is False
    # 同一个时刻换个时区表达，结论必须一致。
    assert query._intraday_still_fresh(
        "2026-10-13T06:45:00+00:00", datetime(2026, 10, 13, 17, 0, tzinfo=CN)
    ) is True
    # 比较基准也换算：西五区 10-12 23:00 == 东八区 10-13 12:00，属盘中 → 新鲜。
    # 若只丢标记，就会拿 10-12 去比快照的 10-13，误判成跨日。
    assert query._intraday_still_fresh(
        "2026-10-13T06:45:00+00:00", datetime(2026, 10, 12, 23, 0, tzinfo=west5)
    ) is True
    # 快照时间是 naive（落盘格式就是本地时间）→ 按东八区解释，同样判新鲜。
    assert query._intraday_still_fresh("2026-10-13T14:45:00", close) is True


def test_naive_inputs_are_read_as_cn_local_regardless_of_system_tz() -> None:
    """naive 的时间戳一律按东八区解读，不受跑测试的机器时区影响。

    这是个真踩到的坑：``datetime.astimezone(CN_TZ)`` 对 **naive** 值是按**系统
    本地时区**解释的。CI runner 是 UTC，naive 的 ``14:59`` 会被当成 UTC 14:59，
    换算成东八区变成 ``22:59`` —— 平白挪 8 小时，把盘中判成收盘后。所以必须先
    ``replace(tzinfo=CN_TZ)`` 补上时区再换算。

    断言直接钉结论：同一组 naive 输入在东八区机器和 UTC 机器上必须同解。
    """
    # 14:59 还是盘中 → 当天任意快照都新鲜。系统时区是 UTC 时，
    # 「丢时区」的实现在这里会得出 False（14:59 naive 被他当 UTC，换算后 22:59）。
    assert query._intraday_still_fresh("2026-10-13T13:44:50", datetime(2026, 10, 13, 14, 59)) is True
    assert query._intraday_still_fresh("2026-10-13T11:25:32", datetime(2026, 10, 13, 14, 0)) is True
    # 收盘后：14:52 的当天快照仍可用；13:44 的判陈旧。
    assert query._intraday_still_fresh("2026-10-13T14:52:00", datetime(2026, 10, 13, 15, 30)) is True
    assert query._intraday_still_fresh("2026-10-13T13:44:50", datetime(2026, 10, 13, 17, 36)) is False
    # naive 的 ``now`` 与「同一个墙上时间标成东八区」必须同解。
    for stamp, wall in (
        ("2026-10-13T13:44:50", datetime(2026, 10, 13, 14, 59)),
        ("2026-10-13T14:52:00", datetime(2026, 10, 13, 15, 30)),
        ("2026-10-13T13:44:50", datetime(2026, 10, 13, 17, 36)),
    ):
        assert query._intraday_still_fresh(stamp, wall) is query._intraday_still_fresh(
            stamp, wall.replace(tzinfo=CN)
        )


def test_stocks_today_uses_late_intraday_before_close_partition(tmp_path, monkeypatch) -> None:
    """15:00–16:30 收盘分区未就绪时，用当天 14:5x 的收盘前快照，而不是退回昨天。

    评审给的场景：15:30。此前只看时钟的实现会退回上一交易日收盘，比 14:5x 的
    快照还旧 —— 属于修出来的回归。现在按 captured_at 判，仍采用当天晚些的快照。
    """
    monkeypatch.setattr(query, "cn_today", lambda: date(2026, 10, 13))
    monkeypatch.setattr(query, "cn_now", lambda: datetime(2026, 10, 13, 15, 30))
    _two_name_universe(tmp_path)
    store.write_rows(tmp_path, "stock_intraday", "2026-10-13", [
        {
            "symbol": "000001.SZ", "code": "000001", "name": "平安银行",
            "trade_date": "2026-10-13", "captured_at": "2026-10-13T14:52:00",
            "main_net": 8e8, "source": "akshare_em_rank",
        },
        {
            "symbol": "600519.SH", "code": "600519", "name": "贵州茅台",
            "trade_date": "2026-10-13", "captured_at": "2026-10-13T14:52:00",
            "main_net": -1e8, "source": "akshare_em_rank",
        },
    ])
    # 只有上一交易日的收盘分区，今天收盘分区尚未就绪。
    _close_pair(tmp_path, "2026-10-12", maotai=-5e8, pingan=3e8)
    today = query.board(tmp_path)["stocks_today"]
    assert today["snapshot"] == "intraday", "收盘分区未就绪时该用当天收盘前快照"
    assert today["trade_date"] == "2026-10-13"
    assert today["captured_at"] == "2026-10-13T14:52:00"
    assert today["stale_after_close"] is True
    assert [row["code"] for row in today["inflow"]] == ["000001"]
    assert today["inflow"][0]["main_net"] == 8e8


def test_stocks_today_after_close_ignores_stale_intraday(tmp_path, monkeypatch) -> None:
    """收盘后定格在午盘的快照不再代表「今日」：改读当天收盘。

    线上曾出现 17:36 仍显示「盘中 13:44」—— 当天盘中快照停在午盘，收盘分区
    又没落下来，页面就把定格在 13:44 的旧快照当成实时。13:44 早于 14:45，
    收盘后判为陈旧。
    """
    monkeypatch.setattr(query, "cn_today", lambda: date(2026, 10, 13))
    monkeypatch.setattr(query, "cn_now", lambda: datetime(2026, 10, 13, 17, 36))
    _two_name_universe(tmp_path)
    # 当天盘中快照停在 13:44（模拟线上），收盘分区也已就绪。
    store.write_rows(tmp_path, "stock_intraday", "2026-10-13", [
        {
            "symbol": "000001.SZ", "code": "000001", "name": "平安银行",
            "trade_date": "2026-10-13", "captured_at": "2026-10-13T13:44:50",
            "main_net": 8e8, "source": "akshare_em_rank",
        },
        {
            "symbol": "600519.SH", "code": "600519", "name": "贵州茅台",
            "trade_date": "2026-10-13", "captured_at": "2026-10-13T13:44:50",
            "main_net": -1e8, "source": "akshare_em_rank",
        },
    ])
    _close_pair(tmp_path, "2026-10-13", maotai=-5e8, pingan=3e8)
    today = query.board(tmp_path)["stocks_today"]
    assert today["snapshot"] == "close", "收盘后不该再把午盘快照当今日"
    assert today["trade_date"] == "2026-10-13"
    assert today["captured_at"] is None
    assert [row["code"] for row in today["inflow"]] == ["000001"]
    assert today["inflow"][0]["main_net"] == 3e8


def test_sector_board_splits_inflow_and_outflow(tmp_path) -> None:
    """板块榜从全量排名里各取流入、流出，items 保持等于流入。"""
    rows = []
    for index in range(13):
        rows.append({
            "name": f"流入{index:02d}",
            "trade_date": "2026-10-13",
            "captured_at": "2026-10-13T10:30:00",
            "snapshot": "intraday",
            "net_inflow": float(13 - index) * 1e8,
            "rank": index + 1,
            "source": "akshare_em",
        })
    for index in range(13):
        rows.append({
            "name": f"流出{index:02d}",
            "trade_date": "2026-10-13",
            "captured_at": "2026-10-13T10:30:00",
            "snapshot": "intraday",
            "net_inflow": -float(index + 1) * 1e8,
            "rank": 20 + index,
            "source": "akshare_em",
        })
    rows.append({
        "name": "持平",
        "trade_date": "2026-10-13",
        "captured_at": "2026-10-13T10:30:00",
        "snapshot": "intraday",
        "net_inflow": 0.0,
        "rank": 40,
        "source": "akshare_em",
    })
    store.write_rows(tmp_path, "industry", "2026-10-13", rows)
    full = query.sectors("industry", data_dir=tmp_path)
    assert len(full["items"]) == 27
    assert "inflow" not in full
    board = query.board(tmp_path)
    industry = board["industry"]
    assert [row["name"] for row in industry["inflow"]] == [f"流入{index:02d}" for index in range(12)]
    assert [row["name"] for row in industry["outflow"]] == [f"流出{index:02d}" for index in range(12, 0, -1)]
    assert industry["items"] == industry["inflow"]
    assert all(row["net_inflow"] > 0 for row in industry["inflow"])
    assert all(row["net_inflow"] < 0 for row in industry["outflow"])
    names = {row["name"] for row in industry["inflow"] + industry["outflow"]}
    assert "持平" not in names
    assert "流入12" not in names
    assert "流出00" not in names
    assert board["concept"]["items"] == []
    assert board["concept"]["inflow"] == []
    assert board["concept"]["outflow"] == []


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


def test_stock_intraday_respects_session_and_gap() -> None:
    assert scheduler.STOCK_INTRADAY_GAP_S == 6 * 60
    state: dict = {"kinds": {}}
    assert "stock_intraday" not in scheduler.due_tasks(
        _day(10, 0), trading=True, state=state, flags=_flags(stock_intraday=False), breakers_open=set(),
    )
    assert "stock_intraday" not in scheduler.due_tasks(
        _day(10, 0), trading=True, state=state, flags=_flags(), breakers_open={"akshare"},
    )
    # 间隔看成功落盘，不看 last_attempt_at。空状态或只有尝试记录时仍然该拉。
    assert "stock_intraday" in scheduler.due_tasks(
        _day(10, 0), trading=True, state=state, flags=_flags(), breakers_open=set(),
    )
    attempted = {"kinds": {"stock_intraday": {"last_attempt_at": _day(10, 0).isoformat()}}}
    assert "stock_intraday" in scheduler.due_tasks(
        _day(10, 0) + timedelta(seconds=30),
        trading=True, state=attempted, flags=_flags(), breakers_open=set(),
    )
    recent = {"kinds": {"stock_intraday": {"last_success_at": _day(10, 0).isoformat()}}}
    assert "stock_intraday" not in scheduler.due_tasks(
        _day(10, 0) + timedelta(seconds=30),
        trading=True, state=recent, flags=_flags(), breakers_open=set(),
    )
    assert "stock_intraday" not in scheduler.due_tasks(
        _day(10, 5), trading=True, state=recent, flags=_flags(), breakers_open=set(),
    )
    assert "stock_intraday" in scheduler.due_tasks(
        _day(10, 6), trading=True, state=recent, flags=_flags(), breakers_open=set(),
    )
    snapped = {
        "kinds": {
            "stock_intraday": {"last_snapshot": _day(10, 0).strftime("%Y-%m-%dT%H:%M:%S")},
        },
    }
    assert "stock_intraday" not in scheduler.due_tasks(
        _day(10, 0) + timedelta(seconds=30),
        trading=True, state=snapped, flags=_flags(), breakers_open=set(),
    )


def test_stock_intraday_gap_polarity_and_boundaries() -> None:
    """``_intraday_due()`` 的间隔边界与调用处**极性**回归护栏。

    ``_intraday_due()`` 回答的是「这一轮**该不该拉**」：**到期返回 True**。所以
    ``due_tasks`` 里必须写成 ``and _intraday_due(...)``。它与紧邻的 ``_recent()``
    （「最近跑过」，未到期返回 True，调用处写 ``not _recent(...)``）**返回值语义相反**，
    两个函数放在一起很容易被误改成 ``and not _intraday_due(...)`` —— 一旦改错，
    判定整体反转：首轮不拉、成功后立刻重拉。本用例就是拦这一改动的。

    断言是成对的：首轮必须入队、6 分钟内必须不入队、满 6 分钟必须入队。这三种状态
    放在一起，``and`` 与 ``and not`` 的两种写法不可能同时满足；补上 ``not`` 会让首轮
    那一条立刻失败。

    与 ``test_stock_intraday_respects_session_and_gap`` 的分工：那条覆盖开关、熔断、
    时段与基本间隔，本条补**边界值**、``last_snapshot`` 优先级、坏时间戳与极性护栏。
    """
    at = _day(10, 0)

    def due(moment: datetime, kind_state: dict) -> list[str]:
        return scheduler.due_tasks(
            moment,
            trading=True,
            state={"kinds": {"stock_intraday": kind_state}},
            flags=_flags(),
            breakers_open=set(),
        )

    # 首轮：没有任何成功时刻 -> 该拉，保证第一份快照能落盘。
    assert scheduler._intraday_due({}, at) is True
    assert "stock_intraday" in due(at, {})

    # 只有 last_attempt_at：那是「尝试」不是「成功」-> 仍然该拉。
    only_attempt = {"last_attempt_at": at.isoformat()}
    assert scheduler._intraday_due(only_attempt, at) is True
    assert "stock_intraday" in due(at, only_attempt)

    # 成功未满 6 分钟 -> 不该拉。含 0 秒与 359 秒两侧边界。
    fresh = {"last_success_at": at.isoformat()}
    for seconds in (0, 1, 30, 60, 300, 359):
        moment = at + timedelta(seconds=seconds)
        assert scheduler._intraday_due(fresh, moment) is False, seconds
        assert "stock_intraday" not in due(moment, fresh), seconds

    # 满 6 分钟 -> 该拉。含恰好 360 秒这一条边界。
    for seconds in (360, 361, 420, 3600):
        moment = at + timedelta(seconds=seconds)
        assert scheduler._intraday_due(fresh, moment) is True, seconds
        assert "stock_intraday" in due(moment, fresh), seconds

    # last_snapshot 优先于 last_success_at：两个字段冲突时以快照时刻为准。
    mixed = {
        "last_snapshot": at.isoformat(),
        "last_success_at": (at - timedelta(hours=2)).isoformat(),
    }
    assert scheduler._intraday_due(mixed, at + timedelta(seconds=30)) is False
    assert scheduler._intraday_due(mixed, at + timedelta(minutes=10)) is True

    # last_snapshot 写成无时区串时按上海时区解释，不能当成 UTC 而提前到期。
    assert scheduler._intraday_due(
        {"last_snapshot": at.strftime("%Y-%m-%dT%H:%M:%S")},
        at + timedelta(seconds=30),
    ) is False

    # 坏时间戳 / 空串 / None 退化为「首轮，该拉」，不能因此当天再也不落盘。
    for broken in ("不是时间", "", None):
        assert scheduler._intraday_due({"last_success_at": broken}, at) is True, broken

    # 间隔满足也不是唯一闸门：时段、开关、熔断仍然各自拦一次。
    gap_ok = {"last_success_at": (at - timedelta(minutes=10)).isoformat()}
    for hour, minute in ((9, 0), (12, 0), (15, 30)):
        moment = datetime(2026, 10, 9, hour, minute, tzinfo=CN)
        assert "stock_intraday" not in due(moment, gap_ok), (hour, minute)
    assert "stock_intraday" not in scheduler.due_tasks(
        at, trading=True, state={"kinds": {"stock_intraday": gap_ok}},
        flags=_flags(stock_intraday=False), breakers_open=set(),
    )
    assert "stock_intraday" not in scheduler.due_tasks(
        at, trading=True, state={"kinds": {"stock_intraday": gap_ok}},
        flags=_flags(), breakers_open={"akshare"},
    )


def test_intraday_snapshot_is_latest_today_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    monkeypatch.setenv("FUND_FLOW_INTRADAY_PRIMARY", "ths")
    for name in (
        "FUND_FLOW_STOCK", "FUND_FLOW_STOCK_INTRADAY", "FUND_FLOW_SECTOR",
        "FUND_FLOW_MARGIN", "FUND_FLOW_ETF_SHARES", "FUND_FLOW_SOUTHBOUND",
        "FUND_FLOW_NORTHBOUND_TURNOVER", "FUND_FLOW_LHB_BACKUP",
    ):
        monkeypatch.delenv(name, raising=False)
    calls: list[tuple[str, dict]] = []

    def ak(name, **kwargs):
        calls.append((name, kwargs))
        if len(calls) == 1:
            return [{"股票代码": "600519", "股票简称": "贵州茅台", "净额": "1.5亿"}]
        return [
            {"股票代码": "600519", "股票简称": "贵州茅台", "净额": "2亿"},
            {"股票代码": "000001", "股票简称": "平安银行", "净额": "-8000万"},
        ]

    worker = scheduler.FundFlowWorker()
    assert worker.run_once(
        now=_day(10, 0), data_dir=tmp_path, clients=SourceClients(ak_call=ak),
        symbols=[], trading=True, busy=False,
    ) == ["stock_intraday"]
    assert worker.run_once(
        now=_day(10, 6), data_dir=tmp_path, clients=SourceClients(ak_call=ak),
        symbols=[], trading=True, busy=False,
    ) == ["stock_intraday"]
    frame = store.read_partition(tmp_path, "stock_intraday", "2026-10-09")
    assert frame.height == 3
    assert calls[0] == ("stock_fund_flow_individual", {"symbol": "即时"})
    latest = query.intraday_latest(
        ["600519.SH", "000002.SZ"], data_dir=tmp_path, today="2026-10-09",
    )
    assert latest["captured_at"] == "2026-10-09T10:06:00"
    assert latest["source"] == "akshare_ths"
    assert latest["items"] == [{
        "code": "600519", "name": "贵州茅台", "main_net": 2e8,
        "super_net": None, "large_net": None, "medium_net": None, "small_net": None,
        "source": "akshare_ths",
    }]
    stale = query.intraday_latest(["600519.SH"], data_dir=tmp_path, today="2026-10-10")
    assert stale == {"trade_date": None, "captured_at": None, "source": None, "items": []}


def test_intraday_retries_then_keeps_previous_snapshot_on_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUND_FLOW_INTRADAY_PRIMARY", "ths")
    sleeps: list[float] = []
    calls = {"n": 0}

    def ak(name, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("read timed out")
        return [{"股票代码": "600519", "股票简称": "贵州茅台", "净额": 100.0}]

    result = jobs.sync_stock_intraday(
        tmp_path,
        SourceClients(ak_call=ak),
        trade_date="2026-10-09",
        captured_at="2026-10-09T10:00:00",
        breaker=CircuitBreaker("akshare", clock=lambda: 0.0),
        sleeper=sleeps.append,
    )
    assert sleeps == [2.0]
    assert result.errors == []
    assert store.read_partition(tmp_path, "stock_intraday", "2026-10-09")["main_net"][0] == 100.0

    empty = jobs.sync_stock_intraday(
        tmp_path,
        SourceClients(ak_call=lambda name, **kwargs: []),
        trade_date="2026-10-09",
        captured_at="2026-10-09T10:06:00",
        breaker=CircuitBreaker("akshare", clock=lambda: 0.0),
        sleeper=lambda _seconds: None,
    )
    assert empty.errors == ["同花顺即时个股资金流为空"]
    assert store.read_partition(tmp_path, "stock_intraday", "2026-10-09").height == 1


def test_intraday_does_not_run_outside_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    monkeypatch.delenv("FUND_FLOW_STOCK", raising=False)
    monkeypatch.delenv("FUND_FLOW_STOCK_INTRADAY", raising=False)

    def ak(name, **kwargs):
        raise AssertionError(name)

    ran = scheduler.FundFlowWorker().run_once(
        now=_day(16, 0),
        data_dir=tmp_path,
        clients=SourceClients(ak_call=ak),
        symbols=[],
        trading=True,
        busy=False,
    )
    assert ran == []


def test_live_individual_flow_uses_shared_gate(monkeypatch) -> None:
    seen: dict = {}

    def fake_call(name, **kwargs):
        seen["name"] = name
        seen["kwargs"] = kwargs
        return [{"股票代码": "600519", "净额": 1}]

    monkeypatch.setattr("app.eastmoney.akshare_gate.call_akshare", fake_call)
    from app.fund_flow.sources import live_clients

    rows = live_clients().ak("stock_fund_flow_individual", symbol="即时")
    assert seen == {"name": "stock_fund_flow_individual", "kwargs": {"symbol": "即时"}}
    assert rows[0]["股票代码"] == "600519"


def test_old_intraday_partition_without_order_columns_still_reads(tmp_path) -> None:
    path = store.partition_file(tmp_path, "stock_intraday", "2026-10-09")
    path.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600519"],
        "code": ["600519"],
        "name": ["贵州茅台"],
        "trade_date": ["2026-10-09"],
        "captured_at": ["2026-10-09T10:06:00"],
        "main_net": [2e8],
        "source": ["akshare_ths"],
    }).write_parquet(path)
    frame = store.read_partition(tmp_path, "stock_intraday", "2026-10-09")
    assert frame["main_net"][0] == 2e8
    assert frame["super_net"][0] is None
    latest = query.intraday_latest(["600519.SH"], data_dir=tmp_path, today="2026-10-09")
    assert latest["source"] == "akshare_ths"
    assert latest["items"][0]["main_net"] == 2e8
    assert latest["items"][0]["super_net"] is None


def test_intraday_api_filters_symbols(tmp_path, monkeypatch) -> None:
    store.write_rows(tmp_path, "stock_intraday", "2026-10-09", [{
        "symbol": "600519", "code": "600519", "name": "贵州茅台",
        "trade_date": "2026-10-09", "captured_at": "2026-10-09T10:06:00",
        "main_net": 2e8, "source": "akshare_ths",
    }])
    monkeypatch.setattr(query, "_dir", lambda data_dir=None: tmp_path)
    monkeypatch.setattr(query, "cn_today", lambda: date(2026, 10, 9))
    app = FastAPI()
    from app.api.fund_flow import router
    app.include_router(router)
    client = TestClient(app)
    body = client.get("/api/fund-flow/intraday", params={"symbols": "600519.SH,000001.SZ"}).json()
    assert body["source"] == "akshare_ths"
    assert body["items"] == [{
        "code": "600519", "name": "贵州茅台", "main_net": 2e8,
        "super_net": None, "large_net": None, "medium_net": None, "small_net": None,
        "source": "akshare_ths",
    }]
    assert body["captured_at"] == "2026-10-09T10:06:00"


def _eastmoney_rank_rows(count: int = store.FULL_MARKET_MIN) -> list[dict]:
    """东财当日排名的一行真实口径，其余行只凑满全市场门槛。

    茅台这一行的主力等于超大单加大单，用来证明落盘读的是主力列，不是把两档加总后再改符号。
    净占比故意放一个很小的数，映射如果读错列会立刻对不上。
    """
    rows = [{
        "代码": "600519",
        "名称": "贵州茅台",
        "今日主力净流入-净额": -1234567.5,
        "今日主力净流入-净占比": 1.25,
        "今日超大单净流入-净额": -2000000.0,
        "今日超大单净流入-净占比": -2.0,
        "今日大单净流入-净额": 765432.5,
        "今日大单净流入-净占比": 0.8,
        "今日中单净流入-净额": 100.0,
        "今日中单净流入-净占比": 0.01,
        "今日小单净流入-净额": -50.25,
        "今日小单净流入-净占比": -0.01,
    }]
    for index in range(1, count):
        rows.append({
            "代码": f"{index:06d}",
            "名称": "测",
            "今日主力净流入-净额": float(index),
            "今日超大单净流入-净额": float(-index),
            "今日大单净流入-净额": float(index),
            "今日中单净流入-净额": float(index),
            "今日小单净流入-净额": float(-index),
        })
    return rows


def _breaker() -> CircuitBreaker:
    return CircuitBreaker("akshare", clock=lambda: 0.0)


def test_intraday_eastmoney_primary_keeps_yuan_and_sign(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FUND_FLOW_INTRADAY_PRIMARY", raising=False)
    calls: list[tuple[str, dict]] = []

    def ak(name, **kwargs):
        calls.append((name, kwargs))
        if name == "stock_individual_fund_flow_rank":
            return _eastmoney_rank_rows()
        raise AssertionError(name)

    result = jobs.sync_stock_intraday(
        tmp_path,
        SourceClients(ak_call=ak),
        trade_date="2026-10-09",
        captured_at="2026-10-09T10:06:00",
        breaker=_breaker(),
    )
    assert result.errors == []
    assert calls == [("stock_individual_fund_flow_rank", {"indicator": "今日"})]
    frame = store.read_partition(tmp_path, "stock_intraday", "2026-10-09")
    assert frame.height == store.FULL_MARKET_MIN
    row = frame.filter(pl.col("code") == "600519").to_dicts()[0]
    assert row["main_net"] == -1234567.5
    assert row["super_net"] == -2000000.0
    assert row["large_net"] == 765432.5
    assert row["medium_net"] == 100.0
    assert row["small_net"] == -50.25
    assert row["source"] == "akshare_em_rank"
    assert row["main_net"] == row["super_net"] + row["large_net"]
    other = frame.filter(pl.col("code") == "000001").to_dicts()[0]
    assert other["main_net"] == 1.0
    latest = query.intraday_latest(["600519.SH"], data_dir=tmp_path, today="2026-10-09")
    assert latest["source"] == "akshare_em_rank"
    assert latest["items"] == [{
        "code": "600519",
        "name": "贵州茅台",
        "main_net": -1234567.5,
        "super_net": -2000000.0,
        "large_net": 765432.5,
        "medium_net": 100.0,
        "small_net": -50.25,
        "source": "akshare_em_rank",
    }]


def test_intraday_falls_back_when_eastmoney_fails(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("FUND_FLOW_INTRADAY_PRIMARY", "eastmoney")
    calls: list[str] = []

    def ak(name, **kwargs):
        calls.append(name)
        if name == "stock_individual_fund_flow_rank":
            raise TimeoutError("read timed out")
        return [{"股票代码": "600519", "股票简称": "贵州茅台", "净额": "1.5亿"}]

    sleeps: list[float] = []
    with caplog.at_level(logging.WARNING):
        result = jobs.sync_stock_intraday(
            tmp_path,
            SourceClients(ak_call=ak),
            trade_date="2026-10-09",
            captured_at="2026-10-09T10:06:00",
            breaker=_breaker(),
            sleeper=sleeps.append,
        )
    assert sleeps == []
    assert calls == ["stock_individual_fund_flow_rank", "stock_fund_flow_individual"]
    assert result.errors == []
    assert any("改走同花顺" in record.message and "read timed out" in record.message for record in caplog.records)
    frame = store.read_partition(tmp_path, "stock_intraday", "2026-10-09")
    assert frame.height == 1
    assert frame["main_net"][0] == 1.5e8
    assert frame["source"][0] == "akshare_ths"
    assert frame["super_net"][0] is None


def test_intraday_falls_back_when_eastmoney_has_too_few_rows(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("FUND_FLOW_INTRADAY_PRIMARY", "eastmoney")
    def ak(name, **kwargs):
        if name == "stock_individual_fund_flow_rank":
            return _eastmoney_rank_rows(3)
        return [
            {"股票代码": "000001", "股票简称": "平安银行", "净额": "-8000万"},
        ]

    with caplog.at_level(logging.WARNING):
        result = jobs.sync_stock_intraday(
            tmp_path,
            SourceClients(ak_call=ak),
            trade_date="2026-10-09",
            captured_at="2026-10-09T10:12:00",
            breaker=_breaker(),
        )
    assert result.errors == []
    assert any("只有 3 行" in record.message and "改走同花顺" in record.message for record in caplog.records)
    frame = store.read_partition(tmp_path, "stock_intraday", "2026-10-09")
    assert frame.height == 1
    assert frame["code"][0] == "000001"
    assert frame["main_net"][0] == -8e7
    assert frame["source"][0] == "akshare_ths"
    assert "600519" not in frame["code"].to_list()


def test_intraday_primary_switch(tmp_path, monkeypatch, caplog) -> None:
    calls: list[str] = []

    def ak(name, **kwargs):
        calls.append(name)
        if name == "stock_individual_fund_flow_rank":
            return _eastmoney_rank_rows()
        return [{"股票代码": "600519", "股票简称": "贵州茅台", "净额": 100.0}]

    def run(captured: str) -> None:
        result = jobs.sync_stock_intraday(
            tmp_path,
            SourceClients(ak_call=ak),
            trade_date="2026-10-09",
            captured_at=captured,
            breaker=_breaker(),
        )
        assert result.errors == []

    monkeypatch.setenv("FUND_FLOW_INTRADAY_PRIMARY", "ths")
    run("2026-10-09T10:00:00")
    assert calls == ["stock_fund_flow_individual"]
    assert store.read_partition(tmp_path, "stock_intraday", "2026-10-09").filter(
        pl.col("captured_at") == "2026-10-09T10:00:00"
    )["source"][0] == "akshare_ths"

    calls.clear()
    monkeypatch.setenv("FUND_FLOW_INTRADAY_PRIMARY", "EastMoney")
    run("2026-10-09T10:06:00")
    assert calls == ["stock_individual_fund_flow_rank"]

    calls.clear()
    monkeypatch.delenv("FUND_FLOW_INTRADAY_PRIMARY", raising=False)
    run("2026-10-09T10:12:00")
    assert calls == ["stock_individual_fund_flow_rank"]

    calls.clear()
    with caplog.at_level(logging.WARNING):
        monkeypatch.setenv("FUND_FLOW_INTRADAY_PRIMARY", "nope")
        run("2026-10-09T10:18:00")
    assert calls == ["stock_individual_fund_flow_rank"]
    assert any("无法识别" in record.message for record in caplog.records)


def test_live_rank_uses_shared_gate(monkeypatch) -> None:
    seen: dict = {}

    def fake_call(name, **kwargs):
        seen["name"] = name
        seen["kwargs"] = kwargs
        return [{"代码": "600519", "今日主力净流入-净额": -1.5}]

    monkeypatch.setattr("app.eastmoney.akshare_gate.call_akshare", fake_call)
    from app.fund_flow.sources import live_clients

    rows = live_clients().ak("stock_individual_fund_flow_rank", indicator="今日")
    assert seen == {"name": "stock_individual_fund_flow_rank", "kwargs": {"indicator": "今日"}}
    assert rows[0]["今日主力净流入-净额"] == -1.5


def test_intraday_fetch_does_not_block_other_jobs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FUND_FLOW_ENABLED", "true")
    monkeypatch.setenv("FUND_FLOW_SECTOR", "true")
    monkeypatch.delenv("FUND_FLOW_INTRADAY_PRIMARY", raising=False)
    for name in (
        "FUND_FLOW_STOCK", "FUND_FLOW_MARGIN", "FUND_FLOW_ETF_SHARES", "FUND_FLOW_SOUTHBOUND",
        "FUND_FLOW_NORTHBOUND_TURNOVER", "FUND_FLOW_LHB_BACKUP",
    ):
        monkeypatch.delenv(name, raising=False)
    jobs.reset_sector_cache()
    rank_entered = threading.Event()
    release_rank = threading.Event()
    rank_calls = {"n": 0}

    def ak(name, **kwargs):
        if name == "stock_individual_fund_flow_rank":
            rank_calls["n"] += 1
            rank_entered.set()
            release_rank.wait(3)
            return _eastmoney_rank_rows()
        if name == "stock_fund_flow_industry":
            assert rank_entered.wait(3), "板块任务没有和东财分页重叠"
            return [{"行业": "白酒", "净额": "1.2亿"}]
        if name == "stock_fund_flow_concept":
            return [{"概念": "白酒", "净额": "1亿"}]
        raise AssertionError(name)

    worker = scheduler.FundFlowWorker()
    clients = SourceClients(ak_call=ak)
    try:
        ran = worker.run_once(
            now=_day(10, 0),
            data_dir=tmp_path,
            clients=clients,
            symbols=[],
            trading=True,
            busy=False,
            detach_intraday=True,
        )
        assert ran[0] == "stock_intraday"
        assert "sector_intraday" in ran
        assert worker._intraday_running()
        again = worker.run_once(
            now=_day(10, 6),
            data_dir=tmp_path,
            clients=clients,
            symbols=[],
            trading=True,
            busy=False,
            detach_intraday=True,
        )
        assert "stock_intraday" not in again
        assert rank_calls["n"] == 1
    finally:
        release_rank.set()
        worker.wait_intraday(5)
        jobs.reset_sector_cache()
    frame = store.read_partition(tmp_path, "stock_intraday", "2026-10-09")
    assert frame.height == store.FULL_MARKET_MIN
    assert frame.filter(pl.col("code") == "600519")["source"][0] == "akshare_em_rank"
    # 10:00 和 10:06 各一份板块快照。第二轮没有再打东财。
    assert store.read_partition(tmp_path, "industry", "2026-10-09").height == 2
