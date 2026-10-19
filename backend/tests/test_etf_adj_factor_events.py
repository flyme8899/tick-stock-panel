"""ETF 单次除权因子：份额折算、现金分红、幻影行删除和可重复重建。"""
from __future__ import annotations

import urllib.error
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.fund_flow import etf_inflow
from app.services import etf_adj_factor as etf_adj
from scripts.rebuild_etf_adj_factor import _parse_symbols, rebuild

SINA_510500 = """
var sh510500hfq={"total":5,"data":[
{"d":"2024-05-17","f":"1","s":"0.3210812570337000","u":"0.0870000000000000"},
{"d":"2022-08-29","f":"1","s":"0.3210812570337000","u":"0.0000000000000000"},
{"d":"2022-08-26","f":"1","s":"0.3210812570337000","u":"0.0000000000000000"},
{"d":"2015-04-15","f":"1","s":"0.2803248300000000","u":"0.0000000000000000"},
{"d":"1900-01-01","f":"1","s":"1.0000000000000000","u":"0.0000000000000000"}
]}; /* comment with a brace } */
"""

SINA_512100 = """
var sh512100hfq={"total":3,"data":[
{"d":"2022-09-05","f":"1","s":"0.3655500000000000","u":"0"},
{"d":"2022-08-04","f":"1","s":"1","u":"0"},
{"d":"1900-01-01","f":"1","s":"1","u":"0"}
]}
"""

SINA_SPLIT_AND_CASH = """
var sz159922hfq={"total":2,"data":[
{"d":"2024-12-02","f":"1","s":"2.5","u":"0.1292"},
{"d":"1900-01-01","f":"1","s":"1","u":"0"}
]}
"""

SINA_EMPTY = """
var sz159915hfq={"total":1,"data":[
{"d":"1900-01-01","f":"1","s":"1","u":"0"}
]}
"""


def _closes_510500() -> dict[date, float]:
    return {
        date(2015, 4, 14): 1.0,
        date(2015, 4, 15): 3.5,
        date(2022, 8, 25): 7.236,
        date(2022, 8, 26): 7.220,
        date(2022, 8, 29): 6.300,
        date(2024, 5, 16): 5.513,
        date(2024, 5, 17): 5.504,
    }


def test_510500_split_lands_on_price_gap_not_sina_record_day():
    actions = etf_adj.corporate_actions(etf_adj.parse_sina_hfq_payload(SINA_510500))
    rows = {row["trade_date"]: row for row in etf_adj.factor_rows("510500.SH", actions, _closes_510500())}
    split = rows[date(2022, 8, 29)]
    assert date(2022, 8, 26) not in rows
    assert split["ex_factor"] == pytest.approx(0.3210812570337 / 0.28032483)
    assert abs(split["ex_factor"] - 1.14539) < 1e-4
    dividend = rows[date(2024, 5, 17)]
    assert dividend["ex_factor"] == pytest.approx(5.513 / (5.513 - 0.087))
    assert abs(dividend["ex_factor"] - 1.0) < etf_inflow._SPLIT_FACTOR_GAP
    assert abs(split["ex_factor"] - 1.0) >= etf_inflow._SPLIT_FACTOR_GAP


def test_512100_reverse_split_ignores_zero_row_before_the_event():
    actions = etf_adj.corporate_actions(etf_adj.parse_sina_hfq_payload(SINA_512100))
    assert [action.record_date for action in actions] == [date(2022, 9, 5)]
    closes = {date(2022, 9, 2): 0.982, date(2022, 9, 5): 2.713}
    rows = etf_adj.factor_rows("512100.SH", actions, closes)
    assert len(rows) == 1
    assert rows[0]["trade_date"] == date(2022, 9, 5)
    assert rows[0]["ex_factor"] == pytest.approx(0.36555)
    assert abs(rows[0]["ex_factor"] - 1.0) >= etf_inflow._SPLIT_FACTOR_GAP


def test_split_plus_cash_uses_post_split_dividend():
    actions = etf_adj.corporate_actions(etf_adj.parse_sina_hfq_payload(SINA_SPLIT_AND_CASH))
    closes = {date(2024, 11, 29): 6.159, date(2024, 12, 2): 2.40}
    rows = etf_adj.factor_rows("159922.SZ", actions, closes)
    post = 6.159 / 2.5 - 0.1292
    assert rows[0]["ex_factor"] == pytest.approx(6.159 / post)
    assert rows[0]["trade_date"] == date(2024, 12, 2)


