"""业绩预告 / 业绩快报 / 业绩报表（东财数据中心）。

与 ``financial_sync``（TickFlow 三大报表、指标、股本）并列，存同一个 ``financials/`` 目录，
但按报告期分区，保留公告日，可以做"公告当天才知道"的时点研究::

    financials/em_forecast/report_period=YYYY-MM-DD/part.parquet   业绩预告 RPT_PUBLIC_OP_NEWPREDICT
    financials/em_express/report_period=YYYY-MM-DD/part.parquet    业绩快报 RPT_FCI_PERFORMANCEE
    financials/em_report/report_period=YYYY-MM-DD/part.parquet     业绩报表 RPT_LICO_FN_CPD

字段口径与 akshare ``stock_yjyg_em`` / ``stock_yjkb_em`` / ``stock_yjbb_em`` 相同（同一批接口），
这里直接走 ``em_get``，和资金流共用东财限流。金额单位是元，比例单位是百分数。

去重键（同键保留 ``update_date`` / ``eitime`` / ``fetched_at`` 最新的一行）：

- 预告：(code, report_period, source, announce_date, indicator_code)。一份预告按指标
  （净利润、扣非、营收、EPS…）拆成多行，不加指标会把同一公告的不同指标压成一行。
- 快报、报表：(code, report_period, source, announce_date)。

注意：东财业绩快报接口只给"库里当前版本"。公司发正式年报后，快报行可能被改写
（例如数值清空、``update_date`` 变新），历史上的快报原值不一定还在。回补拿到的是
今天的样子；每日增量从上线起才逐日留存。业绩报表同理，更正公告会覆盖同一公告日的行。

用法（容器内）::

    # 每日增量：调度器工作日 18:40 自动跑最近 2 个报告期
    python -m app.services.earnings_sync --latest 2
    # 历史回补 2018Q1 至今（35 个报告期），顺序请求，走东财限流
    python -m app.services.earnings_sync --from 2018-03-31 --spacing 1.0
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import CN_TZ

logger = logging.getLogger(__name__)

Get = Callable[..., Any]

PAGE_SIZE = 500
PAGE_LIMIT = 200  # 单个报告期最多 10 万行，够用；打满仍是满页就报错，不写残缺分区
DEFAULT_SPACING_S = 1.0
FIRST_PERIOD = "2018-03-31"
JOB_HOUR, JOB_MINUTE = 18, 40

_WEB_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_SEC_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_REFERER = "https://data.eastmoney.com/bbsj/"


@dataclass(frozen=True)
class Dataset:
    name: str  # 目录名 financials/<name>
    table: str  # 快照表名
    label: str
    url: str
    report: str
    period_field: str
    sort: str
    # (输出列, 源字段, 类型)
    fields: tuple[tuple[str, str, Any], ...]
    keys: tuple[str, ...]


_COMMON = (
    ("code", "SECURITY_CODE", pl.String),
    ("symbol", "SECUCODE", pl.String),
    ("name", "SECURITY_NAME_ABBR", pl.String),
    ("announce_date", "NOTICE_DATE", "date"),
    ("trade_market", "TRADE_MARKET", pl.String),
    ("trade_market_code", "TRADE_MARKET_CODE", pl.String),
    ("security_type_code", "SECURITY_TYPE_CODE", pl.String),
    ("org_code", "ORG_CODE", pl.String),
)

FORECAST = Dataset(
    name="em_forecast", table="financial_forecast", label="业绩预告",
    url=_SEC_URL, report="RPT_PUBLIC_OP_NEWPREDICT", period_field="REPORT_DATE",
    sort="NOTICE_DATE,SECURITY_CODE",
    fields=(
        *_COMMON,
        ("indicator_code", "PREDICT_FINANCE_CODE", pl.String),
        ("indicator", "PREDICT_FINANCE", pl.String),
        ("predict_type", "PREDICT_TYPE", pl.String),
        ("amt_lower", "PREDICT_AMT_LOWER", pl.Float64),
        ("amt_upper", "PREDICT_AMT_UPPER", pl.Float64),
        ("amt_mid", "FORECAST_JZ", pl.Float64),
        ("chg_pct_lower", "ADD_AMP_LOWER", pl.Float64),
        ("chg_pct_upper", "ADD_AMP_UPPER", pl.Float64),
        ("chg_pct_mid", "INCREASE_JZ", pl.Float64),
        ("prev_year_same_period", "PREYEAR_SAME_PERIOD", pl.Float64),
        ("forecast_state", "FORECAST_STATE", pl.String),
        ("is_latest", "IS_LATEST", pl.String),
        ("content", "PREDICT_CONTENT", pl.String),
        ("reason", "CHANGE_REASON_EXPLAIN", pl.String),
        ("update_date", "UPDATE_DATE", "date"),
        ("eitime", "EITIME", pl.String),
    ),
    keys=("code", "report_period", "source", "announce_date", "indicator_code"),
)

EXPRESS = Dataset(
    name="em_express", table="financial_express", label="业绩快报",
    url=_SEC_URL, report="RPT_FCI_PERFORMANCEE", period_field="REPORT_DATE",
    sort="UPDATE_DATE,SECURITY_CODE",
    fields=(
        *_COMMON,
        ("basic_eps", "BASIC_EPS", pl.Float64),
        ("revenue", "TOTAL_OPERATE_INCOME", pl.Float64),
        ("revenue_prev", "TOTAL_OPERATE_INCOME_SQ", pl.Float64),
        ("revenue_yoy", "YSTZ", pl.Float64),
        ("revenue_qoq", "DJDYSHZ", pl.Float64),
        ("net_profit", "PARENT_NETPROFIT", pl.Float64),
        ("net_profit_prev", "PARENT_NETPROFIT_SQ", pl.Float64),
        ("net_profit_yoy", "JLRTBZCL", pl.Float64),
        ("net_profit_qoq", "DJDJLHZ", pl.Float64),
        ("bvps", "PARENT_BVPS", pl.Float64),
        ("roe_weighted", "WEIGHTAVG_ROE", pl.Float64),
        ("industry", "PUBLISHNAME", pl.String),
        ("qdate", "QDATE", pl.String),
        ("is_new", "ISNEW", pl.String),
        ("update_date", "UPDATE_DATE", "date"),
        ("eitime", "EITIME", pl.String),
    ),
    keys=("code", "report_period", "source", "announce_date"),
)

REPORT = Dataset(
    name="em_report", table="financial_report", label="业绩报表",
    url=_WEB_URL, report="RPT_LICO_FN_CPD", period_field="REPORTDATE",
    sort="UPDATE_DATE,SECURITY_CODE",
    fields=(
        *_COMMON,
        ("basic_eps", "BASIC_EPS", pl.Float64),
        ("deduct_eps", "DEDUCT_BASIC_EPS", pl.Float64),
        ("revenue", "TOTAL_OPERATE_INCOME", pl.Float64),
        ("revenue_yoy", "YSTZ", pl.Float64),
        ("revenue_qoq", "YSHZ", pl.Float64),
        ("net_profit", "PARENT_NETPROFIT", pl.Float64),
        ("net_profit_yoy", "SJLTZ", pl.Float64),
        ("net_profit_qoq", "SJLHZ", pl.Float64),
        ("bps", "BPS", pl.Float64),
        ("roe_weighted", "WEIGHTAVG_ROE", pl.Float64),
        ("ocf_per_share", "MGJYXJJE", pl.Float64),
        ("gross_margin", "XSMLL", pl.Float64),
        ("industry", "PUBLISHNAME", pl.String),
        ("board_name", "BOARD_NAME", pl.String),
        ("dividend_plan", "ASSIGNDSCRPT", pl.String),
        ("qdate", "QDATE", pl.String),
        ("is_new", "ISNEW", pl.String),
        ("update_date", "UPDATE_DATE", "date"),
        ("eitime", "EITIME", pl.String),
    ),
    keys=("code", "report_period", "source", "announce_date"),
)

DATASETS: tuple[Dataset, ...] = (FORECAST, EXPRESS, REPORT)
DATASET_BY_NAME = {d.name: d for d in DATASETS} | {d.table: d for d in DATASETS}


def schema_of(ds: Dataset) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col, _src, typ in ds.fields:
        out[col] = pl.String if typ == "date" else typ
    out["report_period"] = pl.String
    out["source"] = pl.String
    out["fetched_at"] = pl.String
    return out


# ---------------- 报告期 ----------------

_QE = ((3, 31), (6, 30), (9, 30), (12, 31))


def quarter_ends(start: str, end: date) -> list[str]:
    """[start, end] 内的季末日（YYYY-MM-DD），升序。"""
    first = date.fromisoformat(start)
    out = []
    for year in range(first.year, end.year + 1):
        for m, d in _QE:
            q = date(year, m, d)
            if first <= q <= end:
                out.append(q.isoformat())
    return out


def latest_periods(today: date, n: int = 2) -> list[str]:
    """不晚于今天的最近 n 个季末。"""
    found = quarter_ends(f"{today.year - 2}-03-31", today)
    return found[-n:]


# ---------------- 拉取 ----------------

def _day(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("/", "-").split("T", 1)[0].split(" ", 1)[0]
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10] or None


def _num(value: object) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def map_row(ds: Dataset, row: dict, *, period: str, fetched_at: str) -> dict | None:
    code = str(row.get("SECURITY_CODE") or "").strip()
    if not code:
        return None
    if _day(row.get(ds.period_field)) not in (None, period):
        return None
    out: dict[str, Any] = {}
    for col, src, typ in ds.fields:
        raw = row.get(src)
        if typ == "date":
            out[col] = _day(raw)
        elif typ == pl.Float64:
            out[col] = _num(raw)
        else:
            out[col] = None if raw is None else (str(raw).strip() or None)
    out["report_period"] = period
    out["source"] = f"eastmoney_{ds.report}"
    out["fetched_at"] = fetched_at
    return out


def fetch_period(
    ds: Dataset,
    period: str,
    *,
    get: Get,
    page_size: int = PAGE_SIZE,
    page_limit: int = PAGE_LIMIT,
    sleeper: Callable[[float], None] = time.sleep,
    spacing_s: float = 0.0,
    now: Callable[[], datetime] = lambda: datetime.now(CN_TZ),
) -> list[dict]:
    """拉一个报告期的全部行。页数打满还没完就报错，调用方不落盘。"""
    fetched_at = now().isoformat(timespec="seconds")
    rows: list[dict] = []
    page = 1
    while True:
        if page > 1 and spacing_s > 0:
            sleeper(spacing_s)
        resp = get(
            ds.url,
            params={
                "sortColumns": ds.sort,
                "sortTypes": "-1,-1",
                "pageSize": str(page_size),
                "pageNumber": str(page),
                "reportName": ds.report,
                "columns": "ALL",
                "filter": f"({ds.period_field}='{period}')",
                "source": "WEB",
                "client": "WEB",
            },
            headers={"Referer": _REFERER},
            timeout=(10, 30),
        )
        payload = resp.json() if hasattr(resp, "json") else resp
        if not isinstance(payload, dict):
            raise RuntimeError(f"{ds.label} {period} 返回异常")
        if not payload.get("success"):
            if int(payload.get("code") or 0) == 9201:  # 没有数据
                break
            raise RuntimeError(f"{ds.label} {period}: {str(payload.get('message'))[:200]}")
        result = payload.get("result") or {}
        data = result.get("data") or []
        for raw in data:
            if isinstance(raw, dict):
                item = map_row(ds, raw, period=period, fetched_at=fetched_at)
                if item is not None:
                    rows.append(item)
        pages = int(result.get("pages") or 0)
        if not data or len(data) < page_size or (pages and page >= pages):
            break
        if page >= page_limit:
            raise RuntimeError(f"{ds.label} {period} 超过 {page_limit} 页，停止以免写残缺分区")
        page += 1
    return rows


# ---------------- 存储 ----------------

def dataset_dir(data_dir: Path, ds: Dataset) -> Path:
    return data_dir / "financials" / ds.name


def partition_file(data_dir: Path, ds: Dataset, period: str) -> Path:
    return dataset_dir(data_dir, ds) / f"report_period={period}" / "part.parquet"


def frame_of(ds: Dataset, rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=schema_of(ds), orient="row") if rows else pl.DataFrame(
        schema=schema_of(ds))


def merge_rows(ds: Dataset, previous: pl.DataFrame | None, incoming: pl.DataFrame) -> pl.DataFrame:
    """合并后按去重键保留最新一行（update_date、eitime、fetched_at 依次比较，空值最旧）。"""
    schema = schema_of(ds)
    frames = [f for f in (previous, incoming) if f is not None and f.height]
    if not frames:
        return pl.DataFrame(schema=schema)
    merged = pl.concat(frames, how="diagonal_relaxed")
    for col, typ in schema.items():
        if col not in merged.columns:
            merged = merged.with_columns(pl.lit(None, dtype=typ).alias(col))
    merged = merged.select([pl.col(c).cast(t, strict=False) for c, t in schema.items()])
    order = ["update_date", "eitime", "fetched_at"]
    merged = merged.sort(order, nulls_last=False)
    merged = merged.unique(subset=list(ds.keys), keep="last", maintain_order=True)
    return merged.sort(["code", "announce_date"] + (
        ["indicator_code"] if "indicator_code" in ds.keys else []), nulls_last=True)


def write_period(data_dir: Path, ds: Dataset, period: str, rows: list[dict]) -> int:
    from app.services.fs_utils import atomic_write_parquet

    path = partition_file(data_dir, ds, period)
    previous = pl.read_parquet(path) if path.exists() else None
    merged = merge_rows(ds, previous, frame_of(ds, rows))
    if merged.is_empty():
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(merged, path)
    return merged.height


def read_dataset(data_dir: Path, ds: Dataset) -> pl.DataFrame:
    root = dataset_dir(data_dir, ds)
    files = sorted(root.glob("report_period=*/part.parquet"))
    if not files:
        return pl.DataFrame(schema=schema_of(ds))
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")


@contextmanager
def _exclusive(data_dir: Path) -> Iterator[bool]:
    """跨进程互斥：每日增量和历史回补不同时写同一批分区。拿不到锁就返回 False。"""
    lock_dir = data_dir / "financials"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fh = (lock_dir / ".em_earnings.lock").open("a")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()


@dataclass
class SyncResult:
    wrote: dict[str, int] = field(default_factory=dict)  # "<dataset>/<period>" -> 合并后行数
    fetched: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    skipped_locked: bool = False


def sync_periods(
    data_dir: Path,
    periods: list[str],
    *,
    datasets: tuple[Dataset, ...] = DATASETS,
    get: Get | None = None,
    spacing_s: float = DEFAULT_SPACING_S,
    sleeper: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = logger.info,
) -> SyncResult:
    """按报告期顺序拉取并合并写入。某期失败不写该期，继续下一期。"""
    result = SyncResult()
    getter = get or _default_get()
    with _exclusive(data_dir) as ok:
        if not ok:
            result.skipped_locked = True
            log("业绩数据：另一个同步正在进行，本次跳过")
            return result
        first = True
        for period in periods:
            for ds in datasets:
                if not first and spacing_s > 0:
                    sleeper(spacing_s)
                first = False
                key = f"{ds.name}/{period}"
                try:
                    rows = fetch_period(ds, period, get=getter, sleeper=sleeper,
                                        spacing_s=spacing_s)
                except Exception as exc:  # noqa: BLE001  单期失败不影响其它期
                    result.errors.append(f"{key}: {exc}")
                    logger.warning("业绩数据 %s 失败: %s", key, exc)
                    continue
                result.fetched[key] = len(rows)
                if rows:
                    result.wrote[key] = write_period(data_dir, ds, period, rows)
                log(f"{ds.label} {period}: 拉到 {len(rows)} 行，分区合并后 "
                    f"{result.wrote.get(key, 0)} 行")
    return result


def _default_get() -> Get:
    from app.eastmoney.client import em_get

    return em_get


# ---------------- 每日增量（调度器调用） ----------------

def enabled() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return (os.environ.get("EARNINGS_SYNC_ENABLED", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def run_daily(data_dir: Path | None = None, *, today: date | None = None,
              n_periods: int = 2) -> SyncResult | None:
    """盘后增量：最近 n 个报告期的预告、快报、报表各拉一遍并合并。"""
    if not enabled():
        logger.info("业绩数据每日增量未启用（EARNINGS_SYNC_ENABLED=0 或测试进程）")
        return None
    if data_dir is None:
        from app.config import settings

        data_dir = Path(settings.data_dir)
    from app.market_time import cn_now

    periods = latest_periods(today or cn_now().date(), n_periods)
    res = sync_periods(data_dir, periods)
    logger.info("业绩数据每日增量完成 periods=%s wrote=%d errors=%d",
                periods, sum(res.wrote.values()), len(res.errors))
    return res


def register_job(scheduler) -> None:
    """工作日 18:40（北京时间）跑最近 2 个报告期。同步函数，APScheduler 放线程池执行。"""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_daily,
        trigger=CronTrigger(day_of_week="mon-fri", hour=JOB_HOUR, minute=JOB_MINUTE,
                            timezone="Asia/Shanghai"),
        id="earnings_em_daily",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3 * 3600,
        replace_existing=True,
    )


# ---------------- 命令行 ----------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="业绩预告/快报/报表（东财数据中心）同步与回补")
    ap.add_argument("--latest", type=int, default=None, help="最近 N 个报告期（每日增量用 2）")
    ap.add_argument("--from", dest="start", default=None, help=f"回补起始报告期，如 {FIRST_PERIOD}")
    ap.add_argument("--to", dest="end", default=None, help="回补截止日期，默认今天")
    ap.add_argument("--datasets", default="", help="逗号分隔：em_forecast,em_express,em_report")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--spacing", type=float, default=DEFAULT_SPACING_S, help="请求间隔（秒），另有东财共用限流")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if (args.latest is None) == (args.start is None):
        print("需要 --latest 或 --from 之一")
        return 2
    from app.market_time import cn_now

    today = cn_now().date()
    if args.latest is not None:
        periods = latest_periods(today, max(1, args.latest))
    else:
        end = date.fromisoformat(args.end) if args.end else today
        periods = quarter_ends(args.start, end)
    names = [s.strip() for s in args.datasets.split(",") if s.strip()]
    try:
        datasets = tuple(DATASET_BY_NAME[n] for n in names) if names else DATASETS
    except KeyError as exc:
        print(f"未知数据集 {exc}")
        return 2
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        from app.config import settings

        data_dir = Path(settings.data_dir)
    print(f"报告期 {len(periods)} 个：{periods[0]} .. {periods[-1]}；数据集 "
          f"{','.join(d.name for d in datasets)} -> {data_dir}", flush=True)
    res = sync_periods(data_dir, periods, datasets=datasets, spacing_s=args.spacing,
                       log=lambda m: print(m, flush=True))
    if res.skipped_locked:
        return 3
    for item in res.errors:
        print(f"error {item}")
    print(f"done partitions={len(res.wrote)} fetched={sum(res.fetched.values())} "
          f"errors={len(res.errors)}", flush=True)
    return 1 if res.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
