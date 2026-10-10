"""向前扩展历史数据 — 完全独立于 daily_pipeline 的盘后管道。

用户从日 K 卡片手动触发,指定往前补的时长 (x 天/月/年)。
支持三种资产: stock / etf / index — 各自独立落盘, 深度需分别补。

流程:
  1. 获取当前最早日期 (按资产族)
  2. 向前拉日 K batch (start = 最早日期 - offset, end = 最早日期)
  3. 向前拉除权因子 (同范围, 仅股票族)
  4. 全量重算 enriched (股票族) / 增量 enriched (ETF、指数族)
  5. 刷新视图 + 缓存

⚠️ 本模块不导入 daily_pipeline 的任何函数,只复用基础设施:
  - kline_sync.sync_and_persist_daily_batch / sync_adj_factor
  - index_sync.sync_and_persist_etf_daily / sync_and_persist_index_daily
  - indicators.pipeline.run_pipeline
  - pipeline_jobs.JobStore
  - tickflow.repository.KlineRepository
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

from app.services import kline_sync
from app.services.pipeline_jobs import job_store
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

# 支持的资产类型 (与挖掘 availability 的 asset_type 口径一致)。
ASSET_TYPES = ("stock", "etf", "index")

# 资产类型 → 日K/enriched 分区目录名, 用于统计扩展后的天数。
_ASSET_DIRS = {
    "stock": ("kline_daily", "kline_daily_enriched"),
    "etf": ("kline_etf_daily", "kline_etf_enriched"),
    "index": ("kline_index_daily", "kline_index_enriched"),
}


def _noop(stage: str, pct: int, msg: str, **kwargs) -> None:  # noqa: ARG001
    pass


def _invalidate(table: str | None = None) -> None:
    from app.api.data import invalidate_data_cache
    invalidate_data_cache(table)


def _resolve_universe(capset: CapabilitySet) -> list[str]:
    """解析标的池 — 与 daily_pipeline 独立的副本。"""
    if capset.has(Cap.KLINE_DAILY_BATCH):
        try:
            from app.tickflow.pools import get_pool
            all_a = get_pool("CN_Equity_A", refresh=True)
            if all_a:
                return sorted(all_a)
        except Exception as e:
            logger.warning("CN_Equity_A pool unavailable: %s", e)

    from app.tickflow.pools import DEMO_SYMBOLS, get_pool as _get_pool
    from app.config import settings
    from pathlib import Path
    import polars as pl
    base: set[str] = set(DEMO_SYMBOLS)
    base.update(_get_pool("watchlist"))
    d = Path(settings.data_dir)
    inst_path = d / "instruments" / "instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path, columns=["symbol"])
            base.update(inst["symbol"].to_list())
        except Exception as e:
            logger.warning("instruments supplement failed: %s", e)
    return sorted(base)


def _refresh_single_view(repo: KlineRepository, name: str) -> None:
    """刷新单个 DuckDB 视图。"""
    d = repo.store.data_dir.as_posix()
    paths = {
        "kline_daily": f"{d}/kline_daily/**/*.parquet",
        "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
        "kline_minute": f"{d}/kline_minute/**/*.parquet",
        "adj_factor": f"{d}/adj_factor/**/*.parquet",
        "instruments": f"{d}/instruments/**/*.parquet",
    }
    path = paths.get(name)
    if not path:
        return
    try:
        repo.db.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{path}', union_by_name=true)"
        )
    except Exception as e:
        logger.warning("refresh view %s failed: %s", name, e)


def compute_offset(value: int, unit: str) -> timedelta:
    """将用户输入的 value + unit 转成 timedelta。"""
    if unit == "day":
        return timedelta(days=value)
    elif unit == "month":
        return timedelta(days=value * 30)
    elif unit == "year":
        return timedelta(days=value * 365)
    else:
        raise ValueError(f"不支持的单位: {unit}")


def run_extend_history(
    repo: KlineRepository,
    capset: CapabilitySet,
    value: int,
    unit: str,
    on_progress: Callable | None = None,
    asset_type: str = "stock",
) -> dict:
    """向前扩展历史数据的主函数。

    asset_type: stock / etf / index。三族各自独立落盘, 深度需分别补 ——
    例如只补过股票时, ETF 仍停在近一年, ETF 的 balanced 挖掘会照样卡门槛。

    完全独立于 daily_pipeline.run_now(),不调用其任何逻辑。
    返回结果 dict 供 job_store 记录。
    """
    if asset_type not in ASSET_TYPES:
        return {"error": f"不支持的资产类型: {asset_type}"}

    emit = on_progress or _noop

    # 0. 计算时间偏移
    offset = compute_offset(value, unit)
    today = date.today()

    # 1. 获取当前最早日期 (按资产族, 各表深度可能不同)
    emit("extend_history", 2, "检查当前数据范围…")
    earliest = repo.earliest_daily_date_for(asset_type)

    if not earliest:
        label = {"stock": "股票", "etf": "ETF", "index": "指数"}[asset_type]
        return {"error": f"本地无{label}日K数据,请先执行一次完整同步"}

    new_start = earliest - offset
    # 不能超过今天
    if new_start >= earliest:
        return {"error": "扩展范围无效,请增大时间跨度"}

    start_str = new_start.strftime("%Y-%m-%d")
    end_str = earliest.strftime("%Y-%m-%d")

    if asset_type == "stock":
        written_daily, written_adj, universe_size = _extend_stock(
            repo, capset, new_start, earliest, today, start_str, end_str, emit,
        )
    else:
        written_daily, written_adj, universe_size = _extend_index_or_etf(
            repo, capset, asset_type, new_start, earliest, start_str, end_str, emit,
        )

    daily_dirname, enriched_dirname_ = _ASSET_DIRS[asset_type]
    enriched_dir = repo.store.data_dir / enriched_dirname_
    enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_dir.exists() else 0
    daily_dir = repo.store.data_dir / daily_dirname
    daily_days = len(list(daily_dir.glob("date=*"))) if daily_dir.exists() else 0

    emit("extend_history", 100, f"完成,已扩展至 {new_start}")

    return {
        "asset_type": asset_type,
        "earliest_before": earliest.isoformat(),
        "earliest_after": new_start.isoformat(),
        "daily_rows": written_daily,
        "daily_days": daily_days,
        "adj_factor_rows": written_adj,
        "enriched_days": enriched_days,
        "universe_size": universe_size,
    }


def _extend_stock(
    repo: KlineRepository,
    capset: CapabilitySet,
    new_start: date,
    earliest: date,
    today: date,
    start_str: str,
    end_str: str,
    emit: Callable,
) -> tuple[int, int, int]:
    """股票族: 日K batch + 除权因子 + 全量重建 enriched。返回 (日K行, 除权因子行, 标的数)。"""
    # 2. 解析标的池
    emit("extend_history", 5, "解析标的池…")
    universe = _resolve_universe(capset)
    if not universe:
        return 0, 0, 0
    emit("extend_history", 8, f"标的池: {len(universe)} 只")

    # 3. 拉日 K
    emit("extend_history", 10, f"获取日K [{start_str} ~ {end_str}]…")
    logger.info("extend_history: daily K [%s ~ %s], %d symbols", start_str, end_str, len(universe))

    def _daily_chunk(cur: int, tot: int) -> None:
        emit("extend_history", 10 + int(35 * cur / tot),
             f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    written_daily = kline_sync.sync_and_persist_daily_batch(
        universe, repo, capset,
        start_date=datetime.combine(new_start, datetime.min.time()),
        end_date=datetime.combine(earliest, datetime.min.time()),
        on_chunk_done=_daily_chunk,
    )
    emit("extend_history", 45, f"日K 完成,写入 {written_daily} 行")
    logger.info("extend_history: daily K done, %d rows", written_daily)
    _refresh_single_view(repo, "kline_daily")
    _invalidate("daily")

    # 4. 拉除权因子 (新范围)
    written_adj = 0
    adj_start = datetime.combine(new_start, datetime.min.time())
    adj_end = datetime.combine(today, datetime.min.time())
    adj_start_str = new_start.strftime("%Y-%m-%d")
    adj_end_str = today.strftime("%Y-%m-%d")

    from app.services import preferences as _prefs
    adj_provider = _prefs.get_adj_factor_provider()
    can_sync_adj = capset.has(Cap.ADJ_FACTOR) or adj_provider != "tickflow"
    if can_sync_adj:
        emit("extend_history", 48, f"获取除权因子 [{adj_start_str} ~ {adj_end_str}]…")
        logger.info("extend_history: adj_factor [%s ~ %s]", adj_start_str, adj_end_str)

        def _adj_chunk(cur: int, tot: int) -> None:
            emit("extend_history", 48 + int(10 * cur / tot),
                 f"除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

        written_adj, _affected = kline_sync.sync_adj_factor(
            universe, repo, capset,
            start_time=adj_start, end_time=adj_end,
            on_chunk_done=_adj_chunk,
        )
        emit("extend_history", 60, f"除权因子完成,{written_adj} 行")
        logger.info("extend_history: adj_factor done, %d rows", written_adj)
        _refresh_single_view(repo, "adj_factor")
        _invalidate("adj_factor")
    else:
        emit("extend_history", 60, "除权因子跳过(无权限)")
        logger.info("extend_history: adj_factor skipped, no ADJ_FACTOR capability")

    # 5. 全量重算 enriched
    emit("extend_history", 65, "全量计算 enriched…")
    logger.info("extend_history: full enriched rebuild start")

    from app.indicators.pipeline import run_pipeline
    run_pipeline()

    _refresh_single_view(repo, "kline_enriched")
    _invalidate("enriched")

    # 6. 刷新视图
    emit("extend_history", 95, "刷新视图…")
    _refresh_single_view(repo, "kline_daily")
    _refresh_single_view(repo, "kline_enriched")
    _refresh_single_view(repo, "adj_factor")
    _invalidate(None)

    return written_daily, written_adj, len(universe)


def _extend_index_or_etf(
    repo: KlineRepository,
    capset: CapabilitySet,
    asset_type: str,
    new_start: date,
    earliest: date,
    start_str: str,
    end_str: str,
    emit: Callable,
) -> tuple[int, int, int]:
    """ETF / 指数族: 走 index_sync 的区间同步 (自带 enriched 增量重算)。

    返回 (日K行, 除权因子行=0, 标的数)。除权因子不适用于指数; ETF 的
    除权由 sync_and_persist_etf_daily 内部用 adj_factor_etf 处理。
    """
    from app.services import index_sync

    label = "ETF" if asset_type == "etf" else "指数"

    # 解析标的池用于上报规模 (同步函数内部会自行取 instruments)。
    symbols = _resolve_index_universe(repo, asset_type)
    emit("extend_history", 8, f"{label}标的池: {len(symbols)} 只")
    if not symbols:
        return 0, 0, 0

    emit("extend_history", 10, f"获取{label}日K [{start_str} ~ {end_str}]…")
    logger.info("extend_history: %s daily K [%s ~ %s], %d symbols",
                asset_type, start_str, end_str, len(symbols))

    def _chunk(cur: int, tot: int) -> None:
        emit("extend_history", 10 + int(55 * cur / tot),
             f"{label}日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    start_dt = datetime.combine(new_start, datetime.min.time())
    end_dt = datetime.combine(earliest, datetime.min.time())

    if asset_type == "etf":
        written_daily = index_sync.sync_and_persist_etf_daily(
            repo, capset,
            start_date=start_dt, end_date=end_dt,
            on_chunk_done=_chunk,
        )
        view = "kline_etf_daily"
        enriched_view = "kline_etf_enriched"
    else:
        written_daily = index_sync.sync_and_persist_index_daily(
            repo, capset,
            start_date=start_dt, end_date=end_dt,
            on_chunk_done=_chunk,
        )
        view = "kline_index_daily"
        enriched_view = "kline_index_enriched"

    emit("extend_history", 70, f"{label}日K 完成,写入 {written_daily} 行")
    logger.info("extend_history: %s daily K done, %d rows", asset_type, written_daily)

    # 刷新视图与缓存
    emit("extend_history", 90, "刷新视图…")
    _refresh_single_view(repo, view)
    _refresh_single_view(repo, enriched_view)
    if hasattr(repo, "refresh_index_views"):
        repo.refresh_index_views()
    _invalidate(None)

    return written_daily, 0, len(symbols)


def _resolve_index_universe(repo: KlineRepository, asset_type: str) -> list[str]:
    """取 ETF 或指数的本地标的代码, 仅用于上报规模。"""
    try:
        if asset_type == "etf":
            df = repo.get_etf_instruments()
        else:
            df = repo.get_index_instruments()
        if df is not None and not df.is_empty() and "symbol" in df.columns:
            if asset_type == "index" and "asset_type" in df.columns:
                # index_instruments 合并存了指数+ETF, 取指数族时排掉 ETF。
                df = df.filter(df["asset_type"] != "etf")
            return sorted(set(df["symbol"].to_list()))
    except Exception as e:  # noqa: BLE001
        logger.warning("resolve %s universe failed: %s", asset_type, e)
    return []

