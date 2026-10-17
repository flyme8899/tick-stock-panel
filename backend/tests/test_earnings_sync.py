"""业绩预告/快报/报表同步：分页、字段映射、去重合并、残缺保护、互斥锁、报告期与调度。"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import earnings_sync as es


class FakeGet:
    """按 (reportName, pageNumber) 返回预置页，记录请求。"""

    def __init__(self, pages: dict[str, list[list[dict]]], fail: set[str] | None = None,
                 empty: set[str] | None = None):
        self.pages = pages
        self.fail = fail or set()
        self.empty = empty or set()
        self.calls: list[dict] = []

    def __call__(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append(dict(params))
        rep = params["reportName"]
        if rep in self.fail:
            raise RuntimeError("网络错误")
        if rep in self.empty:
            return {"success": False, "code": 9201, "message": "返回数据为空"}
        pages = self.pages.get(rep, [])
        n = int(params["pageNumber"])
        data = pages[n - 1] if n <= len(pages) else []
        return {"success": True, "code": 0, "result": {"pages": len(pages), "data": data}}


def _fc(code, notice, ind="004", update=None, amt=1.0, period="2026-09-30"):
    return {"SECURITY_CODE": code, "SECUCODE": f"{code}.SZ", "SECURITY_NAME_ABBR": "某股",
            "NOTICE_DATE": f"{notice} 00:00:00", "REPORT_DATE": f"{period} 00:00:00",
            "PREDICT_FINANCE_CODE": ind, "PREDICT_FINANCE": "归母净利润", "PREDICT_TYPE": "预增",
            "PREDICT_AMT_LOWER": amt, "PREDICT_AMT_UPPER": "-", "ADD_AMP_LOWER": "12.5",
            "UPDATE_DATE": f"{update} 00:00:00" if update else None, "IS_LATEST": "T"}


def _rp(code, notice, update, profit, period="2026-06-30"):
    return {"SECURITY_CODE": code, "SECUCODE": f"{code}.SZ", "SECURITY_NAME_ABBR": "某股",
            "NOTICE_DATE": f"{notice} 00:00:00", "UPDATE_DATE": f"{update} 00:00:00",
            "REPORTDATE": f"{period} 00:00:00", "PARENT_NETPROFIT": profit, "BASIC_EPS": 0.1,
            "ISNEW": "1", "QDATE": "2026Q2"}


def _now():
    return datetime(2026, 10, 17, 18, 40, tzinfo=CN_TZ)


def test_quarter_ends_and_latest_periods():
    q = es.quarter_ends("2018-03-31", date(2026, 10, 17))
    assert len(q) == 35 and q[0] == "2018-03-31" and q[-1] == "2026-09-30"
    assert es.latest_periods(date(2026, 10, 17), 2) == ["2026-06-30", "2026-09-30"]
    assert es.latest_periods(date(2027, 1, 20), 2) == ["2026-09-30", "2026-12-31"]
    assert es.latest_periods(date(2026, 3, 30), 2) == ["2025-09-30", "2025-12-31"]


def test_fetch_paginates_and_maps_fields():
    get = FakeGet({"RPT_PUBLIC_OP_NEWPREDICT": [
        [_fc("000001", "2026-10-10"), _fc("000002", "2026-10-11")],
        [_fc("000003", "2026-10-12"), _fc("000009", "2026-10-12", period="2026-06-30")],
    ]})
    rows = es.fetch_period(es.FORECAST, "2026-09-30", get=get, page_size=2, now=_now)
    assert [c["pageNumber"] for c in get.calls] == ["1", "2"]
    assert get.calls[0]["filter"] == "(REPORT_DATE='2026-09-30')"
    assert [r["code"] for r in rows] == ["000001", "000002", "000003"]  # 报告期对不上的行丢掉
    r = rows[0]
    assert r["announce_date"] == "2026-10-10" and r["report_period"] == "2026-09-30"
    assert r["code"] == "000001" and r["symbol"] == "000001.SZ"
    assert r["amt_lower"] == 1.0 and r["amt_upper"] is None and r["chg_pct_lower"] == 12.5
    assert r["source"] == "eastmoney_RPT_PUBLIC_OP_NEWPREDICT"
    assert r["fetched_at"] == "2026-10-17T18:40:00+08:00"


def test_fetch_raises_when_truncated():
    full = [[_fc(f"{i:06d}", "2026-10-10") for i in range(2)]] * 3
    get = FakeGet({"RPT_PUBLIC_OP_NEWPREDICT": full})
    with pytest.raises(RuntimeError, match="超过"):
        es.fetch_period(es.FORECAST, "2026-09-30", get=get, page_size=2, page_limit=2)


def test_fetch_empty_code_9201():
    get = FakeGet({}, empty={"RPT_LICO_FN_CPD"})
    assert es.fetch_period(es.REPORT, "2018-03-31", get=get) == []


def test_forecast_keeps_each_indicator_and_dedupes(tmp_path):
    rows = es.fetch_period(es.FORECAST, "2026-09-30", get=FakeGet({"RPT_PUBLIC_OP_NEWPREDICT": [[
        _fc("000001", "2026-10-10", "004", amt=1.0),
        _fc("000001", "2026-10-10", "005", amt=2.0),
        _fc("000001", "2026-10-10", "004", update="2026-10-12", amt=9.0),  # 同键更新版
    ]]}), now=_now)
    n = es.write_period(tmp_path, es.FORECAST, "2026-09-30", rows)
    assert n == 2
    df = pl.read_parquet(es.partition_file(tmp_path, es.FORECAST, "2026-09-30"))
    got = dict(zip(df["indicator_code"], df["amt_lower"], strict=True))
    assert got == {"004": 9.0, "005": 2.0}


def test_merge_keeps_old_rows_and_takes_latest_update(tmp_path):
    p = "2026-06-30"
    first = es.fetch_period(es.REPORT, p, get=FakeGet({"RPT_LICO_FN_CPD": [[
        _rp("000001", "2026-08-15", "2026-08-15", 100.0),
        _rp("000002", "2026-08-20", "2026-08-20", 50.0),
    ]]}), now=_now)
    es.write_period(tmp_path, es.REPORT, p, first)
    # 第二天：000001 更正（同公告日、更新日更晚），000002 不再出现，000003 新增
    second = es.fetch_period(es.REPORT, p, get=FakeGet({"RPT_LICO_FN_CPD": [[
        _rp("000001", "2026-08-15", "2026-09-01", 101.0),
        _rp("000003", "2026-08-30", "2026-08-30", 7.0),
    ]]}), now=_now)
    assert es.write_period(tmp_path, es.REPORT, p, second) == 3
    df = es.read_dataset(tmp_path, es.REPORT)
    got = {r["code"]: (r["net_profit"], r["update_date"]) for r in df.iter_rows(named=True)}
    assert got == {"000001": (101.0, "2026-09-01"), "000002": (50.0, "2026-08-20"),
                   "000003": (7.0, "2026-08-30")}
    # 再写同样的数据，行数不变（幂等）
    assert es.write_period(tmp_path, es.REPORT, p, second) == 3


def test_sync_periods_continues_after_failure(tmp_path):
    get = FakeGet({"RPT_LICO_FN_CPD": [[_rp("000001", "2026-08-15", "2026-08-15", 1.0)]],
                   "RPT_FCI_PERFORMANCEE": []},
                  fail={"RPT_PUBLIC_OP_NEWPREDICT"})
    slept = []
    res = es.sync_periods(tmp_path, ["2026-06-30"], get=get, spacing_s=0.5,
                          sleeper=slept.append, log=lambda _m: None)
    assert res.wrote == {"em_report/2026-06-30": 1}
    assert res.fetched["em_express/2026-06-30"] == 0
    assert len(res.errors) == 1 and "em_forecast" in res.errors[0]
    assert slept == [0.5, 0.5]  # 三个数据集之间各隔一次
    assert not es.partition_file(tmp_path, es.FORECAST, "2026-06-30").exists()


def test_sync_skips_when_locked(tmp_path):
    get = FakeGet({})
    with es._exclusive(tmp_path) as ok:
        assert ok
        res = es.sync_periods(tmp_path, ["2026-06-30"], get=get, log=lambda _m: None)
    assert res.skipped_locked and get.calls == []


def test_run_daily_disabled_in_tests(tmp_path):
    assert es.enabled() is False
    assert es.run_daily(tmp_path) is None


def test_register_job():
    from apscheduler.schedulers.background import BackgroundScheduler

    s = BackgroundScheduler(timezone="Asia/Shanghai")
    es.register_job(s)
    job = s.get_job("earnings_em_daily")
    assert job is not None
    assert "hour='18'" in str(job.trigger) and "minute='40'" in str(job.trigger)


def test_cli_requires_one_mode(capsys):
    assert es.main([]) == 2
    assert es.main(["--latest", "2", "--from", "2018-03-31"]) == 2
    assert es.main(["--latest", "1", "--datasets", "nope"]) == 2


def test_cli_backfill_uses_injected_getter(tmp_path, monkeypatch):
    get = FakeGet({"RPT_LICO_FN_CPD": [[_rp("000001", "2018-04-20", "2018-04-20", 5.0,
                                            period="2018-03-31")]]})
    monkeypatch.setattr(es, "_default_get", lambda: get)
    code = es.main(["--from", "2018-03-31", "--to", "2018-04-30", "--datasets", "em_report",
                    "--data-dir", str(tmp_path), "--spacing", "0"])
    assert code == 0
    assert es.read_dataset(tmp_path, es.REPORT)["code"].to_list() == ["000001"]
