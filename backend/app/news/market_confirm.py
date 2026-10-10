"""热门事件的盘面验证。只使用当时已经能看到的行情和资金。

隔夜消息（上一交易日收盘后到下一交易日 9:25，含周末）在 9:25 集合竞价出来之后按竞价验证计分。
盘中消息看发布后 5、15、30 分钟相对发布前的增量，用来区分消息推动和本来就在涨的标的。
已经验证的事件再看下午和下一交易日，分成一日游和主线。异动若发生在消息之前，标成消息滞后确认。
同一窗口里多条事件映射到同一标的时，按时间和映射强弱分配，不重复计算同一段涨跌。
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
        event["confirmation"] = score_confirmation(event, tape, now, peers=events)


def load_market_tape(now: datetime, events: list[dict]) -> dict:
    try:
        return _load_market_tape(now, events)
    except Exception:
        logger.exception("盘面验证读取失败")
        session = latest_session_day(now)
        return _blank_tape(session, live=in_continuous_session(now))


def score_confirmation(event: dict, tape: dict, now: datetime, *, peers: list[dict] | None = None) -> dict:
    """给一条事件打盘面分。首见之前的成交不计入验证分，只用来判断消息是否滞后。"""
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
    lagged, pre_excess = _preceding_move(stock_rows, first, bull)
    deadline = _auction_deadline(first)
    if deadline is not None and now.astimezone(CN_TZ) < deadline:
        return _result(
            0, False, "无", "无", "等待竞价", deadline.date(), False,
            {"pre_return": round(pre_excess, 4)},
            phase="auction", lagged=lagged, horizon="",
        )
    if deadline is not None and _has_auction_bars(stock_rows, deadline.date()):
        return _score_auction(
            event, tape, stock_rows, first, deadline.date(), bull, session, live, peers or [],
            lagged, pre_excess,
        )
    if _is_intraday(first):
        return _score_intraday(
            event, tape, stock_rows, first, bull, session, live, peers or [], lagged, pre_excess,
        )
    index_bars = [
        bar for bar in (tape.get("index_bars") or [])
        if _usable(bar, first) and _on_day(bar, _confirm_day(first))
    ]
    flows = _concept_flows(event, tape, first)
    nets = _stock_flows(stock_rows, first)
    if not stock_rows and not flows:
        waited = _after_session(first, session)
        return _result(
            0, False, "无", "无", _session_text(session, live, waited=waited), session, live and not waited, {},
            phase="session", lagged=lagged,
        )
    returns = []
    limit_count = 0
    abnormal = False
    volumes: list[float] = []
    baselines: list[float] = []
    minutes: list[float] = []
    shares: list[float] = []
    for symbol, row in stock_rows:
        end = _clip_end(event, peers or [], symbol, tape)
        share = _overlap_share(event, peers or [], symbol, tape)
        bars = [
            bar for bar in (row.get("bars") or [])
            if _usable(bar, first) and _before(bar, end) and _on_day(bar, _confirm_day(first))
        ]
        if not bars:
            continue
        shares.append(share)
        ret = _return_since(bars, row.get("prev_close"), first)
        if ret is not None:
            returns.append(ret * share)
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
    sector_net = _scale_flow(sum(flows) if flows else None, shares)
    main_net = _scale_flow(sum(nets) if nets else None, shares)
    move = _move_points(excess, vol_ratio, limit_count, sector_net, bull)
    strength_points = _strength_points(excess, breadth, main_net, bull)
    hits, observed = _persistence(stock_rows, first, bull)
    persist_points = 20 if hits >= 3 else 14 if hits >= 2 else 6 if hits == 1 else 0
    if move > 0 or limit_count or (vol_ratio or 0) >= 2 or _flow_confirms(sector_net, bull):
        abnormal = True
    if _after_session(first, session) and not returns and not flows and not nets:
        return _result(
            0, False, "无", "无", "尚无首见之后的盘面", session, False, {},
            phase="session", lagged=lagged,
        )
    total = min(_CONFIRM_MAX, move + strength_points + persist_points)
    strength = "强" if strength_points >= 20 else "中" if strength_points >= 10 else "弱" if strength_points > 0 else "无"
    persistence = "持续" if hits >= 2 else "短暂" if hits == 1 else "无"
    verified = strength != "无" or abnormal
    horizon = _horizon(stock_rows, first, bull, verified)
    if horizon == "一日游":
        persist_points = min(persist_points, 6)
        total = min(_CONFIRM_MAX, move + strength_points + persist_points)
    detail = {
        "excess_pct": round(excess, 4),
        "breadth": round(breadth, 4),
        "limit_count": limit_count,
        "vol_ratio": round(vol_ratio, 4) if vol_ratio is not None else None,
        "main_net": round(main_net, 2) if main_net is not None else None,
        "sector_net_inflow": round(sector_net, 2) if sector_net is not None else None,
        "windows": observed,
        "windows_hit": hits,
        "pre_return": round(pre_excess, 4),
        "share": round(min(shares) if shares else 1.0, 4),
    }
    return _result(
        total, abnormal, strength, persistence,
        _session_text(session, live, waited=False), session, live, detail,
        phase="session", lagged=lagged, horizon=horizon,
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
    index_bars: list[dict] = []
    concept_flows: dict[str, list[dict]] = {name: [] for name in concepts}
    for day in _tape_days(now, events):
        _fill_daily(data_dir, day, stocks)
        _fill_minutes(data_dir, day, stocks)
        _fill_stock_flow(data_dir, day, stocks)
        index_bars.extend(_index_bars(data_dir, day))
        for name, rows in _concept_flow_map(data_dir, day, concepts).items():
            concept_flows.setdefault(name, []).extend(rows)
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
    *,
    phase: str = "session",
    lagged: bool = False,
    horizon: str = "",
) -> dict:
    return {
        "label": label,
        "session": session.isoformat() if session else None,
        "live": live,
        "score": round(min(_CONFIRM_MAX, max(0.0, score)), 4),
        "abnormal": abnormal,
        "strength": strength,
        "persistence": persistence,
        "phase": phase if phase in {"auction", "intraday", "session"} else "session",
        "lagged": bool(lagged),
        "horizon": horizon if horizon in {"主线", "一日游"} else "",
        "detail": {
            "excess_pct": detail.get("excess_pct"),
            "breadth": detail.get("breadth"),
            "limit_count": int(detail.get("limit_count") or 0),
            "vol_ratio": detail.get("vol_ratio"),
            "main_net": detail.get("main_net"),
            "sector_net_inflow": detail.get("sector_net_inflow"),
            "windows": int(detail.get("windows") or 0),
            "windows_hit": int(detail.get("windows_hit") or 0),
            "pre_return": detail.get("pre_return"),
            "auction_open_pct": detail.get("auction_open_pct"),
            "auction_vol_ratio": detail.get("auction_vol_ratio"),
            "high_open_breadth": detail.get("high_open_breadth"),
            "window_5": detail.get("window_5"),
            "window_15": detail.get("window_15"),
            "window_30": detail.get("window_30"),
            "share": detail.get("share"),
        },
    }


def _auction_deadline(first: datetime) -> datetime | None:
    """上一收盘到下一 9:25 的消息，确认时点是下一集合竞价。盘中消息返回 None。"""
    local = first.astimezone(CN_TZ)
    if local.weekday() >= 5 or local.time() > dt_time(15, 0):
        day = local.date() + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return datetime.combine(day, dt_time(9, 25), CN_TZ)
    if local.time() <= dt_time(9, 25):
        return datetime.combine(local.date(), dt_time(9, 25), CN_TZ)
    return None


def _confirm_day(first: datetime) -> date:
    deadline = _auction_deadline(first)
    if deadline is not None:
        return deadline.date()
    if first.weekday() >= 5:
        day = first.date() + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return day
    return first.date()


def _is_intraday(first: datetime) -> bool:
    if _auction_deadline(first) is not None or first.weekday() >= 5:
        return False
    clock = first.time()
    return dt_time(9, 25) < clock <= dt_time(15, 0)


def _next_weekday(day: date) -> date:
    cursor = day + timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor


def _tape_days(now: datetime, events: list[dict]) -> list[date]:
    latest = latest_session_day(now)
    days = {latest}
    for event in events:
        first = _first_at(event)
        if first is None:
            continue
        day = _confirm_day(first)
        if day <= latest:
            days.add(day)
        nxt = _next_weekday(day)
        if nxt <= latest:
            days.add(nxt)
    return sorted(days)


def _on_day(bar: dict, day: date) -> bool:
    at = _parse_at(bar.get("at"))
    return at is not None and at.date() == day


def _before(bar: dict, end: datetime | None) -> bool:
    if end is None:
        return True
    at = _parse_at(bar.get("at"))
    return at is not None and at < end


def _auction_bars(row: dict, day: date) -> list[dict]:
    found = []
    for bar in row.get("bars") or []:
        at = _parse_at(bar.get("at"))
        if at is None or at.date() != day or bar.get("kind") == "day":
            continue
        if dt_time(9, 15) <= at.time() <= dt_time(9, 25):
            found.append(bar)
    return found


def _has_auction_bars(stock_rows: list[tuple[str, dict]], day: date) -> bool:
    return any(_auction_bars(row, day) for _symbol, row in stock_rows)


def _add_trading_minutes(start: datetime, minutes: int) -> datetime:
    cursor = start.astimezone(CN_TZ).replace(second=0, microsecond=0)
    left = minutes
    guard = 0
    while left > 0 and guard < 2000:
        guard += 1
        cursor += timedelta(minutes=1)
        if cursor.weekday() >= 5:
            cursor = datetime.combine(_next_weekday(cursor.date()), dt_time(9, 30), CN_TZ)
            continue
        clock = cursor.time()
        if dt_time(11, 30) < clock < dt_time(13, 0):
            cursor = datetime.combine(cursor.date(), dt_time(13, 0), CN_TZ)
            continue
        if dt_time(9, 30) <= clock <= dt_time(11, 30) or dt_time(13, 0) <= clock <= dt_time(15, 0):
            left -= 1
    return cursor


def _weight(event: dict, symbol: str) -> float:
    for stock in event.get("mentioned_stocks") or []:
        if isinstance(stock, dict) and str(stock.get("key") or "") == symbol:
            return 3.0
    try:
        mapping = float(event.get("mapping") or 0)
    except (TypeError, ValueError):
        mapping = 0.0
    return 1.0 + min(max(mapping, 0.0), 35.0) / 35.0


def _competitors(event: dict, peers: list[dict], symbol: str, tape: dict) -> list[dict]:
    first = _first_at(event)
    if first is None:
        return [event]
    deadline = _auction_deadline(first)
    found = []
    for other in peers or [event]:
        other_first = _first_at(other)
        if other_first is None or symbol not in _event_symbols(other, tape):
            continue
        if deadline is not None:
            if _auction_deadline(other_first) == deadline:
                found.append(other)
            continue
        if _auction_deadline(other_first) is not None or other_first.date() != first.date():
            continue
        found.append(other)
    return found or [event]


def _clip_end(event: dict, peers: list[dict], symbol: str, tape: dict) -> datetime | None:
    first = _first_at(event)
    if first is None or _auction_deadline(first) is not None:
        return None
    cut = None
    for other in _competitors(event, peers, symbol, tape):
        if other is event:
            continue
        other_first = _first_at(other)
        if other_first is None or other_first <= first + timedelta(seconds=60):
            continue
        if cut is None or other_first < cut:
            cut = other_first
    return cut


def _overlap_share(event: dict, peers: list[dict], symbol: str, tape: dict) -> float:
    """同一分钟里的事件按映射强弱分；竞价则同一开盘由全部隔夜事件分。"""
    first = _first_at(event)
    group = _competitors(event, peers, symbol, tape)
    if _auction_deadline(first) is not None:
        weights = [_weight(item, symbol) for item in group]
        total = sum(weights)
        return (_weight(event, symbol) / total) if total else 1.0
    close = []
    for other in group:
        other_first = _first_at(other)
        if first is not None and other_first is not None and abs((other_first - first).total_seconds()) <= 60:
            close.append(other)
    if len(close) <= 1:
        return 1.0
    weights = [_weight(item, symbol) for item in close]
    total = sum(weights)
    return (_weight(event, symbol) / total) if total else 1.0


def _scale_flow(value: float | None, shares: list[float]) -> float | None:
    if value is None or not shares:
        return value
    factor = min(shares)
    if factor >= 1:
        return value
    return value * factor


def _preceding_move(stock_rows: list[tuple[str, dict]], first: datetime, bull: bool) -> tuple[bool, float]:
    """首见之前的涨跌停或已有明显涨跌，说明这段行情不是消息推出来的。"""
    returns = []
    limited = False
    wanted = _BULL_SIGNALS if bull else _BEAR_SIGNALS
    for symbol, row in stock_rows:
        pre = []
        for bar in row.get("bars") or []:
            at = _parse_at(bar.get("at"))
            if at is None or at >= first or bar.get("kind") == "day":
                continue
            pre.append(bar)
        if not pre:
            continue
        ordered = sorted(pre, key=lambda item: _parse_at(item.get("at")) or first)
        last = _num(ordered[-1].get("close"))
        prev = _num(row.get("prev_close"))
        base = prev if prev and prev > 0 else (_num(ordered[0].get("open")) or last)
        if last and base and base > 0:
            change = last / base - 1
            returns.append(change if bull else -change)
        if _signals(symbol, row, ordered, first) & wanted:
            limited = True
    excess = sum(returns) / len(returns) if returns else 0.0
    return limited or excess >= 0.02, excess


def _horizon(stock_rows: list[tuple[str, dict]], first: datetime, bull: bool, verified: bool) -> str:
    if not verified:
        return ""
    day = _confirm_day(first)
    afternoon: list[dict] = []
    later: list[dict] = []
    for _symbol, row in stock_rows:
        for bar in row.get("bars") or []:
            at = _parse_at(bar.get("at"))
            if at is None or bar.get("kind") == "day" or at < first:
                continue
            if at.date() == day and at.time() >= dt_time(13, 0):
                afternoon.append(bar)
            elif at.date() > day:
                later.append(bar)
    hold = False
    fade = False
    observed = False
    for bars in (afternoon, later):
        if len(bars) < 1:
            continue
        observed = True
        ret = _series_return(sorted(bars, key=lambda item: _parse_at(item.get("at")) or first))
        if (ret > 0) == bull and abs(ret) >= 0.002:
            hold = True
        elif (ret > 0) != bull and abs(ret) >= 0.003:
            fade = True
    if hold:
        return "主线"
    if observed and fade:
        return "一日游"
    return ""


def _score_auction(
    event: dict,
    tape: dict,
    stock_rows: list[tuple[str, dict]],
    first: datetime,
    day: date,
    bull: bool,
    session: date | None,
    live: bool,
    peers: list[dict],
    lagged: bool,
    pre_excess: float,
) -> dict:
    opens = []
    limit_count = 0
    volumes = 0.0
    baselines = []
    shares = []
    wanted = _BULL_SIGNALS if bull else _BEAR_SIGNALS
    for symbol, row in stock_rows:
        bars = _auction_bars(row, day)
        if not bars:
            continue
        share = _overlap_share(event, peers, symbol, tape)
        shares.append(share)
        ordered = sorted(bars, key=lambda item: _parse_at(item.get("at")) or first)
        price = _num(ordered[-1].get("close"))
        prev = _num(row.get("prev_close"))
        if price and prev and prev > 0:
            opens.append((price / prev - 1) * share)
        if _signals(symbol, row, ordered, first) & wanted:
            if "limit_up" in _signals(symbol, row, ordered, first) and bull:
                limit_count += 1
            if "limit_down" in _signals(symbol, row, ordered, first) and not bull:
                limit_count += 1
        for bar in ordered:
            volumes += _num(bar.get("volume")) or 0.0
        baseline = _num(row.get("prior_volume")) or _num(row.get("baseline_volume"))
        if baseline and baseline > 0:
            baselines.append(baseline)
    index_bars = [
        bar for bar in (tape.get("index_bars") or [])
        if _on_day(bar, day) and _auction_bars({"bars": [bar]}, day)
    ]
    index_ret = 0.0
    if index_bars:
        ordered = sorted(index_bars, key=lambda item: _parse_at(item.get("at")) or first)
        base = _num(ordered[0].get("open")) or _num(ordered[0].get("close"))
        last = _num(ordered[-1].get("close"))
        if base and last and base > 0:
            index_ret = last / base - 1
    basket = sum(opens) / len(opens) if opens else 0.0
    excess = (basket - index_ret) if bull else (index_ret - basket)
    high_open = 0.0
    if opens:
        high_open = len([item for item in opens if (item > 0) == bull and abs(item) >= 0.01]) / len(opens)
    vol_ratio = None
    if volumes and baselines:
        expected = sum(base * 10 / 240.0 for base in baselines)
        if expected > 0:
            vol_ratio = volumes / expected
    flows = []
    for name in event.get("concepts") or []:
        for item in (tape.get("concepts") or {}).get(str(name)) or []:
            at = _parse_at(item.get("at")) if isinstance(item, dict) else None
            if at is None or at.date() != day or not (dt_time(9, 15) <= at.time() <= dt_time(9, 25)):
                continue
            number = _num(item.get("value"))
            if number is not None:
                flows.append(number)
    sector_net = _scale_flow(sum(flows) if flows else None, shares)
    move = _move_points(excess, vol_ratio, limit_count, sector_net, bull)
    strength_points = _strength_points(excess, high_open, None, bull)
    hits, observed = _persistence(stock_rows, datetime.combine(day, dt_time(9, 25), CN_TZ), bull)
    persist_points = 20 if hits >= 3 else 14 if hits >= 2 else 6 if hits == 1 else 0
    abnormal = move > 0 or limit_count > 0 or (vol_ratio or 0) >= 2 or _flow_confirms(sector_net, bull)
    total = min(_CONFIRM_MAX, move + strength_points + persist_points)
    strength = "强" if strength_points >= 20 else "中" if strength_points >= 10 else "弱" if strength_points > 0 else "无"
    persistence = "持续" if hits >= 2 else "短暂" if hits == 1 else "无"
    horizon = _horizon(stock_rows, first, bull, strength != "无" or abnormal)
    if horizon == "一日游":
        persist_points = min(persist_points, 6)
        total = min(_CONFIRM_MAX, move + strength_points + persist_points)
    detail = {
        "excess_pct": round(excess, 4),
        "breadth": round(high_open, 4),
        "limit_count": limit_count,
        "vol_ratio": round(vol_ratio, 4) if vol_ratio is not None else None,
        "main_net": None,
        "sector_net_inflow": round(sector_net, 2) if sector_net is not None else None,
        "windows": observed,
        "windows_hit": hits,
        "pre_return": round(pre_excess, 4),
        "auction_open_pct": round(basket, 4),
        "auction_vol_ratio": round(vol_ratio, 4) if vol_ratio is not None else None,
        "high_open_breadth": round(high_open, 4),
        "share": round(min(shares) if shares else 1.0, 4),
    }
    return _result(
        total, abnormal, strength, persistence,
        _session_text(session, live, waited=False), session, live, detail,
        phase="auction", lagged=lagged, horizon=horizon,
    )


def _score_intraday(
    event: dict,
    tape: dict,
    stock_rows: list[tuple[str, dict]],
    first: datetime,
    bull: bool,
    session: date | None,
    live: bool,
    peers: list[dict],
    lagged: bool,
    pre_excess: float,
) -> dict:
    day = first.date()
    index_all = [bar for bar in (tape.get("index_bars") or []) if _on_day(bar, day)]
    window_excess = {5: [], 15: [], 30: []}
    limit_count = 0
    volumes: list[float] = []
    baselines: list[float] = []
    span_minutes: list[float] = []
    shares: list[float] = []
    post_returns = []
    wanted = _BULL_SIGNALS if bull else _BEAR_SIGNALS
    for symbol, row in stock_rows:
        end = _clip_end(event, peers, symbol, tape)
        share = _overlap_share(event, peers, symbol, tape)
        day_bars = [bar for bar in (row.get("bars") or []) if _on_day(bar, day) and bar.get("kind") != "day"]
        pre = [bar for bar in day_bars if (_parse_at(bar.get("at")) or first) < first]
        pre_limited = bool(_signals(symbol, row, pre, first) & wanted) if pre else False
        pre_price = _num(pre[-1].get("close")) if pre else None
        if pre_price is None and first.time() <= dt_time(9, 30):
            pre_price = _num(row.get("prev_close"))
        scored_window = None
        for minutes in (30, 15, 5):
            stop = _add_trading_minutes(first, minutes)
            if end is not None and end < stop:
                stop = end
            window = [
                bar for bar in day_bars
                if first <= (_parse_at(bar.get("at")) or first) < stop
            ]
            if not window:
                continue
            ordered = sorted(window, key=lambda item: _parse_at(item.get("at")) or first)
            last = _num(ordered[-1].get("close"))
            base = pre_price or _num(ordered[0].get("open")) or last
            if not last or not base or base <= 0:
                continue
            ret = (last / base - 1) * share
            idx = [
                bar for bar in index_all
                if first <= (_parse_at(bar.get("at")) or first) < stop
            ]
            index_ret = _series_return(idx) if idx else 0.0
            excess = (ret - index_ret) if bull else (index_ret - ret)
            window_excess[minutes].append(excess)
            if scored_window is None:
                scored_window = (ordered, excess, ret)
        if scored_window is None:
            continue
        shares.append(share)
        ordered, excess, ret = scored_window
        post_returns.append(ret if bull else -ret)
        if not pre_limited and _signals(symbol, row, ordered, first) & wanted:
            if bull and "limit_up" in _signals(symbol, row, ordered, first):
                limit_count += 1
            if not bull and "limit_down" in _signals(symbol, row, ordered, first):
                limit_count += 1
        volume, minute = _volume_span(ordered)
        baseline = _num(row.get("baseline_volume"))
        if volume and baseline and baseline > 0:
            volumes.append(volume)
            baselines.append(baseline)
            span_minutes.append(minute)
    def _mean(values: list[float]) -> float | None:
        if not values:
            return None
        return sum(values) / len(values)

    excess = _mean(window_excess[30] or window_excess[15] or window_excess[5]) or 0.0
    # 发布前已经走出的行情不再算作这条消息的验证。
    already = pre_excess >= 0.01 and excess < 0.003
    breadth = 0.0
    if post_returns and not already:
        breadth = len([item for item in post_returns if item >= 0.001]) / len(post_returns)
    vol_ratio = None
    if volumes and not already:
        expected = sum(base * minute / 240.0 for base, minute in zip(baselines, span_minutes, strict=True))
        if expected > 0:
            vol_ratio = sum(volumes) / expected
    stop = _add_trading_minutes(first, 30)
    nets = []
    for _symbol, row in stock_rows:
        for item in row.get("flows") or []:
            if not isinstance(item, dict) or item.get("kind") == "day":
                continue
            at = _parse_at(item.get("at"))
            if at is None or at < first or at >= stop:
                continue
            number = _num(item.get("value"))
            if number is not None:
                nets.append(number)
    main_net = _scale_flow(sum(nets) if nets else None, shares)
    if already:
        excess = 0.0
        limit_count = 0
        main_net = None
        vol_ratio = None
    move = 0 if already else _move_points(excess, vol_ratio, limit_count, None, bull)
    strength_points = 0 if already else _strength_points(excess, breadth, main_net, bull)
    hits = 0
    observed = 0
    for minutes in (5, 15, 30):
        value = _mean(window_excess[minutes])
        if value is None:
            continue
        observed += 1
        if abs(value) >= 0.002 and (value > 0) == bull:
            hits += 1
    if already:
        hits = 0
        observed = 0
    persist_points = 20 if hits >= 3 else 14 if hits >= 2 else 6 if hits == 1 else 0
    abnormal = (not already) and (
        move > 0 or limit_count > 0 or (vol_ratio or 0) >= 2 or _flow_confirms(main_net, bull)
    )
    total = min(_CONFIRM_MAX, move + strength_points + persist_points)
    strength = "强" if strength_points >= 20 else "中" if strength_points >= 10 else "弱" if strength_points > 0 else "无"
    persistence = "持续" if hits >= 2 else "短暂" if hits == 1 else "无"
    horizon = _horizon(stock_rows, first, bull, strength != "无" or abnormal)
    if horizon == "一日游":
        persist_points = min(persist_points, 6)
        total = min(_CONFIRM_MAX, move + strength_points + persist_points)
    detail = {
        "excess_pct": round(excess, 4),
        "breadth": round(breadth, 4),
        "limit_count": limit_count,
        "vol_ratio": round(vol_ratio, 4) if vol_ratio is not None else None,
        "main_net": round(main_net, 2) if main_net is not None else None,
        "sector_net_inflow": None,
        "windows": observed,
        "windows_hit": hits,
        "pre_return": round(pre_excess, 4),
        "window_5": _mean(window_excess[5]),
        "window_15": _mean(window_excess[15]),
        "window_30": _mean(window_excess[30]),
        "share": round(min(shares) if shares else 1.0, 4),
    }
    if detail["window_5"] is not None:
        detail["window_5"] = round(detail["window_5"], 4)
    if detail["window_15"] is not None:
        detail["window_15"] = round(detail["window_15"], 4)
    if detail["window_30"] is not None:
        detail["window_30"] = round(detail["window_30"], 4)
    return _result(
        total, abnormal, strength, persistence,
        _session_text(session, live, waited=False), session, live, detail,
        phase="intraday", lagged=lagged, horizon=horizon,
    )


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
        kept = []
        for old in stocks[symbol]["bars"]:
            at = _parse_at(old.get("at"))
            if old.get("kind") == "day" and at is not None and at.date() == session:
                continue
            kept.append(old)
        stocks[symbol]["bars"] = kept + bars


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
        if not slot.get("name"):
            slot["name"] = str(row.get("name") or "")
        if slot.get("prev_close") is None:
            slot["prev_close"] = _num(row.get("prev_close"))
        if slot.get("prior_high") is None:
            slot["prior_high"] = _num(before.get("high_60d")) or _num(before.get("high"))
        if slot.get("prior_low") is None:
            slot["prior_low"] = _num(before.get("low_60d")) or _num(before.get("low"))
        if slot.get("prior_volume") is None:
            slot["prior_volume"] = _num(before.get("volume"))
        if slot.get("baseline_volume") is None:
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
