"""ETF 净流入（亿元）。

份额差乘当日收盘价，再除以 1 亿。领航者稿件的金额本来就是亿元，从
``data/news/news.sqlite`` 的 ``etf_flow`` 按交易日和代码接上，不再读
已经不存在的 ``data/etf_flow`` parquet。

1 日用上一份份额落盘做前值。两份落盘之间如果隔了超过 1 个交易日，或者
复权因子出现拆分级别的跳变，这一日留空，不把缺口或拆分写成申赎金额。
5 日 / 20 日是这些 1 日值的滚动合计，窗口按份额落盘份数往后数，和份额
变化用的「前 N 份落盘」同一套锚点。窗口里有任何一天算不出，合计就留空。
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from itertools import pairwise
from pathlib import Path

import polars as pl

from app.fund_flow import normalize, store
from app.market_time import CN_TZ

# 5 日、20 日与领航者稿的 net_5d / net_20d 对齐。1 日是滚动的基本步长。
INFLOW_WINDOWS = (1, 5, 20)
# 拆分会让份额和价格一起跳。事件因子偏离 1 达到这个幅度就不当成申赎。
# 现金分红的因子贴近 1，不在这里剔除。
_SPLIT_FACTOR_GAP = 0.05
_DIVERGE_RATIO = 0.20
_FLOW_KEYS = ("net_1d", "net_5d", "net_20d")
_ISO_DAY = 10


def step_inflow_yi(
    shares_t: object,
    shares_prev: object,
    close_t: object,
    *,
    adjacent: bool,
    split: bool,
) -> float | None:
    """(份额_T − 份额_prev) × 收盘价_T / 1e8。单位亿元。

    ``adjacent`` 为假表示两份落盘之间隔了超过 1 个交易日。``split`` 为真表示
    这一步跨过拆分。两种情况都留空，不用 0 填充。
    """
    if not adjacent or split:
        return None
    shares_now = _finite(shares_t)
    shares_then = _finite(shares_prev)
    price = _finite(close_t)
    if shares_now is None or shares_then is None or price is None or price <= 0:
        return None
    return (shares_now - shares_then) * price / 1e8


def rolling_inflow(steps: list[float | None], window: int) -> float | None:
    """最近 ``window`` 个落盘步长的合计。步数不够或其中有空值时返回 None。"""
    if window < 1 or len(steps) < window:
        return None
    chunk = [value for value in steps[-window:] if value is not None]
    if len(chunk) != window:
        return None
    return float(sum(chunk))


def flows_diverged(
    computed: object,
    article: object,
    *,
    ratio: float = _DIVERGE_RATIO,
) -> bool:
    """|a−b| / max(|a|,|b|) 超过 ``ratio`` 时为真。缺一边或两边都是 0 时不标记。"""
    left = _finite(computed)
    right = _finite(article)
    if left is None or right is None:
        return False
    scale = max(abs(left), abs(right))
    if scale == 0:
        return False
    return abs(left - right) / scale > ratio


def latest_inflows(
    data_dir: Path,
    *,
    as_of: str | None = None,
) -> dict[str, dict[str, float | None]]:
    """截至 ``as_of``（默认最新份额日）每只 ETF 的 1/5/20 日净流入，单位亿元。

    只给截至日仍有份额的代码。算不出的窗口不出现在内层字典里时，调用方按空处理。
    """
    dates = store.list_dates(data_dir, "etf_shares")
    if as_of is not None:
        dates = [day for day in dates if day <= as_of]
    needed = max(INFLOW_WINDOWS) + 1
    if len(dates) < 2:
        return {}
    window = dates[-needed:]
    calendar = _trading_calendar(data_dir)
    index = {day: pos for pos, day in enumerate(calendar)}
    shares = {day: _share_map(data_dir, day) for day in window}
    closes = {day: _close_map(data_dir, day) for day in window[1:]}
    events = _split_events(data_dir)
    codes = list(shares.get(window[-1], {}))
    if not codes:
        return {}
    steps_meta: list[tuple[str, str, bool, set[str]]] = []
    for prev, current in pairwise(window):
        prev_i = index.get(prev)
        cur_i = index.get(current)
        adjacent = prev_i is not None and cur_i is not None and (cur_i - prev_i) == 1
        split_codes = _codes_with_split(events, prev, current) if adjacent else set()
        steps_meta.append((prev, current, adjacent, split_codes))
    found: dict[str, dict[str, float | None]] = {}
    for code in codes:
        series: list[float | None] = []
        for prev, current, adjacent, split_codes in steps_meta:
            series.append(step_inflow_yi(
                shares.get(current, {}).get(code),
                shares.get(prev, {}).get(code),
                closes.get(current, {}).get(code),
                adjacent=adjacent,
                split=code in split_codes,
            ))
        values = {
            "net_inflow_1d": rolling_inflow(series, 1),
            "net_inflow_5d": rolling_inflow(series, 5),
            "net_inflow_20d": rolling_inflow(series, 20),
        }
        if any(value is not None for value in values.values()):
            found[code] = values
    return found


def navigator_by_code(data_dir: Path, trade_date: str) -> dict[str, dict[str, float | None]]:
    """领航者稿按交易日 + 6 位代码给出的净流入，单位亿元。

    交易日优先用标题里的月日（发布日用来定年），其次才用抽取结果里的
    ``trade_date``。同一天有多篇时，后入库的非空数字覆盖先入库的。
    """
    path = Path(data_dir) / "news" / "news.sqlite"
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = _navigator_rows(conn, trade_date)
    finally:
        conn.close()
    found: dict[str, dict[str, float | None]] = {}
    for _row_id, title, published_at, raw_text in rows:
        raw = _load_raw(raw_text)
        if raw is None:
            continue
        if _article_day(str(title or ""), published_at, raw) != trade_date:
            continue
        extracted = raw.get("extracted")
        if not isinstance(extracted, dict):
            continue
        for code, numbers in _extracted_flows(extracted).items():
            slot = found.setdefault(code, {key: None for key in _FLOW_KEYS})
            for key in _FLOW_KEYS:
                if numbers[key] is not None:
                    slot[key] = numbers[key]
    return found


def _navigator_rows(conn: sqlite3.Connection, trade_date: str) -> list:
    """只取可能属于这一交易日的稿，避免每次打开资金页扫完全部图片 JSON。"""
    title_like = _title_like(trade_date)
    narrowed = """
        SELECT id, title, published_at, raw_json
        FROM news_items
        WHERE source = 'etf_flow' AND raw_json IS NOT NULL
          AND (
            json_extract(raw_json, '$.extracted.trade_date') = ?
            OR json_extract(raw_json, '$.trade_date') = ?
            OR IFNULL(title, '') LIKE ?
          )
        ORDER BY id ASC
    """
    full = """
        SELECT id, title, published_at, raw_json
        FROM news_items
        WHERE source = 'etf_flow' AND raw_json IS NOT NULL
        ORDER BY id ASC
    """
    try:
        if title_like is None:
            return conn.execute(full).fetchall()
        return conn.execute(narrowed, (trade_date, trade_date, title_like)).fetchall()
    except sqlite3.Error:
        try:
            return conn.execute(full).fetchall()
        except sqlite3.Error:
            return []


def _title_like(trade_date: str) -> str | None:
    if len(trade_date) != _ISO_DAY:
        return None
    try:
        month = int(trade_date[5:7])
        day = int(trade_date[8:10])
    except ValueError:
        return None
    return f"%{month}月{day}日%"


def _article_day(title: str, published_at: object, raw: dict) -> str:
    published = _parse_published(published_at)
    if published is not None and title:
        from app.news.etf_flow import trade_date_from_title

        derived = trade_date_from_title(title, published)
        if derived:
            return derived
    extracted = raw.get("extracted")
    candidates = []
    if isinstance(extracted, dict):
        candidates.append(extracted.get("trade_date"))
    candidates.append(raw.get("trade_date"))
    for value in candidates:
        day = _iso_day(value)
        if day:
            return day
    return ""


def _extracted_flows(extracted: dict) -> dict[str, dict[str, float | None]]:
    found: dict[str, dict[str, float | None]] = {}
    groups = []
    etfs = extracted.get("etfs")
    broad = extracted.get("broad_index")
    if isinstance(etfs, list):
        groups.append(etfs)
    if isinstance(broad, list):
        groups.append(broad)
    for group in groups:
        for row in group:
            if not isinstance(row, dict):
                continue
            code = _code(row.get("code"))
            if not code or code in found:
                continue
            found[code] = {key: _finite(row.get(key)) for key in _FLOW_KEYS}
    return found


def _trading_calendar(data_dir: Path) -> list[str]:
    """本地日 K 分区即交易日历。ETF 与股票两边取覆盖更长的那份。"""
    best: list[str] = []
    for name in ("kline_etf_daily", "kline_daily"):
        days = _partition_dates(Path(data_dir) / name)
        if len(days) > len(best):
            best = days
    return best


def _partition_dates(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    found: list[str] = []
    for path in root.glob("date=*"):
        day = path.name.removeprefix("date=")
        if len(day) == _ISO_DAY and path.is_dir():
            found.append(day)
    return sorted(set(found))


def _share_map(data_dir: Path, day: str) -> dict[str, float]:
    frame = store.read_partition(data_dir, "etf_shares", day)
    out: dict[str, float] = {}
    if frame.is_empty() or "code" not in frame.columns or "shares" not in frame.columns:
        return out
    for code, shares in frame.select("code", "shares").iter_rows():
        number = _finite(shares)
        text = _code(code)
        if text and number is not None:
            out[text] = number
    return out


def _close_map(data_dir: Path, day: str) -> dict[str, float]:
    path = Path(data_dir) / "kline_etf_daily" / f"date={day}" / "part.parquet"
    if not path.is_file():
        return {}
    frame = _read_parquet(path)
    if frame.is_empty() or "close" not in frame.columns:
        return {}
    symbol_col = "symbol" if "symbol" in frame.columns else ("code" if "code" in frame.columns else None)
    if symbol_col is None:
        return {}
    out: dict[str, float] = {}
    for symbol, close in frame.select(symbol_col, "close").iter_rows():
        code = _code(symbol)
        price = _finite(close)
        if code and price is not None and price > 0:
            out[code] = price
    return out


def _split_events(data_dir: Path) -> dict[str, list[tuple[str, float]]]:
    """``adj_factor_etf`` 里的除权事件。因子是单次事件比值，不是累积值。"""
    base = Path(data_dir) / "adj_factor_etf"
    if not base.is_dir():
        return {}
    frames: list[pl.DataFrame] = []
    for path in sorted(base.glob("**/*.parquet")):
        frame = _read_parquet(path)
        if not frame.is_empty():
            frames.append(frame)
    if not frames:
        return {}
    frame = frames[0] if len(frames) == 1 else pl.concat(frames, how="diagonal_relaxed")
    if "ex_factor" not in frame.columns:
        return {}
    symbol_col = "symbol" if "symbol" in frame.columns else ("code" if "code" in frame.columns else None)
    date_col = next((name for name in ("trade_date", "date") if name in frame.columns), None)
    if symbol_col is None or date_col is None:
        return {}
    grouped: dict[str, list[tuple[str, float]]] = {}
    for symbol, raw_day, factor in frame.select(symbol_col, date_col, "ex_factor").iter_rows():
        code = _code(symbol)
        day = _iso_day(raw_day)
        value = _finite(factor)
        if code and day and value is not None:
            grouped.setdefault(code, []).append((day, value))
    for rows in grouped.values():
        rows.sort(key=lambda item: item[0])
    return grouped


def _codes_with_split(
    events: dict[str, list[tuple[str, float]]],
    prev: str,
    current: str,
) -> set[str]:
    found: set[str] = set()
    for code, rows in events.items():
        for day, factor in rows:
            if day <= prev:
                continue
            if day > current:
                break
            if abs(factor - 1.0) >= _SPLIT_FACTOR_GAP:
                found.add(code)
                break
    return found


def _read_parquet(path: Path) -> pl.DataFrame:
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError):
        return pl.DataFrame()


def _load_raw(raw_text: object) -> dict | None:
    if not isinstance(raw_text, str) or not raw_text:
        return None
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _parse_published(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN_TZ)
    return parsed


def _code(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        text = f"{value:06d}" if 0 <= value <= 999999 else str(value)
    elif isinstance(value, float) and math.isfinite(value) and value == int(value):
        number = int(value)
        text = f"{number:06d}" if 0 <= number <= 999999 else str(number)
    else:
        text = str(value)
    return normalize.code6(text)


def _iso_day(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = value.isoformat() if hasattr(value, "isoformat") else str(value).strip()
    if len(text) >= _ISO_DAY and text[4] == "-" and text[7] == "-":
        return text[:_ISO_DAY]
    return ""


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("，", "").replace("+", "")
        if not text or text in {"-", "—", "null", "None", "nan"}:
            return None
        value = text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number
