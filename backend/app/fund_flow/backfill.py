"""按交易日回补全市场个股资金流。

容器内::

    python -m app.fund_flow.backfill --days 120 --data-dir /app/data
    python -m app.fund_flow.backfill --from 2026-04-01 --to 2026-10-15 --data-dir /app/data

走东财数据中心 ``RPT_DMSK_TS_FUNDFLOW``，金额由万元换成元，写入
``fund_flow/stock/date=YYYY-MM-DD/part.parquet``。``symbol`` 用带交易所后缀的代码。

出网走 ``em_get``。共用限流默认间隔 1 秒，``EASTMONEY_MIN_INTERVAL`` 最快 0.5 秒。
``--concurrency`` 让多天并行排页，实际请求仍被这把限流串起来，不会另开更快的连接。
``--spacing`` 是回补自己的最小间隔，请求本身已经比它慢时不再额外睡。

有本地日 K 分区时只补这些交易日；没有时按工作日。某一天失败不写该分区。
写入会盖掉同一天的 ``akshare_ths``。已经是东财来源的行不会被同花顺再盖回去。
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from app.fund_flow import store
from app.fund_flow.datacenter import PAGE_SIZE, Get, fetch_market_day
from app.fund_flow.jobs import local_trading_days

logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 3
DEFAULT_SPACING_S = 0.5

Clock = Callable[[], float]
Sleeper = Callable[[float], None]


@dataclass
class BackfillResult:
    requested: list[str] = field(default_factory=list)
    wrote: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def wrote_rows(self) -> int:
        return sum(self.wrote.values())


def _weekdays_between(start: date, end: date) -> list[str]:
    days: list[str] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def _weekdays_ending(today: date, count: int) -> list[str]:
    found: list[str] = []
    cursor = today
    # 周末和长假会占掉自然日。留足余量，避免数不满。
    guard = count * 3 + 14
    while len(found) < count and guard > 0:
        if cursor.weekday() < 5:
            found.append(cursor.isoformat())
        cursor -= timedelta(days=1)
        guard -= 1
    found.reverse()
    return found


def resolve_dates(
    data_dir: Path,
    *,
    days: int | None = None,
    start: str | None = None,
    end: str | None = None,
    today: date | None = None,
) -> list[str]:
    """``--days`` 与 ``--from/--to`` 二选一。交易日优先用本地日 K 分区。"""
    if days is not None and (start is not None or end is not None):
        raise ValueError("--days 和 --from/--to 不能一起用")
    if days is None and start is None:
        raise ValueError("需要 --days 或 --from")
    if today is None:
        from app.market_time import cn_now

        today = cn_now().date()
    known = local_trading_days(data_dir)
    if days is not None:
        if days <= 0:
            raise ValueError("--days 要大于 0")
        if known:
            picked = [item for item in known if item <= today]
            return [item.isoformat() for item in picked[-days:]]
        return _weekdays_ending(today, days)
    start_day = date.fromisoformat(start or "")
    end_day = date.fromisoformat(end) if end else today
    if end_day < start_day:
        raise ValueError("--to 早于 --from")
    if known:
        return [item.isoformat() for item in known if start_day <= item <= end_day]
    return _weekdays_between(start_day, end_day)


def _paced_get(
    getter: Get,
    *,
    spacing_s: float,
    sleeper: Sleeper,
    clock: Clock,
) -> Get:
    lock = threading.Lock()
    last: float | None = None

    def paced(url: str, *, params=None, headers=None, timeout=None):
        nonlocal last
        with lock:
            if last is not None and spacing_s > 0:
                gap = spacing_s - (clock() - last)
                if gap > 0:
                    sleeper(gap)
            try:
                return getter(url, params=params, headers=headers, timeout=timeout)
            finally:
                last = clock()

    return paced


def backfill_dates(
    data_dir: Path,
    dates: list[str],
    *,
    get: Get | None = None,
    sleeper: Sleeper | None = None,
    clock: Clock | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    spacing_s: float = DEFAULT_SPACING_S,
    page_size: int = PAGE_SIZE,
) -> BackfillResult:
    """逐日拉全市场并合并写入。失败的日期不落盘。"""
    result = BackfillResult(requested=list(dates))
    if not dates:
        return result
    getter = get or _default_getter()
    paced = _paced_get(
        getter,
        spacing_s=spacing_s,
        sleeper=sleeper or time.sleep,
        clock=clock or time.monotonic,
    )
    workers = max(1, concurrency)

    def one(day: str) -> tuple[str, str, int, str]:
        try:
            rows = fetch_market_day(day, get=paced, page_size=page_size)
        except Exception as exc:  # noqa: BLE001
            logger.warning("资金流回补 %s 失败: %s", day, exc)
            return day, "error", 0, str(exc)
        if not rows:
            logger.info("资金流回补 %s 无数据，跳过", day)
            return day, "skip", 0, ""
        wrote = store.write_rows(data_dir, "stock", day, rows)
        logger.info("资金流回补 %s 写入后 %s 行", day, wrote)
        return day, "ok", wrote, ""

    if workers == 1 or len(dates) == 1:
        outcomes = [one(day) for day in dates]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(one, dates))
    for day, status, wrote, message in outcomes:
        if status == "ok":
            result.wrote[day] = wrote
        elif status == "skip":
            result.skipped.append(day)
        else:
            result.errors.append(f"{day}: {message}")
    return result


def _default_getter() -> Get:
    from app.eastmoney.client import em_get

    return em_get


def _data_dir(raw: str | None) -> Path:
    if raw:
        return Path(raw)
    from app.config import settings

    return Path(settings.data_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回补 A 股个股日频主力资金流（东财数据中心）")
    parser.add_argument("--days", type=int, default=None, help="最近 N 个交易日，例如 120")
    parser.add_argument("--from", dest="start", default=None, help="起始交易日 YYYY-MM-DD")
    parser.add_argument("--to", dest="end", default=None, help="结束交易日 YYYY-MM-DD，默认今天")
    parser.add_argument("--data-dir", default=None, help="数据目录，默认 settings.data_dir")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--spacing", type=float, default=DEFAULT_SPACING_S, help="请求最小间隔（秒）"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.concurrency < 1:
        print("--concurrency 要大于 0")
        return 2
    if args.spacing < 0:
        print("--spacing 不能为负")
        return 2
    data_dir = _data_dir(args.data_dir)
    try:
        dates = resolve_dates(data_dir, days=args.days, start=args.start, end=args.end)
    except ValueError as exc:
        print(exc)
        return 2
    if not dates:
        print("没有可回补的交易日")
        return 1
    print(f"回补 {len(dates)} 个交易日 {dates[0]} .. {dates[-1]} -> {data_dir}")
    result = backfill_dates(
        data_dir,
        dates,
        concurrency=args.concurrency,
        spacing_s=args.spacing,
    )
    for day in dates:
        if day in result.wrote:
            print(f"{day} wrote={result.wrote[day]}")
        elif day in result.skipped:
            print(f"{day} skipped")
    for item in result.errors:
        print(f"error {item}")
    print(
        f"done days={len(result.wrote)} rows={result.wrote_rows} "
        f"skipped={len(result.skipped)} errors={len(result.errors)}"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
