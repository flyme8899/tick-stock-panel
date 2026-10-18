"""ETF 单次除权因子：份额折算、现金分红、幻影行删除和可重复重建。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.fund_flow import etf_inflow
from app.services import etf_adj_factor as etf_adj
from scripts.rebuild_etf_adj_factor import rebuild

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


def test_cumulative_factor_nulls_inflow_but_real_dividend_does_not(tmp_path):
    """线上把累计因子写在分红日时，|f-1|>=0.05 会把申赎留空。单次分红因子不会。"""
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

    assert _inflow(1.08) is None
    assert _inflow(5.513 / (5.513 - 0.087)) == pytest.approx(2.0)
    assert _inflow(0.3210812570337 / 0.28032483) is None
    assert _inflow(0.36555) is None
