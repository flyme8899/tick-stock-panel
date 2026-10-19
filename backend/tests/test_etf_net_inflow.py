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
    # 缺口里即使带了折算因子也不补金额。
    assert etf_inflow.step_inflow_yi(3e8, 1e8, 2.0, adjacent=False, split=False, factor=2.0) is None
    assert etf_inflow.step_inflow_yi(3e8, 1e8, 2.0, adjacent=True, split=False, factor=0.0) is None


def test_record_day_factor_keeps_real_creation_for_split_and_reverse() -> None:
    # 2 倍拆分：登记日份额已翻倍，另有 1 亿份折算后的真实申购。价格仍是折算前的 2 元。
    # (3e8 − 1e8×2) × 2 / 2 / 1e8 = 1 亿元。不还原则是 (3e8−1e8)×2/1e8 = 4 亿元。
    assert etf_inflow.step_inflow_yi(3e8, 1e8, 2.0, adjacent=True, split=False, factor=2.0) == pytest.approx(1.0)
    # 4 合 1：没有真实申赎时金额是 0，不是巨额赎回。
    assert etf_inflow.step_inflow_yi(0.5e8, 2e8, 1.0, adjacent=True, split=False, factor=0.25) == pytest.approx(0.0)
    # 缩股后再申购 0.1 亿份（折算后）。折算前价格 1 元，相当于折算后 4 元。
    assert etf_inflow.step_inflow_yi(0.6e8, 2e8, 1.0, adjacent=True, split=False, factor=0.25) == pytest.approx(0.4)


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
    assert row["change_15d"] == pytest.approx(15e8)


def test_split_on_ex_date_keeps_real_flow_and_raw_share_change(tmp_path) -> None:
    # 周五登记、周一除权。份额在周五已经 ×2，价格要到周一才减半。因子记在周一。
    record, ex = _iso(DAY0), _iso(DAY1)
    _write_shares(tmp_path, "2026-10-08", [("510300", 10e8)])
    _write_shares(tmp_path, record, [("510300", 20e8)])
    _write_shares(tmp_path, ex, [("510300", 21e8)])
    _write_close(tmp_path, record, {"510300": 2.0})
    _write_close(tmp_path, ex, {"510300": 1.0})
    _mark_trading(tmp_path, "2026-10-08")
    _write_factor(tmp_path, "510300", ex, 2.0)
    # 登记日：份额差仍是原始跳变，金额按因子还原成 0，不再是 (20−10)×2 = 20 亿元。
    record_row = etf_inflow.latest_inflows(tmp_path, as_of=record)["510300"]
    assert record_row["net_inflow_1d"] == pytest.approx(0.0)
    # 除权日不再置空。真实申购 1 亿份 × 1 元 = 1 亿元。份额差保持原始份数。
    row = _board_row(tmp_path, "510300")
    assert row["share_change"] == pytest.approx(1e8)
    assert row["net_inflow_1d"] == pytest.approx(1.0)


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


def test_split_inside_window_adjusts_record_day_and_keeps_sums(tmp_path) -> None:
    # 只标工作日。除权日是 9 月 7 日（周一），登记日是前一个交易日 9 月 4 日（周五），
    # 中间隔着周末。登记日份额已经 ×2、收盘价仍是 2 元；除权日价格 1 元。
    days: list[date] = []
    cursor = date(2026, 9, 1)
    while len(days) < 8:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    shares = [10e8, 11e8, 12e8, 24e8, 25e8, 26e8, 27e8, 28e8]
    for index, day in enumerate(days):
        iso = day.isoformat()
        _write_shares(tmp_path, iso, [("510300", shares[index])])
        _mark_trading(tmp_path, iso)
        if index:
            _write_close(tmp_path, iso, {"510300": 2.0 if index == 3 else 1.0})
    assert days[3].isoformat() == "2026-09-04"
    assert days[4].isoformat() == "2026-09-07"
    _write_factor(tmp_path, "510300", days[4].isoformat(), 2.0)
    record = etf_inflow.latest_inflows(tmp_path, as_of=days[3].isoformat())["510300"]
    ex = etf_inflow.latest_inflows(tmp_path, as_of=days[4].isoformat())["510300"]
    # 不还原时登记日是 (24−12)×2 = 24 亿元。还原后没有额外申赎，是 0。
    assert record["net_inflow_1d"] == pytest.approx(0.0)
    assert ex["net_inflow_1d"] == pytest.approx(1.0)
    row = _board_row(tmp_path, "510300")
    assert row["net_inflow_1d"] == pytest.approx(1.0)
    # 最近 5 步：登记日 0 + 除权日 1 + 之后三天各 1。窗口不再因为除权日留空而整段作废。
    assert row["net_inflow_5d"] == pytest.approx(4.0)
    assert row["net_inflow_20d"] is None