def test_integer_split_stays_exact():
    text = """
    var sz159220hfq={"total":2,"data":[
    {"d":"2025-11-10","f":"1","s":"2","u":"0"},
    {"d":"1900-01-01","f":"1","s":"1","u":"0"}]}
    """
    actions = etf_adj.corporate_actions(etf_adj.parse_sina_hfq_payload(text))
    rows = etf_adj.factor_rows(
        "159220.SZ", actions, {date(2025, 11, 7): 1.24, date(2025, 11, 10): 0.627},
    )
    assert rows[0]["ex_factor"] == 2.0


def test_cash_dividend_without_prev_close_is_not_stored_as_one():
    actions = etf_adj.corporate_actions(etf_adj.parse_sina_hfq_payload(
        'var x={"data":[{"d":"2024-05-17","s":"1","u":"0.087"},{"d":"1900-01-01","s":"1","u":"0"}]}'
    ))
    assert etf_adj.factor_rows("510500.SH", actions, {}) == []


def test_replace_drops_phantom_and_second_run_is_idempotent(tmp_path):
    store = tmp_path / "adj_factor_etf" / "all.parquet"
    store.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["510500.SH", "510500.SH", "159915.SZ", "512100.SH", "588000.SH"],
        "trade_date": [
            date(2022, 8, 26), date(2020, 1, 2), date(2024, 1, 2), date(2022, 9, 5), date(2024, 6, 1),
        ],
        "ex_factor": [1.8, 1.2, 1.5, 0.9, 1.5],
    }).write_parquet(store)

    texts = {
        "510500.SH": SINA_510500,
        "512100.SH": SINA_512100,
        "159915.SZ": SINA_EMPTY,
    }
    prices = {
        "510500.SH": _closes_510500(),
        "512100.SH": {date(2022, 9, 2): 0.982, date(2022, 9, 5): 2.713},
    }

    def fetch_text(symbol):
        if symbol == "588000.SH":
            raise OSError("sina down")
        return texts[symbol]

    def fetch_prices(symbol, start, end):
        return {day: price for day, price in prices.get(symbol, {}).items() if start <= day <= end}

    symbols = ["510500.SH", "512100.SH", "159915.SZ", "588000.SH"]
    first = rebuild(
        tmp_path, symbols, fetch_text=fetch_text, fetch_prices=fetch_prices, sleep_s=0,
    )
    assert first["written"] is True
    backup = Path(first["backup"])
    assert backup.is_file()
    # 与因子目录并列，净流入的 **/*.parquet 扫不到这份旧表。
    assert backup.parent == tmp_path / "backup" / "adj_factor_etf"
    assert list((tmp_path / "adj_factor_etf").glob("**/*.parquet")) == [store]
    assert "588000.SH" in first["failed"]
    stored = pl.read_parquet(store).sort(["symbol", "trade_date"])
    symbols_left = set(stored["symbol"].to_list())
    assert "159915.SZ" not in symbols_left
    assert "588000.SH" in symbols_left
    assert float(stored.filter(pl.col("symbol") == "588000.SH")["ex_factor"][0]) == 1.5
    split = stored.filter(
        (pl.col("symbol") == "510500.SH") & (pl.col("trade_date") == date(2022, 8, 29))
    )
    assert split.height == 1
    assert stored.filter(
        (pl.col("symbol") == "510500.SH") & (pl.col("trade_date") == date(2022, 8, 26))
    ).is_empty()
    assert stored.filter(pl.col("symbol") == "512100.SH")["ex_factor"][0] == pytest.approx(0.36555)

    backup_dir = tmp_path / "backup" / "adj_factor_etf"
    before = sorted(backup_dir.iterdir())
    second = rebuild(
        tmp_path, symbols, fetch_text=fetch_text, fetch_prices=fetch_prices, sleep_s=0,
    )
    assert second["written"] is False
    assert second["affected"] == []
    assert sorted(backup_dir.iterdir()) == before


def test_dry_run_does_not_touch_the_store(tmp_path):
    store = tmp_path / "adj_factor_etf" / "all.parquet"
    store.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["159915.SZ"],
        "trade_date": [date(2024, 1, 2)],
        "ex_factor": [1.5],
    }).write_parquet(store)
    summary = rebuild(
        tmp_path, ["159915.SZ"], dry_run=True, sleep_s=0,
        fetch_text=lambda symbol: SINA_EMPTY, fetch_prices=lambda *a, **k: {},
    )
    assert summary["written"] is False
    assert summary["affected"] == ["159915.SZ"]
    assert not (tmp_path / "backup").exists()
    assert pl.read_parquet(store)["ex_factor"][0] == 1.5


