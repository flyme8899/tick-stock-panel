"""三套基本面选股预设。公告日次日才生效，避免用到尚未披露的报表。

指标表里的 ROE、增速、利润率、资产负债率按百分数数值使用（12 表示 12%）。
由金额现算的比率用小数。商誉、借款、增速分母缺失时不通过，不填 0。

行业用当前同花顺快照（ext_hy_ths），不是历史成分。M1 的 ROE 排名和 M3 的
ROIC 中位数都在二级行业内计算。
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 2
BASIC_FILTER = {"enabled": False}
_RESULT_LIMIT = 8000
_BANKS = ("银行", "非银金融")

_INCOME_COLS = (
    "revenue", "operating_cost", "operating_profit", "income_tax", "total_profit",
    "net_income", "net_income_attributable", "net_income_deducted",
)
_BALANCE_COLS = (
    "total_assets", "total_liabilities", "total_equity", "goodwill",
    "short_term_borrowing", "long_term_borrowing",
)
_CASH_COLS = ("net_operating_cash_flow",)
_METRICS_COLS = (
    "roe", "revenue_yoy", "net_margin", "net_income_yoy", "debt_to_asset_ratio", "bps",
)


def meta_m1() -> dict:
    return _meta(
        "fundamental_m1",
        "基本面 M1 主推",
        "主推。扣非净利同比在 0 到 400% 之间，且增速环比变化率大于 -10%；"
        "同花顺二级行业 ROE 前 30%；营收同比为正且增速环比变化率大于 -10%；"
        "单季毛利率环比不低于 -1 个百分点；资产负债率低于 70%；商誉/总资产低于 25%；"
        "经营现金流为正；PE(TTM) 大于 0 且小于 80。公告次日生效。",
        [],
    )


def meta_m2() -> dict:
    return _meta(
        "fundamental_m2",
        "基本面 M2",
        "三年平均 ROE/PB 大于 2.5%，最近三份季报净利率都大于 10% 且平均净利增速大于 20%，"
        "三年平均 ROE 大于 10%，最新年报 ROIC 大于资本成本（默认 8%），"
        "连续五个会计年度经营现金流为正，最新年报净利增速高于上年。"
        "排除同花顺一级行业银行和非银金融。公告次日生效。",
        [{
            "id": "wacc",
            "label": "资本成本 WACC（0.08 表示 8%）",
            "type": "float",
            "default": 0.08,
            "min": 0.0,
            "max": 0.30,
            "step": 0.005,
        }],
    )


def meta_m3() -> dict:
    return _meta(
        "fundamental_m3",
        "基本面 M3",
        "资产负债率低于 55%，经营现金流/营收大于 5%，营收同比为正，"
        "最近三个会计年度 ROIC 都高于同花顺二级行业中位数，"
        "最近三年经营现金流都为正且四年里至少两年同比增加，"
        "PE(TTM) 大于 0 且小于 80。公告次日生效。",
        [],
    )


def _meta(strategy_id: str, name: str, description: str, params: list) -> dict:
    return {
        "id": strategy_id,
        "name": name,
        "description": description,
        "tags": ["基本面"],
        "asset_types": ["stock"],
        "timeframes": ["1d"],
        "params": params,
        "scoring": {},
        "order_by": "symbol",
        "descending": False,
        "limit": _RESULT_LIMIT,
    }


def filter_history_m1(df: pl.DataFrame, params: dict | None) -> pl.DataFrame:
    return run_screen(df, params, model="m1")


def filter_history_m2(df: pl.DataFrame, params: dict | None) -> pl.DataFrame:
    return run_screen(df, params, model="m2")


def filter_history_m3(df: pl.DataFrame, params: dict | None) -> pl.DataFrame:
    return run_screen(df, params, model="m3")


def run_screen(
    panel: pl.DataFrame,
    params: dict | None,
    *,
    model: str,
    tables: dict[str, pl.DataFrame] | None = None,
    industry: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """返回面板中通过筛选的行。tables 缺省时从 data_dir 读财务 parquet。"""
    if panel.is_empty() or "symbol" not in panel.columns or "date" not in panel.columns:
        return panel.head(0)
    options = params or {}
    if tables is None:
        data_dir = _data_dir(options)
        tables = _load_tables(data_dir)
        industry = _load_industry(data_dir)
    base = _panel_base(panel)
    if base.is_empty():
        return panel.head(0)
    passed = base.filter(_mask(base, tables, industry, model, options)).select("symbol", "date")
    if passed.is_empty():
        return panel.head(0)
    dated = panel.with_columns(
        pl.col("symbol").cast(pl.Utf8),
        _as_date("date").alias("_screen_date"),
    )
    return dated.join(
        passed.rename({"date": "_screen_date"}),
        on=["symbol", "_screen_date"],
        how="inner",
    ).drop("_screen_date")


def _mask(base, tables, industry, model: str, params: dict) -> pl.Series:
    income = _with_np(_quarter_versions(tables.get("income"), _INCOME_COLS))
    balance = _quarter_versions(tables.get("balance_sheet"), _BALANCE_COLS)
    cash = _quarter_versions(tables.get("cash_flow"), _CASH_COLS)
    metrics = _quarter_versions(tables.get("metrics"), _METRICS_COLS)
    shares = _share_versions(tables.get("shares"))
    work = base.with_row_index("_ord")
    frame = _attach_latest(work, _union_keys(income, metrics), "latest_qkey")
    frame = _with_industry(frame, industry)
    if model == "m1":
        return _m1(frame, income, balance, cash, metrics, shares)
    annual = _annual_only(_union_keys(income, metrics, cash))
    frame = _attach_latest(frame, annual, "annual_qkey")
    if model == "m2":
        return _m2(frame, income, balance, cash, metrics, params)
    if model == "m3":
        return _m3(frame, income, balance, cash, metrics, shares)
    raise ValueError(f"unknown fundamental screen {model}")


def _m1(frame, income, balance, cash, metrics, shares) -> pl.Series:
    frame = _offsets(frame, income, "latest_qkey", (0, 1, 2, 3, 4, 5), (
        "net_income_deducted", "revenue", "operating_cost", "np_attr", "month",
    ), "i")
    frame = _offsets(frame, metrics, "latest_qkey", (0, 1), (
        "roe", "revenue_yoy", "debt_to_asset_ratio",
    ), "m")
    frame = _offsets(frame, balance, "latest_qkey", (0,), (
        "goodwill", "total_assets", "total_liabilities",
    ), "b")
    frame = _offsets(frame, cash, "latest_qkey", (0,), _CASH_COLS, "c")
    frame = _attach_shares(frame, shares)
    frame = frame.with_columns(
        _yoy("i0_net_income_deducted", "i4_net_income_deducted").alias("g0"),
        _yoy("i1_net_income_deducted", "i5_net_income_deducted").alias("g1"),
        _revenue_growth("m0_revenue_yoy", "i0_revenue", "i4_revenue").alias("rev_g0"),
        _revenue_growth("m1_revenue_yoy", "i1_revenue", "i5_revenue").alias("rev_g1"),
        *_gross_margin_pair(),
        _debt("m0_debt_to_asset_ratio", "b0_total_liabilities", "b0_total_assets").alias("debt"),
        _goodwill_ratio().alias("gw"),
        _pe().alias("pe"),
        _roe_pct().alias("roe_pct"),
    )
    ok = (
        (pl.col("g0") > 0) & (pl.col("g0") < 400) & (_chg("g0", "g1") > -0.10)
        & (pl.col("rev_g0") > 0) & (_chg("rev_g0", "rev_g1") > -0.10)
        & (pl.col("roe_pct") >= 0.70)
        & (pl.col("gm_now") - pl.col("gm_prev") >= -0.01)
        & (pl.col("debt") < 70)
        & pl.col("b0_goodwill").is_not_null() & (pl.col("gw") < 0.25)
        & (pl.col("c0_net_operating_cash_flow") > 0)
        & (pl.col("pe") > 0) & (pl.col("pe") < 80)
    )
    return _hit(frame, ok)


def _m2(frame, income, balance, cash, metrics, params: dict) -> pl.Series:
    frame = _offsets(frame, metrics, "latest_qkey", (0, 1, 2), (
        "net_margin", "net_income_yoy", "bps",
    ), "q")
    frame = _offsets(frame, metrics, "annual_qkey", (0, 4, 8), ("roe", "net_income_yoy"), "a")
    frame = _offsets(frame, income, "annual_qkey", (0, 4, 8), (
        "net_income_attributable", "net_income", "operating_profit", "income_tax", "total_profit",
    ), "ai")
    frame = _offsets(frame, balance, "annual_qkey", (0,), (
        "total_equity", "short_term_borrowing", "long_term_borrowing",
    ), "ab")
    frame = _offsets(frame, cash, "annual_qkey", (0, 4, 8, 12, 16), _CASH_COLS, "ac")
    roe_avg = (pl.col("a0_roe") + pl.col("a4_roe") + pl.col("a8_roe")) / 3
    pb = pl.when((pl.col("q0_bps") > 0) & (pl.col("_px") > 0)).then(pl.col("_px") / pl.col("q0_bps")).otherwise(None)
    ok = (
        (roe_avg / pb > 2.5)
        & (pl.col("q0_net_margin") > 10) & (pl.col("q1_net_margin") > 10) & (pl.col("q2_net_margin") > 10)
        & (((pl.col("q0_net_income_yoy") + pl.col("q1_net_income_yoy") + pl.col("q2_net_income_yoy")) / 3) > 20)
        & (roe_avg > 10)
        & (_roic("ai0_", "ab0_") > _wacc(params))
        & pl.all_horizontal([pl.col(f"ac{i}_net_operating_cash_flow") > 0 for i in (0, 4, 8, 12, 16)])
        & (_np_growth("a0_net_income_yoy", "ai0_", "ai4_") > _np_growth("a4_net_income_yoy", "ai4_", "ai8_"))
        & pl.col("industry_l1").is_not_null()
        & ~pl.col("industry_l1").is_in(list(_BANKS))
    )
    return _hit(frame, ok)


def _m3(frame, income, balance, cash, metrics, shares) -> pl.Series:
    frame = _offsets(frame, metrics, "latest_qkey", (0,), ("debt_to_asset_ratio", "revenue_yoy"), "m")
    frame = _offsets(frame, income, "latest_qkey", (0, 1, 2, 3, 4), (
        "revenue", "np_attr", "month",
    ), "i")
    frame = _offsets(frame, balance, "latest_qkey", (0,), ("total_liabilities", "total_assets"), "b")
    frame = _offsets(frame, cash, "latest_qkey", (0,), _CASH_COLS, "c")
    frame = _offsets(frame, cash, "annual_qkey", (0, 4, 8, 12), _CASH_COLS, "ac")
    frame = _attach_shares(frame, shares)
    frame = _with_roic_years(frame, income, balance)
    increases = [
        (pl.col("ac0_net_operating_cash_flow") > pl.col("ac4_net_operating_cash_flow")).fill_null(False),
        (pl.col("ac4_net_operating_cash_flow") > pl.col("ac8_net_operating_cash_flow")).fill_null(False),
        (pl.col("ac8_net_operating_cash_flow") > pl.col("ac12_net_operating_cash_flow")).fill_null(False),
    ]
    ok = (
        (_debt("m0_debt_to_asset_ratio", "b0_total_liabilities", "b0_total_assets") < 55)
        & (pl.col("i0_revenue") > 0)
        & (pl.col("c0_net_operating_cash_flow") / pl.col("i0_revenue") > 0.05)
        & (_revenue_growth("m0_revenue_yoy", "i0_revenue", "i4_revenue") > 0)
        & pl.col("roic_above_median")
        & (pl.col("ac0_net_operating_cash_flow") > 0)
        & (pl.col("ac4_net_operating_cash_flow") > 0)
        & (pl.col("ac8_net_operating_cash_flow") > 0)
        & (pl.sum_horizontal(item.cast(pl.Int8) for item in increases) >= 2)
        & (pl.col("pe") > 0) & (pl.col("pe") < 80)
    )
    return _hit(frame, ok)


def _hit(frame: pl.DataFrame, expr: pl.Expr) -> pl.Series:
    scored = frame.with_columns(expr.fill_null(False).alias("_hit"))
    if "_ord" in scored.columns:
        scored = scored.sort("_ord").unique("_ord", keep="last")
    return scored["_hit"]


def _with_roic_years(frame: pl.DataFrame, income, balance) -> pl.DataFrame:
    years = _december_years(income, balance)
    empty = frame.with_columns(pl.lit(False).alias("roic_above_median"), _pe().alias("pe"))
    if not years:
        return empty
    slim = frame.select("symbol", "date", "industry_l2")
    pieces = []
    for year in years:
        qkey = year * 4 + 3
        part = _asof_qkey(slim, income, qkey, (
            "operating_profit", "income_tax", "total_profit",
        ), "i_")
        part = _asof_qkey(part, balance, qkey, (
            "total_equity", "short_term_borrowing", "long_term_borrowing",
        ), "b_")
        pieces.append(part.select(
            "symbol", "date", "industry_l2",
            pl.lit(year).cast(pl.Int32).alias("year"),
            _roic("i_", "b_").alias("roic"),
        ))
    long = pl.concat(pieces, how="vertical")
    med = (
        long.filter(
            pl.col("roic").is_not_null()
            & pl.col("industry_l2").is_not_null()
            & (pl.col("industry_l2") != "")
        )
        .group_by(["date", "industry_l2", "year"])
        .agg(pl.col("roic").median().alias("roic_med"))
    )
    compared = long.join(med, on=["date", "industry_l2", "year"], how="left").with_columns(
        (pl.col("roic") > pl.col("roic_med")).fill_null(False).alias("above")
    )
    keyed = frame.with_columns((((pl.col("annual_qkey") - 3) // 4).cast(pl.Int32)).alias("annual_year"))
    above = pl.lit(True)
    for offset, name in ((0, "y0"), (1, "y1"), (2, "y2")):
        side = compared.select(
            "symbol", "date",
            (pl.col("year") + offset).alias("annual_year"),
            pl.col("above").alias(name),
        ).unique(["symbol", "date", "annual_year"], keep="last")
        keyed = keyed.join(side, on=["symbol", "date", "annual_year"], how="left")
        above = above & pl.col(name).fill_null(False)
    return keyed.with_columns(above.alias("roic_above_median"), _pe().alias("pe"))


def _data_dir(params: dict) -> Path:
    override = params.get("data_dir")
    if override:
        return Path(override)
    from app.config import settings

    return Path(settings.data_dir)


def _load_tables(data_dir: Path) -> dict[str, pl.DataFrame]:
    loaded = {}
    for name in ("income", "balance_sheet", "cash_flow", "metrics", "shares"):
        frame = _read_financial(data_dir, name)
        if frame is not None:
            loaded[name] = frame
    if not loaded:
        logger.warning("基本面预设没有读到 data/financials，筛选结果为空")
    return loaded


def _read_financial(data_dir: Path, name: str) -> pl.DataFrame | None:
    folder = data_dir / "financials" / name
    files = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
    frames = []
    for path in files:
        try:
            frames.append(pl.read_parquet(path))
        except Exception as exc:  # noqa: BLE001 — 一张坏表不应让整个选股接口失败
            logger.warning("读取 %s 失败: %s", path, exc)
    if not frames:
        return None
    return pl.concat(frames, how="diagonal_relaxed")


def _load_industry(data_dir: Path) -> pl.DataFrame | None:
    path = data_dir / "ext_data" / "ext_hy_ths" / "part.parquet"
    if not path.is_file():
        return None
    try:
        return pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取同花顺行业失败: %s", exc)
        return None


def _panel_base(panel: pl.DataFrame) -> pl.DataFrame:
    if "raw_close" in panel.columns:
        price = pl.col("raw_close")
    elif "close" in panel.columns:
        price = pl.col("close")
    else:
        price = pl.lit(None, dtype=pl.Float64)
    return (
        panel.select(
            pl.col("symbol").cast(pl.Utf8),
            _as_date("date").alias("date"),
            price.cast(pl.Float64, strict=False).alias("_px"),
        )
        .filter(pl.col("symbol").is_not_null() & pl.col("date").is_not_null())
        .unique(["symbol", "date"], keep="last")
        .sort(["symbol", "date"])
    )


def _as_date(column: str) -> pl.Expr:
    return pl.col(column).cast(pl.Utf8).str.slice(0, 10).str.to_date(strict=False)


def _quarter_versions(frame: pl.DataFrame | None, columns: tuple[str, ...]) -> pl.DataFrame | None:
    return _prepare(frame, columns, quarters_only=True)


def _share_versions(frame: pl.DataFrame | None) -> pl.DataFrame | None:
    prepared = _prepare(frame, ("total_shares",), quarters_only=False)
    if prepared is None:
        return None
    return prepared.with_columns(pl.col("period_end").dt.epoch("d").cast(pl.Int32).alias("qkey"))


def _prepare(frame: pl.DataFrame | None, columns: tuple[str, ...], *, quarters_only: bool) -> pl.DataFrame | None:
    if frame is None or frame.is_empty() or not {"symbol", "period_end", "announce_date"} <= set(frame.columns):
        return None
    present = [column for column in columns if column in frame.columns]
    if not present:
        return None
    out = frame.select(
        pl.col("symbol").cast(pl.Utf8),
        _as_date("period_end").alias("period_end"),
        _as_date("announce_date").alias("announce_date"),
        *[pl.col(column).cast(pl.Float64, strict=False).alias(column) for column in present],
    ).filter(
        pl.col("symbol").is_not_null()
        & pl.col("period_end").is_not_null()
        & pl.col("announce_date").is_not_null()
    )
    if quarters_only:
        out = out.filter(pl.col("period_end").dt.month().is_in([3, 6, 9, 12]))
    if out.is_empty():
        return None
    return (
        out.with_columns(
            (
                pl.col("period_end").dt.year().cast(pl.Int32) * 4
                + ((pl.col("period_end").dt.month().cast(pl.Int32) - 1) // 3)
            ).alias("qkey"),
            pl.col("announce_date").dt.offset_by("1d").alias("effective_from"),
            pl.col("period_end").dt.month().cast(pl.Int8).alias("month"),
        )
        .sort(["symbol", "qkey", "effective_from", "announce_date"])
        .unique(["symbol", "qkey", "effective_from"], keep="last")
    )


def _with_np(income: pl.DataFrame | None) -> pl.DataFrame | None:
    if income is None:
        return None
    choices = [pl.col(column) for column in ("net_income_attributable", "net_income") if column in income.columns]
    value = pl.coalesce(choices) if choices else pl.lit(None, dtype=pl.Float64)
    return income.with_columns(value.alias("np_attr"))


def _union_keys(*frames: pl.DataFrame | None) -> pl.DataFrame | None:
    parts = [
        frame.select("symbol", "qkey", "effective_from")
        for frame in frames
        if frame is not None and not frame.is_empty()
    ]
    if not parts:
        return None
    return pl.concat(parts, how="vertical").unique()


def _annual_only(frame: pl.DataFrame | None) -> pl.DataFrame | None:
    if frame is None:
        return None
    out = frame.filter((pl.col("qkey") % 4) == 3)
    return None if out.is_empty() else out


def _attach_latest(base: pl.DataFrame, versions: pl.DataFrame | None, out_col: str) -> pl.DataFrame:
    if out_col in base.columns:
        base = base.drop(out_col)
    if versions is None or versions.is_empty():
        return base.with_columns(pl.lit(None, dtype=pl.Int32).alias(out_col))
    events = (
        versions.group_by(["symbol", "effective_from"])
        .agg(pl.col("qkey").max().alias("qkey"))
        .sort(["symbol", "effective_from"])
        .with_columns(pl.col("qkey").cum_max().over("symbol").cast(pl.Int32).alias(out_col))
        .select("symbol", "effective_from", out_col)
        .sort(["symbol", "effective_from"])
    )
    return (
        base.sort(["symbol", "date"])
        .join_asof(
            events, left_on="date", right_on="effective_from", by="symbol",
            strategy="backward", check_sortedness=False,
        )
        .drop("effective_from", strict=False)
    )


def _offsets(base, versions, qkey_col: str, offsets: tuple[int, ...], columns: tuple[str, ...], prefix: str):
    frame = base
    for offset in offsets:
        frame = _attach_offset(frame, versions, qkey_col, offset, columns, f"{prefix}{offset}_")
    return frame


def _attach_offset(base, versions, qkey_col, offset: int, columns: tuple[str, ...], prefix: str):
    if versions is None or versions.is_empty() or qkey_col not in base.columns:
        return _fill_missing(base, columns, prefix)
    present = [column for column in columns if column in versions.columns]
    right = versions.select(
        "symbol",
        pl.col("qkey").cast(pl.Int32).alias("_q"),
        "effective_from",
        *[pl.col(column).alias(f"{prefix}{column}") for column in present],
    ).sort(["symbol", "_q", "effective_from"])
    # 正的 offset 是往过去的季度，4 表示一年前，不是未来报表。
    left = base.with_columns((pl.col(qkey_col).cast(pl.Int32) - offset).cast(pl.Int32).alias("_q")).with_row_index("_i")
    known = left.filter(pl.col("_q").is_not_null()).sort(["symbol", "_q", "date"])
    if known.is_empty() or right.is_empty():
        joined = _fill_missing(left, present, prefix)
    else:
        hit = known.join_asof(
            right, left_on="date", right_on="effective_from", by=["symbol", "_q"],
            strategy="backward", check_sortedness=False,
        ).drop("effective_from", strict=False)
        unknown = _fill_missing(left.filter(pl.col("_q").is_null()), present, prefix)
        joined = pl.concat([hit, unknown], how="diagonal_relaxed").sort("_i")
    joined = _fill_missing(joined, columns, prefix)
    return joined.drop("_q", "_i", strict=False)


def _fill_missing(frame: pl.DataFrame, columns: tuple[str, ...] | list[str], prefix: str) -> pl.DataFrame:
    for column in columns:
        name = f"{prefix}{column}"
        if name not in frame.columns:
            dtype = pl.Int8 if column == "month" else pl.Float64
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(name))
    return frame


def _asof_qkey(base, versions, qkey: int, columns: tuple[str, ...], prefix: str):
    tagged = base.with_columns(pl.lit(qkey).cast(pl.Int32).alias("_fixed_q"))
    subset = None if versions is None else versions.filter(pl.col("qkey") == qkey)
    return _attach_offset(tagged, subset, "_fixed_q", 0, columns, prefix).drop("_fixed_q", strict=False)


def _attach_shares(frame: pl.DataFrame, shares: pl.DataFrame | None) -> pl.DataFrame:
    keyed = _attach_latest(frame, shares, "share_key")
    return _attach_offset(keyed, shares, "share_key", 0, ("total_shares",), "s0_").drop("share_key", strict=False)


def _with_industry(frame: pl.DataFrame, industry: pl.DataFrame | None) -> pl.DataFrame:
    parsed = _parse_industry(industry)
    if parsed is None:
        return frame.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("industry_l1"),
            pl.lit(None, dtype=pl.Utf8).alias("industry_l2"),
        )
    return frame.join(parsed, on="symbol", how="left")


def _parse_industry(frame: pl.DataFrame | None) -> pl.DataFrame | None:
    if frame is None or frame.is_empty() or "所属同花顺行业" not in frame.columns:
        return None
    if "symbol" in frame.columns:
        symbol = pl.col("symbol")
    elif "股票代码" in frame.columns:
        symbol = pl.col("股票代码")
    else:
        return None
    parts = pl.col("所属同花顺行业").cast(pl.Utf8).str.strip_chars().str.split("-")
    return (
        frame.select(
            symbol.cast(pl.Utf8).alias("symbol"),
            parts.list.get(0, null_on_oob=True).str.strip_chars().alias("industry_l1"),
            parts.list.get(1, null_on_oob=True).str.strip_chars().alias("industry_l2"),
        )
        .filter(pl.col("symbol").is_not_null() & (pl.col("symbol") != ""))
        .unique("symbol", keep="last")
    )


def _yoy(current: str, base: str) -> pl.Expr:
    return (
        pl.when(pl.col(base).is_not_null() & (pl.col(base).abs() > 0) & pl.col(current).is_not_null())
        .then((pl.col(current) - pl.col(base)) / pl.col(base).abs() * 100)
        .otherwise(None)
    )


def _chg(current: str, previous: str) -> pl.Expr:
    return (
        pl.when(pl.col(previous).is_not_null() & (pl.col(previous).abs() > 0) & pl.col(current).is_not_null())
        .then((pl.col(current) - pl.col(previous)) / pl.col(previous).abs())
        .otherwise(None)
    )


def _revenue_growth(reported: str, current: str, base: str) -> pl.Expr:
    return pl.coalesce(pl.col(reported), _yoy(current, base))


def _gross_margin_pair() -> list[pl.Expr]:
    now = (
        pl.when(pl.col("i0_month") == 3)
        .then(_margin("i0_revenue", "i0_operating_cost"))
        .otherwise(_margin_diff("i0_revenue", "i1_revenue", "i0_operating_cost", "i1_operating_cost"))
    )
    prev = (
        pl.when(pl.col("i0_month") == 6)
        .then(_margin("i1_revenue", "i1_operating_cost"))
        .otherwise(_margin_diff("i1_revenue", "i2_revenue", "i1_operating_cost", "i2_operating_cost"))
    )
    return [now.alias("gm_now"), prev.alias("gm_prev")]


def _margin(rev: str, cost: str) -> pl.Expr:
    return (
        pl.when(pl.col(rev).is_not_null() & pl.col(cost).is_not_null() & (pl.col(rev) > 0))
        .then((pl.col(rev) - pl.col(cost)) / pl.col(rev))
        .otherwise(None)
    )


def _margin_diff(rev: str, prev_rev: str, cost: str, prev_cost: str) -> pl.Expr:
    single_rev = pl.col(rev) - pl.col(prev_rev)
    single_cost = pl.col(cost) - pl.col(prev_cost)
    return (
        pl.when(
            pl.col(rev).is_not_null() & pl.col(prev_rev).is_not_null()
            & pl.col(cost).is_not_null() & pl.col(prev_cost).is_not_null()
            & (single_rev > 0)
        )
        .then((single_rev - single_cost) / single_rev)
        .otherwise(None)
    )


def _debt(reported: str, liabilities: str, assets: str) -> pl.Expr:
    computed = pl.when(pl.col(assets) > 0).then(pl.col(liabilities) / pl.col(assets) * 100).otherwise(None)
    return pl.coalesce(pl.col(reported), computed)


def _goodwill_ratio() -> pl.Expr:
    return pl.when(pl.col("b0_total_assets") > 0).then(pl.col("b0_goodwill") / pl.col("b0_total_assets")).otherwise(None)


def _pe() -> pl.Expr:
    quarters = [_single_np(offset) for offset in range(4)]
    ttm = quarters[0]
    for part in quarters[1:]:
        ttm = ttm + part
    ready = pl.all_horizontal(part.is_not_null() for part in quarters)
    return (
        pl.when(ready & (ttm > 0) & (pl.col("s0_total_shares") > 0) & (pl.col("_px") > 0))
        .then(pl.col("_px") * pl.col("s0_total_shares") / ttm)
        .otherwise(None)
    )


def _single_np(offset: int) -> pl.Expr:
    current = pl.col(f"i{offset}_np_attr")
    month = pl.col(f"i{offset}_month")
    previous = pl.col(f"i{offset + 1}_np_attr")
    return pl.when(month == 3).then(current).when(previous.is_not_null()).then(current - previous).otherwise(None)


def _roe_pct() -> pl.Expr:
    group = ["date", "industry_l2"]
    eligible = pl.col("m0_roe").is_not_null() & pl.col("industry_l2").is_not_null() & (pl.col("industry_l2") != "")
    rank = pl.when(eligible).then(pl.col("m0_roe")).otherwise(None).rank(method="average").over(group)
    count = eligible.cast(pl.Int32).sum().over(group)
    return pl.when(eligible & (count > 0)).then(rank / count).otherwise(None)


def _roic(income_prefix: str, balance_prefix: str) -> pl.Expr:
    tax = pl.col(f"{income_prefix}income_tax")
    profit = pl.col(f"{income_prefix}total_profit")
    operating = pl.col(f"{income_prefix}operating_profit")
    equity = pl.col(f"{balance_prefix}total_equity")
    short = pl.col(f"{balance_prefix}short_term_borrowing")
    long = pl.col(f"{balance_prefix}long_term_borrowing")
    rate = tax / profit
    capital = equity + short + long
    ok = (
        (profit > 0) & tax.is_not_null() & (rate >= 0) & (rate <= 1)
        & operating.is_not_null()
        & equity.is_not_null() & short.is_not_null() & long.is_not_null()
        & (capital > 0)
    )
    return pl.when(ok).then(operating * (1 - rate) / capital).otherwise(None)


def _np_growth(reported: str, current_prefix: str, previous_prefix: str | None = None) -> pl.Expr:
    reported_value = pl.col(reported)
    if previous_prefix is None:
        return reported_value
    left = pl.coalesce(pl.col(f"{current_prefix}net_income_attributable"), pl.col(f"{current_prefix}net_income"))
    right = pl.coalesce(pl.col(f"{previous_prefix}net_income_attributable"), pl.col(f"{previous_prefix}net_income"))
    computed = (
        pl.when(right.is_not_null() & (right.abs() > 0) & left.is_not_null())
        .then((left - right) / right.abs() * 100)
        .otherwise(None)
    )
    return pl.coalesce(reported_value, computed)


def _wacc(params: dict) -> float:
    raw = params.get("wacc", 0.08) if params else 0.08
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.08
    if value > 1:
        value /= 100.0
    return value


def _december_years(*frames) -> list[int]:
    years: set[int] = set()
    for frame in frames:
        if frame is None or "period_end" not in frame.columns:
            continue
        for item in frame.filter(pl.col("period_end").dt.month() == 12)["period_end"].unique().to_list():
            if item is not None:
                years.add(int(item.year))
    return sorted(years)
