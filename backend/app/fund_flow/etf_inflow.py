"""ETF 净流入（亿元）。

份额差乘当日收盘价，再除以 1 亿。领航者稿件的金额本来就是亿元，从
``data/news/news.sqlite`` 的 ``etf_flow`` 按交易日和代码接上，不再读
已经不存在的 ``data/etf_flow`` parquet。

1 日用上一份份额落盘做前值。两份落盘之间如果隔了超过 1 个交易日，这一日
留空，不把缺口写成申赎金额。份额折算的 ``ex_factor`` 记在除权日 E，但交易所
份额在登记日 R（E 的前一个交易日，按交易日历而不是自然日）已经是折算后的
数量，当天收盘价仍是折算前的。价格因子偏离 1 超过 5% 只是候选：R 日份额
要真的按份额因子 f 跳变，才在 R 还原真实申赎。现金分红不改份额基数，
159207、515100 这种大额分红即使价格因子超过 5% 也不还原。E 的份额和价格
都在折算后，按普通份额差计算，不再留空。
5 日 / 20 日是这些 1 日值的滚动合计，窗口按份额落盘份数往后数，和份额
变化用的「前 N 份落盘」同一套锚点。窗口里有任何一天算不出，合计就留空。
"""
from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_left
from datetime import datetime
from itertools import pairwise
from pathlib import Path

import polars as pl

from app.fund_flow import normalize, store
from app.market_time import CN_TZ