def test_remote_closes_fill_gaps_and_keep_local_prices(tmp_path):
    part = tmp_path / "kline_etf_daily" / "date=2022-09-02"
    part.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["512100.SH"],
        "date": [date(2022, 9, 2)],
        "close": [0.982],
    }).write_parquet(part / "part.parquet")

    def fetch_prices(symbol, start, end):
        assert symbol == "512100.SH"
        return {
            date(2022, 9, 2): 9.99,
            date(2022, 9, 5): 2.713,
        }

    summary = rebuild(
        tmp_path, ["512100.SH"], sleep_s=0,
        fetch_text=lambda symbol: SINA_512100,
        fetch_prices=fetch_prices,
    )
    assert summary["written"] is True
    stored = pl.read_parquet(tmp_path / "adj_factor_etf" / "all.parquet")
    assert stored.height == 1
    assert stored["trade_date"][0] == date(2022, 9, 5)
    assert stored["prev_close"][0] == pytest.approx(0.982)
    assert stored["ex_factor"][0] == pytest.approx(0.36555)


def test_ex_date_factor_does_not_blank_that_days_flow(tmp_path):
    """大因子记在除权日时，不再把除权日当天的申赎留空。

    折算还原落在前一个交易日。这一步的两份落盘都在除权日一侧，份额没有再跳，
    金额按普通份额差计算。单次分红因子偏离 1 不到拆分阈值，同样不改金额。
    """
    from app.fund_flow import query, store

    prev, current = "2026-05-15", "2026-05-18"
    for day, shares in ((prev, 10e8), (current, 11e8)):
        store.write_rows(tmp_path, "etf_shares", day, [{
            "symbol": "510500", "code": "510500", "name": "中证500ETF",
            "trade_date": day, "shares": shares, "source": "akshare",
        }])
    part = tmp_path / "kline_etf_daily" / f"date={current}"
    part.mkdir(parents=True)
    pl.DataFrame({"symbol": ["510500.SH"], "close": [2.0]}).write_parquet(part / "part.parquet")
    (tmp_path / "kline_etf_daily" / f"date={prev}").mkdir(parents=True)

    def _inflow(factor: float) -> float | None:
        path = tmp_path / "adj_factor_etf" / "all.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": ["510500.SH"],
            "trade_date": [date(2026, 5, 18)],
            "ex_factor": [factor],
        }).write_parquet(path)
        rows = query.board(tmp_path)["etf_shares"]["items"]
        return next(row for row in rows if row["code"] == "510500")["net_inflow_1d"]

    assert _inflow(1.08) == pytest.approx(2.0)
    assert _inflow(5.513 / (5.513 - 0.087)) == pytest.approx(2.0)
    assert _inflow(0.3210812570337 / 0.28032483) == pytest.approx(2.0)
    assert _inflow(0.36555) == pytest.approx(2.0)


def _seed_factor(root: Path, symbol: str, day: date, factor: float) -> Path:
    path = root / "adj_factor_etf" / "all.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [symbol],
        "trade_date": [day],
        "ex_factor": [factor],
    }).write_parquet(path)
    return path


def test_tencent_http_501_keeps_the_old_row(tmp_path):
    path = _seed_factor(tmp_path, "510500.SH", date(2024, 5, 17), 1.5)

    def fetch_prices(symbol, start, end):
        raise urllib.error.HTTPError(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            501, "Not Implemented", hdrs=None, fp=None,
        )

    summary = rebuild(
        tmp_path, ["510500.SH"], sleep_s=0,
        fetch_text=lambda symbol: SINA_510500,
        fetch_prices=fetch_prices,
    )
    assert summary["written"] is False
    assert summary["failed"] == ["510500.SH"]
    assert summary["affected"] == []
    stored = pl.read_parquet(path)
    assert stored.height == 1
    assert stored["ex_factor"][0] == 1.5
    assert stored["trade_date"][0] == date(2024, 5, 17)


def test_tencent_empty_bars_do_not_drop_the_dividend(tmp_path):
    path = _seed_factor(tmp_path, "510500.SH", date(2024, 5, 17), 1.016)
    summary = rebuild(
        tmp_path, ["510500.SH"], sleep_s=0,
        fetch_text=lambda symbol: SINA_510500,
        fetch_prices=lambda symbol, start, end: {},
    )
    assert summary["written"] is False
    assert "510500.SH" in summary["failed"]
    assert pl.read_parquet(path)["ex_factor"][0] == 1.016


