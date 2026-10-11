"""各数据集的一次同步。网络调用都经过注入的 ``SourceClients``。"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.fund_flow import config, normalize, store
from app.fund_flow.circuit import CircuitBreaker, CircuitOpenError, call_with_retry
from app.fund_flow.sources import NORTHBOUND_SYMBOL, SOUTHBOUND_SYMBOL, SourceClients

BACKFILL_DATES = 120
# 深市 ETF 份额回补起点。官方报表一次超过 6 个月会返回空表，窗口留在 150 天内。
ETF_SHARES_HISTORY_START = "2025-10-10"
SZSE_WINDOW_DAYS = 150
NORMAL_RPS = 2.5
SLOW_RPS = 1.0
FAIL_RATE = 0.05
PACE_MIN_ATTEMPTS = 20
SECTOR_CACHE_S = 60.0

Sleeper = Callable[[float], None]
Clock = Callable[[], float]
HISTORY_SOURCES = frozenset({"efinance", "mx"})

logger = logging.getLogger(__name__)


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


def _empty_frame_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "length mismatch" in text and "0 elements" in text


def _select_bill_dates(
    rows: list[dict],
    *,
    backfill: bool,
    target: str | None,
    trade_date: str | None,
) -> list[dict]:
    if backfill or not trade_date:
        return _keep_dates(rows, backfill=backfill, since=target)
    # 指定交易日还不完整时，要能写回这一天，不能只留更晚的日期。
    return [row for row in rows if row["trade_date"] >= trade_date]


def _present_sources(frame) -> dict[str, str]:
    found: dict[str, str] = {}
    if frame.is_empty() or "code" not in frame.columns:
        return found
    sources = frame["source"].to_list() if "source" in frame.columns else [""] * frame.height
    for code, source in zip(frame["code"].to_list(), sources, strict=False):
        if code:
            found[str(code)] = str(source or "")
    return found


def _rank_attempts(window: str) -> tuple[tuple[str, dict, str], ...]:
    if window == "5d":
        return (
            ("stock_fund_flow_individual", {"symbol": "5日排行"}, "akshare_ths"),
            ("stock_individual_fund_flow_rank", {"indicator": "5日"}, "akshare_em_rank"),
        )
    return (
        ("stock_fund_flow_individual", {"symbol": "即时"}, "akshare_ths"),
        ("stock_individual_fund_flow_rank", {"indicator": "今日"}, "akshare_em_rank"),
    )


def _load_rank(clients: SourceClients, window: str) -> tuple[list[dict], str]:
    """同花顺全市场排名优先。东财排名是备选，失败不计入 akshare 熔断。"""
    last = ""
    for name, kwargs, source in _rank_attempts(window):
        for _attempt in range(2):
            try:
                rows = clients.ak(name, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                logger.warning("全市场资金排名 %s 失败: %s", name, exc)
                continue
            if rows:
                return rows, source
            break
    if last:
        logger.warning("全市场资金排名 %s 没有可用结果: %s", window, last)
    return [], ""


_ENTIRE_GAP_RATIO = 0.8
_BOARD_LABEL = {
    "sh_main": "沪市主板",
    "star": "科创板",
    "sz_main": "深市主板",
    "sz_gem": "创业板",
    "bj": "北交所",
}
# push2 行情列表在部分云服务器上一直 502。个股资金流改走数据中心，和龙虎榜、两融、PE 同一套。
_SZ_MAIN_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_SZ_MAIN_REPORT = "RPT_DMSK_TS_FUNDFLOW"
_SZ_MAIN_COLUMNS = "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,NET_INFLOW,BIGDEAL_NET,SUPERDEAL_NET"
_SZ_MAIN_PAGE = 500
_DC_EMPTY = 9201


def _entirely_missing_labels(missing_codes: list[str], universe_codes: list[str]) -> list[str]:
    """缺失占该板块应有只数达到 80% 才算整段缺失。只缺几只时不报整段。"""
    def counts(codes: list[str]) -> dict[str, int]:
        found: dict[str, int] = {}
        for code in codes:
            board = store.board_of(code)
            if board == "unknown":
                continue
            found[board] = found.get(board, 0) + 1
        return found

    missing = counts(missing_codes)
    universe = counts(universe_codes)
    labels = []
    for board, need in sorted(universe.items()):
        if need <= 0:
            continue
        if missing.get(board, 0) / need >= _ENTIRE_GAP_RATIO:
            labels.append(_BOARD_LABEL.get(board, board))
    return labels


def fetch_sz_main_rank(trade_date: str, *, get=None) -> list[dict]:
    """深市主板个股资金流，东财数据中心按交易日分页。

    ``NET_INFLOW`` 是主力净流入，``BIGDEAL_NET`` 是大单，``SUPERDEAL_NET`` 是超大单。
    这三个字段的单位是万元，落盘前换成元。
    """
    from app.fund_flow.units import BARE_WAN, money_to_yuan
    from app.selection_fields.timeouts import REQUEST_TIMEOUT

    if get is None:
        from app.eastmoney.client import em_get

        get = em_get
    shaped: list[dict] = []
    page = 1
    while page <= 20:
        response = get(
            _SZ_MAIN_URL,
            params={
                "reportName": _SZ_MAIN_REPORT,
                "columns": _SZ_MAIN_COLUMNS,
                "pageNumber": str(page),
                "pageSize": str(_SZ_MAIN_PAGE),
                "sortColumns": "SECURITY_CODE",
                "sortTypes": "1",
                "source": "WEB",
                "client": "WEB",
                "filter": f"(TRADE_DATE='{trade_date}')",
            },
            headers={"Referer": "https://data.eastmoney.com/zjlx/detail.html"},
            timeout=REQUEST_TIMEOUT,
        )
        payload = response.json() if hasattr(response, "json") else response
        if not isinstance(payload, dict):
            raise RuntimeError("深市主板资金流返回异常")
        if not payload.get("success"):
            if int(payload.get("code") or 0) == _DC_EMPTY:
                break
            raise RuntimeError(str(payload.get("message") or "深市主板资金流失败")[:300])
        result = payload.get("result") or {}
        rows = result.get("data") or []
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = normalize.code6(str(row.get("SECURITY_CODE") or ""))
            if store.board_of(code) != "sz_main":
                continue
            shaped.append({
                "股票代码": code,
                "股票简称": row.get("SECURITY_NAME_ABBR") or "",
                "今日主力净流入-净额": money_to_yuan(row.get("NET_INFLOW"), bare=BARE_WAN),
                "今日大单净流入-净额": money_to_yuan(row.get("BIGDEAL_NET"), bare=BARE_WAN),
                "今日超大单净流入-净额": money_to_yuan(row.get("SUPERDEAL_NET"), bare=BARE_WAN),
            })
        pages = int(result.get("pages") or 0)
        if (pages and page >= pages) or len(rows) < _SZ_MAIN_PAGE:
            break
        page += 1
    return normalize.normalize_rank_rows(
        shaped, trade_date=trade_date, source="eastmoney_sz_main", window="today",
    )


def _rank_fallback(clients: SourceClients, symbols: list[str], trade_date: str) -> list[dict]:
    wanted = {code for symbol in symbols if (code := normalize.code6(symbol))}
    if not wanted:
        return []
    today_raw, today_source = _load_rank(clients, "today")
    five_raw, five_source = _load_rank(clients, "5d")
    today_rows = normalize.normalize_rank_rows(
        today_raw, trade_date=trade_date, source=today_source or "akshare_ths", window="today",
    ) if today_raw else []
    five_rows = normalize.normalize_rank_rows(
        five_raw, trade_date=trade_date, source=five_source or "akshare_ths", window="5d",
    ) if five_raw else []
    merged: dict[str, dict] = {}
    for row in today_rows:
        if row["code"] in wanted:
            merged[row["code"]] = row
    for row in five_rows:
        if row["code"] not in wanted:
            continue
        current = merged.get(row["code"])
        if current is None:
            merged[row["code"]] = row
        else:
            current["main_net_5d"] = row.get("main_net_5d")
    return list(merged.values())


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
    trade_date: str | None = None,
) -> RunResult:
    """全市场个股账单。首跑每个代码留最近 120 个日期，之后只补缺。

    ``trade_date`` 对应的分区不足全市场时，已有代码也会重拉。熔断只停下东财
    逐只请求，已经拿到的行照常落盘；还没请求的代码改走全市场排名，不整批交给妙想。
    """
    result = RunResult(kind="stock")
    pace = pace or Pace()
    sleep = sleeper or time.sleep
    key = config.mx_api_key() if mx_key is None else mx_key
    quota_left = mx_max_calls if mx_max_calls is not None else config.mx_max_calls()
    mx_calls = 0
    latest = store.latest_date(data_dir, "stock")
    backfill = latest is None
    target = None if backfill else latest
    refresh_day = False
    refresh_boards: set[str] | None = None
    universe = len({code for symbol in symbols if (code := normalize.code6(symbol))})
    if trade_date:
        current = store.read_partition(data_dir, "stock", trade_date)
        expected, full = store.board_coverage(data_dir)
        count = store.code_count(current)
        if count < store.ranking_limit(universe):
            # 样本连门槛都没到：整份重拉，不限板块。
            refresh_day = True
        else:
            # 样本够，但上游缺整段板块（同花顺个股榜不覆盖深主板）。
            # 只补缺的那几个板块里的代码，已有的照常 skip —— 否则 refresh_day
            # 会让 _skip 对所有代码返回 False，每轮把 5882 只全量重打一遍东财，
            # 而缺口又永远补不上，等于 30 分钟一次的无限全量重跑。
            gaps = store.missing_boards(current, expected=expected, full=full)
            if gaps:
                refresh_boards = set(gaps)
        present_sources = _present_sources(current)
    elif target is not None:
        present_sources = _present_sources(store.read_partition(data_dir, "stock", target))
    else:
        present_sources = {}

    def _skip(code: str) -> bool:
        if code not in present_sources:
            return False
        if trade_date is None:
            return not backfill
        if refresh_day:
            return False
        if refresh_boards is not None:
            # 只重拉缺口板块；其它板块已经拉到的代码保持 skip。
            return store.board_of(code) not in refresh_boards
        return True

    sz_batch = bool(trade_date and refresh_boards and "sz_main" in refresh_boards)

    def _skip_history(code: str) -> bool:
        if sz_batch and store.board_of(code) == "sz_main":
            return True
        return _skip(code)

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
    if sz_batch:
        batched: list[dict] = []
        try:
            batched = fetch_sz_main_rank(trade_date or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("深市主板资金流整批失败: %s", exc)
            result.errors.append(f"深市主板资金流整批失败: {exc}")
        else:
            if batched:
                collected.extend(batched)
            else:
                result.errors.append("深市主板资金流整批为空，保留上次结果")

    def _dated(raw: list[dict], symbol: str, source: str) -> list[dict]:
        return _select_bill_dates(
            normalize.normalize_bills(raw, symbol=symbol, source=source),
            backfill=backfill,
            target=target,
            trade_date=trade_date,
        )

    for symbol in symbols:
        code = normalize.code6(symbol)
        if not code or _skip_history(code):
            continue
        try:
            raw = _paced("efinance", efinance, lambda symbol=symbol: clients.history_bill(symbol))
            rows = _dated(raw, symbol, "efinance")
            if rows:
                collected.extend(rows)
            else:
                missing.append(symbol)
        except CircuitOpenError as exc:
            # 熔断只停掉后续逐只请求。已拿到的行在函数末尾落盘，剩下的改走排名。
            logger.warning("个股资金流熔断，本轮不再逐只请求: %s", exc)
            result.errors.append(str(exc))
            missing.append(symbol)
            break
        except Exception as exc:  # noqa: BLE001
            missing.append(symbol)
            result.errors.append(f"{code} efinance: {exc}")

    if missing and not key:
        logger.info("未配置 MX_APIKEY，跳过妙想补齐（%s 只）", len(missing))

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
        collected.extend(_dated(raw, symbol, "mx"))
        if quota_left <= 0:
            break

    if trade_date:
        got = {
            row["code"]
            for row in collected
            if row.get("trade_date") == trade_date and row.get("main_net") is not None and row.get("code")
        }
        needed = [
            symbol for symbol in symbols
            if (code := normalize.code6(symbol)) and code not in got and not _skip(code)
        ]
        if needed:
            ranked = _rank_fallback(clients, needed, trade_date)
            if ranked:
                collected.extend(ranked)
            else:
                logger.warning("全市场排名没有补上 %s 只股票", len(needed))
            # 上游排名本身可能缺整段板块（同花顺个股资金榜实测缺深主板 000/001/002/003，
            # 约 1612 只）。补完后仍然缺的按板块聚合记 WARNING —— 让「上游数据源覆盖不全」
            # 这件事在日志里可见，而不是默默写下一个看似完整的 3723 行分区。
            got_now = {
                row["code"] for row in (*collected, *ranked)
                if row.get("code") and row.get("trade_date") == trade_date
            }
            still = [symbol for symbol in needed if (code := normalize.code6(symbol)) and code not in got_now]
            still_codes = [code for symbol in still if (code := normalize.code6(symbol))]
            universe_codes = [code for symbol in symbols if (code := normalize.code6(symbol))]
            gaps = _entirely_missing_labels(still_codes, universe_codes)
            logger.warning(
                "全市场资金排名 %s 覆盖不全: 仍缺 %d 只%s",
                trade_date, len(still),
                f"（整段缺板块: {', '.join(gaps)}）" if gaps else "",
            )

    for day, rows in _group_dates(collected).items():
        result.wrote += store.write_rows(data_dir, "stock", day, rows)
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


def _pull_ak(clients: SourceClients, name: str, kwargs: dict, breaker: CircuitBreaker, sleeper):
    """空表不当成源故障，避免深交所没公布时把 akshare 熔断掉。"""

    def _call():
        try:
            return clients.ak(name, **kwargs)
        except Exception as exc:
            if _empty_frame_error(exc):
                logger.warning("两融 %s 空表，跳过: %s", name, exc)
                return []
            raise

    return call_with_retry("akshare", _call, breaker, sleeper=sleeper)


def _szse_summary_exists(data_dir: Path, trade_date: str) -> bool:
    frame = store.read_partition(data_dir, "margin", trade_date)
    if frame.is_empty() or "market" not in frame.columns or "row_kind" not in frame.columns:
        return False
    matched = frame.filter((pl.col("market") == "szse") & (pl.col("row_kind") == "summary"))
    return not matched.is_empty()


def _load_szse_day(
    clients: SourceClients,
    trade_date: str,
    breaker: CircuitBreaker,
    sleeper,
) -> list[dict]:
    compact = trade_date.replace("-", "")
    rows: list[dict] = []
    summary = _pull_ak(clients, "stock_margin_szse", {"date": compact}, breaker, sleeper)
    rows.extend(
        row for row in normalize.normalize_margin_summary(
            summary, market="szse", trade_date=trade_date,
        )
        if row["trade_date"] == trade_date
    )
    detail = _pull_ak(clients, "stock_margin_detail_szse", {"date": compact}, breaker, sleeper)
    rows.extend(normalize.normalize_margin_detail(detail, market="szse", trade_date=trade_date))
    return rows


def sync_margin(
    data_dir: Path,
    clients: SourceClients,
    *,
    trade_date: str,
    breaker: CircuitBreaker,
    sleeper: Sleeper | None = None,
    known_days: list[date] | None = None,
) -> RunResult:
    """两融 T+1。``trade_date`` 是要补的交易日，不是运行当天。

    深交所汇总空表时跳过并记日志，上交所已经拿到的行仍写入。再试上一交易日的深市。
    """
    result = RunResult(kind="margin")
    compact = trade_date.replace("-", "")
    calls = [
        ("sse", "summary", "stock_margin_sse", {"start_date": compact, "end_date": compact}),
        ("szse", "summary", "stock_margin_szse", {"date": compact}),
        ("sse", "detail", "stock_margin_detail_sse", {"date": compact}),
        ("szse", "detail", "stock_margin_detail_szse", {"date": compact}),
    ]
    rows: list[dict] = []
    szse_summary_empty = False
    for market, row_kind, name, kwargs in calls:
        try:
            raw = _pull_ak(clients, name, kwargs, breaker, sleeper)
        except CircuitOpenError as exc:
            result.errors.append(str(exc))
            break
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{name}: {exc}")
            continue
        if row_kind == "summary":
            parsed = normalize.normalize_margin_summary(raw, market=market, trade_date=trade_date)
            parsed = [row for row in parsed if row["trade_date"] == trade_date]
            if market == "szse" and not parsed:
                szse_summary_empty = True
                logger.warning("深交所两融 %s 无汇总，保留上交所", trade_date)
                continue
        else:
            parsed = normalize.normalize_margin_detail(raw, market=market, trade_date=trade_date)
        rows.extend(parsed)
    if szse_summary_empty:
        prev = previous_session(date.fromisoformat(trade_date), known_days).isoformat()
        if prev != trade_date and not _szse_summary_exists(data_dir, prev):
            try:
                prev_rows = _load_szse_day(clients, prev, breaker, sleeper)
            except Exception as exc:  # noqa: BLE001
                logger.warning("深交所两融改试 %s 失败: %s", prev, exc)
                prev_rows = []
            if prev_rows:
                result.wrote += store.write_rows(data_dir, "margin", prev, prev_rows)
            else:
                logger.warning("深交所两融上一交易日 %s 仍无汇总", prev)
        result.errors.append(f"stock_margin_szse: {trade_date} 无数据，已跳过")
    if rows:
        result.wrote += store.write_rows(data_dir, "margin", trade_date, rows)
    store.update_kind_state(
        data_dir, "margin",
        last_error=result.errors[-1] if result.errors else None,
        target_date=trade_date,
    )
    return result


def iter_szse_windows(start: date, end: date) -> list[tuple[date, date]]:
    """深交所基金规模报表超过 6 个月会返回空表。按 150 天切窗口。"""
    if end < start:
        return []
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=SZSE_WINDOW_DAYS - 1))
        windows.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return windows


def szse_windows_for_dates(days: list[str]) -> list[tuple[date, date]]:
    """按请求到的日期聚类。两头相距超过 150 天就切开，避免空窗被接口丢掉。"""
    parsed = sorted({date.fromisoformat(day) for day in days if len(day) == 10})
    if not parsed:
        return []
    windows: list[tuple[date, date]] = []
    start = parsed[0]
    end = parsed[0]
    for day in parsed[1:]:
        if (day - start).days >= SZSE_WINDOW_DAYS:
            windows.append((start, end))
            start = day
        end = day
    windows.append((start, end))
    return windows


def etf_share_history_dates(
    *,
    start: str = ETF_SHARES_HISTORY_START,
    end: str | None = None,
    known_days: list[date] | None = None,
    today: date | None = None,
) -> list[str]:
    """``2025-10-10`` 到最近一日。有交易日列表时只用交易日，否则用工作日。"""
    start_day = date.fromisoformat(start)
    if end is not None:
        end_day = date.fromisoformat(end)
    elif today is not None:
        end_day = today
    else:
        from app.market_time import cn_now

        end_day = cn_now().date()
    if end_day < start_day:
        return []
    if known_days is not None:
        picked = [day for day in known_days if start_day <= day <= end_day]
        return [day.isoformat() for day in sorted(set(picked))]
    days: list[str] = []
    cursor = start_day
    while cursor <= end_day:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def local_trading_days(data_dir: Path) -> list[date]:
    """本地日 K 分区（kline_daily/date=YYYY-MM-DD）即交易日历。"""
    root = Path(data_dir) / "kline_daily"
    if not root.exists():
        return []
    found: list[date] = []
    for path in root.glob("date=*"):
        try:
            found.append(date.fromisoformat(path.name.removeprefix("date=")))
        except ValueError:
            continue
    return sorted(found)


def _is_empty_table_error(exc: BaseException) -> bool:
    """akshare 在休市日拿到空表时会抛「None of [Index(...)] are in the [columns]」。"""
    return isinstance(exc, KeyError) and "are in the [columns]" in str(exc)


def _compact(day: str) -> str:
    return day.replace("-", "")


def _load_sse_shares(clients, trade_date: str, breaker: CircuitBreaker, sleeper) -> list[dict]:
    compact = _compact(trade_date)

    def fetch() -> list[dict]:
        try:
            return clients.ak("fund_etf_scale_sse", date=compact)
        except KeyError as exc:
            # 休市日空表不是源故障，不能算进熔断。
            if _is_empty_table_error(exc):
                return []
            raise

    raw = call_with_retry("akshare", fetch, breaker, sleeper=sleeper)
    return [
        row for row in normalize.normalize_etf_shares(raw)
        if row["trade_date"] == trade_date
    ]


def _load_szse_shares(
    clients,
    start: date,
    end: date,
    breaker: CircuitBreaker,
    sleeper,
    *,
    keep: set[str] | None = None,
) -> dict[str, list[dict]]:
    start_compact = start.strftime("%Y%m%d")
    end_compact = end.strftime("%Y%m%d")
    raw = call_with_retry(
        "akshare",
        lambda: clients.ak(
            "fund_scale_daily_szse",
            start_date=start_compact,
            end_date=end_compact,
            symbol="ETF",
        ),
        breaker,
        sleeper=sleeper,
    )
    grouped: dict[str, list[dict]] = {}
    for row in normalize.normalize_etf_shares(raw):
        day = row["trade_date"]
        if keep is not None and day not in keep:
            continue
        grouped.setdefault(day, []).append(row)
    return grouped


def backfill_etf_share_history(
    data_dir: Path,
    clients: SourceClients,
    *,
    breaker: CircuitBreaker,
    sleeper: Sleeper | None = None,
    start: str = ETF_SHARES_HISTORY_START,
    end: str | None = None,
    known_days: list[date] | None = None,
    today: date | None = None,
) -> RunResult:
    """回补 ``start``（默认 2025-10-10）到 ``end`` 的沪深 ETF 份额。

    没传 ``known_days`` 时先用本地日 K 分区当交易日历，避开节假日；
    日历覆盖不到起点时才退回工作日（节假日空表由 ``_load_sse_shares`` 兜住）。
    """
    if known_days is None:
        calendar = local_trading_days(data_dir)
        if calendar and min(calendar) <= date.fromisoformat(start):
            known_days = calendar
    return backfill_etf_shares(
        data_dir,
        clients,
        trade_dates=etf_share_history_dates(
            start=start, end=end, known_days=known_days, today=today,
        ),
        breaker=breaker,
        sleeper=sleeper,
    )


def backfill_etf_shares(
    data_dir: Path,
    clients: SourceClients,
    *,
    trade_dates: list[str],
    breaker: CircuitBreaker,
    sleeper: Sleeper | None = None,
) -> RunResult:
    """按日回补 ETF 份额。深市按窗口请求，再和沪市按日落到同一张表。"""
    result = RunResult(kind="etf_shares")
    days = sorted({day for day in trade_dates if len(day) == 10})
    sse_rows: dict[str, list[dict]] = {}
    for trade_date in days:
        try:
            sse_rows[trade_date] = _load_sse_shares(clients, trade_date, breaker, sleeper)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"fund_etf_scale_sse {trade_date}: {exc}")
    szse_rows: dict[str, list[dict]] = {}
    wanted = set(days)
    for win_start, win_end in szse_windows_for_dates(days):
        label = f"{win_start.isoformat()}..{win_end.isoformat()}"
        try:
            grouped = _load_szse_shares(
                clients, win_start, win_end, breaker, sleeper, keep=wanted,
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"fund_scale_daily_szse {label}: {exc}")
            continue
        if not grouped:
            result.errors.append(f"fund_scale_daily_szse {label} 无数据")
            continue
        for day, rows in grouped.items():
            szse_rows.setdefault(day, []).extend(rows)
    for trade_date in days:
        rows = sse_rows.get(trade_date, []) + szse_rows.get(trade_date, [])
        if not rows:
            continue
        result.wrote += store.write_rows(data_dir, "etf_shares", trade_date, rows)
    store.update_kind_state(
        data_dir, "etf_shares",
        last_error=result.errors[-1] if result.errors else None,
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
    """当日沪深 ETF 份额。一边失败仍写入另一边，并留下错误以便重试。"""
    result = RunResult(kind="etf_shares")
    rows: list[dict] = []
    day = date.fromisoformat(trade_date)
    try:
        rows.extend(_load_sse_shares(clients, trade_date, breaker, sleeper))
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"fund_etf_scale_sse: {exc}")
    try:
        grouped = _load_szse_shares(clients, day, day, breaker, sleeper, keep={trade_date})
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"fund_scale_daily_szse: {exc}")
        grouped = {}
    szse = grouped.get(trade_date) or []
    if not szse and not any(item.startswith("fund_scale_daily_szse") for item in result.errors):
        result.errors.append("fund_scale_daily_szse 无数据")
    rows.extend(szse)
    if rows:
        result.wrote = store.write_rows(data_dir, "etf_shares", trade_date, rows)
    store.update_kind_state(
        data_dir, "etf_shares",
        last_error=result.errors[-1] if result.errors else None,
    )
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