# 5 日、20 日与领航者稿的 net_5d / net_20d 对齐。1 日是滚动的基本步长。
INFLOW_WINDOWS = (1, 5, 20)
# 价格因子偏离 1 达到这个幅度，才成为折算候选。现金分红的价格因子可以
# 更大（159207 约 5.1%，515100 约 7.3%），不能单靠这个阈值还原。
_SPLIT_FACTOR_GAP = 0.05
# 登记日残差 |份额_R − 份额_{R−1}×f| 不超过折算本身带来的份额变动的这个比例，
# 才确认份额按 f 跳了。159537 同日申购的残差约占 0.52；515100 分红约占 0.87。
_REBASE_SLACK = 0.65
# 幅度小于这档时，一笔取整的申赎可能在数值上刚好等于价格因子。这种份额差
# 落在 10 万份申赎单位上，仍是申购或赎回，不是折算。159207 的 +1400 万份就是。
_AMBIGUOUS_FACTOR = 0.15
_CREATION_UNIT = 100_000
_CASH_EPS = 1e-6
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
    factor: float | None = None,
) -> float | None:
    """(份额_T − 份额_prev) × 收盘价_T / 1e8。单位亿元。

    ``adjacent`` 为假表示两份落盘之间隔了超过 1 个交易日，留空。
    ``factor`` 是登记日的份额折算因子（新份额 / 旧份额）。这一天份额已经
    折算、收盘价还没有，真实申赎是
    (份额_T − 份额_prev × f) × 收盘价_T / f。和
    (份额_T / f − 份额_prev) × 收盘价_T 相同。f > 1 是拆分，f < 1 是缩股。
    ``split`` 仍表示调用方要求这一步留空，登记日还原不走这条路径。
    """
    if not adjacent or split:
        return None
    shares_now = _finite(shares_t)
    shares_then = _finite(shares_prev)
    price = _finite(close_t)
    ratio = _finite(factor) if factor is not None else None
    if shares_now is None or shares_then is None or price is None or price <= 0:
        return None
    if ratio is not None:
        if ratio <= 0:
            return None
        return (shares_now - shares_then * ratio) * price / ratio / 1e8
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
    # 因子记在除权日，还原落在前一个交易日。日历用全量日 K，不限于份额窗口，
    # 这样回看登记日时除权日已经在日历里，也能找到 R。
    adjustments = _registration_adjustments(events, calendar)
    codes = list(shares.get(window[-1], {}))
    if not codes:
        return {}
    steps_meta: list[tuple[str, str, bool]] = []
    for prev, current in pairwise(window):
        prev_i = index.get(prev)
        cur_i = index.get(current)
        adjacent = prev_i is not None and cur_i is not None and (cur_i - prev_i) == 1
        steps_meta.append((prev, current, adjacent))
    found: dict[str, dict[str, float | None]] = {}
    for code in codes:
        series: list[float | None] = []
        code_factors = adjustments.get(code, {})
        for prev, current, adjacent in steps_meta:
            factor = code_factors.get(current) if adjacent else None
            if factor is not None and not _shares_rebased(
                shares.get(prev, {}).get(code),
                shares.get(current, {}).get(code),
                factor,
            ):
                factor = None
            series.append(step_inflow_yi(
                shares.get(current, {}).get(code),
                shares.get(prev, {}).get(code),
                closes.get(current, {}).get(code),
                adjacent=adjacent,
                split=False,
                factor=factor,
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
    """``adj_factor_etf`` 里达到折算幅度的份额因子，键是除权日。

    纯折算时份额因子就是 ``ex_factor``。同一天还有现金分红时，价格因子不等于
    份额因子，用前收和分红还原新份额 / 旧份额。同一除权日重复出现时保留最后一行。
    """
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
    columns = [symbol_col, date_col, "ex_factor"]
    has_dividend = "dividend" in frame.columns
    has_prev = "prev_close" in frame.columns
    if has_dividend:
        columns.append("dividend")
    if has_prev:
        columns.append("prev_close")
    grouped: dict[str, dict[str, float]] = {}
    for row in frame.select(columns).iter_rows(named=True):
        code = _code(row[symbol_col])
        day = _iso_day(row[date_col])
        value = _conversion_factor(
            row["ex_factor"],
            row["dividend"] if has_dividend else None,
            row["prev_close"] if has_prev else None,
        )
        if code and day and value is not None and abs(value - 1.0) >= _SPLIT_FACTOR_GAP:
            grouped.setdefault(code, {})[day] = value
    return {
        code: sorted(days.items())
        for code, days in grouped.items()
    }


def _conversion_factor(ex_factor: object, dividend: object, prev_close: object) -> float | None:
    """份额折算比。没有现金分红时就是 ``ex_factor``。"""
    factor = _finite(ex_factor)
    if factor is None or factor <= 0:
        return None
    cash = _finite(dividend)
    prev = _finite(prev_close)
    if cash is None or cash <= _CASH_EPS or prev is None or prev <= 0:
        return factor
    base = prev / factor + cash
    if base <= 0:
        return factor
    return prev / base


def _previous_trading_day(calendar: list[str], day: str) -> str | None:
    """``day`` 之前的最近一个交易日。``day`` 本身不在日历里时同样往前找。"""
    if not calendar:
        return None
    pos = bisect_left(calendar, day)
    if pos == 0:
        return None
    return calendar[pos - 1]


def _registration_adjustments(
    events: dict[str, list[tuple[str, float]]],
    calendar: list[str],
) -> dict[str, dict[str, float]]:
    """除权日 E → 登记日 R 上的份额因子。R 是 E 的前一个交易日。

    自然日减一天会把周五的登记日错到周日，周末后的除权日就还原不到份额跳变。
    """
    found: dict[str, dict[str, float]] = {}
    for code, rows in events.items():
        for ex_day, factor in rows:
            record = _previous_trading_day(calendar, ex_day)
            if record is None or factor <= 0:
                continue
            found.setdefault(code, {})[record] = factor
    return found


def confirmed_rebase_factors(
    data_dir: Path,
    *,
    after: str,
    on_or_before: str,
) -> dict[str, list[tuple[str, float]]]:
    """已确认的登记日份额因子，供份额变化换算成当前份额口径。

    返回 ``code → [(登记日, f), ...]``，按登记日升序。只收录
    ``after < 登记日 <= on_or_before``。因子记在除权日，这里挪到前一交易日；
    登记日及其前一交易日的份额还要通过 ``_shares_rebased``。现金分红、
    份额没按 f 跳变、或这两天缺份额时不返回，调用方保持原始相减。
    """
    if not after or not on_or_before or after >= on_or_before:
        return {}
    calendar = _trading_calendar(data_dir)
    adjustments = _registration_adjustments(_split_events(data_dir), calendar)
    pending: list[tuple[str, str, str, float]] = []
    needed: set[str] = set()
    for code, rows in adjustments.items():
        for record_day, factor in rows.items():
            if not (after < record_day <= on_or_before):
                continue
            prev = _previous_trading_day(calendar, record_day)
            if prev is None:
                continue
            pending.append((code, record_day, prev, factor))
            needed.add(record_day)
            needed.add(prev)
    shares = {day: _share_map(data_dir, day) for day in needed}
    found: dict[str, list[tuple[str, float]]] = {}
    for code, record_day, prev, factor in pending:
        if not _shares_rebased(
            shares.get(prev, {}).get(code),
            shares.get(record_day, {}).get(code),
            factor,
        ):
            continue
        found.setdefault(code, []).append((record_day, factor))
    for rows in found.values():
        rows.sort()
    return found


def _shares_rebased(shares_prev: object, shares_now: object, factor: float) -> bool:
    """登记日份额是否真的按因子跳变。份额缺失时不还原。

    现金分红只改价格。残差相对折算份额变动超过 ``_REBASE_SLACK`` 的，份额
    没有按 f 重定基数。幅度较小的因子还有一种碰巧：当天申购份数正好等于
    前一日份额 × (f−1)，残差接近 0，看起来像一次干净的折算。申赎按 10 万份
    取整，这种整段份额差仍是申购，不还原。2 倍及以上的折算不走这条例外，
    同日大额申购（159537 等）的残差留在折算结果里。
    """
    before = _finite(shares_prev)
    after = _finite(shares_now)
    ratio = _finite(factor)
    if before is None or after is None or ratio is None or ratio <= 0 or before == 0:
        return False
    moved = before * (ratio - 1.0)
    if moved == 0:
        return False
    if abs(after - before * ratio) > abs(moved) * _REBASE_SLACK:
        return False
    on_grid = abs(ratio - 1.0) < _AMBIGUOUS_FACTOR and _on_creation_grid(after - before)
    return not on_grid


def _on_creation_grid(delta: float) -> bool:
    """份额差是否落在 10 万份申赎单位上，含不变。允许 1 份舍入。"""
    units = round(delta / _CREATION_UNIT)
    return abs(delta - units * _CREATION_UNIT) <= 1.0


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
