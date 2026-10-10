"""热门事件的盘面验证。只使用首见时刻之后的行情和资金，避免用到消息出来之前的涨跌。

异动沿用推送里的涨跌停、炸板和新高新低判断。力度看超额涨跌、上涨家数占比和主力净流入。
持续度看首见之后有几个分时窗口或交易日仍顺着事件方向走。
周末和盘后没有当时的行情时，改用最近一个交易日，并在结果里标明。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from app.market_time import CN_TZ, current_trading_day, in_continuous_session
from app.news.push import abnormal_signals

logger = logging.getLogger(__name__)

_INDEX = "000300.SH"
_MOVE_MAX = 30
_STRENGTH_MAX = 30
_PERSIST_MAX = 20
_CONFIRM_MAX = _MOVE_MAX + _STRENGTH_MAX + _PERSIST_MAX
_WINDOWS = (
    (dt_time(9, 30), dt_time(10, 30)),
    (dt_time(10, 30), dt_time(11, 30)),
    (dt_time(13, 0), dt_time(14, 0)),
    (dt_time(14, 0), dt_time(15, 1)),
)
_BULL_SIGNALS = frozenset({"limit_up", "new_high", "broken"})
_BEAR_SIGNALS = frozenset({"limit_down", "new_low"})


def latest_session_day(now: datetime) -> date:
    """周末、盘前用上一个交易日；盘中和收盘后用当天。"""
    local = now.astimezone(CN_TZ)
    if local.weekday() >= 5 or local.time() < dt_time(9, 30):
        day = local.date()
        if local.weekday() < 5 and local.time() < dt_time(9, 30):
            day -= timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return day
    return current_trading_day(local)


def attach_confirmations(events: list[dict], now: datetime) -> None:
    tape = load_market_tape(now, events)
    for event in events:
        event["confirmation"] = score_confirmation(event, tape, now)


def load_market_tape(now: datetime, events: list[dict]) -> dict:
    try:
        return _load_market_tape(now, events)
    except Exception:
        logger.exception("盘面验证读取失败")
        session = latest_session_day(now)
        return _blank_tape(session, live=in_continuous_session(now))


def score_confirmation(event: dict, tape: dict, now: datetime) -> dict:
    """给一条事件打盘面分。tape 里早于首见的 bar 和资金不算。"""
    first = _first_at(event)
    session = _session_date(tape)
    live = bool(tape.get("live")) and in_continuous_session(now)
    if first is None:
        return _result(0, False, "无", "无", _session_text(session, live, waited=False), session, live, {})
    if session is not None and first.date() > session and not live:
        return _result(
            0, False, "无", "无", "尚无首见之后的盘面", session, False, {},
        )
    symbols = _event_symbols(event, tape)
    direction = str(event.get("direction") or "利好")
    bull = direction != "利空"
    stock_rows = []
    for symbol in symbols:
        row = (tape.get("stocks") or {}).get(symbol)
        if isinstance(row, dict):
            stock_rows.append((symbol, row))
    index_bars = [bar for bar in (tape.get("index_bars") or []) if _usable(bar, first)]
    flows = _concept_flows(event, tape, first)
    nets = _stock_flows(stock_rows, first)
    if not stock_rows and not flows:
        waited = _after_session(first, session)
        return _result(
            0, False, "无", "无", _session_text(session, live, waited=waited), session, live and not waited, {},
        )
    returns = []
    limit_count = 0
    abnormal = False
    volumes: list[float] = []
    baselines: list[float] = []
    minutes: list[float] = []
    for symbol, row in stock_rows:
        bars = [bar for bar in (row.get("bars") or []) if _usable(bar, first)]
        if not bars:
            continue
        ret = _return_since(bars, row.get("prev_close"), first)
        if ret is not None:
            returns.append(ret)
        signals = _signals(symbol, row, bars, first)
        wanted = _BULL_SIGNALS if bull else _BEAR_SIGNALS
        if signals & wanted:
            abnormal = True
        if "limit_up" in signals and bull:
            limit_count += 1
        if "limit_down" in signals and not bull:
            limit_count += 1
        volume, minute = _volume_span(bars)
        baseline = _num(row.get("baseline_volume"))
        if volume and baseline and baseline > 0 and minute > 0:
            volumes.append(volume)
            baselines.append(baseline)
            minutes.append(minute)
    index_ret = _series_return(index_bars)
    basket = sum(returns) / len(returns) if returns else 0.0
    excess = (basket - index_ret) if bull else (index_ret - basket)
    breadth = 0.0
    if returns:
        good = [item for item in returns if (item > 0) == bull and abs(item) >= 0.001]
        breadth = len(good) / len(returns)
    vol_ratio = None
    if volumes:
        expected = sum(base * minute / 240.0 for base, minute in zip(baselines, minutes, strict=True))
        if expected > 0:
            vol_ratio = sum(volumes) / expected
    sector_net = sum(flows) if flows else None
    main_net = sum(nets) if nets else None
    move = _move_points(excess, vol_ratio, limit_count, sector_net, bull)
    strength_points = _strength_points(excess, breadth, main_net, bull)
    hits, observed = _persistence(stock_rows, first, bull)
    persist_points = 20 if hits >= 3 else 14 if hits >= 2 else 6 if hits == 1 else 0
    if move > 0 or limit_count or (vol_ratio or 0) >= 2 or _flow_confirms(sector_net, bull):
        abnormal = True
    if _after_session(first, session) and not returns and not flows and not nets:
        return _result(0, False, "无", "无", "尚无首见之后的盘面", session, False, {})
    total = min(_CONFIRM_MAX, move + strength_points + persist_points)
    strength = "强" if strength_points >= 20 else "中" if strength_points >= 10 else "弱" if strength_points > 0 else "无"
    persistence = "持续" if hits >= 2 else "短暂" if hits == 1 else "无"
    detail = {
        "excess_pct": round(excess, 4),
        "breadth": round(breadth, 4),
        "limit_count": limit_count,
        "vol_ratio": round(vol_ratio, 4) if vol_ratio is not None else None,
        "main_net": round(main_net, 2) if main_net is not None else None,
        "sector_net_inflow": round(sector_net, 2) if sector_net is not None else None,
        "windows": observed,
        "windows_hit": hits,
    }
    return _result(
        total, abnormal, strength, persistence,
        _session_text(session, live, waited=False), session, live, detail,
    )


def _load_market_tape(now: datetime, events: list[dict]) -> dict:
    from app.config import settings

    data_dir = Path(settings.data_dir)
    session = latest_session_day(now)
    live = in_continuous_session(now)
    concepts = sorted({
        str(name).strip()
        for event in events
        for name in (event.get("concepts") or [])
        if str(name).strip()
    })
    members = _members(data_dir, concepts)
    symbols = sorted(_all_symbols(events, members))
    stocks: dict[str, dict] = {}
    for symbol in symbols:
        stocks[symbol] = {"bars": [], "flows": []}
    _fill_daily(data_dir, session, stocks)
    _fill_minutes(data_dir, session, stocks)
    _fill_stock_flow(data_dir, session, stocks)
    index_bars = _index_bars(data_dir, session)
    concept_flows = _concept_flow_map(data_dir, session, concepts)
    return {
        "session": session.isoformat(),
        "live": live,
        "members": members,
        "index_bars": index_bars,
        "stocks": stocks,
        "concepts": concept_flows,
    }


def _blank_tape(session: date, *, live: bool) -> dict:
    return {
        "session": session.isoformat(),
        "live": live,
        "members": {},
        "index_bars": [],
        "stocks": {},
        "concepts": {},
    }


def _result(
    score: float,
    abnormal: bool,
    strength: str,
    persistence: str,
    label: str,
    session: date | None,
    live: bool,
    detail: dict,
) -> dict:
    return {
        "label": label,
        "session": session.isoformat() if session else None,
        "live": live,
        "score": round(min(_CONFIRM_MAX, max(0.0, score)), 4),
        "abnormal": abnormal,
        "strength": strength,
        "persistence": persistence,
        "detail": {
            "excess_pct": detail.get("excess_pct"),
            "breadth": detail.get("breadth"),
            "limit_count": int(detail.get("limit_count") or 0),
            "vol_ratio": detail.get("vol_ratio"),
            "main_net": detail.get("main_net"),
            "sector_net_inflow": detail.get("sector_net_inflow"),
            "windows": int(detail.get("windows") or 0),
            "windows_hit": int(detail.get("windows_hit") or 0),
        },
    }


def _after_session(first: datetime, session: date | None) -> bool:
    if session is None:
        return False
    return first > datetime.combine(session, dt_time(15, 0), CN_TZ)


def _session_text(session: date | None, live: bool, *, waited: bool) -> str:
    if waited:
        return "尚无首见之后的盘面"
    if live:
        return "盘中"
    if session is None:
        return "暂无行情"
    return f"最近交易日 {session.month}月{session.day}日"


def _session_date(tape: dict) -> date | None:
    raw = tape.get("session")
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _first_at(event: dict) -> datetime | None:
    first = event.get("_first")
    parsed = _parse_at(first)
    if parsed is not None:
        return parsed
    return None


def _event_symbols(event: dict, tape: dict) -> list[str]:
    from app.news.service import asset_kind_of

    found: list[str] = []
    for stock in event.get("mentioned_stocks") or []:
        if not isinstance(stock, dict):
            continue
        key = str(stock.get("key") or "").strip()
        name = str(stock.get("name") or key)
        if not key or asset_kind_of(key, name) == "etf":
            continue
        if key not in found:
            found.append(key)
    members = tape.get("members") or {}
    for concept in event.get("concepts") or []:
        for symbol in members.get(str(concept)) or []:
            text = str(symbol).strip()
            if text and text not in found and asset_kind_of(text, text) != "etf":
                found.append(text)
    return found[:40]


def _all_symbols(events: list[dict], members: dict[str, list[str]]) -> set[str]:
    from app.news.service import asset_kind_of

    found: set[str] = set()
    for event in events:
        for stock in event.get("mentioned_stocks") or []:
            if not isinstance(stock, dict):
                continue
            key = str(stock.get("key") or "").strip()
            name = str(stock.get("name") or key)
            if key and asset_kind_of(key, name) != "etf":
                found.add(key)
        for concept in event.get("concepts") or []:
            for symbol in members.get(str(concept)) or []:
                text = str(symbol).strip()
                if text and asset_kind_of(text, text) != "etf":
                    found.add(text)
    return found


def _usable(bar: dict, first: datetime) -> bool:
    at = _parse_at(bar.get("at"))
    if at is None or at < first:
        return False
    if bar.get("kind") == "day":
        opened = datetime.combine(at.date(), dt_time(9, 30), CN_TZ)
        if first > opened:
            return False
    return True


def _return_since(bars: list[dict], prev_close, first: datetime) -> float | None:
    ordered = sorted(bars, key=lambda item: _parse_at(item.get("at")) or first)
    last = _num(ordered[-1].get("close"))
    if last is None or last <= 0:
        return None
    opened = datetime.combine(first.date(), dt_time(9, 30), CN_TZ)
    base = _num(prev_close) if first <= opened else None
    if base is None or base <= 0:
        base = _num(ordered[0].get("open")) or _num(ordered[0].get("close"))
    if base is None or base <= 0:
        return None
    return last / base - 1


def _series_return(bars: list[dict]) -> float:
    if not bars:
        return 0.0
    ordered = sorted(bars, key=lambda item: _parse_at(item.get("at")) or datetime.min.replace(tzinfo=CN_TZ))
    base = _num(ordered[0].get("open")) or _num(ordered[0].get("close"))
    last = _num(ordered[-1].get("close"))
    if not base or not last or base <= 0:
        return 0.0
    return last / base - 1


def _signals(symbol: str, row: dict, bars: list[dict], first: datetime) -> set[str]:
    ordered = sorted(bars, key=lambda item: _parse_at(item.get("at")) or first)
    highs = [_num(item.get("high")) for item in ordered]
    lows = [_num(item.get("low")) for item in ordered]
    highs = [item for item in highs if item is not None]
    lows = [item for item in lows if item is not None]
    at = _parse_at(ordered[-1].get("at")) or first
    payload = {
        "symbol": symbol,
        "name": str(row.get("name") or ""),
        "close": _num(ordered[-1].get("close")),
        "raw_close": _num(ordered[-1].get("close")),
        "high": max(highs) if highs else None,
        "low": min(lows) if lows else None,
        "open": _num(ordered[0].get("open")) or _num(ordered[0].get("close")),
        "prev_close": _num(row.get("prev_close")),
        "prior_high": _num(row.get("prior_high")),
        "prior_low": _num(row.get("prior_low")),
        "qfq_close": _num(ordered[-1].get("close")),
        "trade_date": at.date(),
    }
    return abnormal_signals(payload)


def _volume_span(bars: list[dict]) -> tuple[float, float]:
    volume = 0.0
    if any(item.get("kind") == "day" for item in bars):
        for item in bars:
            volume += _num(item.get("volume")) or 0.0
        return volume, 240.0
    for item in bars:
        volume += _num(item.get("volume")) or 0.0
    return volume, float(max(len(bars), 1))


def _move_points(excess: float, vol_ratio: float | None, limit_count: int, sector_net: float | None, bull: bool) -> int:
    points = 0
    if excess >= 0.02:
        points += 10
    elif excess >= 0.01:
        points += 6
    elif excess >= 0.003:
        points += 3
    if vol_ratio is not None and vol_ratio >= 2:
        points += 8
    elif vol_ratio is not None and vol_ratio >= 1.5:
        points += 4
    if limit_count >= 2:
        points += 8
    elif limit_count == 1:
        points += 4
    if _flow_confirms(sector_net, bull):
        points += 4 if abs(sector_net or 0) >= 1e8 else 2
    return min(_MOVE_MAX, points)


def _strength_points(excess: float, breadth: float, main_net: float | None, bull: bool) -> int:
    points = 0
    if excess >= 0.03:
        points += 14
    elif excess >= 0.015:
        points += 10
    elif excess >= 0.005:
        points += 6
    if breadth >= 0.7:
        points += 10
    elif breadth >= 0.5:
        points += 6
    elif breadth > 0:
        points += 3
    if _flow_confirms(main_net, bull):
        points += 6 if abs(main_net or 0) >= 5e7 else 3
    return min(_STRENGTH_MAX, points)


def _flow_confirms(value: float | None, bull: bool) -> bool:
    if value is None or value == 0:
        return False
    return value > 0 if bull else value < 0


def _persistence(stock_rows: list[tuple[str, dict]], first: datetime, bull: bool) -> tuple[int, int]:
    by_day: dict[date, list[dict]] = {}
    for _symbol, row in stock_rows:
        for bar in row.get("bars") or []:
            if not _usable(bar, first):
                continue
            at = _parse_at(bar.get("at"))
            if at is None or bar.get("kind") == "day":
                if at is not None and bar.get("kind") == "day":
                    by_day.setdefault(at.date(), []).append(bar)
                continue
            by_day.setdefault(at.date(), []).append(bar)
    if len(by_day) >= 2:
        hits = 0
        for bars in by_day.values():
            ret = _series_return(bars)
            if (ret > 0.002) == bull and abs(ret) >= 0.002:
                hits += 1
        return hits, len(by_day)
    if not by_day:
        return 0, 0
    day, bars = next(iter(by_day.items()))
    del day
    hits = 0
    observed = 0
    for start, end in _WINDOWS:
        window = []
        for bar in bars:
            at = _parse_at(bar.get("at"))
            if at is not None and start <= at.time() < end:
                window.append(bar)
        if len(window) < 2 and not any(item.get("kind") == "day" for item in window):
            continue
        observed += 1
        ret = _series_return(window)
        if abs(ret) >= 0.002 and (ret > 0) == bull:
            hits += 1
    return hits, observed


def _concept_flows(event: dict, tape: dict, first: datetime) -> list[float]:
    values = []
    concepts = tape.get("concepts") or {}
    for name in event.get("concepts") or []:
        for item in concepts.get(str(name)) or []:
            if not isinstance(item, dict) or not _flow_after(item, first):
                continue
            number = _num(item.get("value"))
            if number is not None:
                values.append(number)
    return values


def _stock_flows(stock_rows: list[tuple[str, dict]], first: datetime) -> list[float]:
    values = []
    for _symbol, row in stock_rows:
        for item in row.get("flows") or []:
            if isinstance(item, dict) and _flow_after(item, first):
                number = _num(item.get("value"))
                if number is not None:
                    values.append(number)
    return values


def _flow_after(item: dict, first: datetime) -> bool:
    at = _parse_at(item.get("at"))
    if at is None or at < first:
        return False
    if item.get("kind") == "day":
        opened = datetime.combine(at.date(), dt_time(9, 30), CN_TZ)
        if first > opened:
            return False
    return True


def _members(data_dir: Path, concepts: list[str]) -> dict[str, list[str]]:
    if not concepts or not (data_dir / "ext_data" / "ext_gn_ths").exists():
        return {}
    try:
        from app.services.picker import (
            build_label_index,
            load_dimensions_from_dir,
            match_sector_symbols,
        )
        index = build_label_index(load_dimensions_from_dir(data_dir))
    except Exception:  # noqa: BLE001
        logger.info("盘面验证没有读到概念成分")
        return {}
    found = {}
    for name in concepts:
        symbols = sorted(match_sector_symbols(name, name, index))[:30]
        if symbols:
            found[name] = symbols
    return found


def _fill_minutes(data_dir: Path, session: date, stocks: dict[str, dict]) -> None:
    path = data_dir / "kline_minute" / f"date={session.isoformat()}" / "part.parquet"
    frame = _read_frame(path)
    if frame is None:
        return
    grouped: dict[str, list[dict]] = {}
    for row in frame.iter_rows(named=True):
        symbol = str(row.get("symbol") or "")
        if symbol not in stocks:
            continue
        bar = _bar_from(row, kind="minute")
        if bar is not None:
            grouped.setdefault(symbol, []).append(bar)
    for symbol, bars in grouped.items():
        stocks[symbol]["bars"] = bars


def _fill_daily(data_dir: Path, session: date, stocks: dict[str, dict]) -> None:
    path = data_dir / "kline_daily_enriched" / f"date={session.isoformat()}" / "part.parquet"
    frame = _read_frame(path)
    if frame is None:
        return
    previous = _read_frame(
        data_dir / "kline_daily_enriched" / f"date={_previous_weekday(session).isoformat()}" / "part.parquet",
    )
    prior = {}
    if previous is not None:
        for row in previous.iter_rows(named=True):
            prior[str(row.get("symbol") or "")] = row
    for row in frame.iter_rows(named=True):
        symbol = str(row.get("symbol") or "")
        slot = stocks.get(symbol)
        if slot is None:
            continue
        before = prior.get(symbol) or {}
        slot["name"] = str(row.get("name") or slot.get("name") or "")
        slot["prev_close"] = _num(row.get("prev_close"))
        slot["prior_high"] = _num(before.get("high_60d")) or _num(before.get("high"))
        slot["prior_low"] = _num(before.get("low_60d")) or _num(before.get("low"))
        slot["baseline_volume"] = _num(before.get("vol_ma5")) or _num(row.get("vol_ma5"))
        bar = _bar_from(row, kind="day", on=session)
        if bar is not None:
            slot["bars"].append(bar)


def _fill_stock_flow(data_dir: Path, session: date, stocks: dict[str, dict]) -> None:
    from app.fund_flow.store import read_partition

    frame = read_partition(data_dir, "stock", session.isoformat())
    if frame.is_empty():
        return
    at = datetime.combine(session, dt_time(15, 0), CN_TZ)
    for row in frame.iter_rows(named=True):
        symbol = str(row.get("symbol") or "")
        code = str(row.get("code") or "")
        target = stocks.get(symbol) or stocks.get(code)
        value = _num(row.get("main_net"))
        if target is None or value is None:
            continue
        target.setdefault("flows", []).append({"at": at, "value": value, "kind": "day"})


def _index_bars(data_dir: Path, session: date) -> list[dict]:
    minute = _read_frame(data_dir / "kline_minute" / f"date={session.isoformat()}" / "part.parquet")
    bars = []
    if minute is not None:
        for row in minute.iter_rows(named=True):
            if str(row.get("symbol") or "") != _INDEX:
                continue
            bar = _bar_from(row, kind="minute")
            if bar is not None:
                bars.append(bar)
    if bars:
        return bars
    daily = _read_frame(data_dir / "kline_index_enriched" / f"date={session.isoformat()}" / "part.parquet")
    if daily is None:
        daily = _read_frame(data_dir / "kline_index_daily" / f"date={session.isoformat()}" / "part.parquet")
    if daily is None:
        return []
    for row in daily.iter_rows(named=True):
        if str(row.get("symbol") or "") not in {_INDEX, "000300"}:
            continue
        bar = _bar_from(row, kind="day", on=session)
        if bar is not None:
            return [bar]
    return []


def _concept_flow_map(data_dir: Path, session: date, concepts: list[str]) -> dict[str, list[dict]]:
    from app.fund_flow.store import read_partition

    if not concepts:
        return {}
    wanted = set(concepts)
    found: dict[str, list[dict]] = {name: [] for name in concepts}
    for kind in ("concept", "industry"):
        frame = read_partition(data_dir, kind, session.isoformat())
        if frame.is_empty():
            continue
        for row in frame.iter_rows(named=True):
            name = str(row.get("name") or "")
            if name not in wanted:
                continue
            value = _num(row.get("net_inflow"))
            if value is None:
                continue
            at = _parse_at(row.get("captured_at")) or datetime.combine(session, dt_time(15, 0), CN_TZ)
            kind_name = "snapshot" if row.get("captured_at") else "day"
            found[name].append({"at": at, "value": value, "kind": kind_name})
    return found


def _read_frame(path: Path):
    if not path.exists():
        return None
    import polars as pl
    return pl.read_parquet(path)


def _bar_from(row: dict, *, kind: str, on: date | None = None) -> dict | None:
    if kind == "day":
        day = on or _parse_at(row.get("date"))
        if isinstance(day, datetime):
            day = day.date()
        if not isinstance(day, date):
            return None
        at = datetime.combine(day, dt_time(15, 0), CN_TZ)
    else:
        at = _parse_at(row.get("datetime"))
        if at is None:
            return None
    close = _num(row.get("close"))
    if close is None:
        return None
    return {
        "at": at,
        "open": _num(row.get("open")) or close,
        "high": _num(row.get("high")) or close,
        "low": _num(row.get("low")) or close,
        "close": close,
        "volume": _num(row.get("volume")) or 0.0,
        "kind": kind,
    }


def _previous_weekday(day: date) -> date:
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _parse_at(value) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=CN_TZ)
        return value.astimezone(CN_TZ)
    if isinstance(value, date):
        return datetime.combine(value, dt_time(15, 0), CN_TZ)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=CN_TZ)
        return parsed.astimezone(CN_TZ)
    return None


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number