def test_empty_sina_data_does_not_delete_existing_rows(tmp_path):
    path = _seed_factor(tmp_path, "159915.SZ", date(2024, 1, 2), 1.5)
    summary = rebuild(
        tmp_path, ["159915.SZ"], sleep_s=0,
        fetch_text=lambda symbol: 'var x={"total":0,"data":[]}',
        fetch_prices=lambda symbol, start, end: {},
    )
    assert summary["written"] is False
    assert summary["failed"] == ["159915.SZ"]
    stored = pl.read_parquet(path)
    assert stored["symbol"][0] == "159915.SZ"
    assert stored["ex_factor"][0] == 1.5


def test_rebuild_symbols_only_touches_the_requested_codes(tmp_path):
    path = tmp_path / "adj_factor_etf" / "all.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["510500.SH", "159915.SZ"],
        "trade_date": [date(2020, 1, 2), date(2024, 1, 2)],
        "ex_factor": [1.8, 1.5],
    }).write_parquet(path)
    seen: list[str] = []

    def fetch_text(symbol):
        seen.append(symbol)
        return SINA_EMPTY

    summary = rebuild(
        tmp_path, _parse_symbols("510500.SH, 512100.SH"), sleep_s=0,
        fetch_text=fetch_text, fetch_prices=lambda *args, **kwargs: {},
    )
    assert seen == ["510500.SH", "512100.SH"]
    assert summary["symbols"] == 2
    stored = pl.read_parquet(path)
    assert stored["symbol"].to_list() == ["159915.SZ"]
    assert stored["ex_factor"][0] == 1.5


def test_scheduled_sync_runs_full_weekly_and_retries_failures_only(tmp_path, monkeypatch):
    calls: list[str] = []

    def fetch_text(symbol):
        calls.append(symbol)
        if symbol == "588000.SH":
            return 'var x={"data":[]}'
        return SINA_EMPTY

    def _tencent_forbidden(*_args, **_kwargs):
        raise AssertionError("盘后同步不应请求腾讯")

    monkeypatch.setattr(etf_adj, "fetch_tencent_range", _tencent_forbidden)
    symbols = ["510500.SH", "588000.SH"]
    first = etf_adj.scheduled_sync(
        symbols, tmp_path, today=date(2026, 10, 18), fetch_text=fetch_text, sleep_s=0,
    )
    assert first.skipped is False
    assert calls == symbols
    assert first.failed == ["588000.SH"]

    calls.clear()
    second = etf_adj.scheduled_sync(
        symbols, tmp_path, today=date(2026, 10, 19), fetch_text=fetch_text, sleep_s=0,
    )
    assert second.skipped is False
    assert calls == ["588000.SH"]

    calls.clear()

    def fetch_ok(symbol):
        calls.append(symbol)
        return SINA_EMPTY

    rested = etf_adj.scheduled_sync(
        symbols, tmp_path, today=date(2026, 10, 20), fetch_text=fetch_ok, sleep_s=0,
    )
    assert calls == ["588000.SH"]
    assert rested.failed == []

    calls.clear()
    skipped = etf_adj.scheduled_sync(
        symbols, tmp_path, today=date(2026, 10, 21),
        fetch_text=lambda symbol: (_ for _ in ()).throw(AssertionError(symbol)),
        sleep_s=0,
    )
    assert skipped.skipped is True
    assert calls == []

    calls.clear()
    again = etf_adj.scheduled_sync(
        symbols, tmp_path, today=date(2026, 10, 25), fetch_text=fetch_text, sleep_s=0,
    )
    assert again.skipped is False
    assert calls == symbols


def _write_etf_close(root: Path, day: str, symbol: str, close: float) -> None:
    part = root / "kline_etf_daily" / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [symbol],
        "date": [date.fromisoformat(day)],
        "close": [close],
    }).write_parquet(part / "part.parquet")


