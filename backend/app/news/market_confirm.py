"""热门事件的盘面验证。只使用当时已经能看到的行情和资金。

隔夜消息（上一交易日收盘后到下一交易日 9:25，含周末）在 9:25 集合竞价出来之后按竞价验证计分。
9:25 到 9:30 连续竞价尚未开始，确认用的交易日仍切到当天，否则竞价高开读不到。
盘中消息看发布后 5、15、30 分钟相对发布前的增量，用来区分消息推动和本来就在涨的标的。
已经验证的事件再看下午和下一交易日，分成一日游和主线。异动若发生在消息之前，标成消息滞后确认。
同一窗口里多条事件映射到同一标的时，按时间和映射强弱分配，不重复计算同一段涨跌。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from app.market_time import CN_TZ, current_trading_day, in_continuous_session
from app.news.push import abnormal_signals

logger = logging.getLogger(__name__)

_FRAME_LOCK = threading.Lock()
_FRAME_CACHE: dict[tuple, object] = {}
_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: tuple | None = None

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


def confirm_session_day(now: datetime) -> date:
    """盘面确认用的交易日。9:25 竞价出来后就是当天，不等 9:30。"""
    local = now.astimezone(CN_TZ)
    if local.weekday() < 5 and local.time() >= dt_time(9, 25):
        return local.date()
    return latest_session_day(local)


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
        session = confirm_session_day(now)
        return _blank_tape(session, live=in_continuous_session(now))


_ORDER_BULL = frozenset({"大笔买入", "封涨停板", "火箭发射"})
_ORDER_BEAR = frozenset({"大笔卖出", "封跌停板"})


def score_confirmation(event: dict, tape: dict, now: datetime, *, peers: list[dict] | None = None) -> dict:
    """给一条事件打盘面分，并用盘口异动做补充确认。"""
    result = _score_confirmation(event, tape, now, peers=peers)
    return _apply_order_changes(event, tape, result)


def _score_confirmation(event: dict, tape: dict, now: datetime, *, peers: list[dict] | None = None) -> dict:
    """给一条事件打盘面分。首见之前的成交不计入验证分，只用来判断消息是否滞后。"""
    first = _first_at(event)
    session = _session_date(tape)
    live = bool(tape.get("live")) and in_continuous_session(now)
    if first is None:
        return _result(0, False, "无", "无", _session_text(session, live, waited=False), session, live, {},
                       state="unverifiable")
    # 竞价时点还没到时，周末稿的日期会晚于最近一个交易日。先标待开盘验证，
    # 不要被「首见晚于最近交易日」这条短路走掉。已知休市日把确认时点推到下一开市。
    gate = _validation_deadline(first)
    waiting = gate is not None and now.astimezone(CN_TZ) < gate
    # 隔夜稿的确认日可能晚于「最近交易日」（9:25 竞价已出、连续竞价未开）。
    # 这种稿走竞价计分，不能被「首见晚于最近交易日」短路成 0 分。
    if (
        not waiting
        and gate is None
        and session is not None
        and first.date() > session
        and not live
    ):
        return _result(
            0, False, "无", "无", "尚无首见之后的盘面", session, False, {},
            state="unverifiable",
        )
    symbols = _event_symbols(event, tape)
    # 方向留空（中性）时不改 bull 口径：这里只做事前行情检测，不生产用户可见
    # 的方向标签。默认利好已由 hot_events 侧改为留空。
    direction = str(event.get("direction") or "")
    bull = direction != "利空"
    stock_rows = []
    for symbol in symbols:
        row = (tape.get("stocks") or {}).get(symbol)
        if isinstance(row, dict):
            stock_rows.append((symbol, row))
    lagged, pre_excess = _preceding_move(stock_rows, first, bull)
    # 还没开盘：验证分是 0，不是扣分。盘面出来之后再按竞价或连续竞价计。
    if gate is not None and now.astimezone(CN_TZ) < gate:
        return _result(
            0, False, "无", "无", "待开盘验证", gate.date(), False,
            {"pre_return": round(pre_excess, 4)},
            phase="auction", lagged=lagged, horizon="", pending=True, state="pending",
        )
    if gate is not None and _has_auction_bars(stock_rows, gate.date()):
        return _score_auction(
            event, tape, stock_rows, first, gate.date(), bull, session, live, peers or [],
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
            phase="session", lagged=lagged, state="unverifiable",
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
    # **没有标的收益观测时 `basket = 0`**，于是 `excess = (0 - index_ret)`（利好时）——
    # 指数跌幅被整体当成了标的**超额收益**。指数跌 ≥0.3% 就能给一个没有 A 股价格观测的
    # 事件打出分、标 `scored`、甚至拿到 `strength`；指数一下跌就给利好事件"发验证"。
    # 判据用 `not returns`：**这是"有没有价格观测"的唯一硬证据**，`pre_excess` 非零只说明
    # 消息**之前**有行情，不能为消息**之后**的计分背书。三个计分路径统一按此拦截。
    #
    # **必须在算 move/strength_points 之前清零 `excess`** —— 否则那两个函数仍会读到
    # 被指数污染的值、凭空给分（这是评审 B 指出的口径问题）。清零后 flows-only 的
    # 事件仍可按资金面（`_flow_confirms(sector_net/main_net)`）正常计分。
    if not returns:
        excess = 0.0
        # 连资金流、盘口窗口都没有 → 什么都没观测到 → `unverifiable`（无标可验）。
        # 有资金流但无价格 bar → 观测到了资金面但未兑现 → 继续走计分，末尾按 strength 判。
        if not (flows or nets or abs(pre_excess) > 0):
            return _result(
                0, False, "无", "无", "尚无首见之后的盘面", session, False,
                {"excess_pct": 0.0, "pre_return": round(pre_excess, 4)},
                phase="session", lagged=lagged, state="unverifiable",
            )
    move = _move_points(excess, vol_ratio, limit_count, sector_net, bull)
    strength_points = _strength_points(excess, breadth, main_net, bull)
    hits, observed = _persistence(stock_rows, first, bull)
    persist_points = 20 if hits >= 3 else 14 if hits >= 2 else 6 if hits == 1 else 0
    if move > 0 or limit_count or (vol_ratio or 0) >= 2 or _flow_confirms(sector_net, bull):
        abnormal = True
    if _after_session(first, session) and not returns and not flows and not nets and abs(pre_excess) == 0:
        # 消息在收盘后、且盘口什么都没读到 → 无从验证。
        # 注意 `abs(pre_excess) == 0` 这个条件：若消息前本就有行情（pre_excess 非零），
        # 说明我们读过盘口，只是这条消息没推动它 —— 那是「验证未兑现」，不是「无标可验」，
        # 该继续往下走主计分路径，由末尾按 total 判 state。
        return _result(
            0, False, "无", "无", "尚无首见之后的盘面", session, False, {},
            phase="session", lagged=lagged, state="unverifiable",
        )
    total = min(_CONFIRM_MAX, move + strength_points + persist_points)
    strength = "强" if strength_points >= 20 else "中" if strength_points >= 10 else "弱" if strength_points > 0 else "无"
    persistence = "持续" if hits >= 2 else "短暂" if hits == 1 else "无"
    # **`verified`（喂 _horizon / 一日游封顶）与 `scored`（喂 state）是两个不同的判据，
    # 不可合并。**
    #   - `verified` 沿用原口径 `strength != "无" or abnormal`：它回答「盘面**动没动**」。
    #     涨停/放量/资金流确认（abnormal）也算动过，据此才有「主线/一日游」标签。
    #   - `scored` 用 `strength != "无"`：它回答「**强度**验证没有」。`move`/`persist`
    #     打出的分只是异动，不构成强度已验证 —— 否则 push.py 用 state 说「已验证」、
    #     前端按 strength 说「无验证」，两边矛盾。
    # 反例（评审第 1 条）：`excess ∈ [0.3%, 0.5%)` → `move=3, strength_points=0`
    # → `total=3` 却 `strength="无"`；若按 total>0 标 scored 会被误放进推送。
    # 注意**不可**把 verified 改成 `strength != "无"` —— 那会连带 `_horizon` 返回空
    # （丢掉「一日游」标签），进而使 `persist_points` 不再被压到 6，**意外抬高 total
    # 并改变排序**（这是评审 A 指出的副作用，属行为回归）。
    verified = strength != "无" or abnormal
    scored = strength != "无"
    horizon = _horizon(stock_rows, first, bull, verified)
    if horizon == "一日游":
        persist_points = min(persist_points, 6)
        total = min(_CONFIRM_MAX, move + strength_points + persist_points)
    detail = {
        "excess_pct": round(excess, 4),
        "breadth": round(breadth, 4),
        "limit_count": limit_count,
        "limit_reason_hits": _limit_reason_hits(event),
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
        # 用 `scored`（= strength != "无"）而非 `verified`，与 push/前端同口径。
        # 「有观测但没兑现」→ 「验证未兑现」；连观测都没有 → 「无标可验」。
        # 「观测过」的证据：看过盘口窗口（observed>0）、拿到超额收益/资金流，或
        # 消息前本就有行情（pre_excess 非零 —— 说明确实读了盘口，只是消息没推动它）。
        state="scored" if scored else (
            "unconfirmed"
            if (observed > 0 or returns or flows or nets or abs(pre_excess) > 0)
            else "unverifiable"
        ),
    )


def _load_market_tape(now: datetime, events: list[dict]) -> dict:
    from app.config import settings

    data_dir = Path(settings.data_dir)
    session = confirm_session_day(now)
    live = in_continuous_session(now)
    concepts = sorted({
        str(name).strip()
        for event in events
        for name in (event.get("concepts") or [])
        if str(name).strip()
    })
    members = _members(data_dir, concepts, session)
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
        "changes": _order_changes(data_dir, session),
    }


def _blank_tape(session: date, *, live: bool) -> dict:
    return {
        "session": session.isoformat(),
        "live": live,
        "members": {},
        "index_bars": [],
        "stocks": {},
        "concepts": {},
        "changes": {},
    }


def _order_changes(data_dir: Path, session: date) -> dict:
    try:
        from app.event_context.query import changes_for

        return changes_for(data_dir, session.isoformat())
    except Exception:
        logger.warning("盘口异动没读到", exc_info=True)
        return {}


def _apply_order_changes(event: dict, tape: dict, result: dict) -> dict:
    changes = tape.get("changes") if isinstance(tape, dict) else None
    if not isinstance(changes, dict) or not changes:
        return result
    if result.get("pending") or str(result.get("label") or "") in {"等待竞价", "待开盘验证"}:
        return result
    first = _first_at(event)
    if first is None:
        return result
    session = _session_date(tape)
    bull = str(event.get("direction") or "") != "利空"
    wanted = _ORDER_BULL if bull else _ORDER_BEAR
    hit = False
    for symbol in _event_symbols(event, tape):
        for row in _change_rows(changes, symbol):
            if str(row.get("kind") or "") not in wanted:
                continue
            stamp = _change_clock(row.get("at"), session)
            if stamp is not None and stamp >= first:
                hit = True
                break
        if hit:
            break
    if not hit:
        return result
    updated = dict(result)
    updated["abnormal"] = True
    if updated.get("strength") == "无":
        updated["strength"] = "弱"
        updated["score"] = round(max(float(updated.get("score") or 0), 6.0), 4)
    return updated


def _change_rows(changes: dict, symbol: str) -> list:
    rows = list(changes.get(symbol) or [])
    digits = "".join(ch for ch in symbol if ch.isdigit())
    code = digits[-6:] if len(digits) >= 6 else ""
    if code and code != symbol:
        rows.extend(changes.get(code) or [])
    return rows


def _change_clock(at: object, session: date | None) -> datetime | None:
    text = str(at or "")
    parts = text.split(":")
    if session is None or len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return None
    return datetime(session.year, session.month, session.day, hour, minute, second, tzinfo=CN_TZ)


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
    pending: bool = False,
    state: str = "",
) -> dict:
    """盘面验证结果。

    `state` 把「可验证性」这一维度收敛成单一枚举，供下游显式分流：

      - `unverifiable` 无标可验：事件没有可用的 A 股标的/资金流，盘面无从验证。
      - `unconfirmed`  验证未兑现：**有**观测（有标的、看过盘面），但没兑现。
      - `pending`      待开盘验证：确认时点还没到。
      - `scored`       已计分：正常打出了分。

    在此之前，「无标可验」与「验证未兑现」都表现为 `score=0` + `strength="无"`，
    下游无法区分。线上实测（16:09 快照，`score=0` 共 381 条）两者同时存在：
    `unverifiable` 185 条（48.6%）、`unconfirmed` 40 条（10.5%）
    （如「奥克股份终止重组复牌」excess=-4.96%、观测 4 个窗口）
    —— 异质簇，任何基于 `score == 0` 的判断都不可靠。

    注意 **`score`/`strength` 的值域一律不变**：`unverifiable` 时 `score` 仍是 0、
    `strength` 仍是 "无"，以保持 `_confirmation_points`、`service._public_confirmation`
    的值域白名单、前端既有的 `strength === '无'` 分支全部向后兼容。
    """
    if state not in {"unverifiable", "unconfirmed", "pending", "scored"}:
        # 兜底（正常情况下调用点都显式传 state）：
        # 待开盘 → pending；**`strength != "无"` → scored**（与三个计分路径同口径：
        # move/persist 打出的分只是异动，不等于强度已验证）；否则按「无标可验」。
        state = "pending" if pending else ("scored" if strength not in {"", "无"} else "unverifiable")
    return {
        "label": label,
        "session": session.isoformat() if session else None,
        "live": live,
        "pending": bool(pending),
        "state": state,
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
            "limit_reason_hits": int(detail.get("limit_reason_hits") or 0),
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
        day = deadline.date()
    elif first.weekday() >= 5:
        day = first.date() + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    else:
        day = first.date()
    # 休市日没有这场开盘。稿子归到下一开市日，周一早上才不会把节假日的消息丢掉。
    if _known_holiday(day):
        return _next_open_day(day)
    return day


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


_CALENDAR_TTL_S = 1800.0
_CALENDAR_LOCK = threading.Lock()
_CALENDAR: tuple[float, frozenset[date] | None] = (0.0, None)


def _fuyao_trading_days() -> frozenset[date] | None:
    """fuyao 交易日集合。没配日历时返回 None，调用方只跳过周末。"""
    global _CALENDAR
    now_mono = time.monotonic()
    with _CALENDAR_LOCK:
        fetched, cached = _CALENDAR
        if fetched and now_mono - fetched < _CALENDAR_TTL_S:
            return cached
    found: frozenset[date] | None = None
    try:
        from app.data_providers import custom as custom_sources

        if custom_sources.is_custom_provider("fuyao"):
            raw = custom_sources.get_provider("fuyao").trading_days()
            if raw:
                found = frozenset(raw)
    except Exception:  # noqa: BLE001 — 日历不可用时退回周末判断
        found = None
    with _CALENDAR_LOCK:
        if found is None and cached is not None:
            _CALENDAR = (now_mono, cached)
            return cached
        _CALENDAR = (now_mono, found)
        return found


def _known_holiday(day: date) -> bool:
    """工作日但交易日历明确休市。没有日历时不当休市。

    日历在年底截断之后的工作日不在集合里，不当休市，否则会一路跳到 16 天后。
    """
    if day.weekday() >= 5:
        return False
    days = _fuyao_trading_days()
    if not days:
        return False
    if day > max(days):
        return False
    return day not in days


def _clock_day_closed(now: datetime) -> bool:
    """周末，或交易日历上的休市日。"""
    local = now.astimezone(CN_TZ)
    return local.weekday() >= 5 or _known_holiday(local.date())


def _next_open_day(day: date) -> date:
    """day 之后的第一个开市日。没有日历时只跳过周六日。

    最多看 16 天。日历到头或连着休市时，退回下一个工作日，不落在周末。
    """
    cursor = day + timedelta(days=1)
    for _ in range(16):
        if cursor.weekday() < 5 and not _known_holiday(cursor):
            return cursor
        cursor += timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor


def _prev_open_day(day: date) -> date:
    """day 之前的最后一个开市日。"""
    cursor = day - timedelta(days=1)
    for _ in range(16):
        if cursor.weekday() < 5 and not _known_holiday(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return cursor


def _validation_deadline(first: datetime) -> datetime | None:
    """盘面验证可以开始的时刻。名义确认日或发布日是已知休市日时，推到下一开市 9:25。"""
    deadline = _auction_deadline(first)
    if deadline is None:
        local = first.astimezone(CN_TZ)
        if _known_holiday(local.date()):
            return datetime.combine(_next_open_day(local.date()), dt_time(9, 25), CN_TZ)
        return None
    day = deadline.date()
    if day.weekday() < 5 and not _known_holiday(day):
        return deadline
    return datetime.combine(_next_open_day(day), dt_time(9, 25), CN_TZ)


def _tape_days(now: datetime, events: list[dict]) -> list[date]:
    latest = confirm_session_day(now)
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
    # **没有任何标的竞价价格观测**（`opens` 空）时 `basket = 0`，
    # 于是 `excess = (0 - index_ret)`（利好时）—— 指数**竞价跌幅**被整体当成标的超额收益。
    # 实测：指数竞价跌 1.5% 就能让一个 `windows=0, auction_open_pct=0` 的事件拿到
    # `score=16, strength="中", state="scored", abnormal=True`，并进钉钉推送。
    # 没有价格观测，`excess` 就**必须清零**（不能留 `-index_ret`），否则：
    #   ① `_move_points`/`_strength_points` 会凭空给分；
    #   ② 即使只靠 flows 也继续计分，中间仍会读这个被污染的 `excess`。
    # 清零后 flows-only 的事件仍可按资金面计分（`sector_net` 走 `_flow_confirms`）。
    if not opens:
        excess = 0.0
    # flows-only（无价格观测但有资金流）→ 计分依赖资金面，属「观测到但未兑现」的口径。
    # 完全没有任何观测 → 直接返回 `unverifiable`，不进计分。
    if not opens and not flows:
        return _result(
            0, False, "无", "无", "尚无首见之后的盘面", session, False,
            {"excess_pct": 0.0, "pre_return": round(pre_excess, 4),
             "auction_open_pct": 0.0, "windows": 0},
            phase="auction", lagged=lagged, state="unverifiable",
        )
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
        "limit_reason_hits": _limit_reason_hits(event),
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
        # 与主路径同口径：只有 `strength != "无"` 才算「已计分」。
        state="scored" if strength != "无" else (
            "unconfirmed" if (observed > 0 or opens or flows) else "unverifiable"
        ),
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
        "limit_reason_hits": _limit_reason_hits(event),
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
        # 与主路径同口径：只有 `strength != "无"` 才算「已计分」。
        state="scored" if strength != "无" else (
            "unconfirmed"
            if (observed > 0 or post_returns or nets or abs(pre_excess) > 0)
            else "unverifiable"
        ),
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


def _limit_reason_hits(event: dict) -> int:
    """已经对上涨停题材的概念个数。没有核对结果时是 0，不另加分。"""
    info = event.get("limit_reason")
    if not isinstance(info, dict):
        return 0
    verified = info.get("verified")
    if not isinstance(verified, list):
        return 0
    return len([item for item in verified if str(item).strip()])


def _members(
    data_dir: Path,
    concepts: list[str],
    session: date | None = None,
) -> dict[str, list[str]]:
    """概念成分。涨停原因对上的股票排在前面，最多 30 只。"""
    found: dict[str, list[str]] = {}
    if concepts and (data_dir / "ext_data" / "ext_gn_ths").exists():
        try:
            from app.services.picker import match_sector_symbols

            index = _cached_label_index(data_dir)
            if index is None:
                logger.info("盘面验证没有读到概念成分")
            else:
                for name in concepts:
                    symbols = sorted(match_sector_symbols(name, name, index))[:30]
                    if symbols:
                        found[name] = symbols
        except Exception:  # noqa: BLE001
            logger.info("盘面验证没有读到概念成分")
    if session is not None and concepts:
        for name, symbols in _limit_theme_members(data_dir, session, concepts).items():
            merged: list[str] = []
            for symbol in [*symbols, *(found.get(name) or [])]:
                text = str(symbol).strip()
                if text and text not in merged:
                    merged.append(text)
                if len(merged) >= 30:
                    break
            if merged:
                found[name] = merged
    return found


def _limit_theme_members(
    data_dir: Path,
    session: date,
    concepts: list[str],
) -> dict[str, list[str]]:
    try:
        from app.limit_board.query import theme_symbols

        return theme_symbols(data_dir, session.isoformat(), concepts)
    except Exception:  # noqa: BLE001
        logger.info("盘面验证没有读到涨停原因成分")
        return {}


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


def _dimension_stamp(data_dir: Path) -> tuple | None:
    stamps = []
    found = False
    for name in ("ext_gn_ths", "ext_hy_ths"):
        path = data_dir / "ext_data" / name / "part.parquet"
        if path.is_file():
            stat = path.stat()
            stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
            found = True
        else:
            stamps.append((str(path), 0, 0))
    if not found:
        return None
    return tuple(stamps)


def _cached_label_index(data_dir: Path):
    """概念成分和行业路径按文件戳缓存，盘面验证不用每次重读全市场代码表。"""
    global _INDEX_CACHE
    stamp = _dimension_stamp(data_dir)
    if stamp is None:
        return None
    with _INDEX_LOCK:
        cached = _INDEX_CACHE
    if cached is not None and cached[0] == stamp:
        return cached[1]
    from app.services.picker import build_label_index, load_dimensions_from_dir

    index = build_label_index(load_dimensions_from_dir(data_dir))
    with _INDEX_LOCK:
        _INDEX_CACHE = (stamp, index)
    return index


def _read_frame(path: Path):
    if not path.exists():
        return None
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _FRAME_LOCK:
        cached = _FRAME_CACHE.get(key)
    if cached is not None:
        return cached
    import polars as pl

    frame = pl.read_parquet(path)
    with _FRAME_LOCK:
        if len(_FRAME_CACHE) >= 24:
            _FRAME_CACHE.pop(next(iter(_FRAME_CACHE)))
        _FRAME_CACHE[key] = frame
    return frame


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
