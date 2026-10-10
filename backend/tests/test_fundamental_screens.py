"""基本面 M1/M2/M3：公告日次日生效，缺数不通过。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.strategy.engine import StrategyEngine
from app.strategy.fundamental_screens import filter_history_m1, run_screen

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2024, 8, 16)
ANNOUNCE = date(2024, 8, 15)


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def _m1_pack(symbol: str = "000001.SZ", *, roe: float = 40.0, deducted: float = 286.0, cost: float = 1260.0, goodwill=10.0, debt: float = 40.0, ocf: float = 5.0, price: float = 48.0, announce: str = "2024-08-15"):
    income = [
        ("2023-03-31", "2023-04-28", 100, 800, 480, 20),
        ("2023-06-30", "2023-08-20", 220, 1700, 1020, 40),
        ("2023-09-30", "2023-10-28", 300, 2500, 1500, 70),
        ("2023-12-31", "2024-04-20", 400, 3400, 2040, 110),
        ("2024-03-31", "2024-04-25", 130, 1000, 600, 20),
        ("2024-06-30", announce, deducted, 2100, cost, 50),
    ]
    metrics = [
        ("2024-03-31", "2024-04-25", 5.0, 10.0, 80.0),
        ("2024-06-30", announce, roe, 12.0, debt),
    ]
    return {
        "income": _frame([
            {
                "symbol": symbol, "period_end": period, "announce_date": ann,
                "net_income_deducted": ded, "revenue": rev, "operating_cost": cst,
                "net_income_attributable": np,
            }
            for period, ann, ded, rev, cst, np in income
        ]),
        "metrics": _frame([
            {
                "symbol": symbol, "period_end": period, "announce_date": ann,
                "roe": row_roe, "revenue_yoy": yoy, "debt_to_asset_ratio": row_debt,
            }
            for period, ann, row_roe, yoy, row_debt in metrics
        ]),
        "balance_sheet": _frame([{
            "symbol": symbol, "period_end": "2024-06-30", "announce_date": announce,
            "goodwill": goodwill, "total_assets": 100.0, "total_liabilities": 40.0,
        }]),
        "cash_flow": _frame([{
            "symbol": symbol, "period_end": "2024-06-30", "announce_date": announce,
            "net_operating_cash_flow": ocf,
        }]),
        "shares": _frame([{
            "symbol": symbol, "period_end": "2024-06-30", "announce_date": announce,
            "total_shares": 100.0,
        }]),
    }, price


def _panel(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [item[0] for item in rows],
        "date": [item[1] for item in rows],
        "close": [item[2] * 10 for item in rows],
        "raw_close": [item[2] for item in rows],
    })


def _industry(rows: list[tuple[str, str]]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [item[0] for item in rows],
        "所属同花顺行业": [item[1] for item in rows],
    })


def _merge(packs: list[dict]) -> dict[str, pl.DataFrame]:
    keys = packs[0].keys()
    return {key: pl.concat([pack[key] for pack in packs], how="diagonal_relaxed") for key in keys}


def _hits(panel, tables, industry, model="m1", params=None) -> set[tuple[str, date]]:
    out = run_screen(panel, params, model=model, tables=tables, industry=industry)
    if out.is_empty():
        return set()
    return set(zip(out["symbol"].to_list(), out["date"].to_list(), strict=True))


def test_m1_waits_until_the_day_after_announcement_and_ranks_inside_industry():
    winner, _ = _m1_pack("000001.SZ", roe=40)
    peer_high, _ = _m1_pack("000002.SZ", roe=7)
    peer_mid, _ = _m1_pack("000004.SZ", roe=6)
    peer_low, _ = _m1_pack("000003.SZ", roe=5)
    alone, _ = _m1_pack("000009.SZ", roe=1)
    tables = _merge([winner, peer_high, peer_mid, peer_low, alone])
    industry = _industry([
        ("000001.SZ", "消费-食品-饮料"),
        ("000002.SZ", "消费-食品-饮料"),
        ("000004.SZ", "消费-食品-饮料"),
        ("000003.SZ", "消费-食品-饮料"),
        ("000009.SZ", "银行-国有行-国有行"),
    ])
    panel = _panel([
        (symbol, day, 48.0)
        for symbol in ("000001.SZ", "000002.SZ", "000004.SZ", "000003.SZ", "000009.SZ")
        for day in (ANNOUNCE, AS_OF)
    ])
    hits = _hits(panel, tables, industry)
    assert ("000001.SZ", ANNOUNCE) not in hits
    assert ("000001.SZ", AS_OF) in hits
    assert ("000002.SZ", AS_OF) in hits
    assert ("000004.SZ", AS_OF) not in hits
    assert ("000003.SZ", AS_OF) not in hits
    assert ("000009.SZ", AS_OF) in hits


def test_m1_rejects_boundary_and_missing_goodwill():
    cases = {
        "growth": _m1_pack(deducted=1200)[0],
        "margin": _m1_pack(cost=1273)[0],
        "debt": _m1_pack(debt=70)[0],
        "goodwill": _m1_pack(goodwill=25)[0],
        "missing_goodwill": _m1_pack(goodwill=None)[0],
        "ocf": _m1_pack(ocf=0)[0],
        "pe": _m1_pack(price=96)[0],
    }
    industry = _industry([("000001.SZ", "消费-食品-饮料")])
    panel = _panel([("000001.SZ", AS_OF, 48.0)])
    assert _hits(panel, _m1_pack()[0], industry)
    for name, tables in cases.items():
        price = 96.0 if name == "pe" else 48.0
        got = _hits(_panel([("000001.SZ", AS_OF, price)]), tables, industry)
        assert got == set(), name


def test_restatement_replaces_only_after_its_own_announce_date():
    tables, _ = _m1_pack()
    original = tables["income"]
    revised = original.filter(pl.col("period_end") == "2024-06-30").with_columns(
        pl.lit("2024-09-01").alias("announce_date"),
        pl.lit(1200.0).alias("net_income_deducted"),
    )
    tables = {**tables, "income": pl.concat([original, revised], how="diagonal_relaxed")}
    industry = _industry([("000001.SZ", "消费-食品-饮料")])
    panel = _panel([
        ("000001.SZ", date(2024, 8, 20), 48.0),
        ("000001.SZ", date(2024, 9, 1), 48.0),
        ("000001.SZ", date(2024, 9, 2), 48.0),
    ])
    hits = _hits(panel, tables, industry)
    assert ("000001.SZ", date(2024, 8, 20)) in hits
    assert ("000001.SZ", date(2024, 9, 1)) in hits
    assert ("000001.SZ", date(2024, 9, 2)) not in hits


def test_m2_rules_wacc_and_bank_exclusion():
    tables = _m2_tables()
    industry = _industry([("600000.SH", "消费-食品-饮料")])
    panel = _panel([
        ("600000.SH", date(2024, 4, 30), 20.0),
        ("600000.SH", date(2024, 5, 1), 20.0),
    ])
    assert _hits(panel, tables, industry, "m2") == {("600000.SH", date(2024, 5, 1))}
    assert _hits(panel, tables, industry, "m2", {"wacc": 0.20}) == set()
    assert _hits(panel, tables, industry, "m2", {"wacc": 8}) == {("600000.SH", date(2024, 5, 1))}
    banks = _industry([("600000.SH", "银行-股份制-股份制")])
    assert _hits(panel, tables, banks, "m2") == set()
    broker = _industry([("600000.SH", "非银金融-证券-证券")])
    assert _hits(panel, tables, broker, "m2") == set()
    broken = {
        **tables,
        "balance_sheet": tables["balance_sheet"].with_columns(pl.lit(None).alias("short_term_borrowing")),
    }
    assert _hits(panel, broken, industry, "m2") == set()


def test_m3_median_cashflow_streak_and_pe():
    subject = _m3_tables("000001.SZ", roic=0.16, ocf_path=(10, 12, 11, 14))
    peer = _m3_tables("000002.SZ", roic=0.10, ocf_path=(10, 12, 11, 14))
    tables = _merge([subject, peer])
    industry = _industry([
        ("000001.SZ", "消费-食品-饮料"),
        ("000002.SZ", "消费-食品-饮料"),
    ])
    panel = _panel([
        ("000001.SZ", AS_OF, 48.0),
        ("000002.SZ", AS_OF, 48.0),
    ])
    hits = _hits(panel, tables, industry, "m3")
    assert hits == {("000001.SZ", AS_OF)}

    weak = _merge([
        _m3_tables("000001.SZ", roic=0.16, ocf_path=(10, 9, 11, 10)),
        peer,
    ])
    assert _hits(panel, weak, industry, "m3") == set()
    pricey = _panel([("000001.SZ", AS_OF, 96.0), ("000002.SZ", AS_OF, 48.0)])
    assert ("000001.SZ", AS_OF) not in _hits(pricey, tables, industry, "m3")


def test_screen_reads_financial_parquet(tmp_path: Path):
    tables, _ = _m1_pack()
    for name, frame in tables.items():
        folder = tmp_path / "financials" / name
        folder.mkdir(parents=True)
        frame.write_parquet(folder / "part.parquet")
    industry = _industry([("000001.SZ", "消费-食品-饮料")])
    ind = tmp_path / "ext_data" / "ext_hy_ths"
    ind.mkdir(parents=True)
    industry.write_parquet(ind / "part.parquet")
    panel = _panel([("000001.SZ", AS_OF, 48.0), ("000001.SZ", ANNOUNCE, 48.0)])
    hits = filter_history_m1(panel, {"data_dir": str(tmp_path)})
    assert hits["date"].to_list() == [AS_OF]
    assert filter_history_m1(panel, {"data_dir": str(tmp_path / "missing")}).is_empty()


def test_presets_register_with_descriptions():
    engine = StrategyEngine(strategy_dirs=[ROOT / "app" / "strategy" / "builtin"])
    listed = {item["id"]: item for item in engine.list_strategies()}
    for strategy_id, phrase in (
        ("fundamental_m1", "主推"),
        ("fundamental_m2", "资本成本"),
        ("fundamental_m3", "中位数"),
    ):
        assert phrase in listed[strategy_id]["description"]
        assert listed[strategy_id]["asset_types"] == ["stock"]
        assert engine.get(strategy_id).basic_filter["enabled"] is False
    assert engine.get("fundamental_m2").meta["params"][0]["default"] == 0.08


def _m2_tables() -> dict[str, pl.DataFrame]:
    symbol = "600000.SH"
    income = []
    balance = []
    cash = []
    metrics = []
    for year, roe, yoy, ocf in (
        (2019, 11.0, 5.0, 10.0),
        (2020, 12.0, 6.0, 11.0),
        (2021, 15.0, 8.0, 12.0),
        (2022, 16.0, 10.0, 13.0),
        (2023, 14.0, 18.0, 14.0),
    ):
        announced = f"{year + 1}-04-30"
        period = f"{year}-12-31"
        income.append({
            "symbol": symbol, "period_end": period, "announce_date": announced,
            "operating_profit": 100.0, "income_tax": 20.0, "total_profit": 80.0,
            "net_income_attributable": 60.0,
        })
        balance.append({
            "symbol": symbol, "period_end": period, "announce_date": announced,
            "total_equity": 400.0, "short_term_borrowing": 50.0, "long_term_borrowing": 50.0,
        })
        cash.append({
            "symbol": symbol, "period_end": period, "announce_date": announced,
            "net_operating_cash_flow": ocf,
        })
        metrics.append({
            "symbol": symbol, "period_end": period, "announce_date": announced,
            "roe": roe, "net_income_yoy": yoy, "net_margin": 15.0, "bps": 10.0,
        })
    for period, announced, margin, yoy in (
        ("2023-06-30", "2023-08-30", 16.0, 30.0),
        ("2023-09-30", "2023-10-30", 15.0, 25.0),
    ):
        metrics.append({
            "symbol": symbol, "period_end": period, "announce_date": announced,
            "roe": 13.0, "net_income_yoy": yoy, "net_margin": margin, "bps": 10.0,
        })
    return {
        "income": _frame(income),
        "balance_sheet": _frame(balance),
        "cash_flow": _frame(cash),
        "metrics": _frame(metrics),
        "shares": _frame([]),
    }


def _m3_tables(symbol: str, *, roic: float, ocf_path: tuple[float, float, float, float]) -> dict[str, pl.DataFrame]:
    # roic 参数是 2023 年目标。2022、2021 依次高 0.02、0.04，保证连续三年高于 0.10 的对手。
    income = []
    balance = []
    cash = []
    metrics = []
    targets = {2021: roic + 0.04, 2022: roic + 0.02, 2023: roic}
    for year, flow in zip((2020, 2021, 2022, 2023), ocf_path, strict=True):
        announced = f"{year + 1}-04-30"
        period = f"{year}-12-31"
        target = targets.get(year, 0.10)
        operating = target * 500 / 0.75
        income.append({
            "symbol": symbol, "period_end": period, "announce_date": announced,
            "operating_profit": operating, "income_tax": 20.0, "total_profit": 80.0,
            "revenue": 1000.0, "net_income_attributable": 40.0,
        })
        balance.append({
            "symbol": symbol, "period_end": period, "announce_date": announced,
            "total_equity": 400.0, "short_term_borrowing": 50.0, "long_term_borrowing": 50.0,
            "total_assets": 800.0, "total_liabilities": 300.0,
        })
        cash.append({
            "symbol": symbol, "period_end": period, "announce_date": announced,
            "net_operating_cash_flow": flow,
        })
    # 最新季报决定负债率、现金流/营收、营收增速和 PE。
    income.extend([
        {"symbol": symbol, "period_end": "2023-06-30", "announce_date": "2023-08-20", "revenue": 400.0, "net_income_attributable": 40.0, "operating_cost": 200.0},
        {"symbol": symbol, "period_end": "2023-09-30", "announce_date": "2023-10-28", "revenue": 700.0, "net_income_attributable": 70.0, "operating_cost": 350.0},
        {"symbol": symbol, "period_end": "2023-12-31", "announce_date": "2024-04-30", "revenue": 1000.0, "net_income_attributable": 110.0, "operating_cost": 500.0, "operating_profit": targets[2023] * 500 / 0.75, "income_tax": 20.0, "total_profit": 80.0},
        {"symbol": symbol, "period_end": "2024-03-31", "announce_date": "2024-04-25", "revenue": 300.0, "net_income_attributable": 20.0, "operating_cost": 150.0},
        {"symbol": symbol, "period_end": "2024-06-30", "announce_date": "2024-08-15", "revenue": 1000.0, "net_income_attributable": 50.0, "operating_cost": 500.0},
    ])
    # 2023 年报在上面的年度循环里已经有一行，这里再写季报字段会变成同日另一版本。
    # 年度循环的 2023-12-31 保留 ROIC 所需字段；PE 用的归母净利润补到那一行。
    income = [row for row in income if not (row["period_end"] == "2023-12-31" and "operating_cost" in row and row["announce_date"] == "2024-04-30")]
    for row in income:
        if row["period_end"] == "2023-12-31":
            row["net_income_attributable"] = 110.0
            row["revenue"] = 1000.0
    cash.append({
        "symbol": symbol, "period_end": "2024-06-30", "announce_date": "2024-08-15",
        "net_operating_cash_flow": 80.0,
    })
    metrics.append({
        "symbol": symbol, "period_end": "2024-06-30", "announce_date": "2024-08-15",
        "debt_to_asset_ratio": 40.0, "revenue_yoy": 10.0,
    })
    return {
        "income": _frame(income),
        "balance_sheet": _frame(balance),
        "cash_flow": _frame(cash),
        "metrics": _frame(metrics),
        "shares": _frame([{
            "symbol": symbol, "period_end": "2024-06-30", "announce_date": "2024-08-15",
            "total_shares": 100.0,
        }]),
    }
