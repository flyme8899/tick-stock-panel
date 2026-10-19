"""ETF 净流入：份额差 × 收盘价，拆分和缺口留空，领航者稿从 news.sqlite 接上。"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.fund_flow import etf_inflow, query, store
from app.market_time import CN_TZ
from app.news.store import NewsStore

DAY0 = date(2026, 10, 9)  # 周五
DAY1 = date(2026, 10, 12)  # 周一
DAY2 = date(2026, 10, 13)  # 周二


def _iso(day: date) -> str:
    return day.isoformat()


def _write_shares(root: Path, day: str, rows: list[tuple[str, float]]) -> None:
    store.write_rows(root, "etf_shares", day, [
        {
            "symbol": code, "code": code, "name": "沪深300ETF" if code == "510300" else code,
            "trade_date": day, "shares": shares, "source": "akshare",
        }
        for code, shares in rows
    ])


def _write_close(root: Path, day: str, prices: dict[str, float]) -> None:
    part = root / "kline_etf_daily" / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    symbols = [code if "." in code else f"{code}.SH" for code in prices]
    pl.DataFrame({"symbol": symbols, "close": list(prices.values())}).write_parquet(part / "part.parquet")


def _mark_trading(root: Path, day: str, *, kind: str = "kline_etf_daily") -> None:
    (root / kind / f"date={day}").mkdir(parents=True, exist_ok=True)


def _write_factor(root: Path, code: str, day: str, factor: float) -> None:
    path = root / "adj_factor_etf" / "all.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = pl.DataFrame({
        "symbol": [f"{code}.SH"],
        "trade_date": [date.fromisoformat(day)],
        "ex_factor": [factor],
    })
    if path.exists():
        incoming = pl.concat([pl.read_parquet(path), incoming], how="diagonal_relaxed")
    incoming.write_parquet(path)


def _insert_article(
    root: Path,
    trade_date: str,
    etfs: list[dict],
    *,
    source_id: str,
    broad: list[dict] | None = None,
    extracted_date: str | None = None,
    source: str = "etf_flow",
) -> None:
    year, month, day = (int(part) for part in trade_date.split("-"))
    published = datetime(year, month, day, 8, 0, tzinfo=CN_TZ) + timedelta(days=1)
    news = NewsStore(root / "news" / "news.sqlite")
    try:
        news.insert_item(
            source=source,
            source_id=source_id,
            published_at=published,
            author="ETF领航者",
            title=f"{month}月{day}日ETF基金申购和赎回",
            clean_text=f"交易日 {trade_date}",
            raw={
                "trade_date": extracted_date if extracted_date is not None else trade_date,
                "extracted": {
                    "trade_date": extracted_date if extracted_date is not None else trade_date,
                    "unit": "亿元",
                    "etfs": etfs,
                    "broad_index": broad or [],
                    "categories": [{"name": "宽基", "net_1d": 99.0}],
                },
            },
            content_hash=source_id,
            url=f"https://example.com/{source_id}",
            level="flow",
            media_ids=[],
            extra={},
            mentions=[],
        )
    finally:
        news.close()


def _board_row(root: Path, code: str) -> dict:
    rows = query.board(root)["etf_shares"]["items"]
    return next(row for row in rows if row["code"] == code)


def test_step_inflow_converts_share_delta_and_close_to_yi() -> None:
    # 份额增加 1 亿份，收盘价 2 元 → 2 亿元。
    assert etf_inflow.step_inflow_yi(2e8, 1e8, 2.0, adjacent=True, split=False) == pytest.approx(2.0)
    assert etf_inflow.step_inflow_yi(1e8, 2e8, 2.0, adjacent=True, split=False) == pytest.approx(-2.0)
    assert etf_inflow.step_inflow_yi(1e8, 1e8, 2.0, adjacent=True, split=False) == pytest.approx(0.0)


def test_step_inflow_skips_gap_split_and_missing_price() -> None:
    assert etf_inflow.step_inflow_yi(2e8, 1e8, 2.0, adjacent=False, split=False) is None
    assert etf_inflow.step_inflow_yi(2e8, 1e8, 2.0, adjacent=True, split=True) is None
    assert etf_inflow.step_inflow_yi(2e8, 1e8, None, adjacent=True, split=False) is None
    assert etf_inflow.step_inflow_yi(2e8, None, 2.0, adjacent=True, split=False) is None
    assert etf_inflow.step_inflow_yi(2e8, 1e8, 0.0, adjacent=True, split=False) is None
    assert etf_inflow.step_inflow_yi(2e8, 1e8, float("nan"), adjacent=True, split=False) is None


def test_rolling_sum_requires_a_complete_window() -> None:
    steps = [1.0, 2.0, None, 4.0, 5.0]
    assert etf_inflow.rolling_inflow(steps, 1) == pytest.approx(5.0)
    assert etf_inflow.rolling_inflow(steps, 2) == pytest.approx(9.0)
    assert etf_inflow.rolling_inflow(steps, 3) is None
    assert etf_inflow.rolling_inflow(steps, 5) is None
    assert etf_inflow.rolling_inflow([1.0], 5) is None


def test_divergence_uses_relative_gap_above_20_percent() -> None:
    assert etf_inflow.flows_diverged(10.0, 12.0) is False  # 2/12 ≈ 16.7%
    assert etf_inflow.flows_diverged(10.0, 12.5) is False  # 正好 20% 不标记
    assert etf_inflow.flows_diverged(10.0, 13.0) is True  # 3/13 ≈ 23%
    assert etf_inflow.flows_diverged(-10.0, -13.0) is True
    assert etf_inflow.flows_diverged(0.0, 0.0) is False
    assert etf_inflow.flows_diverged(0.0, 1.0) is True
    assert etf_inflow.flows_diverged(None, 1.0) is False
    assert etf_inflow.flows_diverged(1.0, None) is False


def test_board_computes_daily_inflow_from_previous_stored_day(tmp_path) -> None:
    prev, current = _iso(DAY0), _iso(DAY1)
    _write_shares(tmp_path, prev, [("510300", 10e8)])
    _write_shares(tmp_path, current, [("510300", 12e8)])
    _write_close(tmp_path, prev, {"510300.SH": 9.0})
    _write_close(tmp_path, current, {"510300.SH": 2.5})
    row = _board_row(tmp_path, "510300")
    # 用当天收盘价，不用前一日。Δ份额 2e8 × 2.5 / 1e8 = 5 亿元。
    assert row["share_change"] == pytest.approx(2e8)
    assert row["net_inflow_1d"] == pytest.approx(5.0)
    assert row["net_inflow_5d"] is None
    assert row["net_inflow_20d"] is None
    assert row["flow_net"] is None
    assert row["flow_diverged"] is False


def test_five_and_twenty_day_sums_use_each_days_close(tmp_path) -> None:
    days = [(date(2026, 9, 1) + timedelta(days=offset)).isoformat() for offset in range(21)]
    for index, day in enumerate(days):
        _write_shares(tmp_path, day, [("510300", 10e8 + index * 1e8)])
        _mark_trading(tmp_path, day)
        if index:
            # 第 i 步收盘价 = i，份额每天 +1 亿份 → 当日净流入 = i 亿元。
            _write_close(tmp_path, day, {"510300": float(index)})
    row = _board_row(tmp_path, "510300")
    assert row["net_inflow_1d"] == pytest.approx(20.0)
    # 5 日是 16+17+18+19+20，不是「5 日份额差 × 末日收盘价」= 100。
    assert row["net_inflow_5d"] == pytest.approx(16 + 17 + 18 + 19 + 20)
    assert row["net_inflow_20d"] == pytest.approx(sum(range(1, 21)))
    assert row["change_5d"] == pytest.approx(5e8)
    assert row["change_20d"] == pytest.approx(20e8)
    assert "change_15d" not in row


def test_split_on_trade_date_nulls_inflow_but_keeps_share_change(tmp_path) -> None:
    prev, current = _iso(DAY0), _iso(DAY1)
    _write_shares(tmp_path, prev, [("510300", 10e8)])
    _write_shares(tmp_path, current, [("510300", 20e8)])
    _write_close(tmp_path, current, {"510300": 1.0})
    _mark_trading(tmp_path, prev)
    _write_factor(tmp_path, "510300", current, 2.0)
    row = _board_row(tmp_path, "510300")
    assert row["share_change"] == pytest.approx(10e8)
    assert row["net_inflow_1d"] is None


def test_small_dividend_factor_does_not_null_inflow(tmp_path) -> None:
    prev, current = _iso(DAY0), _iso(DAY1)
    _write_shares(tmp_path, prev, [("510300", 10e8)])
    _write_shares(tmp_path, current, [("510300", 11e8)])
    _write_close(tmp_path, current, {"510300": 1.0})
    _mark_trading(tmp_path, prev)
    _write_factor(tmp_path, "510300", current, 1.01)
    row = _board_row(tmp_path, "510300")
    assert row["net_inflow_1d"] == pytest.approx(1.0)


def test_factor_on_previous_day_does_not_null_the_next_step(tmp_path) -> None:
    prev, current = _iso(DAY0), _iso(DAY1)
    _write_shares(tmp_path, prev, [("510300", 10e8)])
    _write_shares(tmp_path, current, [("510300", 11e8)])
    _write_close(tmp_path, current, {"510300": 1.0})
    _mark_trading(tmp_path, prev)
    _write_factor(tmp_path, "510300", prev, 2.0)
    assert _board_row(tmp_path, "510300")["net_inflow_1d"] == pytest.approx(1.0)


def test_gap_over_one_trading_day_nulls_inflow(tmp_path) -> None:
    # 周五直接跳到周二，中间还有周一。份额变化仍按落盘份数，净流入留空。
    friday, tuesday = _iso(DAY0), _iso(DAY2)
    monday = _iso(DAY1)
    _write_shares(tmp_path, friday, [("510300", 10e8)])
    _write_shares(tmp_path, tuesday, [("510300", 13e8)])
    _write_close(tmp_path, tuesday, {"510300": 1.0})
    _mark_trading(tmp_path, friday)
    _mark_trading(tmp_path, monday)
    _mark_trading(tmp_path, tuesday)
    payload = query.board(tmp_path)["etf_shares"]
    row = next(item for item in payload["items"] if item["code"] == "510300")
    assert payload["baselines"]["1"] == friday
    assert row["change_1d"] == pytest.approx(3e8)
    assert row["net_inflow_1d"] is None


def test_weekend_between_stored_days_is_still_one_trading_step(tmp_path) -> None:
    friday, monday = _iso(DAY0), _iso(DAY1)
    _write_shares(tmp_path, friday, [("510300", 10e8), ("159915", 5e8)])
    _write_shares(tmp_path, monday, [("510300", 11e8), ("159915", 5.5e8)])
    _mark_trading(tmp_path, friday)
    _write_close(tmp_path, monday, {"159915.SZ": 4.0, "510300.SH": 3.0})
    assert _board_row(tmp_path, "510300")["net_inflow_1d"] == pytest.approx(3.0)
    assert _board_row(tmp_path, "159915")["net_inflow_1d"] == pytest.approx(2.0)


def test_missing_calendar_or_close_leaves_inflow_null(tmp_path) -> None:
    prev, current = _iso(DAY0), _iso(DAY1)
    _write_shares(tmp_path, prev, [("510300", 10e8)])
    _write_shares(tmp_path, current, [("510300", 11e8)])
    assert _board_row(tmp_path, "510300")["net_inflow_1d"] is None

    _mark_trading(tmp_path, prev)
    _mark_trading(tmp_path, current)
    _write_close(tmp_path, current, {"588000": 1.0})
    assert _board_row(tmp_path, "510300")["net_inflow_1d"] is None


def test_stock_calendar_flags_gap_when_it_is_denser(tmp_path) -> None:
    friday, tuesday = _iso(DAY0), _iso(DAY2)
    monday = _iso(DAY1)
    _write_shares(tmp_path, friday, [("510300", 10e8)])
    _write_shares(tmp_path, tuesday, [("510300", 11e8)])
    # ETF 日 K 只有这两天，单看它会误当成相邻。股票日 K 多了周一。
    _write_close(tmp_path, friday, {"510300": 1.0})
    _write_close(tmp_path, tuesday, {"510300": 1.0})
    _mark_trading(tmp_path, friday, kind="kline_daily")
    _mark_trading(tmp_path, monday, kind="kline_daily")
    _mark_trading(tmp_path, tuesday, kind="kline_daily")
    assert _board_row(tmp_path, "510300")["net_inflow_1d"] is None


def test_code_missing_on_previous_partition_does_not_reuse_older_shares(tmp_path) -> None:
    days = [_iso(DAY0), _iso(DAY1), _iso(DAY2)]
    _write_shares(tmp_path, days[0], [("510300", 10e8)])
    _write_shares(tmp_path, days[1], [("159915", 1e8)])
    _write_shares(tmp_path, days[2], [("510300", 12e8), ("159915", 1e8)])
    for day in days:
        _mark_trading(tmp_path, day)
    _write_close(tmp_path, days[2], {"510300": 1.0, "159915": 1.0})
    assert _board_row(tmp_path, "510300")["net_inflow_1d"] is None


def test_split_inside_window_nulls_the_rolling_sum_only(tmp_path) -> None:
    days = [(date(2026, 9, 1) + timedelta(days=offset)).isoformat() for offset in range(8)]
    for index, day in enumerate(days):
        _write_shares(tmp_path, day, [("510300", 10e8 + index * 1e8)])
        _write_close(tmp_path, day, {"510300": 1.0})
    _write_factor(tmp_path, "510300", days[3], 2.0)
    row = _board_row(tmp_path, "510300")
    assert row["net_inflow_1d"] == pytest.approx(1.0)
    assert row["net_inflow_5d"] is None
    assert row["net_inflow_20d"] is None


def test_navigator_map_uses_title_date_and_ignores_parquet(tmp_path) -> None:
    trade_date = "2026-10-14"
    _write_shares(tmp_path, trade_date, [("510300", 10e8), ("588000", 2e8)])
    _insert_article(
        tmp_path,
        trade_date,
        [{"name": "沪深300ETF", "code": "510300.SH", "net_1d": "37.8", "net_5d": 10, "net_20d": None}],
        source_id="netease:old",
        extracted_date="2026-10-01",
        broad=[{"name": "科创50", "code": 588000, "net_1d": 4.5, "net_5d": 1.0, "net_20d": 8}],
    )
    _insert_article(
        tmp_path,
        trade_date,
        [{"name": "沪深300ETF", "code": "510300", "net_1d": 12.0, "net_5d": 9.0, "net_20d": 3}],
        source_id="netease:new",
        extracted_date="2026-01-01",
    )
    decoy = tmp_path / "etf_flow" / f"date={trade_date}"
    decoy.mkdir(parents=True)
    pl.DataFrame({"code": ["510300"], "net_flow": [99.0]}).write_parquet(decoy / "part.parquet")
    _insert_article(
        tmp_path,
        trade_date,
        [{"name": "别的来源", "code": "510300", "net_1d": 1.0}],
        source_id="other:1",
        source="cls",
    )

    mapped = query._etf_flow_map(tmp_path, trade_date)
    assert mapped["510300"]["net_1d"] == 12.0
    assert mapped["510300"]["net_5d"] == 9.0
    assert mapped["510300"]["net_20d"] == 3.0
    assert mapped["588000"]["net_1d"] == 4.5
    assert "510300" not in query._etf_flow_map(tmp_path, "2026-10-01")

    payload = query.etf_shares(trade_date=trade_date, data_dir=tmp_path)
    by_code = {row["code"]: row for row in payload["items"]}
    assert payload["etf_flow_linked"] is True
    assert by_code["510300"]["flow_net"] == 12.0
    assert by_code["510300"]["flow_net_5d"] == 9.0
    assert by_code["510300"]["flow_net_20d"] == 3.0
    assert by_code["588000"]["flow_net"] == 4.5
    assert by_code["588000"]["flow_net_20d"] == 8


def test_board_flags_row_when_computed_and_article_diverge(tmp_path) -> None:
    prev, current = _iso(DAY0), _iso(DAY1)
    _write_shares(tmp_path, prev, [("510300", 10e8), ("588000", 10e8)])
    _write_shares(tmp_path, current, [("510300", 15e8), ("588000", 15e8)])
    _write_close(tmp_path, current, {"510300": 2.0, "588000": 2.0})
    _mark_trading(tmp_path, prev)
    # Δ份额 5e8 × 2 / 1e8 = 10 亿元。13 相对差约 23%，12 约 16.7%。
    _insert_article(
        tmp_path,
        current,
        [
            {"name": "沪深300ETF", "code": "510300", "net_1d": 13.0, "net_5d": 1.0, "net_20d": 2.0},
            {"name": "科创50ETF", "code": "588000", "net_1d": 12.0, "net_5d": 1.0, "net_20d": 2.0},
        ],
        source_id="netease:cmp",
    )
    diverged = _board_row(tmp_path, "510300")
    aligned = _board_row(tmp_path, "588000")
    assert diverged["net_inflow_1d"] == pytest.approx(10.0)
    assert diverged["flow_net"] == 13.0
    assert diverged["flow_diverged"] is True
    assert aligned["flow_net"] == 12.0
    assert aligned["flow_diverged"] is False


def test_malformed_news_row_is_skipped(tmp_path) -> None:
    path = tmp_path / "news" / "news.sqlite"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE news_items (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            published_at TEXT NOT NULL,
            author TEXT,
            title TEXT,
            clean_text TEXT NOT NULL,
            raw_json TEXT,
            content_hash TEXT NOT NULL,
            url TEXT,
            level TEXT,
            media_ids TEXT,
            extra_json TEXT,
            ingested_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO news_items (
            source, source_id, published_at, title, clean_text, raw_json,
            content_hash, url, level, media_ids, extra_json, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "etf_flow", "bad", "2026-10-15T08:00:00+08:00",
            "10月14日ETF基金申购和赎回", "x", "{not-json",
            "h", "", "flow", "[]", "{}", "2026-10-15T08:00:00+08:00",
        ),
    )
    conn.commit()
    conn.close()
    assert query._etf_flow_map(tmp_path, "2026-10-14") == {}
