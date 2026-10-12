"""个股分析前交给 DSA 的 TSP 上下文。

只在 TSP 进程里读本地数据。DSA 进程通过 HTTP 取已经排好的短文本，
不改 vendor。四段都标新鲜度；读失败写成缺失，不把空值当成 0。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Any

from app.market_time import CN_TZ
from app.news.extract import canonical_symbol, code6_of

logger = logging.getLogger("tsp.dsa.stock_context")

MAX_EVENTS = 4
MAX_RED = 4
MAX_FLOW_DAYS = 10
MIN_FLOW_DAYS = 5
MAX_TITLE = 40
MAX_TEXT = 1800
CONTEXT_CACHE_TTL = 30
_CONTEXT_CACHE_MAX = 64
_cache_lock = threading.Lock()
_context_cache: dict[str, tuple[float, dict]] = {}
_AUCTION_SLICE_MINUTES = 10
_SESSION_MINUTES = 240
_FLOW_STALE_DAYS = 3
_EVENT_STALE = timedelta(hours=6)

_OMIT = object()
_HEADER = (
    "【TSP个股上下文】\n"
    "这是本股分析输入，不是结论。\n"
    "「题材催化」写入「相关热点」和「板块行业分析」。\n"
    "标成高优先级的加红资讯必须写入「利好催化」或「风险警报」，保留标题，不要省略。\n"
    "盘前和集合竞价只有「已确认=是」才算盘面已经验证。\n"
    "标成缺失或过期的数字不要补造。"
)


def normalize_symbol(raw: str) -> str:
    return canonical_symbol(str(raw or "").strip())


def market_phase(now: datetime) -> str:
    """北京时间的交易阶段。集合竞价在 9:25 撮合前仍是指示价。"""
    local = now.astimezone(CN_TZ)
    if local.weekday() >= 5:
        return "休市"
    clock = local.time()
    if clock < dt_time(9, 15):
        return "盘前"
    if clock < dt_time(9, 25):
        return "集合竞价"
    if clock < dt_time(9, 30):
        return "竞价已撮合"
    if clock <= dt_time(11, 30) or dt_time(13, 0) <= clock <= dt_time(15, 0):
        return "盘中"
    if clock < dt_time(13, 0):
        return "午休"
    return "收盘"


def elapsed_session_minutes(now: datetime) -> int | None:
    """连续竞价已过的分钟。竞价阶段返回 None，由 10 分钟切片单独算量比。"""
    local = now.astimezone(CN_TZ)
    clock = local.time()
    if clock < dt_time(9, 30):
        return None
    minute = local.hour * 60 + local.minute
    if clock <= dt_time(11, 30):
        return max(1, minute - (9 * 60 + 30))
    if clock < dt_time(13, 0):
        return 120
    if clock <= dt_time(15, 0):
        return 120 + max(0, minute - 13 * 60)
    return _SESSION_MINUTES


def match_events(events: list[dict], symbol: str) -> list[dict]:
    code = code6_of(symbol)
    if not code:
        return []
    matched = []
    for event in events:
        if not isinstance(event, dict):
            continue
        stocks = event.get("stocks") or event.get("mentioned_stocks") or []
        keys = {
            code6_of(str(item.get("key") or item.get("code") or ""))
            for item in stocks
            if isinstance(item, dict)
        }
        if code in keys:
            matched.append(event)
    matched.sort(key=lambda item: (-_float(item.get("heat")), str(item.get("name") or "")))
    return matched[:MAX_EVENTS]


def quote_from_rows(rows: list[dict], now: datetime) -> dict[str, Any]:
    """用当日原始开盘和前一日原始收盘算竞价涨跌幅。

    close 可能是前复权。原始开盘 = open × raw_close / close，两边都在同一根 K 上。
    缺原始价时不拿复权价顶上。
    """
    today = now.astimezone(CN_TZ).date()
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = _day(row.get("date") or row.get("trade_date"))
        if day is None:
            continue
        parsed.append((day, row))
    parsed.sort(key=lambda item: item[0])
    today_row = next((row for day, row in parsed if day == today), None)
    previous = [row for day, row in parsed if day < today and _positive(row.get("raw_close"))]
    prev_close = _positive(previous[-1].get("raw_close")) if previous else None
    volumes = [
        _positive(row.get("volume"))
        for _day, row in parsed
        if _day < today and _positive(row.get("volume"))
    ][-5:]
    avg = (sum(volumes) / len(volumes)) if volumes else None
    if today_row is None:
        return {
            "trade_date": None,
            "auction_price": None,
            "prev_close": prev_close,
            "volume": None,
            "avg_volume_5d": avg,
            "sample_days": len(volumes),
        }
    return {
        "trade_date": today.isoformat(),
        "auction_price": _raw_open(today_row),
        "prev_close": prev_close,
        "volume": _positive(today_row.get("volume")),
        "avg_volume_5d": avg,
        "sample_days": len(volumes),
    }


def render_context(
    *,
    symbol: str,
    now: datetime,
    events: list[dict] | None,
    events_as_of: str | None = None,
    events_note: str | None = None,
    red_news: list[dict] | None,
    flow_rows: list[dict] | None,
    sector_rank: dict | None = None,
    concept_ranks: list[dict] | None = None,
    quote: dict | None,
    event_lines: Any = _OMIT,
    ima_lines: Any = _OMIT,
    chip: Any = _OMIT,
) -> dict[str, Any]:
    """拼一段有长度上限的上下文。events/news/flow/quote 为 None 表示没读到。"""
    local_now = now.astimezone(CN_TZ)
    missing: list[str] = []
    stale: list[str] = []
    sections = [
        _events_section(events, events_as_of, events_note, local_now, missing, stale),
        _auction_section(quote, local_now, missing, stale),
        _red_section(red_news, events or [], missing),
        _flow_section(flow_rows, sector_rank, concept_ranks, local_now.date(), missing, stale),
    ]
    if event_lines is not _OMIT:
        extra = _event_context_section(event_lines, missing)
        if extra:
            sections.append(extra)
    if ima_lines is not _OMIT:
        extra = _ima_section(ima_lines, missing)
        if extra:
            sections.append(extra)
    if chip is not _OMIT:
        sections.append(_chip_section(chip, missing))
    text = _fit(_HEADER, sections)
    return {
        "symbol": symbol,
        "as_of": local_now.isoformat(timespec="seconds"),
        "text": text,
        "missing": missing,
        "stale": stale,
    }


def clear_context_cache() -> None:
    with _cache_lock:
        _context_cache.clear()


def load_context_cached(symbol: str, *, now: datetime | None = None, repo=None) -> dict[str, Any]:
    """同一只股票 30 秒内复用已拼好的文本。分析重试不会再读一遍事件和资金。"""
    key = normalize_symbol(symbol) or str(symbol or "").strip()
    stamp = time.monotonic()
    with _cache_lock:
        hit = _context_cache.get(key)
        if hit is not None and stamp - hit[0] < CONTEXT_CACHE_TTL:
            return hit[1]
    payload = load_context(symbol, now=now, repo=repo)
    stored = str(payload.get("symbol") or key)
    with _cache_lock:
        _context_cache[stored] = (time.monotonic(), payload)
        if len(_context_cache) > _CONTEXT_CACHE_MAX:
            oldest = min(_context_cache, key=lambda item: _context_cache[item][0])
            _context_cache.pop(oldest, None)
    return payload


def load_context(symbol: str, *, now: datetime | None = None, repo=None) -> dict[str, Any]:
    """读热门事件、加红稿、资金和日K。每一段失败只让该段变成缺失。"""
    moment = now or datetime.now(CN_TZ)
    code = normalize_symbol(symbol) or str(symbol or "").strip()
    events_pack, _events_ok = _capture(lambda: _load_events(code, moment))
    news, _news_ok = _capture(lambda: _load_red_news(code))
    flow_pack, _flow_ok = _capture(lambda: _load_flow(code, (events_pack or {}).get("events") or []))
    bars, _bars_ok = _capture(lambda: _load_bars(repo, code, moment))
    event_lines, event_ok = _capture(lambda: _load_event_lines(code))
    ima_lines, ima_ok = _capture(lambda: _load_ima_lines(code))
    chip, chip_ok = _capture(lambda: _load_chip(code))
    quote = quote_from_rows(bars, moment) if bars is not None else None
    events = None if events_pack is None else events_pack.get("events")
    flow_rows = None if flow_pack is None else flow_pack.get("rows")
    return render_context(
        symbol=code,
        now=moment,
        events=events,
        events_as_of=None if events_pack is None else events_pack.get("as_of"),
        events_note=None if events_pack is None else events_pack.get("note"),
        red_news=news,
        flow_rows=flow_rows,
        sector_rank=None if flow_pack is None else flow_pack.get("sector"),
        concept_ranks=None if flow_pack is None else flow_pack.get("concepts"),
        quote=quote if bars is not None else None,
        event_lines=None if not event_ok else event_lines,
        ima_lines=None if not ima_ok else ima_lines,
        chip=None if not chip_ok else chip,
    )


def _events_section(events, as_of, note, now, missing, stale) -> str:
    if events is None:
        missing.append("hot_events")
        return "## 题材催化\n状态: 缺失"
    if not events:
        missing.append("hot_events")
        return "## 题材催化\n状态: 缺失\n没有对上这只股票的热门事件。"
    status = _event_status(as_of, now)
    if status == "过期":
        stale.append("hot_events")
    lines = [f"## 题材催化\n状态: {status}"]
    if note:
        lines.append(f"说明: {_clip(note, 80)}")
    for event in events[:MAX_EVENTS]:
        lines.append(_event_line(event))
    return "\n".join(lines)


def _event_line(event: dict) -> str:
    title = _clip(event.get("headline") or event.get("name") or "未命名事件", MAX_TITLE)
    when = _clip(event.get("first_seen") or "时间缺失", 16)
    kind = _clip(event.get("category") or "类型缺失", 8)
    heat = event.get("heat")
    heat_text = f"{_float(heat):.1f}" if heat is not None else "缺失"
    direction = event.get("direction") if event.get("direction") in {"利好", "利空"} else "方向缺失"
    red = "加红" if _int(event.get("red_count")) > 0 else "普通"
    concepts = [str(item).strip() for item in (event.get("concepts") or []) if str(item).strip()]
    sector = f" 板块:{'、'.join(concepts[:3])}" if concepts else ""
    return f"- {title} | {when} | {kind} | 热度 {heat_text} | {direction} | {red}{sector}"


def _auction_section(quote, now, missing, stale) -> str:
    phase = market_phase(now)
    if quote is None:
        missing.append("auction")
        return f"## 盘口确认\n阶段: {phase}\n状态: 缺失\n竞价涨跌幅: 缺失\n量比: 缺失\n已确认: 缺失"
    change = _auction_change(quote)
    ratio = _volume_ratio(quote, now)
    confirmed = _confirmed(phase, change, quote.get("trade_date"))
    status = "可用" if quote.get("trade_date") else "缺失"
    if status == "缺失":
        missing.append("auction")
    lines = [
        "## 盘口确认",
        f"阶段: {phase}",
        f"状态: {status}",
        f"竞价涨跌幅: {_pct(change)}",
        f"量比: {_ratio_text(ratio)}",
        f"已确认: {confirmed}",
        f"数据日: {quote.get('trade_date') or '缺失'}",
    ]
    if _int(quote.get("sample_days")) < 5:
        lines.append(f"量比样本: {_int(quote.get('sample_days'))}日，不足5日")
    return "\n".join(lines)


def _red_section(items, events, missing) -> str:
    if items is None:
        missing.append("red_news")
        return "## 加红资讯\n状态: 缺失"
    reds = [item for item in items if isinstance(item, dict) and item.get("red")][:MAX_RED]
    if not reds:
        missing.append("red_news")
        return "## 加红资讯\n状态: 缺失\n没有加红稿。不要编造利好或风险。"
    lines = ["## 加红资讯\n状态: 可用\n高优先级: 下面每一条都要写入「利好催化」或「风险警报」，保留标题。"]
    for item in reds:
        title = _clip(item.get("title") or "未命名", MAX_TITLE)
        when = _clip(item.get("published_at") or "时间缺失", 16)
        direction = _news_direction(item, events)
        lines.append(f"- {when} | {direction} | {title}")
    return "\n".join(lines)


def _flow_section(rows, sector, concepts, today, missing, stale) -> str:
    if rows is None and sector is None and not concepts:
        missing.append("fund_flow")
        return "## 主力资金\n状态: 缺失"
    points = _flow_points(rows or [])
    lines = ["## 主力资金"]
    if not points:
        missing.append("fund_flow_series")
        lines.append("近端净流入: 缺失")
    else:
        status = _flow_status(points[-1][0], today)
        if status == "过期":
            stale.append("fund_flow")
        if len(points) < MIN_FLOW_DAYS:
            lines.append(f"状态: {status}\n近{len(points)}日主力净流入（不足{MIN_FLOW_DAYS}日）: {_flow_text(points)}")
        else:
            lines.append(f"状态: {status}\n近{len(points)}日主力净流入: {_flow_text(points)}")
    lines.append(_rank_line("行业", sector))
    for item in (concepts or [])[:2]:
        lines.append(_rank_line("概念", item))
    if sector is None and not concepts:
        missing.append("sector_rank")
        lines.append("板块资金排名: 缺失")
    return "\n".join(lines)


def _fit(header: str, sections: list[str]) -> str:
    text = header + "\n\n" + "\n\n".join(sections)
    if len(text) <= MAX_TEXT:
        return text
    return text[: MAX_TEXT - 1].rstrip() + "…"


def _load_event_lines(symbol: str) -> list[str]:
    from pathlib import Path

    from app.config import settings
    from app.event_context.query import context_lines

    return context_lines(Path(settings.data_dir), symbol)


def _load_ima_lines(symbol: str) -> list[str]:
    from pathlib import Path

    from app.config import settings
    from app.research_reports.ima_store import context_lines

    return context_lines(Path(settings.data_dir), symbol)


def _ima_section(lines, missing) -> str | None:
    if lines is None:
        missing.append("ima_research")
        return "## 券商研报\n状态: 缺失"
    if not lines:
        return None
    body = "\n".join(f"- {_clip(line, 120)}" for line in lines[:3])
    return (
        "## 券商研报\n状态: 可用\n"
        "下面是知识库研报的标题和短摘要，写入「消息面」时保留券商和标题。\n"
        f"{body}"
    )


def _event_context_section(lines, missing) -> str | None:
    if lines is None:
        missing.append("event_context")
        return "## 事件上下文\n状态: 缺失"
    if not lines:
        return None
    body = "\n".join(f"- {_clip(line, 80)}" for line in lines[:8])
    return f"## 事件上下文\n状态: 可用\n{body}"


def _load_events(symbol: str, now: datetime) -> dict:
    from app.news.hot_snapshot import read_served_snapshot
    from app.news.service import public_hot_event

    snapshot = read_served_snapshot()
    public = [
        public_hot_event(item)
        for item in (snapshot.get("events") or [])
        if isinstance(item, dict)
    ]
    note = str(snapshot.get("hint") or "")
    if snapshot.get("fallback") and "非当日" not in note:
        note = (note + " 非当日事件。").strip()
    return {
        "events": match_events(public, symbol),
        "as_of": str(snapshot.get("computed_at") or snapshot.get("as_of") or snapshot.get("updated_at") or now.isoformat()),
        "note": note,
    }


def _load_red_news(symbol: str) -> list[dict]:
    from app.news.service import news_for_symbol

    payload = news_for_symbol(symbol, hours=48, limit=40)
    return [item for item in (payload.get("items") or []) if isinstance(item, dict) and item.get("red")]


def _load_flow(symbol: str, events: list[dict]) -> dict:
    from app.config import settings
    from app.fund_flow import query
    from app.fund_flow.factors import _industry_by_code, latest_sector_ranks
    from app.fund_flow.normalize import code6

    series = query.stock_series(symbol, limit=MAX_FLOW_DAYS)
    rows = [row for row in (series.get("items") or []) if isinstance(row, dict)]
    root = settings.data_dir
    code = code6(symbol)
    sector = _industry_rank(root, code, _industry_by_code, latest_sector_ranks)
    names = []
    for event in events:
        for name in event.get("concepts") or []:
            text = str(name).strip()
            if text and text not in names:
                names.append(text)
    concepts = []
    if names:
        ranked = latest_sector_ranks(root, "concept")
        by_name = {str(row.get("name")): row for row in ranked.to_dicts()} if not ranked.is_empty() else {}
        for name in names[:3]:
            row = by_name.get(name)
            if row:
                concepts.append({
                    "name": name,
                    "rank": row.get("rank"),
                    "net_inflow": row.get("net_inflow"),
                    "trade_date": row.get("trade_date"),
                })
            if len(concepts) >= 2:
                break
    return {"rows": rows, "sector": sector, "concepts": concepts}


def _industry_rank(root, code: str, industry_by_code, latest_ranks) -> dict | None:
    if not code:
        return None
    industry = industry_by_code(root)
    if industry.is_empty():
        return None
    hit = industry.filter(industry["code"] == code)
    if hit.is_empty():
        return None
    # industry_by_code 已按资金榜口径取二级名；再跑一次 ths_industry_level2 兜底，
    # 防止上游换成三级串或只有一级时与榜名对不上（原来直接拿三级名全等比较，
    # 90 个榜名一个都命中不了，所有股票的「行业资金排名」恒为缺失）。
    from app.fund_flow.factors import ths_industry_level2

    name = ths_industry_level2(hit["industry"][0])
    if not name:
        return None
    ranks = latest_ranks(root, "industry")
    if ranks.is_empty():
        return {"name": name, "rank": None, "net_inflow": None, "trade_date": None}
    matched = ranks.filter(ranks["name"] == name)
    if matched.is_empty():
        return {"name": name, "rank": None, "net_inflow": None, "trade_date": None}
    row = matched.to_dicts()[0]
    return {
        "name": name,
        "rank": row.get("rank"),
        "net_inflow": row.get("net_inflow"),
        "trade_date": row.get("trade_date"),
    }


def _load_chip(symbol: str) -> dict | None:
    """最近交易日的筹码汇总。没有日 K 时返回 None，上下文写成缺失。"""
    if not symbol:
        return None
    from app.chip.query import context_payload
    from app.config import settings

    return context_payload(settings.data_dir, symbol)


def _chip_section(chip, missing) -> str:
    source = "来源: 本地计算（东财算法），估算"
    if not isinstance(chip, dict) or not chip.get("computable"):
        missing.append("chip")
        return f"## 筹码分布\n状态: 缺失\n{source}"
    lines = [
        "## 筹码分布",
        source,
        f"日期: {chip.get('date') or '缺失'}",
        f"获利比例: {_plain_pct(chip.get('benefit_ratio'))}",
        f"平均成本: {_price(chip.get('avg_cost'))}",
        f"平均成本偏离: {_pct(_optional_float(chip.get('avg_cost_bias')))}",
        f"90集中度: {_plain_pct(chip.get('concentration_90'))}",
        f"70集中度: {_plain_pct(chip.get('concentration_70'))}",
        f"上方套牢区: {_trapped_zone_text(chip.get('trapped_zones'))}",
    ]
    return "\n".join(lines)


def _load_bars(repo, symbol: str, now: datetime) -> list[dict] | None:
    if repo is None or not symbol:
        return None
    end = now.astimezone(CN_TZ).date()
    start = end - timedelta(days=20)
    getter = getattr(repo, "get_daily", None)
    if getter is None:
        return None
    frame = getter(symbol, start, end, ["date", "open", "close", "raw_close", "volume"])
    if frame is None or getattr(frame, "is_empty", lambda: True)():
        return []
    return frame.to_dicts()


def _capture(fn):
    try:
        return fn(), True
    except Exception:  # noqa: BLE001 — 一段读失败不能让整段分析上下文消失
        logger.warning("个股上下文有一段没读到", exc_info=True)
        return None, False


def _raw_open(row: dict) -> float | None:
    raw_close = _positive(row.get("raw_close"))
    close = _positive(row.get("close"))
    opened = _positive(row.get("open"))
    if raw_close and close and opened:
        return opened * raw_close / close
    if raw_close and opened is None:
        return raw_close
    return None


def _auction_change(quote: dict) -> float | None:
    price = _positive(quote.get("auction_price"))
    prev = _positive(quote.get("prev_close"))
    if price is None or prev is None:
        return None
    return price / prev - 1


def _volume_ratio(quote: dict, now: datetime) -> float | None:
    volume = _positive(quote.get("volume"))
    avg = _positive(quote.get("avg_volume_5d"))
    if volume is None or avg is None:
        return None
    phase = market_phase(now)
    if phase in {"集合竞价", "竞价已撮合"}:
        expected = avg * _AUCTION_SLICE_MINUTES / _SESSION_MINUTES
    else:
        minutes = elapsed_session_minutes(now)
        if not minutes:
            return None
        expected = avg * minutes / _SESSION_MINUTES
    if expected <= 0:
        return None
    return volume / expected


def _confirmed(phase: str, change: float | None, trade_date: str | None) -> str:
    if not trade_date:
        return "缺失"
    if phase in {"盘前", "集合竞价"}:
        return "否"
    if phase == "休市":
        return "缺失"
    if change is None:
        return "缺失"
    return "是"


def _event_status(as_of: str | None, now: datetime) -> str:
    parsed = _parse_time(as_of)
    if parsed is None:
        return "时间未知"
    if now - parsed > _EVENT_STALE:
        return "过期"
    return "可用"


def _flow_points(rows: list[dict]) -> list[tuple[date, float]]:
    points = []
    for row in rows:
        day = _day(row.get("trade_date") or row.get("date"))
        amount = row.get("main_net")
        if day is None or amount is None:
            continue
        try:
            number = float(amount)
        except (TypeError, ValueError):
            continue
        if number != number:
            continue
        points.append((day, number))
    points.sort(key=lambda item: item[0])
    return points[-MAX_FLOW_DAYS:]


def _flow_status(latest: date, today: date) -> str:
    if today - latest > timedelta(days=_FLOW_STALE_DAYS):
        return "过期"
    return "可用"


def _flow_text(points: list[tuple[date, float]]) -> str:
    return "，".join(f"{day.isoformat()[5:]} {_yuan(amount)}" for day, amount in points)


def _rank_line(kind: str, row: dict | None) -> str:
    if not row:
        return f"{kind}资金排名: 缺失"
    name = _clip(row.get("name") or "未命名", 16)
    rank = row.get("rank")
    rank_text = f"第{int(rank)}" if isinstance(rank, int) or (isinstance(rank, float) and rank == int(rank)) else "排名缺失"
    money = "金额缺失" if row.get("net_inflow") is None else _yuan(row.get("net_inflow"))
    day = row.get("trade_date") or "数据日缺失"
    return f"{kind}资金排名: {name} {rank_text} {money}（数据日 {day}）"


def _news_direction(item: dict, events: list[dict]) -> str:
    title = str(item.get("title") or "")
    for event in events:
        direction = event.get("direction")
        if direction not in {"利好", "利空"}:
            continue
        headline = str(event.get("headline") or event.get("name") or "")
        if len(title) >= 8 and (title in headline or headline in title):
            return direction
    return "方向未标"


def _parse_time(value: str | None) -> datetime | None:
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
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 0 and number == number:
        return number
    return None


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _pct(value: float | None) -> str:
    if value is None:
        return "缺失"
    return f"{value * 100:+.2f}%"


def _plain_pct(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "缺失"
    return f"{number * 100:.2f}%"


def _price(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "缺失"
    return f"{number:.2f}"


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _trapped_zone_text(zones: Any) -> str:
    if not isinstance(zones, list) or not zones:
        return "无明显密集套牢区"
    parts: list[str] = []
    for zone in zones[:3]:
        if not isinstance(zone, dict):
            continue
        share = _optional_float(zone.get("share"))
        share_text = "占比缺失" if share is None else f"{share * 100:.2f}%"
        parts.append(f"{zone.get('low')}-{zone.get('high')} 占 {share_text}")
    return "；".join(parts) if parts else "无明显密集套牢区"


def _ratio_text(value: float | None) -> str:
    if value is None:
        return "缺失"
    return f"{value:.2f}"


def _yuan(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "缺失"
    if abs(number) >= 1e8:
        return f"{number / 1e8:+.2f}亿"
    if abs(number) >= 1e4:
        return f"{number / 1e4:+.0f}万"
    return f"{number:+.0f}元"


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text or "缺失"
    return text[:limit] + "…"
