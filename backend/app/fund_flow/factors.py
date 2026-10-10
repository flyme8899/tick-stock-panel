"""选股和热门事件用的资金因子。

``ff_main_net_5d`` 是最近 5 个已落盘交易日的主力净流入合计（元）。不足 5 日为
null，不用 0 填。按交易日对齐，不用未来日期。

``ff_sector_net_inflow_rank`` 是行业净流入排名（1 为净流入最大）。行业归属用
扩展表「所属同花顺行业」的最后一段，对不上就是 null。这个排名只贴在单日帧上：
行业归属是最新快照，贴进历史多日帧会把今天的行业当成过去的行业。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from app.factors.registry import FactorSpec, get_factor, register_factor, unregister_factor
from app.fund_flow import store
from app.fund_flow.normalize import code6

_STOCK_ONLY = frozenset({"stock"})
_TAG = "fund_flow"

SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        id="ff_main_net_5d",
        label="主力净流入5日",
        group="资金",
        formula_text="最近 5 个已落盘交易日主力净流入合计（元）。不足 5 日为空，不把缺失当成 0",
        kind="base",
        unit="currency",
        warmup_bars=5,
        scale_free=False,
        asset_types=_STOCK_ONLY,
        tags=(_TAG,),
    ),
    FactorSpec(
        id="ff_sector_net_inflow_rank",
        label="板块资金净流入排名",
        group="资金",
        formula_text="同花顺行业资金净流入排名，1 为净流入最大。行业对不上或当日无板块数据时为空",
        kind="base",
        unit="count",
        warmup_bars=1,
        scale_free=True,
        asset_types=_STOCK_ONLY,
        tags=(_TAG,),
    ),
)

_factor_cache: dict[tuple, tuple] = {}


def ensure_registered() -> None:
    """登记两个因子。挖掘用的 FACTOR_COLUMNS 视图会排除 fund_flow 标签。"""
    for spec in SPECS:
        current = get_factor(spec.id)
        if current is not None and current.version >= spec.version:
            continue
        if current is not None:
            unregister_factor(spec.id)
        register_factor(spec)


def _codes(symbols: pl.Series) -> pl.Series:
    return symbols.cast(pl.Utf8).str.extract(r"(\d{6})", 1)


def main_net_frame(data_dir: Path) -> pl.DataFrame:
    """每个代码每个交易日一行。窗口不足 5 日的 ff_main_net_5d 为 null。"""
    frame = store.read_range(data_dir, "stock")
    empty = pl.DataFrame(schema={
        "code": pl.Utf8,
        "trade_date": pl.Utf8,
        "ff_main_net_5d": pl.Float64,
    })
    if frame.is_empty() or "main_net" not in frame.columns:
        return empty
    ordered = frame.sort(["code", "trade_date"])
    return ordered.with_columns(
        pl.col("main_net")
        .rolling_sum(window_size=5, min_samples=5)
        .over("code")
        .alias("ff_main_net_5d")
    ).select("code", "trade_date", "ff_main_net_5d")


def _cached_main(data_dir: Path) -> pl.DataFrame:
    dates = tuple(store.list_dates(data_dir, "stock"))
    key = (str(data_dir), dates)
    cached = _factor_cache.get(key)
    if cached is not None and cached[0] == dates:
        return cached[1]
    frame = main_net_frame(data_dir)
    _factor_cache[key] = (dates, frame)
    return frame


def latest_sector_ranks(data_dir: Path, kind: str, trade_date: str | None = None) -> pl.DataFrame:
    """当日排名。有收盘快照用收盘，否则用当天最后一次盘中快照。"""
    empty = pl.DataFrame(schema={"name": pl.Utf8, "rank": pl.Int64, "net_inflow": pl.Float64, "trade_date": pl.Utf8})
    day = trade_date or store.latest_date(data_dir, kind)
    if day is None:
        return empty
    frame = store.read_partition(data_dir, kind, day)
    needed = ("snapshot", "captured_at", "name", "rank", "net_inflow", "trade_date")
    if frame.is_empty() or any(name not in frame.columns for name in needed):
        return empty
    close = frame.filter(pl.col("snapshot") == "close")
    chosen = close if not close.is_empty() else frame.sort("captured_at").unique(subset=["name"], keep="last")
    return chosen.select("name", "rank", "net_inflow", "trade_date")


def _industry_by_code(data_dir: Path) -> pl.DataFrame:
    path = Path(data_dir) / "ext_data" / "ext_hy_ths" / "part.parquet"
    empty = pl.DataFrame(schema={"code": pl.Utf8, "industry": pl.Utf8})
    if not path.exists():
        return empty
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return empty
    code_col = next((name for name in ("code", "symbol", "股票代码") if name in frame.columns), None)
    if code_col is None or "所属同花顺行业" not in frame.columns:
        return empty
    return frame.select(
        _codes(frame[code_col]).alias("code"),
        pl.col("所属同花顺行业").cast(pl.Utf8).str.split("-").list.last().alias("industry"),
    ).drop_nulls(["code", "industry"])


def attach_columns(df: pl.DataFrame, *, include_rank: bool, data_dir: Path | None = None) -> pl.DataFrame:
    if df.is_empty() or "symbol" not in df.columns or "date" not in df.columns:
        return df
    if data_dir is None:
        from app.config import settings
        data_dir = Path(settings.data_dir)
    if not (Path(data_dir) / "fund_flow").exists():
        return df
    factors = _cached_main(data_dir)
    work = df.with_columns(
        _codes(df["symbol"]).alias("_ff_code"),
        pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("_ff_date"),
    )
    if not factors.is_empty():
        work = work.join(
            factors,
            left_on=["_ff_code", "_ff_date"],
            right_on=["code", "trade_date"],
            how="left",
        )
    elif "ff_main_net_5d" not in work.columns:
        work = work.with_columns(pl.lit(None).cast(pl.Float64).alias("ff_main_net_5d"))
    if include_rank:
        day = work["_ff_date"].drop_nulls().unique().to_list()
        trade_date = day[0] if len(day) == 1 else None
        ranks = latest_sector_ranks(data_dir, "industry", trade_date)
        industry = _industry_by_code(data_dir)
        if not ranks.is_empty() and not industry.is_empty():
            mapped = industry.join(ranks, left_on="industry", right_on="name", how="left")
            mapped = mapped.select(
                "code",
                pl.col("rank").alias("ff_sector_net_inflow_rank"),
            )
            work = work.join(mapped, left_on="_ff_code", right_on="code", how="left")
        elif "ff_sector_net_inflow_rank" not in work.columns:
            work = work.with_columns(pl.lit(None).cast(pl.Int64).alias("ff_sector_net_inflow_rank"))
    return work.drop([name for name in ("_ff_code", "_ff_date") if name in work.columns])


def clear_cache() -> None:
    _factor_cache.clear()


def annotate_hot(candidates: list[dict], data_dir: Path) -> list[dict]:
    """给热门候选补资金字段。没有数据时 fund_flow 为 null，不把空值写成 0。"""
    if not candidates:
        return []
    main = main_net_frame(data_dir)
    latest_day = store.latest_date(data_dir, "stock")
    by_code: dict[str, float | None] = {}
    if latest_day and not main.is_empty():
        latest = main.filter(pl.col("trade_date") == latest_day)
        for row in latest.iter_rows(named=True):
            by_code[row["code"]] = row["ff_main_net_5d"]
    industry = {
        row["name"]: row for row in latest_sector_ranks(data_dir, "industry").iter_rows(named=True)
    }
    concept = {
        row["name"]: row for row in latest_sector_ranks(data_dir, "concept").iter_rows(named=True)
    }
    out = []
    for item in candidates:
        fund = _candidate_flow(item, by_code, industry, concept)
        out.append({**item, "fund_flow": fund})
    return out


def _candidate_flow(item: dict, by_code: dict, industry: dict, concept: dict) -> dict | None:
    kind = item.get("kind")
    key = str(item.get("key") or "")
    name = str(item.get("name") or "")
    payload: dict = {}
    if kind == "etf":
        return None
    if kind == "stock":
        code = code6(key) or code6(name)
        if code in by_code and by_code[code] is not None:
            payload["main_net_5d"] = by_code[code]
    else:
        row = industry.get(name) or industry.get(key) or concept.get(name) or concept.get(key)
        if row is not None:
            payload["sector_net_inflow_rank"] = row["rank"]
            payload["sector_net_inflow"] = row["net_inflow"]
            payload["sector_name"] = row["name"]
    return payload or None