# 交易所份额（深市为份，沪市公告为万份，这里已换成份）和腾讯原始收盘。
# 除权日来自新浪 hfq.js 的价格跳空日；登记日是它的前一个交易日。
# naive 是旧算法在登记日打出来的假申购，用来确认样本没配错。
_CONVERSIONS = [
    {
        "code": "159246",
        "factor": 2.0,
        "prev": ("2026-02-11", 1_614_827_803),
        "record": ("2026-02-12", 3_219_655_606, 2.159),
        "ex": ("2026-02-13", 3_227_655_606, 1.059),
        "record_yi": -0.10795,
        "ex_yi": 0.08472,
        "naive_yi": 34.64823226877,
    },
    {
        "code": "159327",
        # 周五登记、周一除权。自然日减一天会落到周日，还原不到周五的份额跳变。
        "factor": 3.0,
        "prev": ("2026-07-16", 688_524_697),
        "record": ("2026-07-17", 1_984_574_091, 3.348),
        "ex": ("2026-07-20", 1_954_574_091, 1.052),
        "record_yi": -0.90396,
        "ex_yi": -0.3156,
        "naive_yi": 43.39173371112,
    },
    {
        "code": "159220",
        "factor": 2.0,
        "prev": ("2025-11-06", 262_784_763),
        "record": ("2025-11-07", 531_569_526, 1.240),
        "ex": ("2025-11-10", 537_569_526, 0.627),
        "record_yi": 0.0372,
        "ex_yi": 0.03762,
        "naive_yi": 3.3329310612,
    },
    {
        "code": "159537",
        # 登记日残差是 2800 万份折算后的真实申购，不能被还原成 0。
        "factor": 2.0,
        "prev": ("2026-06-24", 53_642_602),
        "record": ("2026-06-25", 135_285_204, 2.404),
        "ex": ("2026-06-26", 140_285_204, 1.180),
        "record_yi": 0.33656,
        "ex_yi": 0.059,
        "naive_yi": 1.96268815208,
    },
    {
        "code": "588200",
        "factor": 3.0,
        "prev": ("2026-07-17", 14_841_894_800),
        "record": ("2026-07-20", 45_929_684_300, 3.539),
        "ex": ("2026-07-21", 44_075_684_300, 1.349),
        "record_yi": 16.562518820333,
        "ex_yi": -25.01046,
        "naive_yi": 1100.196870405,
    },
    {
        # 512200 在 2024-08-09 缩股，份额因子 0.35806260。登记日价格仍是缩股前的。
        "code": "512200",
        "factor": 0.35806260,
        "prev": ("2024-08-08", 9_800_876_000),
        "record": ("2024-08-09", 3_505_597_900, 0.441),
        "ex": ("2024-08-12", 3_498_347_900, 1.191),
        "record_yi": -0.0459304069004,
        "ex_yi": -0.0863475,
        "naive_yi": -27.762176421,
    },
]


def _seed_conversion(root: Path, case: dict) -> None:
    code = case["code"]
    prev_day, prev_shares = case["prev"]
    record_day, record_shares, record_close = case["record"]
    ex_day, ex_shares, ex_close = case["ex"]
    _write_shares(root, prev_day, [(code, prev_shares)])
    _write_shares(root, record_day, [(code, record_shares)])
    _write_shares(root, ex_day, [(code, ex_shares)])
    _mark_trading(root, prev_day)
    _write_close(root, record_day, {code: record_close})
    _write_close(root, ex_day, {code: ex_close})
    _write_factor(root, code, ex_day, case["factor"])


