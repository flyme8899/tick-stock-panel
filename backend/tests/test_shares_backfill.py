"""股本回补：映射、两源不一致不写、幂等、退市跳过。外部源全部 mock。"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.shares_backfill import (
    SOURCE_CNINFO_ONLY,
    SOURCE_TAG,
    LocalRow,
    ShareEvent,
    map_adata,
    map_cninfo,
    needs_share_backfill,
    plan_changes,
    reconcile,
    run_backfill,
    seconds_until_clear,
    warn_lagging_listed_shares,
)


def _write_market(tmp_path, shares: pl.DataFrame, symbols: list[tuple[str, str, float]]) -> pl.DataFrame:
    instruments = pl.DataFrame({
        "symbol": [item[0] for item in symbols],
        "list_status": [item[1] for item in symbols],
    })
    bars = pl.DataFrame({
        "symbol": [item[0] for item in symbols],
        "volume": [item[2] for item in symbols],
    })
    inst = tmp_path / "instruments" / "instruments.parquet"
    inst.parent.mkdir(parents=True)
    instruments.write_parquet(inst)
    bar_path = tmp_path / "kline_daily" / "date=2026-10-17" / "part.parquet"
    bar_path.parent.mkdir(parents=True)
    bars.write_parquet(bar_path)
    path = tmp_path / "financials" / "shares" / "part.parquet"
    path.parent.mkdir(parents=True)
    shares.write_parquet(path)
    return shares


def _agreed_history() -> list[ShareEvent]:
    return [
        ShareEvent(date(2019, 5, 29), date(2019, 5, 30), 422_000_000.0, 320_000_000.0),
        ShareEvent(date(2024, 6, 30), date(2024, 7, 1), 422_000_000.0, 400_000_000.0),
        ShareEvent(date(2025, 7, 10), date(2025, 7, 8), 648_000_000.0, 600_000_000.0),
    ]


def _local_shares() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600065.SH", "600065.SH"],
        "period_end": ["2019-05-29", "2024-06-30"],
        "announce_date": ["2019-05-30", "2024-07-01"],
        "total_shares": [391_000_000.0, 422_000_000.0],
        "float_shares": [300_000_000.0, 400_000_000.0],
        "source": ["tickflow", "tickflow"],
        "note": ["keep", "keep"],
    })


def test_map_cninfo_and_adata_to_share_schema():
    cninfo = map_cninfo("600065.SH", [{
        "证券代码": "600065",
        "变动日期": "2025-07-10",
        "公告日期": "2025-07-08",
        "总股本": "64,800",
        "人民币普通股": 60_000,
        "已流通股份": 64_800,
        "流通受限股份": 4_800,
    }])
    adata = map_adata("600065.SH", [{
        "stock_code": "600065",
        "change_date": date(2025, 7, 10),
        "total_shares": 648_000_000,
        "list_a_shares": 600_000_000,
        "change_reason": "转增",
    }])

    agreed, point_reasons, reason = reconcile(cninfo, adata)

    assert reason == ""
    assert point_reasons == []
    assert agreed[0].period_end == date(2025, 7, 10)
    assert agreed[0].announce_date == date(2025, 7, 8)
    assert agreed[0].total_shares == 648_000_000
    assert agreed[0].float_shares == 600_000_000
    assert agreed[0].source == SOURCE_TAG


def test_cninfo_wan_shares_match_adata_share_count():
    """300067：巨潮 117792.4574 万股，adata 1177924574 股，换算后视为一致。"""
    cninfo = map_cninfo("300067.SZ", [{
        "证券代码": "300067",
        "变动日期": "2026-10-16",
        "公告日期": "2026-10-16",
        "总股本": 117792.4574,
        "人民币普通股": 90_000.5,
        "已流通股份": 100_000.25,
        "流通受限股份": 27_791.9574,
    }])
    adata = map_adata("300067.SZ", [{
        "stock_code": "300067",
        "change_date": "2026-10-16",
        "total_shares": 1_177_924_574,
        "list_a_shares": 900_005_000,
    }])

    assert cninfo[0].total_shares == pytest.approx(1_177_924_574)
    assert cninfo[0].float_shares == pytest.approx(900_005_000)
    agreed, point_reasons, reason = reconcile(cninfo, adata)
    assert reason == ""
    assert point_reasons == []
    assert agreed[0].total_shares == pytest.approx(1_177_924_574)
    assert agreed[0].float_shares == pytest.approx(900_005_000)
    assert agreed[0].source == SOURCE_TAG

    # 换算后仍差超过 1% 时只复核该点，不按数量级再猜一次单位。
    raw = [ShareEvent(date(2026, 10, 16), date(2026, 10, 16), 117792.4574, None)]
    other = [ShareEvent(date(2026, 10, 16), None, 1_177_924_574.0, None)]
    missed, missed_points, missed_reason = reconcile(raw, other)
    assert missed == []
    assert missed_reason == ""
    assert missed_points == ["total_mismatch:2026-10-16"]


def test_sources_within_one_percent_keep_local_value():
    local = [LocalRow(date(2024, 6, 30), date(2024, 7, 1), 100.0, 80.0, "tickflow")]
    agreed = [ShareEvent(date(2024, 6, 30), date(2024, 7, 1), 100.5, 80.2)]

    assert plan_changes("600065.SH", local, agreed) == []
    assert needs_share_backfill(
        last_period=date(2026, 6, 30),
        stale_before=date(2025, 1, 1),
        local_total=100.0,
        external_total=100.5,
    ) is False
    assert needs_share_backfill(
        last_period=date(2026, 6, 30),
        stale_before=date(2025, 1, 1),
        local_total=391_000_000.0,
        external_total=422_000_000.0,
    ) is True


def test_disagreement_does_not_write(tmp_path):
    original = _local_shares()
    _write_market(tmp_path, original, [("600065.SH", "L", 1000.0)])
    path = tmp_path / "financials" / "shares" / "part.parquet"

    def cninfo(_symbol: str) -> list[ShareEvent]:
        return [ShareEvent(date(2025, 7, 10), date(2025, 7, 8), 648_000_000.0, 600_000_000.0)]

    def adata(_symbol: str) -> list[ShareEvent]:
        return [ShareEvent(date(2025, 7, 10), None, 700_000_000.0, 600_000_000.0)]

    result = run_backfill(
        tmp_path,
        apply=True,
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=cninfo,
        fetch_adata=adata,
        sleeper=lambda _seconds: None,
        spacing_s=0,
        now=lambda: datetime(2026, 10, 19, 12, 0, tzinfo=CN_TZ),
    )

    assert result.wrote is False
    assert result.backup is None
    assert result.changes == []
    assert result.reviews[0].reason == "total_mismatch:2025-07-10"
    stored = pl.read_parquet(path)
    assert stored.select(original.columns).equals(original)
    with result.review_report.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["symbol"] == "600065.SH"
    assert rows[0]["reason"] == "total_mismatch:2025-07-10"


def test_apply_is_idempotent_and_dry_run_does_not_write(tmp_path):
    original = _local_shares()
    _write_market(tmp_path, original, [("600065.SH", "L", 1000.0)])
    path = tmp_path / "financials" / "shares" / "part.parquet"
    history = _agreed_history()

    def now() -> datetime:
        return datetime(2026, 10, 19, 12, 0, tzinfo=CN_TZ)

    def fetch(_symbol: str) -> list[ShareEvent]:
        return history

    dry = run_backfill(
        tmp_path,
        apply=False,
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=fetch,
        fetch_adata=fetch,
        sleeper=lambda _seconds: None,
        spacing_s=0,
        now=now,
    )
    assert dry.wrote is False
    assert not (path.parent / "backups").exists()
    assert {(item.action, item.period_end) for item in dry.changes} == {
        ("correct", date(2019, 5, 29)),
        ("append", date(2025, 7, 10)),
    }
    assert pl.read_parquet(path).equals(original)

    applied = run_backfill(
        tmp_path,
        apply=True,
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=fetch,
        fetch_adata=fetch,
        sleeper=lambda _seconds: None,
        spacing_s=0,
        now=now,
    )
    assert applied.wrote is True
    assert applied.backup is not None
    assert pl.read_parquet(applied.backup).equals(original)
    stored = pl.read_parquet(path).sort("period_end")
    assert stored.schema["period_end"] == pl.Utf8
    assert stored["period_end"].to_list() == ["2019-05-29", "2024-06-30", "2025-07-10"]
    corrected = stored.row(0, named=True)
    assert corrected["total_shares"] == 422_000_000
    assert corrected["float_shares"] == 320_000_000
    assert corrected["announce_date"] == "2019-05-30"
    assert corrected["source"] == SOURCE_TAG
    assert corrected["note"] == "keep"
    unchanged = stored.row(1, named=True)
    assert unchanged["source"] == "tickflow"
    assert unchanged["note"] == "keep"
    assert unchanged["total_shares"] == 422_000_000
    appended = stored.row(2, named=True)
    assert appended["total_shares"] == 648_000_000
    assert appended["announce_date"] == "2025-07-08"
    assert appended["source"] == SOURCE_TAG
    assert appended["note"] is None

    again = run_backfill(
        tmp_path,
        apply=True,
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=fetch,
        fetch_adata=fetch,
        sleeper=lambda _seconds: None,
        spacing_s=0,
        now=now,
    )
    assert again.changes == []
    assert again.wrote is False
    assert list((path.parent / "backups").glob("*.parquet")) == [applied.backup]
    assert pl.read_parquet(path).sort("period_end").equals(stored)


def test_cninfo_update_after_adata_is_kept_and_old_mismatch_is_one_point(tmp_path):
    """002548：adata 停在 2025-09-24，巨潮还有 2026-06-30；旧日对不上只复核该点。"""
    original = pl.DataFrame({
        "symbol": ["002548.SZ", "002548.SZ"],
        "period_end": ["2019-05-29", "2025-09-24"],
        "announce_date": ["2019-05-30", "2025-09-25"],
        "total_shares": [391_000_000.0, 800_000_000.0],
        "float_shares": [300_000_000.0, 700_000_000.0],
        "source": ["tickflow", "tickflow"],
    })
    _write_market(tmp_path, original, [("002548.SZ", "", 1000.0)])
    path = tmp_path / "financials" / "shares" / "part.parquet"

    def cninfo(_symbol: str) -> list[ShareEvent]:
        return [
            ShareEvent(date(2019, 5, 29), date(2019, 5, 30), 422_000_000.0, 320_000_000.0),
            ShareEvent(date(2025, 9, 24), date(2025, 9, 25), 828_173_658.0, 700_000_000.0),
            ShareEvent(date(2026, 6, 30), date(2026, 7, 1), 900_000_000.0, 800_000_000.0),
        ]

    def adata(_symbol: str) -> list[ShareEvent]:
        return [
            ShareEvent(date(2019, 5, 29), None, 391_000_000.0, 300_000_000.0),
            ShareEvent(date(2025, 9, 24), None, 828_173_658.0, 700_000_000.0),
        ]

    result = run_backfill(
        tmp_path,
        apply=True,
        symbols=["002548.SZ"],
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=cninfo,
        fetch_adata=adata,
        sleeper=lambda _seconds: None,
        spacing_s=0,
        now=lambda: datetime(2026, 10, 19, 12, 0, tzinfo=CN_TZ),
    )

    assert [(item.action, item.period_end, item.source_new) for item in result.changes] == [
        ("correct", date(2025, 9, 24), SOURCE_TAG),
        ("append", date(2026, 6, 30), SOURCE_CNINFO_ONLY),
    ]
    assert [item.reason for item in result.reviews] == ["total_mismatch:2019-05-29"]
    assert result.wrote is True
    stored = pl.read_parquet(path).sort("period_end")
    assert stored["period_end"].to_list() == ["2019-05-29", "2025-09-24", "2026-06-30"]
    old = stored.row(0, named=True)
    assert old["total_shares"] == 391_000_000
    assert old["source"] == "tickflow"
    agreed = stored.row(1, named=True)
    assert agreed["total_shares"] == 828_173_658
    assert agreed["source"] == SOURCE_TAG
    newest = stored.row(2, named=True)
    assert newest["total_shares"] == 900_000_000
    assert newest["announce_date"] == "2026-07-01"
    assert newest["source"] == SOURCE_CNINFO_ONLY


def test_blank_list_status_follows_recent_trades(tmp_path):
    """list_status 为空时不判退市；近 30 日无成交的股票不入选。"""
    shares = pl.DataFrame({
        "symbol": ["600065.SH", "920157.BJ"],
        "period_end": ["2007-09-30", "2024-06-30"],
        "total_shares": [238_000_000.0, 50_000_000.0],
    })
    inst = tmp_path / "instruments" / "instruments.parquet"
    inst.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600065.SH", "920157.BJ"],
        "list_status": ["", ""],
    }).write_parquet(inst)
    bar = tmp_path / "kline_daily" / "date=2026-10-17" / "part.parquet"
    bar.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["920157.BJ"], "volume": [1000.0]}).write_parquet(bar)
    path = tmp_path / "financials" / "shares" / "part.parquet"
    path.parent.mkdir(parents=True)
    shares.write_parquet(path)
    called: list[str] = []

    def fetch(symbol: str) -> list[ShareEvent]:
        called.append(symbol)
        return [ShareEvent(date(2026, 1, 2), date(2026, 1, 3), 60_000_000.0, None)]

    result = run_backfill(
        tmp_path,
        apply=False,
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=fetch,
        fetch_adata=fetch,
        sleeper=lambda _seconds: None,
        spacing_s=0,
        now=lambda: datetime(2026, 10, 19, 12, 0, tzinfo=CN_TZ),
    )

    assert result.skipped_delisted == []
    assert result.candidates == ["920157.BJ"]
    assert called == ["920157.BJ", "920157.BJ"]
    assert pl.read_parquet(path)["symbol"].to_list() == ["600065.SH", "920157.BJ"]


def test_delisted_symbol_is_skipped(tmp_path):
    _write_market(
        tmp_path,
        pl.DataFrame({
            "symbol": ["000001.SZ"],
            "period_end": ["2007-09-30"],
            "announce_date": ["2007-10-30"],
            "total_shares": [238_000_000.0],
            "float_shares": [200_000_000.0],
            "source": ["tickflow"],
        }),
        [("000001.SZ", "D", 1000.0)],
    )
    called: list[str] = []

    def fetch(symbol: str) -> list[ShareEvent]:
        called.append(symbol)
        raise AssertionError("退市股不应请求外部源")

    result = run_backfill(
        tmp_path,
        apply=True,
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=fetch,
        fetch_adata=fetch,
        sleeper=lambda _seconds: None,
        spacing_s=0,
        now=lambda: datetime(2026, 10, 19, 12, 0, tzinfo=CN_TZ),
    )

    assert called == []
    assert result.candidates == []
    assert result.skipped_delisted == ["000001.SZ"]
    assert result.wrote is False
    assert pl.read_parquet(tmp_path / "financials" / "shares" / "part.parquet")["total_shares"].to_list() == [
        238_000_000.0
    ]


def test_scan_values_includes_latest_drift_over_one_percent(tmp_path):
    shares = pl.DataFrame({
        "symbol": ["300067.SZ"],
        "period_end": ["2026-06-30"],
        "announce_date": ["2026-07-20"],
        "total_shares": [100_000_000.0],
        "float_shares": [80_000_000.0],
        "source": ["tickflow"],
    })
    _write_market(tmp_path, shares, [("300067.SZ", "L", 1000.0)])
    updated = [ShareEvent(date(2026, 10, 16), date(2026, 10, 16), 110_000_000.0, 80_000_000.0)]

    skipped = run_backfill(
        tmp_path,
        apply=False,
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=lambda _symbol: updated,
        fetch_adata=lambda _symbol: updated,
        sleeper=lambda _seconds: None,
        spacing_s=0,
        now=lambda: datetime(2026, 10, 19, 12, 0, tzinfo=CN_TZ),
    )
    assert skipped.candidates == []

    scanned = run_backfill(
        tmp_path,
        apply=False,
        scan_values=True,
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=lambda _symbol: updated,
        fetch_adata=lambda _symbol: updated,
        sleeper=lambda _seconds: None,
        spacing_s=0,
        now=lambda: datetime(2026, 10, 19, 12, 0, tzinfo=CN_TZ),
    )
    assert scanned.candidates == ["300067.SZ"]
    assert [(item.action, item.total_new) for item in scanned.changes] == [("append", 110_000_000.0)]


def test_pause_window_and_spacing(tmp_path):
    assert seconds_until_clear(datetime(2026, 10, 19, 23, 55, tzinfo=CN_TZ)) == 900
    assert seconds_until_clear(datetime(2026, 10, 19, 23, 50, tzinfo=CN_TZ)) == 1200
    assert seconds_until_clear(datetime(2026, 10, 20, 0, 5, tzinfo=CN_TZ)) == 300
    assert seconds_until_clear(datetime(2026, 10, 20, 0, 10, tzinfo=CN_TZ)) == 0
    assert seconds_until_clear(datetime(2026, 10, 19, 23, 49, 59, tzinfo=CN_TZ)) == 0

    _write_market(tmp_path, _local_shares(), [("600065.SH", "L", 1000.0)])

    class Timeline:
        def __init__(self) -> None:
            self.wall = datetime(2026, 10, 19, 23, 55, tzinfo=CN_TZ)
            self.mono = 0.0
            self.slept: list[float] = []

        def now(self) -> datetime:
            return self.wall

        def monotonic(self) -> float:
            return self.mono

        def sleep(self, seconds: float) -> None:
            self.slept.append(seconds)
            self.mono += seconds
            self.wall += timedelta(seconds=seconds)

    timeline = Timeline()
    run_backfill(
        tmp_path,
        apply=False,
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=lambda _symbol: _agreed_history(),
        fetch_adata=lambda _symbol: _agreed_history(),
        sleeper=timeline.sleep,
        monotonic=timeline.monotonic,
        now=timeline.now,
        spacing_s=1.2,
        retries=1,
    )
    assert timeline.slept == pytest.approx([900, 1.2])


def test_fetch_retries_then_succeeds(tmp_path):
    _write_market(tmp_path, _local_shares(), [("600065.SH", "L", 1000.0)])
    calls = {"cninfo": 0}

    def cninfo(_symbol: str) -> list[ShareEvent]:
        calls["cninfo"] += 1
        if calls["cninfo"] < 3:
            raise RuntimeError("temporary")
        return _agreed_history()

    result = run_backfill(
        tmp_path,
        apply=False,
        as_of=date(2026, 10, 17),
        report_dir=tmp_path / "reports",
        fetch_cninfo=cninfo,
        fetch_adata=lambda _symbol: _agreed_history(),
        sleeper=lambda _seconds: None,
        spacing_s=0,
        retries=3,
        now=lambda: datetime(2026, 10, 19, 12, 0, tzinfo=CN_TZ),
    )
    assert calls["cninfo"] == 3
    assert result.errors == []
    assert any(item.action == "append" for item in result.changes)


def test_warn_logs_listed_lag_and_skips_delisted(tmp_path, caplog):
    shares = pl.DataFrame({
        "symbol": ["600065.SH", "000001.SZ", "603157.SH"],
        "period_end": ["2007-09-30", "2007-09-30", "2026-06-30"],
        "total_shares": [238_000_000.0, 100.0, 2_440_000_000.0],
    })
    _write_market(tmp_path, shares, [
        ("600065.SH", "L", 1000.0),
        ("000001.SZ", "D", 1000.0),
        ("603157.SH", "L", 1000.0),
    ])

    with caplog.at_level("WARNING"):
        found = warn_lagging_listed_shares(tmp_path, as_of=date(2026, 10, 17))

    assert found == ["600065.SH"]
    assert "股本滞后" in caplog.text
    assert "000001.SZ" not in caplog.text

    broken = tmp_path / "financials" / "shares" / "part.parquet"
    broken.write_text("not parquet", encoding="utf-8")
    assert warn_lagging_listed_shares(tmp_path, as_of=date(2026, 10, 17)) == []
