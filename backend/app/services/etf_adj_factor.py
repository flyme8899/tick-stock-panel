"""ETF 单次除权因子。

行情源（TickFlow ``ex_factors``、stock-sdk ``close_hfq/close_none``）给的是累计
后复权水平，或带逐日漂移的前复权跳变，不能当作 ``ex_factor``。本模块用新浪
``hfq.js`` 的累计份额因子 ``s`` 和累计现金分红 ``u`` 还原单次事件：

- 份额折算/拆分：``share_ratio = s_t / s_{t-1}``（新份额 / 旧份额）。
- 现金分红：``cash = u_t - u_{t-1}``，按折算后的新份额计。
- 除权价 ``post = prev_close / share_ratio - cash``，
  ``ex_factor = prev_close / post``。纯折算时就是 ``share_ratio``，
  纯分红时就是 ``prev_close / (prev_close - cash)``。

份额折算在新浪上的登记日可能早于价格跳空日（510500 记在 2022-08-26，
价格在 2022-08-29 才跳）。有原始收盘价时，把事件挪到登记日之后、价格比
最接近理论因子的那个交易日。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger(__name__)

_BASELINE = date(1900, 1, 1)
_RATIO_EPS = 1e-6
_CASH_EPS = 1e-6
_HINT_DAYS = 10
# 小于这个幅度的份额变化不当成折算（日常申赎噪声），除权日就用新浪登记日。
_SHARE_MOVE = 0.02
# 原始价比和理论因子的相对误差。小于它才把除权日从登记日挪走。
_DATE_MATCH = 0.03
_FACTOR_EPS = 1e-9
# 本地收盘一次只放进内存的标的数。全市场大约一千多只，不能一次读成 dict。
_CLOSE_BATCH = 32
# 盘后管道全量拉新浪的间隔。中间只重试上次失败的代码，并且不打腾讯。
_FULL_INTERVAL_DAYS = 7
_STATE_NAME = "sync_state.json"
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)

# 与 kline_sync.ADJ_DETAIL_COLS 对齐。bonus/allot 对 ETF 折算没有对应口径，留空。
FACTOR_COLUMNS = (
    "symbol",
    "trade_date",
    "ex_factor",
    "dividend",
    "bonus",
    "allot",
    "allot_price",
    "prev_close",
)

FetchText = Callable[[str], str]
FetchPrices = Callable[[str, date, date], dict[date, float]]


@dataclass(frozen=True)
class EtfAdjSyncOutcome:
    """一次写入的结果。``skipped`` 为真表示未出网、未读收盘。"""

    rows: int
    affected: list[str]
    failed: list[str]
    skipped: bool


@dataclass(frozen=True)
class EtfCorporateAction:
    """一次新浪记录到的公司行为。``hints`` 是紧随其后、s/u 没变的日期。"""

    record_date: date
    share_ratio: float
    cash_div: float
    hints: tuple[date, ...] = ()


def to_sina_code(symbol: str) -> str | None:
    """``510500.SH`` → ``sh510500``。后缀优先，没有后缀时按代码段猜。"""
    text = str(symbol or "").strip().upper()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 6:
        return None
    code = digits[-6:]
    if text.endswith(".SZ"):
        prefix = "sz"
    elif text.endswith(".SH"):
        prefix = "sh"
    elif code.startswith(("15", "16")):
        prefix = "sz"
    elif code.startswith(("5", "6")):
        prefix = "sh"
    else:
        return None
    return prefix + code


def parse_sina_hfq_payload(text: str) -> list[dict]:
    """解析 ``var sh510500hfq={...}; /* ... */``。失败抛 ValueError。"""
    raw = text.strip()
    if "=" in raw:
        raw = raw.split("=", 1)[1]
    raw = raw.split("/*", 1)[0].strip().rstrip(";")
    match = _JSON_OBJECT.search(raw)
    if match is None:
        raise ValueError("sina hfq.js 没有 JSON 对象")
    data = json.loads(match.group(0))
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("sina hfq.js 缺少 data")
    return rows


def corporate_actions(rows: list[dict]) -> list[EtfCorporateAction]:
    """累计 ``s`` / ``u`` 差分。1900-01-01 和 s、u 都没变的行不是事件。"""
    parsed: list[tuple[date, float, float]] = []
    for row in rows:
        if not isinstance(row, dict) or "d" not in row:
            continue
        parsed.append((
            date.fromisoformat(str(row["d"])[:10]),
            float(row.get("s") or 0),
            float(row.get("u") or 0),
        ))
    parsed.sort(key=lambda item: item[0])
    if not parsed:
        return []
    if parsed[0][0] != _BASELINE:
        parsed.insert(0, (_BASELINE, 1.0, 0.0))

    actions: list[EtfCorporateAction] = []
    prev_s: float | None = None
    prev_u: float | None = None
    open_action: dict | None = None
    for day, share, cash in parsed:
        if prev_s is None or day == _BASELINE:
            prev_s, prev_u = share, cash
            continue
        ratio = share / prev_s if prev_s else 1.0
        delta = cash - (prev_u or 0.0)
        share_changed = abs(ratio - 1.0) > _RATIO_EPS
        cash_changed = delta > _CASH_EPS
        if not share_changed and not cash_changed:
            if (
                open_action is not None
                and 0 < (day - open_action["record_date"]).days <= _HINT_DAYS
            ):
                open_action["hints"].append(day)
            prev_s, prev_u = share, cash
            continue
        if open_action is not None:
            actions.append(_freeze_action(open_action))
        open_action = {
            "record_date": day,
            "share_ratio": ratio if share_changed else 1.0,
            "cash_div": delta if cash_changed else 0.0,
            "hints": [],
        }
        prev_s, prev_u = share, cash
    if open_action is not None:
        actions.append(_freeze_action(open_action))
    return actions


def event_ex_factor(
    prev_close: float | None,
    share_ratio: float,
    cash_div: float,
) -> float | None:
    """单次事件的前收 / 除权价。算不出时返回 None，调用方不得用 1 填充。"""
    if share_ratio <= 0:
        return None
    if cash_div <= _CASH_EPS:
        if abs(share_ratio - 1.0) <= _RATIO_EPS:
            return None
        return share_ratio
    if prev_close is None or prev_close <= 0:
        return None
    post = prev_close / share_ratio - cash_div
    if post <= 0:
        return None
    return prev_close / post


def resolve_ex_date(action: EtfCorporateAction, closes: dict[date, float]) -> date:
    """份额折算对到价格跳空日；纯现金分红保持新浪登记日。"""
    if abs(action.share_ratio - 1.0) < _SHARE_MOVE:
        return action.record_date
    candidates: list[date] = [action.record_date, *action.hints]
    limit = action.record_date + timedelta(days=_HINT_DAYS)
    for day in closes:
        if action.record_date < day <= limit:
            candidates.append(day)
    seen: list[date] = []
    for day in candidates:
        if day not in seen:
            seen.append(day)
    scored: list[tuple[float, date]] = []
    for day in seen:
        prev = previous_close(closes, day)
        close = closes.get(day)
        theo = event_ex_factor(prev, action.share_ratio, action.cash_div)
        err = _relative_error(prev, close, theo)
        if err is not None:
            scored.append((err, day))
    if not scored:
        return action.record_date
    scored.sort(key=lambda item: item[0])
    best_err, best_day = scored[0]
    action_err = next((err for err, day in scored if day == action.record_date), None)
    if (
        best_day != action.record_date
        and best_err < _DATE_MATCH
        and (action_err is None or action_err > best_err + 0.01)
    ):
        return best_day
    return action.record_date


def previous_close(closes: dict[date, float], day: date) -> float | None:
    earlier = [item for item in closes if item < day]
    if not earlier:
        return None
    return closes[max(earlier)]


def factor_rows(
    symbol: str,
    actions: list[EtfCorporateAction],
    closes: dict[date, float],
) -> list[dict]:
    """一个标的的事件行。同一除权日的多笔行为连乘，避免 unique 丢掉一笔。"""
    pending: list[dict] = []
    for action in actions:
        ex_date = resolve_ex_date(action, closes)
        prev = previous_close(closes, ex_date)
        factor = event_ex_factor(prev, action.share_ratio, action.cash_div)
        if factor is None or factor <= 0 or abs(factor - 1.0) <= _RATIO_EPS:
            if action.cash_div > _CASH_EPS and factor is None:
                logger.warning(
                    "ETF 除权因子跳过 %s %s: 没有前收盘，现金分红 %.6f 无法定价",
                    symbol, action.record_date, action.cash_div,
                )
            continue
        pending.append({
            "symbol": symbol,
            "trade_date": ex_date,
            "ex_factor": factor,
            "dividend": action.cash_div,
            "prev_close": prev,
        })
    collapsed: dict[date, dict] = {}
    for row in pending:
        day = row["trade_date"]
        current = collapsed.get(day)
        if current is None:
            collapsed[day] = row
            continue
        current["ex_factor"] = float(current["ex_factor"]) * float(row["ex_factor"])
        current["dividend"] = float(current["dividend"]) + float(row["dividend"])
        if row["prev_close"] is not None:
            current["prev_close"] = row["prev_close"]
    return [collapsed[day] for day in sorted(collapsed)]


def rows_frame(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return _empty_factors()
    frame = pl.DataFrame(rows).with_columns(
        pl.col("trade_date").cast(pl.Date),
        pl.col("ex_factor").cast(pl.Float64),
        pl.col("dividend").cast(pl.Float64),
        pl.col("prev_close").cast(pl.Float64),
    )
    return _align_factors(frame)


def load_local_closes(
    data_dir: Path,
    symbols: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, dict[date, float]]:
    """本地 ETF 日 K 的原始收盘价。没有目录或读失败时返回空，不把缺价写成 0。

    ``start`` / ``end`` 把扫描限制在事件窗口内。调用方应按小批量传入 ``symbols``，
    避免一次把全部 ETF 的收盘价放进 Python dict。
    """
    root = Path(data_dir) / "kline_etf_daily"
    if not symbols or not root.is_dir():
        return {}
    paths = sorted(root.glob("date=*/part.parquet"))
    if not paths:
        return {}
    try:
        lazy = (
            pl.scan_parquet([path.as_posix() for path in paths])
            .select("symbol", "date", "close")
            .filter(pl.col("symbol").is_in(symbols))
        )
        if start is not None:
            lazy = lazy.filter(pl.col("date") >= start)
        if end is not None:
            lazy = lazy.filter(pl.col("date") <= end)
        frame = lazy.collect()
    except (OSError, pl.exceptions.PolarsError) as exc:
        logger.warning("读取本地 ETF 日K收盘失败: %s", exc)
        return {}
    found: dict[str, dict[date, float]] = {}
    for symbol, raw_day, close in frame.iter_rows():
        if symbol is None or close is None:
            continue
        price = float(close)
        if price <= 0:
            continue
        day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day)[:10])
        found.setdefault(str(symbol), {})[day] = price
    return found


def plan_replace(
    path: Path,
    new_rows: pl.DataFrame,
    symbols: list[str],
) -> tuple[pl.DataFrame | None, list[str]]:
    """按标的整表替换。返回 (新全表, 发生变化的标的)。无变化时不返回表。

    成功重建但没有事件的标的会删掉旧行（幻影事件）。不在 ``symbols`` 里的标的保留。
    """
    wanted = list(dict.fromkeys(symbols))
    if not wanted:
        return None, []
    existing = _read_store(path)
    incoming = _align_factors(new_rows)
    old_part = existing.filter(pl.col("symbol").is_in(wanted))
    kept = existing.filter(~pl.col("symbol").is_in(wanted))
    merged = (
        pl.concat([kept, incoming], how="diagonal_relaxed")
        .unique(subset=["symbol", "trade_date"], keep="last")
        .sort(["symbol", "trade_date"])
    )
    affected = [
        symbol for symbol in wanted
        if not _same_events(
            old_part.filter(pl.col("symbol") == symbol),
            incoming.filter(pl.col("symbol") == symbol),
        )
    ]
    if not affected:
        return None, []
    return merged, affected


def write_planned(path: Path, merged: pl.DataFrame) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(merged, path)
    return merged.height


def collect_etf_events(
    symbols: list[str],
    data_dir: Path,
    on_chunk_done: Callable[[int, int], None] | None = None,
    *,
    fetch_text: FetchText | None = None,
    fetch_prices: FetchPrices | None = None,
    remote_prices: bool = False,
    sleep_s: float = 0.05,
    close_batch: int = _CLOSE_BATCH,
    price_floor: date | None = None,
) -> tuple[pl.DataFrame, list[str], list[str]]:
    """拉取并计算事件，不写盘。返回 (事件表, 成功标的, 失败标的)。

    新浪 ``data`` 为空、或原始收盘不足以定价时，该标的算失败，调用方必须保留旧行。
    默认不请求腾讯。``fetch_prices`` 或 ``remote_prices=True`` 才会补拉，
    补拉抛错或仍然缺价同样算失败，不能把登记日或丢掉的现金分红写成成功。
    本地收盘按 ``close_batch`` 只标的分批读入，用完即丢。

    ``price_floor`` 是本地日 K 起点。定时同步传入它：只为起点当天及之后的事件
    要价。更早的事件沿用已存储的行，没有存过就跳过，不因此让整只代码失败。
    """
    if not symbols:
        return _empty_factors(), [], []
    text_fn = fetch_text or fetch_sina_text
    if fetch_prices is not None:
        price_fn: FetchPrices | None = fetch_prices
    elif remote_prices:
        price_fn = fetch_tencent_range
    else:
        price_fn = None
    parsed: list[tuple[str, list[EtfCorporateAction]]] = []
    failed: list[str] = []
    for index, symbol in enumerate(symbols):
        try:
            rows = parse_sina_hfq_payload(text_fn(symbol))
            if not rows:
                raise ValueError("sina hfq.js data 为空, 保留旧因子")
            parsed.append((symbol, corporate_actions(rows)))
        except Exception as exc:  # noqa: BLE001
            failed.append(symbol)
            logger.warning("ETF 除权因子 %s 拉取失败, 保留旧值: %s", symbol, exc)
        if sleep_s and index + 1 < len(symbols):
            time.sleep(sleep_s)
        if on_chunk_done:
            on_chunk_done(index + 1, len(symbols))

    frames: list[pl.DataFrame] = []
    replaced: list[str] = []
    stored = _read_store(_store_path(data_dir)) if price_floor is not None else None
    for batch in _chunks(parsed, close_batch):
        priced = [
            (symbol, [action for action in actions if _reaches_price_floor(action, price_floor)])
            for symbol, actions in batch
        ]
        needing = [(symbol, actions) for symbol, actions in priced if actions]
        local: dict[str, dict[date, float]] = {}
        if needing:
            windows = [_price_window(actions) for _, actions in needing]
            local = load_local_closes(
                data_dir,
                [symbol for symbol, _ in needing],
                start=min(window[0] for window in windows),
                end=max(window[1] for window in windows),
            )
        for symbol, actions in priced:
            try:
                closes = _closes_for_actions(
                    symbol, actions, local.get(symbol, {}), price_fn,
                )
                rows = factor_rows(symbol, actions, closes)
                if price_floor is not None and stored is not None:
                    rows = _with_preserved_rows(stored, symbol, price_floor, rows)
                frames.append(rows_frame(rows))
                replaced.append(symbol)
            except Exception as exc:  # noqa: BLE001
                failed.append(symbol)
                logger.warning("ETF 除权因子 %s 定价失败, 保留旧值: %s", symbol, exc)
        del local
    if not frames:
        incoming = _empty_factors()
    elif len(frames) == 1:
        incoming = frames[0]
    else:
        incoming = pl.concat(frames, how="diagonal_relaxed")
    return incoming, replaced, failed


def sync_etf_events(
    symbols: list[str],
    data_dir: Path,
    on_chunk_done: Callable[[int, int], None] | None = None,
    *,
    fetch_text: FetchText | None = None,
    fetch_prices: FetchPrices | None = None,
    remote_prices: bool = False,
    sleep_s: float = 0.05,
    close_batch: int = _CLOSE_BATCH,
) -> tuple[int, list[str]]:
    """拉新浪全历史并替换这些标的的 ``adj_factor_etf``。

    返回 (新表行数, 因子发生变化的 symbol)。拉取或定价失败的标的保留旧行。
    全部失败时不写文件。默认不请求腾讯。``sleep_s`` 只加在请求之间。
    """
    incoming, replaced, failed = collect_etf_events(
        symbols, data_dir, on_chunk_done,
        fetch_text=fetch_text, fetch_prices=fetch_prices,
        remote_prices=remote_prices, sleep_s=sleep_s, close_batch=close_batch,
    )
    outcome = _commit_events(data_dir, incoming, replaced, failed)
    return outcome.rows, outcome.affected


def scheduled_sync(
    symbols: list[str],
    data_dir: Path,
    *,
    today: date | None = None,
    interval_days: int = _FULL_INTERVAL_DAYS,
    on_chunk_done: Callable[[int, int], None] | None = None,
    fetch_text: FetchText | None = None,
    sleep_s: float = 0.05,
    close_batch: int = _CLOSE_BATCH,
) -> EtfAdjSyncOutcome:
    """盘后管道用的轻量同步。不请求腾讯。

    距上次全量不足 ``interval_days`` 个自然日时，只重试上次失败的代码。
    没有待重试的代码就直接跳过：不拉新浪，也不把日 K 收盘读进内存。
    ``all.parquet`` 不在时无视间隔，立刻全量，避免清数据后空等一周。
    只为本地日 K 起点及之后的事件要价；更早的事件保留已存行。
    全量或重试结束后把失败代码记下来，成功的代码从重试名单去掉。
    """
    day = today or _today()
    state = _read_state(data_dir)
    full = _full_sync_due(
        state, day, interval_days, store_missing=not _store_path(data_dir).is_file(),
    )
    universe = list(dict.fromkeys(symbols))
    if full:
        chosen = universe
    else:
        known = set(universe)
        retry = state.get("retry")
        if not isinstance(retry, list):
            retry = []
        chosen = [symbol for symbol in retry if symbol in known]
    if not chosen:
        logger.info("ETF 除权因子本周已全量同步, 跳过")
        return EtfAdjSyncOutcome(rows=0, affected=[], failed=[], skipped=True)
    incoming, replaced, failed = collect_etf_events(
        chosen, data_dir, on_chunk_done,
        fetch_text=fetch_text, remote_prices=False, sleep_s=sleep_s,
        close_batch=close_batch, price_floor=local_kline_start(data_dir),
    )
    outcome = _commit_events(data_dir, incoming, replaced, failed)
    # 一次全量如果没有任何事件，也要留下空表。否则「文件不在就全量」会每天重扫。
    # 清理把文件删掉之后，这里的空表也不在，下一次仍然全量。
    if full and not _store_path(data_dir).is_file():
        write_planned(_store_path(data_dir), _empty_factors())
    _write_state(data_dir, {
        "last_full_date": day.isoformat() if full else state.get("last_full_date"),
        "retry": outcome.failed,
    })
    return outcome


def fetch_sina_text(symbol: str, timeout: float = 20) -> str:
    code = to_sina_code(symbol)
    if not code:
        raise ValueError(f"无法识别 ETF 代码: {symbol}")
    url = f"https://finance.sina.com.cn/realstock/company/{code}/hfq.js"
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn/",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_tencent_range(symbol: str, start: date, end: date) -> dict[date, float]:
    """腾讯日 K 原始收盘，按约两年一片。只补本地没有的区间。"""
    found: dict[date, float] = {}
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=700))
        found.update(_fetch_tencent_closes(symbol, cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return found


def discover_etf_symbols(data_dir: Path) -> list[str]:
    """优先 ETF 维表，否则用已存储因子里的代码。"""
    found: set[str] = set()
    inst_dir = Path(data_dir) / "instruments_etf"
    if inst_dir.is_dir():
        for path in sorted(inst_dir.glob("*.parquet")):
            try:
                frame = pl.read_parquet(path, columns=["symbol"])
            except (OSError, pl.exceptions.PolarsError):
                continue
            if "symbol" in frame.columns:
                found.update(str(item) for item in frame["symbol"].to_list() if item)
    if not found:
        store = Path(data_dir) / "adj_factor_etf" / "all.parquet"
        if store.is_file():
            try:
                frame = pl.read_parquet(store, columns=["symbol"])
                found.update(str(item) for item in frame["symbol"].to_list() if item)
            except (OSError, pl.exceptions.PolarsError):
                return []
    return sorted(found)


def _commit_events(
    data_dir: Path,
    incoming: pl.DataFrame,
    replaced: list[str],
    failed: list[str],
) -> EtfAdjSyncOutcome:
    if not replaced:
        return EtfAdjSyncOutcome(rows=0, affected=[], failed=list(failed), skipped=False)
    path = Path(data_dir) / "adj_factor_etf" / "all.parquet"
    merged, affected = plan_replace(path, incoming, replaced)
    if merged is None:
        rows = _read_store(path).height
        return EtfAdjSyncOutcome(rows=rows, affected=[], failed=list(failed), skipped=False)
    written = write_planned(path, merged)
    logger.info(
        "ETF 除权因子已替换: %d 只变化, %d 只失败保留, 表共 %d 行",
        len(affected), len(failed), written,
    )
    return EtfAdjSyncOutcome(
        rows=written, affected=affected, failed=list(failed), skipped=False,
    )


def _reaches_price_floor(action: EtfCorporateAction, floor: date | None) -> bool:
    """事件的登记日或之后 10 天可能落在本地日 K 里时，才要求价格。"""
    if floor is None:
        return True
    horizon = action.record_date + timedelta(days=_HINT_DAYS)
    if action.hints:
        horizon = max(horizon, max(action.hints))
    return horizon >= floor


def _with_preserved_rows(
    stored: pl.DataFrame,
    symbol: str,
    floor: date,
    rows: list[dict],
) -> list[dict]:
    """起点之前的已存行原样留下。同一天以新算出来的事件为准。没存过的早年事件不补。"""
    if stored.is_empty() or "symbol" not in stored.columns:
        return rows
    kept = stored.filter((pl.col("symbol") == symbol) & (pl.col("trade_date") < floor))
    merged = {
        row["trade_date"]: {
            "symbol": symbol,
            "trade_date": row["trade_date"],
            "ex_factor": row["ex_factor"],
            "dividend": row.get("dividend"),
            "prev_close": row.get("prev_close"),
        }
        for row in kept.iter_rows(named=True)
        if row.get("ex_factor") is not None
    }
    for row in rows:
        merged[row["trade_date"]] = row
    return [merged[day] for day in sorted(merged)]


def _price_window(actions: list[EtfCorporateAction]) -> tuple[date, date]:
    start = min(action.record_date for action in actions) - timedelta(days=20)
    end = max(action.record_date for action in actions) + timedelta(days=_HINT_DAYS + 5)
    return start, end


def _chunks(items: list, size: int):
    step = max(1, size)
    for index in range(0, len(items), step):
        yield items[index:index + step]


def _today() -> date:
    from app.market_time import cn_today

    return cn_today()


def clear_sync_state(data_dir: Path) -> None:
    """删掉定时同步水位。数据页清 parquet 时必须调用，否则会空等最多 7 天。"""
    path = _state_path(data_dir)
    path.unlink(missing_ok=True)
    path.with_suffix(".json.tmp").unlink(missing_ok=True)


def local_kline_start(data_dir: Path) -> date | None:
    """本地 ETF 日 K 最早分区日。没有分区时返回 None，定时同步退回「每个事件都要价」。"""
    root = Path(data_dir) / "kline_etf_daily"
    if not root.is_dir():
        return None
    found: list[date] = []
    for path in root.glob("date=*"):
        text = path.name.removeprefix("date=")[:10]
        try:
            found.append(date.fromisoformat(text))
        except ValueError:
            continue
    return min(found) if found else None


def _store_path(data_dir: Path) -> Path:
    return Path(data_dir) / "adj_factor_etf" / "all.parquet"


def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "adj_factor_etf" / _STATE_NAME


def _read_state(data_dir: Path) -> dict:
    path = _state_path(data_dir)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ETF 除权同步状态不可读, 下次按全量处理: %s", exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(data_dir: Path, state: dict) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _full_sync_due(
    state: dict,
    today: date,
    interval_days: int,
    *,
    store_missing: bool = False,
) -> bool:
    if store_missing:
        return True
    raw = state.get("last_full_date")
    if not raw:
        return True
    try:
        last = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return True
    return (today - last).days >= interval_days


def _freeze_action(raw: dict) -> EtfCorporateAction:
    return EtfCorporateAction(
        record_date=raw["record_date"],
        share_ratio=float(raw["share_ratio"]),
        cash_div=float(raw["cash_div"]),
        hints=tuple(raw["hints"]),
    )


def _relative_error(prev: float | None, close: float | None, theo: float | None) -> float | None:
    if (
        prev is None or close is None or theo is None
        or prev <= 0 or close <= 0 or theo <= 0
    ):
        return None
    return abs(prev / close / theo - 1.0)


def _closes_for_actions(
    symbol: str,
    actions: list[EtfCorporateAction],
    local: dict[date, float],
    fetch_prices: FetchPrices | None,
) -> dict[date, float]:
    """补齐定价所需的原始收盘。缺价或远端失败时抛错，调用方保留旧行。"""
    closes = dict(local)
    if not actions or not any(_needs_prices(action, closes) for action in actions):
        return closes
    if fetch_prices is None:
        raise RuntimeError(f"{symbol} 缺少能对上除权日的原始收盘, 保留旧因子")
    missing = [action for action in actions if _needs_prices(action, closes)]
    start = min(action.record_date for action in missing) - timedelta(days=20)
    end = max(action.record_date for action in missing) + timedelta(days=_HINT_DAYS + 5)
    try:
        fetched = fetch_prices(symbol, start, end)
    except Exception as exc:
        raise RuntimeError(f"{symbol} 补拉原始收盘失败, 保留旧因子") from exc
    # 本地日K优先。远端只补缺日，避免另一套收盘把已经对齐的除权日挪走。
    for day, price in fetched.items():
        if day in closes or not price or price <= 0:
            continue
        closes[day] = float(price)
    if any(_needs_prices(action, closes) for action in actions):
        raise RuntimeError(f"{symbol} 补拉原始收盘后仍无法定价, 保留旧因子")
    return closes


def _needs_prices(action: EtfCorporateAction, closes: dict[date, float]) -> bool:
    if action.cash_div > _CASH_EPS and previous_close(closes, action.record_date) is None:
        return True
    if abs(action.share_ratio - 1.0) < _SHARE_MOVE:
        return False
    ex_date = resolve_ex_date(action, closes)
    prev = previous_close(closes, ex_date)
    theo = event_ex_factor(prev, action.share_ratio, action.cash_div)
    err = _relative_error(prev, closes.get(ex_date), theo)
    return err is None or err > _DATE_MATCH


def _fetch_tencent_closes(symbol: str, start: date, end: date) -> dict[date, float]:
    code = to_sina_code(symbol)
    if not code:
        raise ValueError(f"无法识别 ETF 代码: {symbol}")
    param = f"{code},day,{start.isoformat()},{end.isoformat()},800,"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + param
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    node = (payload.get("data") or {}).get(code) or {}
    rows = node.get("day") or []
    found: dict[date, float] = {}
    for row in rows:
        if not row or len(row) < 3:
            continue
        price = float(row[2])
        if price > 0:
            found[date.fromisoformat(str(row[0])[:10])] = price
    return found


def _empty_factors() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "symbol": pl.String,
        "trade_date": pl.Date,
        "ex_factor": pl.Float64,
        "dividend": pl.Float64,
        "bonus": pl.Float64,
        "allot": pl.Float64,
        "allot_price": pl.Float64,
        "prev_close": pl.Float64,
    })


def _align_factors(frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty():
        return _empty_factors()
    if "trade_date" in frame.columns:
        frame = frame.with_columns(pl.col("trade_date").cast(pl.Date, strict=False))
    for column in ("ex_factor", "dividend", "bonus", "allot", "allot_price", "prev_close"):
        if column in frame.columns:
            frame = frame.with_columns(pl.col(column).cast(pl.Float64, strict=False))
    missing = [
        pl.lit(None, dtype=pl.Float64).alias(column)
        for column in FACTOR_COLUMNS
        if column not in frame.columns and column not in ("symbol", "trade_date")
    ]
    if missing:
        frame = frame.with_columns(missing)
    if "symbol" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias("symbol"))
    return frame.select(list(FACTOR_COLUMNS))


def _read_store(path: Path) -> pl.DataFrame:
    if not path.is_file():
        return _empty_factors()
    return _align_factors(pl.read_parquet(path))


def _same_events(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    left_rows = _event_tuples(left)
    right_rows = _event_tuples(right)
    if len(left_rows) != len(right_rows):
        return False
    return all(_tuple_close(old, new) for old, new in zip(left_rows, right_rows, strict=True))


def _event_tuples(frame: pl.DataFrame) -> list[tuple]:
    if frame.is_empty():
        return []
    ordered = frame.sort("trade_date")
    rows = []
    for day, factor, dividend, prev in ordered.select(
        "trade_date", "ex_factor", "dividend", "prev_close",
    ).iter_rows():
        rows.append((day, factor, dividend, prev))
    return rows


def _tuple_close(left: tuple, right: tuple) -> bool:
    if left[0] != right[0]:
        return False
    for old, new in zip(left[1:], right[1:], strict=True):
        if old is None and new is None:
            continue
        if old is None or new is None:
            return False
        if abs(float(old) - float(new)) > _FACTOR_EPS:
            return False
    return True