@pytest.mark.parametrize("case", _CONVERSIONS, ids=[item["code"] for item in _CONVERSIONS])
def test_conversion_record_day_keeps_real_flow_and_ex_day_is_kept(tmp_path, case) -> None:
    _seed_conversion(tmp_path, case)
    code = case["code"]
    record_day, record_shares, record_close = case["record"]
    _prev_day, prev_shares = case["prev"]
    naive = (record_shares - prev_shares) * record_close / 1e8
    assert naive == pytest.approx(case["naive_yi"])
    record = etf_inflow.latest_inflows(tmp_path, as_of=record_day)[code]
    ex = etf_inflow.latest_inflows(tmp_path, as_of=case["ex"][0])[code]
    assert record["net_inflow_1d"] == pytest.approx(case["record_yi"])
    assert ex["net_inflow_1d"] == pytest.approx(case["ex_yi"])
    # 除权日份额差仍是原始份数，不因金额还原被改写。
    assert _board_row(tmp_path, code)["share_change"] == pytest.approx(case["ex"][1] - record_shares)


def test_159537_five_and_twenty_day_sums_include_record_and_ex_days(tmp_path) -> None:
    code = "159537"
    days = [
        "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05",
        "2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12",
        "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19",
        "2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26",
        "2026-06-29", "2026-06-30",
    ]
    observed = {
        "2026-06-23": (55_642_602, 1.0),
        "2026-06-24": (53_642_602, 2.258),
        "2026-06-25": (135_285_204, 2.404),
        "2026-06-26": (140_285_204, 1.180),
        "2026-06-29": (150_285_204, 1.221),
        "2026-06-30": (150_285_204, 1.258),
    }
    flat = 55_642_602
    for day in days:
        shares, close = observed.get(day, (flat, 1.0))
        _write_shares(tmp_path, day, [(code, shares)])
        _write_close(tmp_path, day, {code: close})
    _write_factor(tmp_path, code, "2026-06-26", 2.0)
    # 06-27、06-28 是周末，不能标成交易日，否则周一会被当成隔了缺口。
    assert not (tmp_path / "kline_etf_daily" / "date=2026-06-27").exists()
    latest = etf_inflow.latest_inflows(tmp_path)[code]
    # 5 日 = 24 日 −0.04516 + 登记日 0.33656 + 除权日 0.059 + 29 日 0.1221 + 30 日 0。
    assert latest["net_inflow_1d"] == pytest.approx(0.0)
    assert latest["net_inflow_5d"] == pytest.approx(0.4725)
    assert latest["net_inflow_20d"] == pytest.approx(0.4725)
    record = etf_inflow.latest_inflows(tmp_path, as_of="2026-06-25")[code]
    ex = etf_inflow.latest_inflows(tmp_path, as_of="2026-06-26")[code]
    assert record["net_inflow_1d"] == pytest.approx(0.33656)
    assert record["net_inflow_5d"] == pytest.approx(0.2914)
    assert ex["net_inflow_1d"] == pytest.approx(0.059)
    assert ex["net_inflow_5d"] == pytest.approx(0.3504)
    assert _board_row(tmp_path, code)["net_inflow_20d"] == pytest.approx(0.4725)


def test_split_plus_cash_restores_with_share_ratio_not_price_factor(tmp_path) -> None:
    # 同日折算加现金分红时，还原用份额因子，不用价格因子。
    # 数字来自 159922：份额因子 2.5、现金 0.1292、前收 6.159。
    # 价格因子 = 前收 / (前收 / 2.5 − 现金)。日期只是一组周五登记、周一除权。
    code = "159922"
    ratio = 2.5
    cash = 0.1292
    prev_px = 6.159
    ex_factor = prev_px / (prev_px / ratio - cash)
    prev_day, record_day, ex_day = "2026-07-16", "2026-07-17", "2026-07-20"
    created = 10_000_000  # 折算后的真实申购
    _write_shares(tmp_path, prev_day, [(code, 100_000_000)])
    _write_shares(tmp_path, record_day, [(code, 100_000_000 * ratio + created)])
    _write_shares(tmp_path, ex_day, [(code, 100_000_000 * ratio + created)])
    _mark_trading(tmp_path, prev_day)
    _write_close(tmp_path, record_day, {code: prev_px})
    _write_close(tmp_path, ex_day, {code: prev_px / ratio - cash})
    path = tmp_path / "adj_factor_etf" / "all.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": [f"{code}.SZ"],
        "trade_date": [date.fromisoformat(ex_day)],
        "ex_factor": [ex_factor],
        "dividend": [cash],
        "prev_close": [prev_px],
    }).write_parquet(path)
    inflow = etf_inflow.latest_inflows(tmp_path, as_of=record_day)[code]["net_inflow_1d"]
    assert inflow == pytest.approx(created * prev_px / ratio / 1e8)
    assert inflow == pytest.approx(0.24636)


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