def test_scheduled_sync_prices_only_events_on_or_after_local_kline_start(tmp_path):
    """2015 年的折算早于本地日 K（2021-06-01），不能让 510500 永远留在重试名单。"""
    for day, close in (
        ("2021-06-01", 1.0),
        ("2022-08-25", 7.236),
        ("2022-08-26", 7.220),
        ("2022-08-29", 6.300),
        ("2024-05-16", 5.513),
        ("2024-05-17", 5.504),
    ):
        _write_etf_close(tmp_path, day, "510500.SH", close)
    _seed_factor(tmp_path, "510500.SH", date(2015, 4, 15), 0.28032483)

    def fetch_text(symbol):
        assert symbol == "510500.SH"
        return SINA_510500

    first = etf_adj.scheduled_sync(
        ["510500.SH"], tmp_path, today=date(2026, 10, 18),
        fetch_text=fetch_text, sleep_s=0,
    )
    assert first.failed == []
    stored = pl.read_parquet(tmp_path / "adj_factor_etf" / "all.parquet").sort("trade_date")
    by_day = {row["trade_date"]: row["ex_factor"] for row in stored.iter_rows(named=True)}
    assert by_day[date(2015, 4, 15)] == pytest.approx(0.28032483)
    assert date(2022, 8, 26) not in by_day
    assert by_day[date(2022, 8, 29)] == pytest.approx(0.3210812570337 / 0.28032483)
    assert by_day[date(2024, 5, 17)] == pytest.approx(5.513 / (5.513 - 0.087))

    def _no_network(symbol):
        raise AssertionError(symbol)

    skipped = etf_adj.scheduled_sync(
        ["510500.SH"], tmp_path, today=date(2026, 10, 19),
        fetch_text=_no_network, sleep_s=0,
    )
    assert skipped.skipped is True


def test_unstored_event_before_local_start_is_skipped(tmp_path):
    for day, close in (
        ("2021-06-01", 1.0),
        ("2024-05-16", 5.513),
        ("2024-05-17", 5.504),
        ("2022-08-25", 7.236),
        ("2022-08-26", 7.220),
        ("2022-08-29", 6.300),
    ):
        _write_etf_close(tmp_path, day, "510500.SH", close)
    outcome = etf_adj.scheduled_sync(
        ["510500.SH"], tmp_path, today=date(2026, 10, 18),
        fetch_text=lambda symbol: SINA_510500, sleep_s=0,
    )
    assert outcome.failed == []
    days = pl.read_parquet(tmp_path / "adj_factor_etf" / "all.parquet")["trade_date"].to_list()
    assert date(2015, 4, 15) not in days
    assert date(2024, 5, 17) in days


def test_without_price_floor_an_early_event_still_fails_the_symbol(tmp_path):
    for day, close in (
        ("2022-08-25", 7.236),
        ("2022-08-26", 7.220),
        ("2022-08-29", 6.300),
        ("2024-05-16", 5.513),
        ("2024-05-17", 5.504),
    ):
        _write_etf_close(tmp_path, day, "510500.SH", close)
    _incoming, replaced, failed = etf_adj.collect_etf_events(
        ["510500.SH"], tmp_path, fetch_text=lambda symbol: SINA_510500,
        remote_prices=False, sleep_s=0,
    )
    assert replaced == []
    assert failed == ["510500.SH"]


def test_missing_factor_file_forces_full_sync_inside_the_week(tmp_path):
    etf_adj._write_state(tmp_path, {"last_full_date": "2026-10-20", "retry": []})
    calls: list[str] = []

    def fetch_text(symbol):
        calls.append(symbol)
        return SINA_EMPTY

    outcome = etf_adj.scheduled_sync(
        ["510300.SH", "510050.SH"], tmp_path, today=date(2026, 10, 21),
        fetch_text=fetch_text, sleep_s=0,
    )
    assert outcome.skipped is False
    assert calls == ["510300.SH", "510050.SH"]


def test_local_closes_load_in_bounded_batches(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    def load(data_dir, symbols, start=None, end=None):
        assert start is not None and end is not None and start <= end
        seen.append(list(symbols))
        return {
            symbol: {date(2022, 9, 2): 0.982, date(2022, 9, 5): 2.713}
            for symbol in symbols
        }

    monkeypatch.setattr(etf_adj, "load_local_closes", load)
    symbols = [f"51210{index}.SH" for index in range(5)]
    incoming, replaced, failed = etf_adj.collect_etf_events(
        symbols, tmp_path, fetch_text=lambda symbol: SINA_512100,
        remote_prices=False, sleep_s=0, close_batch=2,
    )
    assert failed == []
    assert replaced == symbols
    assert incoming.height == 5
    assert seen and all(len(batch) <= 2 for batch in seen)
    assert [symbol for batch in seen for symbol in batch] == symbols
