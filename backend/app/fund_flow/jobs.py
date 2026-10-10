"""各数据集的一次同步。网络调用都经过注入的 ``SourceClients``。"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from app.fund_flow import config, normalize, store
from app.fund_flow.circuit import CircuitBreaker, CircuitOpenError, call_with_retry
from app.fund_flow.sources import NORTHBOUND_SYMBOL, SOUTHBOUND_SYMBOL, SourceClients

BACKFILL_DATES = 120
NORMAL_RPS = 2.5
SLOW_RPS = 1.0
FAIL_RATE = 0.05
PACE_MIN_ATTEMPTS = 20
SECTOR_CACHE_S = 60.0

Sleeper = Callable[[float], None]
Clock = Callable[[], float]


@dataclass
class Pace:
    """个股单线程节流。失败率超过 5%（至少 20 次）后降到 1 次/秒。"""

    requests_per_sec: float = NORMAL_RPS
    attempts: int = 0
    failures: int = 0

    def note(self, ok: bool) -> None:
        self.attempts += 1
        if not ok:
            self.failures += 1
        if self.attempts >= PACE_MIN_ATTEMPTS and self.failures / self.attempts > FAIL_RATE:
            self.requests_per_sec = SLOW_RPS

    @property
    def delay_s(self) -> float:
        return 1.0 / self.requests_per_sec


@dataclass
class RunResult:
    kind: str
    wrote: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.skipped is None and not self.errors


def _keep_dates(rows: list[dict], *, backfill: bool, since: str | None) -> list[dict]:
    if not rows:
        return []
    if backfill:
        dates = sorted({row["trade_date"] for row in rows})
        allowed = set(dates[-BACKFILL_DATES:])
        return [row for row in rows if row["trade_date"] in allowed]
    if since is None:
        return rows
    return [row for row in rows if row["trade_date"] > since]


def _group_dates(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["trade_date"], []).append(row)
    return grouped


def sync_stock(
    data_dir: Path,
    symbols: list[str],
    clients: SourceClients,
    *,
    efinance: CircuitBreaker,
    mx: CircuitBreaker,
    pace: Pace | None = None,
    sleeper: Sleeper | None = None,
    mx_key: str | None = None,
    mx_max_calls: int | None = None,
) -> RunResult:
    """全市场个股账单。首跑每个代码留最近 120 个日期，之后只补缺。"""
    result = RunResult(kind="stock")
    pace = pace or Pace()
    sleep = sleeper or time.sleep
    key = config.mx_api_key() if mx_key is None else mx_key
    quota_left = mx_max_calls if mx_max_calls is not None else config.mx_max_calls()
    mx_calls = 0
    latest = store.latest_date(data_dir, "stock")
    backfill = latest is None
    target = None if backfill else latest
    present: set[str] = set()
    if target is not None:
        frame = store.read_partition(data_dir, "stock", target)
        if "code" in frame.columns:
            present = set(frame["code"].to_list())

    def _paced(source: str, breaker: CircuitBreaker, fn):
        sleep(pace.delay_s)
        try:
            value = call_with_retry(source, fn, breaker, sleeper=sleep)
        except Exception as exc:
            pace.note(False)
            raise exc
        pace.note(True)
        return value

    missing: list[str] = []
    collected: list[dict] = []
    for index, symbol in enumerate(symbols):
        code = normalize.code6(symbol)
        if not code:
            continue
        if code in present and not backfill:
            continue
        try:
            raw = _paced("efinance", efinance, lambda symbol=symbol: clients.history_bill(symbol))
            rows = _keep_dates(
                normalize.normalize_bills(raw, symbol=symbol, source="efinance"),
                backfill=backfill,
                since=target,
            )
            if rows:
                collected.extend(rows)
            else:
                missing.append(symbol)
        except CircuitOpenError as exc:
            result.errors.append(str(exc))
            pending = [symbol]
            for rest in symbols[index + 1:]:
                rest_code = normalize.code6(rest)
                if rest_code and (backfill or rest_code not in present):
                    pending.append(rest)
            missing.extend(pending)
            break
        except Exception as exc:  # noqa: BLE001
            missing.append(symbol)
            result.errors.append(f"{code} efinance: {exc}")

    for symbol in missing:
        if not key or quota_left <= 0 or mx_calls >= (mx_max_calls or quota_left):
            break
        if not mx.allow():
            result.errors.append("mx circuit open")
            break
        try:
            raw, reported = _paced(
                "mx", mx, lambda symbol=symbol: clients.mx_capital_flow(symbol),
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{normalize.code6(symbol)} mx: {exc}")
            continue
        mx_calls += 1
        if reported is not None:
            quota_left = reported
        else:
            quota_left -= 1
        rows = _keep_dates(
            normalize.normalize_bills(raw, symbol=symbol, source="mx"),
            backfill=backfill,
            since=target,
        )
        collected.extend(rows)
        if quota_left <= 0:
            break

    for trade_date, rows in _group_dates(collected).items():
        result.wrote += store.write_rows(data_dir, "stock", trade_date, rows)
    store.update_kind_state(
        data_dir,
        "stock",
        last_error=result.errors[-1] if result.errors else None,
        pace_rps=pace.requests_per_sec,
        mx_calls=mx_calls,
    )
    return result


_sector_cache: dict[str, tuple[float, list[dict]]] = {}


def reset_sector_cache() -> None:
    _sector_cache.clear()


def fetch_sector_rows(
    clients: SourceClients,
    kind: str,
    *,
    clock: Clock | None = None,
    cache_s: float = SECTOR_CACHE_S,
) -> list[dict]:
    if kind not in {"industry", "concept"}:
        raise ValueError("kind 只能是 industry 或 concept")
    now = (clock or time.monotonic)()
    cached = _sector_cache.get(kind)
    if cached is not None and now - cached[0] < cache_s:
        return cached[1]
    name = "stock_fund_flow_industry" if kind == "industry" else "stock_fund_flow_concept"
    rows = clients.ak(name, symbol="即时")
    _sector_cache[kind] = (now, rows)
    return rows


def sync_sectors(
    data_dir: Path,
    clients: SourceClients,
    *,
    kind: str,
    trade_date: str,
    captured_at: str,
    snapshot: str,
    breaker: CircuitBreaker,
    sleeper: Sleeper | None = None,
    clock: Clock | None = None,
) -> RunResult:
    result = RunResult(kind=kind)
    try:
        raw = call_with_retry(
            "akshare",
            lambda: fetch_sector_rows(clients, kind, clock=clock),
            breaker,
            sleeper=sleeper,
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(str(exc))
        store.update_kind_state(data_dir, kind, last_error=str(exc))
        return result
    rows = normalize.normalize_sectors(
        raw, trade_date=trade_date, captured_at=captured_at, snapshot=snapshot,
    )
    # 收盘快照覆盖同一天的上一份收盘；盘中按捕获时间留多份。
    if snapshot == "close":
        rows = [{**row, "captured_at": f"{trade_date}T15:00:00"} for row in rows]
    result.wrote = store.write_rows(data_dir, kind, trade_date, rows)
    store.update_kind_state(data_dir, kind, last_error=None, last_snapshot=snapshot)
    return result


def sync_margin(
    data_dir: Path,
    clients: SourceClients,
    *,
    trade_date: str,
    breaker: CircuitBreaker,
    sleeper: Sleeper | None = None,
) -> RunResult:
    """两融 T+1。``trade_date`` 是要补的交易日，不是运行当天。"""
    result = RunResult(kind="margin")
    compact = trade_date.replace("-", "")
    calls = [
        ("sse", "summary", "stock_margin_sse", {"start_date": compact, "end_date": compact}),
        ("szse", "summary", "stock_margin_szse", {"date": compact}),
        ("sse", "detail", "stock_margin_detail_sse", {"date": compact}),
        ("szse", "detail", "stock_margin_detail_szse", {"date": compact}),
    ]
    rows: list[dict] = []
    for market, row_kind, name, kwargs in calls:
        try:
            raw = call_with_retry(
                "akshare", lambda name=name, kwargs=kwargs: clients.ak(name, **kwargs),
                breaker, sleeper=sleeper,
            )
        except CircuitOpenError as exc:
            result.errors.append(str(exc))
            break
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{name}: {exc}")
            continue
        if row_kind == "summary":
            parsed = normalize.normalize_margin_summary(raw, market=market)
            parsed = [row for row in parsed if row["trade_date"] == trade_date]
        else:
            parsed = normalize.normalize_margin_detail(raw, market=market, trade_date=trade_date)
        rows.extend(parsed)
    if rows:
        result.wrote = store.write_rows(data_dir, "margin", trade_date, rows)
    store.update_kind_state(
        data_dir, "margin",
        last_error=result.errors[-1] if result.errors else None,
        target_date=trade_date,
    )
    return result


def sync_etf_shares(
    data_dir: Path,
    clients: SourceClients,
    *,
    trade_date: str,
    breaker: CircuitBreaker,
    sleeper: Sleeper | None = None,
) -> RunResult:
    result = RunResult(kind="etf_shares")
    compact = trade_date.replace("-", "")
    try:
        raw = call_with_retry(
            "akshare",
            lambda: clients.ak("fund_etf_scale_sse", date=compact),
            breaker,
            sleeper=sleeper,
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(str(exc))
        store.update_kind_state(data_dir, "etf_shares", last_error=str(exc))
        return result
    rows = [row for row in normalize.normalize_etf_shares(raw) if row["trade_date"] == trade_date]
    result.wrote = store.write_rows(data_dir, "etf_shares", trade_date, rows)
    store.update_kind_state(data_dir, "etf_shares", last_error=None)
    return result


def sync_southbound(
    data_dir: Path,
    clients: SourceClients,
    *,
    breaker: CircuitBreaker,
    sleeper: Sleeper | None = None,
) -> RunResult:
    return _sync_hsgt(
        data_dir, clients, breaker, sleeper,
        kind="southbound",
        symbol=SOUTHBOUND_SYMBOL,
        normalizer=normalize.normalize_southbound,
    )


def sync_northbound_turnover(
    data_dir: Path,
    clients: SourceClients,
    *,
    breaker: CircuitBreaker,
    sleeper: Sleeper | None = None,
) -> RunResult:
    return _sync_hsgt(
        data_dir, clients, breaker, sleeper,
        kind="northbound_turnover",
        symbol=NORTHBOUND_SYMBOL,
        normalizer=normalize.normalize_northbound_turnover,
    )


def _sync_hsgt(data_dir, clients, breaker, sleeper, *, kind, symbol, normalizer) -> RunResult:
    result = RunResult(kind=kind)
    try:
        raw = call_with_retry(
            "akshare",
            lambda: clients.ak("stock_hsgt_hist_em", symbol=symbol),
            breaker,
            sleeper=sleeper,
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(str(exc))
        store.update_kind_state(data_dir, kind, last_error=str(exc))
        return result
    grouped = _group_dates(normalizer(raw))
    if not grouped:
        store.update_kind_state(data_dir, kind, last_error=None)
        return result
    latest = max(grouped)
    for trade_date, rows in grouped.items():
        store.write_rows(data_dir, kind, trade_date, rows)
    result.wrote = len(grouped[latest])
    store.update_kind_state(data_dir, kind, last_error=None, last_date=latest)
    return result


def sync_lhb_backup(
    data_dir: Path,
    clients: SourceClients,
    *,
    trade_date: str,
    breaker: CircuitBreaker,
    sleeper: Sleeper | None = None,
) -> RunResult:
    result = RunResult(kind="lhb")
    compact = trade_date.replace("-", "")
    try:
        raw = call_with_retry(
            "akshare",
            lambda: clients.ak("stock_lhb_detail_em", start_date=compact, end_date=compact),
            breaker,
            sleeper=sleeper,
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(str(exc))
        store.update_kind_state(data_dir, "lhb", last_error=str(exc))
        return result
    rows = [row for row in normalize.normalize_lhb(raw) if row["trade_date"] == trade_date]
    result.wrote = store.write_rows(data_dir, "lhb", trade_date, rows)
    store.update_kind_state(data_dir, "lhb", last_error=None)
    return result


def previous_session(day: date, known_days: list[date] | None = None) -> date:
    """T+1 两融要补的交易日：已知交易日里今天之前的最后一天，否则上一个工作日。"""
    if known_days:
        earlier = [item for item in known_days if item < day]
        if earlier:
            return max(earlier)
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor
