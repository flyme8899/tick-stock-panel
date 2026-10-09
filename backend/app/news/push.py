"""钉钉推送框架。热点候选、异动监控、做T提醒各自开关、时段和冷却。

默认全关。登录失效提醒仍走 host_collector 的短文本，不从这里发送。
行情只读已经算好的 enriched / 资讯库，不额外打 TickFlow。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

from app.config import settings
from app.news.config import (
    SOURCE_LABELS,
    push_master_enabled,
    push_type_enabled,
    webhook_configured,
)
from app.news.dingtalk import send_markdown

logger = logging.getLogger(__name__)

PRIVATE_SOURCES = frozenset({"dws", "zsxq", "ima"})
LINK_SOURCES = frozenset({"cls", "wscn"})
ABNORMAL_SIGNALS = frozenset({
    "limit_up", "broken", "recovery", "limit_down", "new_high", "new_low",
})
SIGNAL_LABELS = {
    "limit_up": "涨停",
    "broken": "炸板",
    "recovery": "跌停翘板",
    "limit_down": "跌停",
    "new_high": "60日新高",
    "new_low": "60日新低",
}
T_LABELS = {
    "above_vwap": "高于分时均价",
    "below_vwap": "低于分时均价",
    "near_high": "接近日内高点",
    "near_low": "接近日内低点",
    "near_prev_close": "回到昨收附近",
}
TYPE_LABELS = {"hot": "热点候选", "abnormal": "异动监控", "t_trade": "做T提醒"}

HOT_CHECK_S = 300
INTRADAY_CHECK_S = 180
SLOT_WINDOW = timedelta(minutes=20)
RATE_LIMIT = 20
RATE_WINDOW_S = 60
ABNORMAL_CAP = 80
T_CAP = 40
VWAP_BAND = 0.015
RANGE_BAND = 0.003
PREV_BAND = 0.003
PREV_AWAY = 0.01
RANGE_MIN = 0.01

_STATE: PushState | None = None
_STATE_LOCK = threading.Lock()


def _clock(name: str, default: dt_time) -> dt_time:
    raw = str(getattr(settings, name, "") or "").strip()
    if not raw:
        return default
    try:
        hour, minute = raw.split(":", 1)
        return dt_time(int(hour), int(minute))
    except ValueError:
        return default


def premarket_at() -> dt_time:
    return _clock("news_push_premarket", dt_time(8, 45))


def postclose_at() -> dt_time:
    return _clock("news_push_postclose", dt_time(15, 40))


def top_n() -> int:
    return max(1, min(int(settings.news_push_top_n or 5), 20))


def min_stories() -> int:
    return max(1, min(int(settings.news_push_min_stories or 2), 20))


def score_jump() -> float:
    try:
        value = float(settings.news_push_score_jump)
    except (TypeError, ValueError):
        value = 0.5
    return value if value > 0 else 0.5


def hot_cooldown_s() -> int:
    return max(60, int(settings.news_push_hot_cooldown_min or 30) * 60)


def symbol_cooldown_s() -> int:
    return max(60, int(settings.news_push_symbol_cooldown_min or 30) * 60)


def t_cooldown_s() -> int:
    return max(60, int(settings.news_push_t_cooldown_min or 20) * 60)


def pick_refs(messages: list[dict], *, limit: int = 3) -> list[dict]:
    """每条候选最多 3 个出处。钉钉、知识星球、ima 只留来源名，不带标题、链接和正文。"""
    refs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for msg in messages:
        source = str(msg.get("source") or "")
        if source in PRIVATE_SOURCES:
            label = str(msg.get("source_label") or SOURCE_LABELS.get(source, source))
            if ("name", label) in seen:
                continue
            seen.add(("name", label))
            refs.append({"kind": "name", "label": label})
        elif source in LINK_SOURCES:
            url = str(msg.get("url") or "")
            label = str(msg.get("source_label") or SOURCE_LABELS.get(source, source))
            title = str(msg.get("title") or label)[:40]
            if url.startswith(("http://", "https://")):
                if ("url", url) in seen:
                    continue
                seen.add(("url", url))
                refs.append({"kind": "link", "label": title, "url": url})
            elif ("name", label) not in seen:
                seen.add(("name", label))
                refs.append({"kind": "name", "label": label})
        if len(refs) >= limit:
            break
    return refs


def _ref_text(ref: dict) -> str:
    if ref.get("kind") == "link" and ref.get("url"):
        return f"[{ref.get('label') or '链接'}]({ref['url']})"
    return str(ref.get("label") or "")


def format_hot_markdown(sectors: list[dict], stocks: list[dict], *, heading: str) -> tuple[str, str] | None:
    """热点候选。heading 已含【热点候选】。没有候选时不发。"""
    blocks = []
    for title, rows in (("热门板块", sectors), ("热门个股", stocks)):
        if not rows:
            continue
        lines = [f"**{title}**"]
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"{index}. {row.get('name') or row.get('key')} · "
                f"提及 {row.get('story_count', 0)} · "
                f"来源 {row.get('source_count', 0)} · "
                f"相对基线 {row.get('growth', 0)} 倍"
            )
            for ref in row.get("refs") or []:
                text = _ref_text(ref)
                if text:
                    lines.append(f"   - {text}")
        blocks.append("\n".join(lines))
    if not blocks:
        return None
    title = heading[:64]
    text = f"### {heading}\n\n" + "\n\n".join(blocks) + "\n\n仅供内部研究，不含资讯正文。"
    return title, text


def format_symbol_markdown(kind: str, rows: list[dict]) -> tuple[str, str] | None:
    label = TYPE_LABELS[kind]
    if not rows:
        return None
    lines = [f"### 【{label}】"]
    for row in rows[:15]:
        extra = row.get("detail") or ""
        suffix = f" · {extra}" if extra else ""
        lines.append(f"- {row.get('name') or row['symbol']} {row['symbol']}{suffix}")
    lines.append("\n仅供内部研究。")
    return f"【{label}】", "\n".join(lines)


def format_test_markdown() -> tuple[str, str]:
    text = (
        "### 【测试】热点候选推送\n\n"
        "这是一条手动测试，没有附带资讯正文。\n\n"
        "示例：半导体 · 提及 3 · 来源 2 · 相对基线 1.8 倍\n\n"
        "正式推送会标成【热点候选】、【异动监控】或【做T提醒】。"
        "登录失效仍是单独的短文本。"
    )
    return "【测试】热点候选推送", text


def hot_material(previous: list[dict] | None, current: list[dict], *, top_n: int, min_stories: int, jump: float) -> list[dict]:
    """新进入前 N 且提及数够，或仍在前 N 但分数跳升。没有上次快照时只建立基线。"""
    if previous is None:
        return []
    prev = {(item["kind"], item["key"]): item for item in previous}
    changed = []
    for item in current:
        if int(item.get("rank") or 0) > top_n:
            continue
        ident = (item["kind"], item["key"])
        old = prev.get(ident)
        if old is None:
            if int(item.get("story_count") or 0) >= min_stories:
                changed.append({**item, "reason": "new"})
            continue
        old_score = float(old.get("score") or 0)
        new_score = float(item.get("score") or 0)
        if old_score > 0 and (new_score - old_score) / old_score >= jump:
            changed.append({**item, "reason": "jump"})
    return changed


def new_edges(previous: dict[str, set[str]] | None, current: dict[str, set[str]]) -> list[tuple[str, str]]:
    """第一次见到当天状态时不推，避免启动时把已经成立的信号全发出去。"""
    if previous is None:
        return []
    found = []
    for symbol, signals in current.items():
        before = previous.get(symbol, set())
        for signal in sorted(signals - before):
            found.append((symbol, signal))
    return found


def t_conditions(row: dict) -> tuple[set[str], float | None]:
    """用当日累计均价和日内高低。窗口不够或价格缺失的条件不成立。"""
    price = _num(row.get("close"))
    high = _num(row.get("high"))
    low = _num(row.get("low"))
    volume = _num(row.get("volume"))
    amount = _num(row.get("amount"))
    prev = _num(row.get("prev_close"))
    found: set[str] = set()
    pct = None
    if price and volume and amount and volume > 0:
        vwap = amount / (volume * 100.0)
        if vwap > 0:
            dev = price / vwap - 1
            if dev >= VWAP_BAND:
                found.add("above_vwap")
            elif dev <= -VWAP_BAND:
                found.add("below_vwap")
    if price and high and low and low > 0 and (high - low) / low >= RANGE_MIN:
        if high > 0 and price / high - 1 >= -RANGE_BAND:
            found.add("near_high")
        if price / low - 1 <= RANGE_BAND:
            found.add("near_low")
    if price and prev and prev > 0:
        pct = price / prev - 1
        if abs(pct) <= PREV_BAND:
            found.add("near_prev_close")
    return found, pct


def filter_prev_close(entered: list[tuple[str, str]], prev_pct: dict[str, float], current_pct: dict[str, float]) -> list[tuple[str, str]]:
    """回到昨收只在先前已经偏离至少 1% 时提醒。"""
    kept = []
    for symbol, reason in entered:
        if reason != "near_prev_close":
            kept.append((symbol, reason))
            continue
        before = prev_pct.get(symbol)
        if before is None or abs(before) < PREV_AWAY:
            continue
        if abs(current_pct.get(symbol) or 0) > PREV_BAND:
            continue
        kept.append((symbol, reason))
    return kept


def in_slot(now: datetime, start: dt_time, window: timedelta = SLOT_WINDOW) -> bool:
    begin = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    return begin <= now < begin + window


def in_spans(now: datetime, spans: tuple[tuple[dt_time, dt_time], ...]) -> bool:
    clock = now.timetz().replace(tzinfo=None)
    return any(start <= clock <= end for start, end in spans)


ABNORMAL_SPANS = (
    (dt_time(9, 25), dt_time(11, 30)),
    (dt_time(13, 0), dt_time(15, 5)),
)
T_SPANS = (
    (dt_time(9, 35), dt_time(11, 25)),
    (dt_time(13, 5), dt_time(14, 55)),
)


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return float(value)


class PushState:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data = self._read()

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"sent_at": [], "slots": {}, "cooldowns": {}, "edges": {}, "t_pct": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        payload.setdefault("sent_at", [])
        payload.setdefault("slots", {})
        payload.setdefault("cooldowns", {})
        payload.setdefault("edges", {})
        payload.setdefault("t_pct", {})
        return payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def rate_ok(self, now_ts: float) -> bool:
        fresh = [item for item in self.data["sent_at"] if now_ts - float(item) < RATE_WINDOW_S]
        self.data["sent_at"] = fresh
        return len(fresh) < RATE_LIMIT

    def mark_sent(self, now_ts: float) -> None:
        self.data["sent_at"].append(now_ts)
        self.data["sent_at"] = [
            item for item in self.data["sent_at"] if now_ts - float(item) < RATE_WINDOW_S
        ]

    def cooling(self, key: str, now_ts: float) -> bool:
        until = float(self.data["cooldowns"].get(key) or 0)
        return now_ts < until

    def cool_until(self, key: str, now_ts: float, seconds: int) -> None:
        self.data["cooldowns"][key] = now_ts + seconds


def get_state() -> PushState:
    global _STATE
    with _STATE_LOCK:
        path = settings.data_dir / "news" / "push_state.json"
        if _STATE is None or _STATE.path != path:
            _STATE = PushState(path)
        return _STATE


def reset_state_for_tests(path: Path) -> PushState:
    global _STATE
    with _STATE_LOCK:
        _STATE = PushState(path)
        return _STATE


def _snapshot_rows(items: list[dict]) -> list[dict]:
    rank = {"sector": 0, "stock": 0}
    rows = []
    for item in items:
        kind = item["kind"]
        rank[kind] = rank.get(kind, 0) + 1
        rows.append({
            "kind": kind,
            "key": item["key"],
            "name": item.get("name") or item["key"],
            "score": item.get("score") or 0,
            "story_count": item.get("story_count") or 0,
            "source_count": len(item.get("sources") or []),
            "growth": item.get("growth") or 0,
            "rank": rank[kind],
        })
    return rows


def _edge_map(state: PushState, kind: str, day: str) -> dict[str, set[str]] | None:
    bucket = state.data["edges"].get(kind) or {}
    if bucket.get("day") != day:
        return None
    raw = bucket.get("symbols") or {}
    return {symbol: set(signals) for symbol, signals in raw.items()}


def _store_edges(state: PushState, kind: str, day: str, current: dict[str, set[str]]) -> None:
    state.data["edges"][kind] = {
        "day": day,
        "symbols": {symbol: sorted(signals) for symbol, signals in current.items()},
    }


def _due(state: PushState, key: str, now_ts: float, interval: int) -> bool:
    last = float(state.data.get(key) or 0)
    return now_ts - last >= interval


def _send(kind: str, title: str, text: str, opener, now_ts: float, state: PushState) -> bool:
    if not state.rate_ok(now_ts):
        logger.info("钉钉推送达到每分钟 %s 条上限，本轮跳过 %s", RATE_LIMIT, TYPE_LABELS.get(kind, kind))
        return False
    send_markdown(
        settings.dingtalk_webhook_url.strip(),
        (settings.dingtalk_secret or "").strip(),
        title,
        text,
        opener=opener,
    )
    state.mark_sent(now_ts)
    return True


def send_test(*, confirm: bool, opener=None, state: PushState | None = None, now: datetime | None = None) -> dict:
    if not confirm:
        raise ValueError("需要明确确认后才发送测试消息")
    if not webhook_configured():
        raise ValueError("未配置钉钉机器人")
    state = state or get_state()
    moment = now or _now()
    title, text = format_test_markdown()
    with state._lock:
        if not _send("test", title, text, opener, moment.timestamp(), state):
            raise RuntimeError("钉钉机器人每分钟最多 20 条，请稍后再试")
        state.save()
    return {"ok": True}


def tick(
    now: datetime | None = None,
    *,
    state: PushState | None = None,
    opener=None,
    trading: bool | None = None,
    hot_loader=None,
    abnormal_loader=None,
    t_loader=None,
) -> list[str]:
    """到点或触发时各发一条。总开关或该类关闭时什么都不做。"""
    if not push_master_enabled():
        return []
    moment = now or _now()
    state = state or get_state()
    sent: list[str] = []
    with state._lock:
        if push_type_enabled("hot") and _push_hot(moment, state, opener, trading, hot_loader):
            sent.append("hot")
        if push_type_enabled("abnormal") and _push_edges(
            moment, state, opener, trading, "abnormal", abnormal_loader,
        ):
            sent.append("abnormal")
        if push_type_enabled("t_trade") and _push_edges(
            moment, state, opener, trading, "t_trade", t_loader,
        ):
            sent.append("t_trade")
        state.save()
    return sent


def _open_slot(now: datetime, state: PushState, day: str) -> str | None:
    if in_slot(now, premarket_at()) and f"{day}:pre" not in state.data["slots"]:
        return "pre"
    if in_slot(now, postclose_at()) and f"{day}:post" not in state.data["slots"]:
        return "post"
    return None


def _push_hot(now: datetime, state: PushState, opener, trading: bool | None, loader) -> bool:
    open_day = _is_trading(now) if trading is None else trading
    if not open_day:
        return False
    stamp = now.timestamp()
    day = now.date().isoformat()
    slot = _open_slot(now, state, day)
    change_due = _due(state, "last_hot_check", stamp, HOT_CHECK_S)
    if slot is None and not change_due:
        return False
    if slot is None and state.cooling("hot", stamp):
        state.data["last_hot_check"] = stamp
        return False
    rows = (loader or _load_hot)()
    snapshot = _snapshot_rows(rows)
    previous = state.data.get("hot_snapshot")
    if previous is not None and state.data.get("hot_day") != day:
        previous = None
    heading = None
    if slot == "pre":
        heading = "【热点候选】盘前"
    elif slot == "post":
        heading = "【热点候选】收盘"
    elif change_due and not state.cooling("hot", stamp):
        changed = hot_material(
            previous, snapshot, top_n=top_n(), min_stories=min_stories(), jump=score_jump(),
        )
        if changed:
            heading = "【热点候选】变化"
    if heading is None:
        state.data["last_hot_check"] = stamp
        state.data["hot_snapshot"] = snapshot
        state.data["hot_day"] = day
        return False
    limit = top_n()
    sectors = [row for row in snapshot if row["kind"] == "sector"][:limit]
    stocks = [row for row in snapshot if row["kind"] == "stock"][:limit]
    if loader is None:
        _attach_refs(sectors, stocks)
    packed = format_hot_markdown(sectors, stocks, heading=heading)
    if packed is None:
        state.data["last_hot_check"] = stamp
        return False
    if not _send("hot", packed[0], packed[1], opener, stamp, state):
        return False
    state.data["last_hot_check"] = stamp
    state.data["hot_snapshot"] = snapshot
    state.data["hot_day"] = day
    if slot:
        state.data["slots"][f"{day}:{slot}"] = stamp
    state.cool_until("hot", stamp, hot_cooldown_s())
    return True


def _push_edges(now, state, opener, trading, kind: str, loader) -> bool:
    open_day = _is_trading(now) if trading is None else trading
    spans = ABNORMAL_SPANS if kind == "abnormal" else T_SPANS
    if not open_day or not in_spans(now, spans):
        return False
    stamp = now.timestamp()
    check_key = f"last_{kind}_check"
    if not _due(state, check_key, stamp, INTRADAY_CHECK_S):
        return False
    state.data[check_key] = stamp
    day = now.date().isoformat()
    loaded = (loader or (_load_abnormal if kind == "abnormal" else _load_t))()
    current, pct, names = _split_loader(loaded)
    previous = _edge_map(state, kind, day)
    entered = new_edges(previous, current)
    if kind == "t_trade":
        old_pct = state.data.get("t_pct") if state.data.get("t_pct_day") == day else {}
        entered = filter_prev_close(entered, old_pct or {}, pct)
    seconds = symbol_cooldown_s() if kind == "abnormal" else t_cooldown_s()
    fresh = [
        (symbol, reason)
        for symbol, reason in entered
        if not state.cooling(f"{kind}:{symbol}:{reason}", stamp)
    ]
    if not fresh:
        _store_edges(state, kind, day, current)
        if kind == "t_trade":
            state.data["t_pct"] = pct
            state.data["t_pct_day"] = day
        return False
    detail_of = _detail_lookup(kind, fresh)
    rows = [
        {
            "symbol": symbol,
            "name": names.get(symbol) or symbol,
            "detail": detail_of.get((symbol, reason), ""),
        }
        for symbol, reason in fresh[:15]
    ]
    packed = format_symbol_markdown(kind, rows)
    if packed is None or not _send(kind, packed[0], packed[1], opener, stamp, state):
        return False
    _store_edges(state, kind, day, current)
    if kind == "t_trade":
        state.data["t_pct"] = pct
        state.data["t_pct_day"] = day
    for symbol, reason in fresh:
        state.cool_until(f"{kind}:{symbol}:{reason}", stamp, seconds)
    return True


def _split_loader(loaded) -> tuple[dict, dict, dict]:
    if len(loaded) == 3:
        current, pct, names = loaded
        return current, pct or {}, names or {}
    current, pct = loaded
    return current, pct or {}, {}


def _detail_lookup(kind: str, fresh) -> dict:
    labels = SIGNAL_LABELS if kind == "abnormal" else T_LABELS
    return {(symbol, reason): labels.get(reason, reason) for symbol, reason in fresh}


def _load_hot() -> list[dict]:
    from app.news.service import hot_candidates
    rows = []
    for kind in ("sector", "stock"):
        for item in hot_candidates(kind=kind, limit=top_n()):
            rows.append({
                "kind": item.kind,
                "key": item.key,
                "name": item.name,
                "score": item.score,
                "story_count": item.story_count,
                "sources": list(item.sources),
                "growth": item.growth,
            })
    return rows


def _attach_refs(sectors: list[dict], stocks: list[dict]) -> None:
    from app.news.service import hot_messages
    for row in [*sectors, *stocks]:
        try:
            messages = hot_messages(row["kind"], row["key"], limit=8)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取候选出处失败 %s: %s", row.get("key"), exc)
            messages = []
        row["refs"] = pick_refs(messages)


def _load_abnormal() -> tuple[dict[str, set[str]], dict, dict]:
    from app.news.service import _repo, hot_candidates
    from app.services.abnormal_moves import build_intraday
    from app.services.watchlist import list_symbols

    symbols = [str(row.get("symbol") or "") for row in list_symbols()]
    symbols = [item for item in symbols if item]
    if _flag_on("news_push_abnormal_include_hot"):
        for item in hot_candidates(kind="stock", limit=top_n()):
            if item.key not in symbols:
                symbols.append(item.key)
    symbols = symbols[:ABNORMAL_CAP]
    repo = _repo()
    if repo is None or not symbols:
        return {}, {}, {}
    payload = build_intraday(repo, limit=ABNORMAL_CAP, symbols=set(symbols))
    current: dict[str, set[str]] = {}
    names = {}
    for row in payload.get("rows") or []:
        symbol = str(row.get("symbol") or "")
        signals = {item for item in (row.get("signals") or []) if item in ABNORMAL_SIGNALS}
        if symbol and signals:
            current[symbol] = signals
            names[symbol] = str(row.get("name") or symbol)
    return current, {}, names


def _load_t() -> tuple[dict[str, set[str]], dict, dict]:
    import polars as pl

    from app.news.service import _repo
    from app.services.watchlist import list_symbols
    from app.strategy import paper

    symbols: list[str] = []
    try:
        accounts = paper.list_accounts(settings.data_dir) or []
    except Exception:  # noqa: BLE001
        accounts = []
    if not accounts:
        accounts = [{"id": paper.DEFAULT_ACCOUNT_ID}]
    for account in accounts:
        account_id = account.get("id") or paper.DEFAULT_ACCOUNT_ID
        try:
            positions = paper.load_positions(settings.data_dir, account_id)
        except Exception:  # noqa: BLE001
            continue
        for symbol, pos in positions.items():
            if int(pos.get("qty") or 0) > 0 and symbol not in symbols:
                symbols.append(symbol)
    if _flag_on("news_push_t_include_watchlist"):
        for row in list_symbols():
            symbol = str(row.get("symbol") or "")
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    symbols = symbols[:T_CAP]
    repo = _repo()
    if repo is None or not symbols:
        return {}, {}, {}
    frame, _date = repo.get_enriched_latest()
    if frame is None or frame.is_empty() or "symbol" not in frame.columns:
        return {}, {}, {}
    frame = frame.filter(pl.col("symbol").is_in(symbols))
    cols = [name for name in ("symbol", "name", "close", "high", "low", "volume", "amount", "prev_close") if name in frame.columns]
    current: dict[str, set[str]] = {}
    pct: dict[str, float] = {}
    names = {}
    for row in frame.select(cols).to_dicts():
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        signals, change = t_conditions(row)
        current[symbol] = signals
        if change is not None:
            pct[symbol] = change
        names[symbol] = str(row.get("name") or symbol)
    return current, pct, names


def _flag_on(field: str) -> bool:
    from app.news.config import _flag
    env = field.upper()
    return _flag(env) is True


def _is_trading(now: datetime) -> bool:
    from app.services.trading_day import is_trading_day
    verdict = is_trading_day(now)
    if verdict is None:
        return now.weekday() < 5
    return bool(verdict)


def _now() -> datetime:
    from app.market_time import cn_now
    return cn_now()

