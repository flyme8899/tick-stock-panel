from datetime import date

import polars as pl
import pytest

from app.event_context import parse
from app.fund_flow import store
from scripts import normalize_fund_flow_symbols as norm


def test_notice_uses_display_time_when_present():
    rows = parse.parse_notices({"data": {"list": [{
        "art_code": "AN1", "title": "某公司:业绩预增公告", "notice_date": "2026-10-15 00:00:00",
        "display_time": "2026-10-15 13:19:06:548",
        "codes": [{"stock_code": "301633", "short_name": "港迪技术"}],
    }]}})
    assert rows[0]["published_at"] == "2026-10-15T13:19:06"
    assert rows[0]["time_precision"] == "second"
    assert rows[0]["available_from"] == "2026-10-15T13:19:06"


def test_notice_date_only_is_available_next_open(monkeypatch):
    monkeypatch.setattr("app.news.market_confirm._known_holiday", lambda day: False)
    timing = parse.notice_timing({"display_time": ""}, "2026-10-16")  # 周五
    assert timing == {"published_at": "", "time_precision": "date", "available_from": "2026-10-19T09:00:00"}


def test_notice_zero_clock_is_not_exact(monkeypatch):
    monkeypatch.setattr("app.news.market_confirm._known_holiday", lambda day: day == date(2026, 10, 19))
    timing = parse.notice_timing({"display_time": "2026-10-16 00:00:00"}, "2026-10-16")
    assert timing["time_precision"] == "date"
    assert timing["available_from"] == "2026-10-20T09:00:00"


def test_published_never_invents_15_00(monkeypatch):
    from app.event_context.jobs import _published

    monkeypatch.setattr("app.news.market_confirm._known_holiday", lambda day: False)
    stamp = _published({"notice_date": "2026-10-15"})
    assert (stamp.date().isoformat(), stamp.hour) == ("2026-10-16", 9)


@pytest.mark.parametrize("raw,want", [
    ("600000", "600000.SH"), ("600000.sh", "600000.SH"), ("000001", "000001.SZ"),
    ("300750", "300750.SZ"), ("688981", "688981.SH"), ("830799", "830799.BJ"),
    ("920001", "920001.BJ"), ("SZ000001", "000001.SZ"), ("", ""), ("abc", "ABC"),
])
def test_suffixed_symbol(raw, want):
    assert store.suffixed_symbol(raw) == want


def _row(symbol, code):
    return {"symbol": symbol, "code": code, "name": "x", "trade_date": "2026-10-09",
            "main_net": 1.0, "source": "t"}


def test_write_and_read_normalize_symbol(tmp_path):
    store.write_rows(tmp_path, "stock", "2026-10-09", [_row("600000", "600000"), _row("000001.SZ", "000001")])
    frame = store.read_partition(tmp_path, "stock", "2026-10-09")
    assert sorted(frame["symbol"].to_list()) == ["000001.SZ", "600000.SH"]


def test_one_off_normalizer_rewrites_and_flags_thin(tmp_path):
    path = store.partition_file(tmp_path, "stock", "2026-10-08")
    path.parent.mkdir(parents=True)
    pl.DataFrame([_row("600000", "600000"), _row("605056.SH", "605056")]).write_parquet(path)
    report = norm.run(tmp_path)
    assert report[0]["changed"] == 1 and report[0]["thin"] is True
    assert sorted(pl.read_parquet(path)["symbol"].to_list()) == ["600000.SH", "605056.SH"]
    assert norm.run(tmp_path)[0]["changed"] == 0


@pytest.mark.parametrize("trading,deferred", [(True, None), (False, "tickflow_sync")])
def test_trading_hours_ignore_minute_lock(monkeypatch, tmp_path, trading, deferred):
    from datetime import datetime

    from app.fund_flow import scheduler

    calls = []

    def fake_busy(*, include_minute=True):
        calls.append(include_minute)
        return include_minute  # 只有分钟锁在持有

    monkeypatch.setattr(scheduler, "tickflow_sync_busy", fake_busy)
    worker = scheduler.FundFlowWorker()
    worker.run_once(
        now=datetime(2026, 10, 15, 14, 0, tzinfo=scheduler.CN_TZ), data_dir=tmp_path,
        clients=object(), symbols=[], trading=trading,
    )
    assert calls[0] is (not trading)
    assert worker.deferred_reason == deferred
