"""DSA 分析价位的盘中提醒。

每份分析的理想买入、止损、目标价记成这只股票的线。新分析替换旧线，
超过 5 个自然日的线删掉。盘中只看实时行情线程已经拿到的原始价，
触及后走现成的钉钉机器人。同一条线 30 分钟内不重复推。
推送不在行情线程里发。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.market_time import CN_TZ
from app.news.extract import canonical_symbol
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger("tsp.dsa.sniper_alerts")

COOLDOWN = timedelta(minutes=30)
STALE_AFTER = timedelta(days=5)
MAX_LINES = 200
_LEVELS = (
    ("ideal_buy", "buy", "理想买入", "down"),
    ("stop_loss", "stop", "止损", "down"),
    ("take_profit", "target", "目标价", "up"),
)
_LABEL = {kind: label for _key, kind, label, _direction in _LEVELS}

_lock = threading.Lock()
_previous: dict[str, float] = {}
_previous_day: dict[str, date] = {}
_queue: list[dict] = []
_worker_started = False
_cache: dict[str, tuple[int, dict]] = {}


def lines_path() -> Path:
    from app.config import settings

    return settings.data_dir / "user_data" / "dsa_sniper_lines.json"


def lines_mtime(path: Path | None = None) -> int:
    target = path or lines_path()
    try:
        return target.stat().st_mtime_ns
    except OSError:
        return 0


def alerts_enabled() -> bool:
    from app.services import preferences

    try:
        return preferences.get_dsa_sniper_alerts_enabled()
    except Exception:  # noqa: BLE001 — 读不到开关时本轮不推，避免误报
        logger.warning("读取价位提醒开关失败", exc_info=True)
        return False


def register(
    payload: dict,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict:
    """用一份分析替换这只股票的线。没有有效价位时保留原来的线。"""
    moment = _local(now)
    symbol = canonical_symbol(str(payload.get("symbol") or ""))
    if not symbol:
        raise ValueError("无法识别股票代码")
    fresh = _lines_from_payload(symbol, payload, moment)
    target = path or lines_path()
    with _lock:
        state = _read(target)
        if not fresh:
            kept = _active(state.get("lines") or [], moment)
            return {"symbol": symbol, "lines": [row for row in kept if row["symbol"] == symbol], "replaced": False}
        if _same_analysis(state.get("lines") or [], symbol, fresh):
            kept = _active(state.get("lines") or [], moment)
            return {"symbol": symbol, "lines": [row for row in kept if row["symbol"] == symbol], "replaced": False}
        others = [row for row in (state.get("lines") or []) if row.get("symbol") != symbol]
        fired = {
            key: value
            for key, value in (state.get("fired") or {}).items()
            if not str(key).startswith(f"{symbol}:")
        }
        lines = _trim(others + fresh, moment)
        _write(target, {"version": 1, "lines": lines, "fired": fired})
        _previous.pop(symbol, None)
        _previous_day.pop(symbol, None)
    return {"symbol": symbol, "lines": fresh, "replaced": True}


def chart_levels(symbol: str, *, now: datetime | None = None, path: Path | None = None) -> list[dict]:
    moment = _local(now)
    code = canonical_symbol(symbol) or symbol
    rows = []
    for line in _active(_read(path or lines_path()).get("lines") or [], moment):
        if line.get("symbol") != code:
            continue
        rows.append({
            "kind": line["kind"],
            "label": line["label"],
            "price": line["price"],
        })
    rows.sort(key=lambda item: ("buy", "stop", "target").index(item["kind"]) if item["kind"] in {"buy", "stop", "target"} else 9)
    return rows


def evaluate(
    lines: list[dict],
    prices: dict[str, float],
    previous: dict[str, float],
    fired: dict[str, str],
    now: datetime,
    *,
    cooldown: timedelta = COOLDOWN,
    gap_from: dict[str, float] | None = None,
) -> tuple[list[dict], dict[str, float], dict[str, str]]:
    """比较上一笔原始价和这一笔。没有参照价时只记账。

    gap_from 是前收或上一交易日的最后一笔。当天第一笔相对它穿过价位时触发，并标成开盘跳空。
    """
    moment = _local(now)
    next_previous = dict(previous)
    next_fired = dict(fired)
    opens = gap_from or {}
    triggers = []
    seen_prev: dict[str, float | None] = {}
    seen_gap: dict[str, bool] = {}
    for line in lines:
        symbol = str(line.get("symbol") or "")
        price = prices.get(symbol)
        if price is None or price <= 0:
            continue
        if symbol not in seen_prev:
            seen_prev[symbol] = next_previous.get(symbol)
            seen_gap[symbol] = seen_prev[symbol] is None and symbol in opens
            if seen_prev[symbol] is None:
                seen_prev[symbol] = opens.get(symbol)
            next_previous[symbol] = price
        prev = seen_prev[symbol]
        if prev is None:
            continue
        kind = str(line.get("kind") or "")
        level = line.get("price")
        if not isinstance(level, (int, float)) or isinstance(level, bool) or level <= 0:
            continue
        direction = "up" if kind == "target" else "down"
        if not _crossed(direction, prev, price, float(level)):
            continue
        key = f"{symbol}:{kind}"
        last = _parse_time(next_fired.get(key))
        if last is not None and moment - last < cooldown:
            continue
        next_fired[key] = moment.isoformat(timespec="seconds")
        triggers.append({
            "id": key,
            "symbol": symbol,
            "name": str(line.get("name") or ""),
            "kind": kind,
            "label": str(line.get("label") or _LABEL.get(kind) or kind),
            "line": round(float(level), 4),
            "price": round(price, 4),
            "analyzed_at": str(line.get("analyzed_at") or ""),
            "gap": bool(seen_gap.get(symbol)),
        })
    return triggers, next_previous, next_fired


def observe_frame(frame, *, now: datetime | None = None, enqueue: bool = True, references=None) -> list[dict]:
    """行情线程调用。只筛有价位线的代码，网络推送另走队列。

    references 是本轮行情补充表，用来取前收。当天还没有盘中价时，用前收判断是不是开盘跳空。
    """
    if frame is None or not alerts_enabled():
        return []
    moment = _local(now)
    target = lines_path()
    with _lock:
        state = _read_cached(target)
        lines = _active(state.get("lines") or [], moment)
        expired = len(lines) != len(state.get("lines") or [])
        if not lines:
            if expired:
                _write(target, {"version": 1, "lines": [], "fired": {}})
            return []
        symbols = {str(row.get("symbol") or "") for row in lines}
        prices = _prices(frame, symbols)
        if not prices:
            if expired:
                _write(target, {"version": 1, "lines": lines, "fired": state.get("fired") or {}})
            return []
        today = moment.date()
        same_day: dict[str, float] = {}
        gap_from: dict[str, float] = {}
        for symbol, prior in _previous.items():
            seen = _previous_day.get(symbol)
            if seen is None or seen == today:
                same_day[symbol] = prior
            else:
                gap_from[symbol] = prior
        for symbol, prior in _prior_closes(references if references is not None else frame, symbols).items():
            if symbol not in same_day:
                gap_from.setdefault(symbol, prior)
        triggers, updated, fired = evaluate(
            lines,
            prices,
            same_day,
            state.get("fired") or {},
            moment,
            gap_from=gap_from,
        )
        _previous.update(updated)
        for symbol in updated:
            _previous_day[symbol] = today
        if triggers or expired:
            _write(target, {"version": 1, "lines": lines, "fired": fired})
        if enqueue and triggers:
            _queue.extend(triggers)
    if enqueue and triggers:
        _ensure_worker()
    return triggers


def alert_text(item: dict) -> tuple[str, str]:
    name = item.get("name") or item.get("symbol") or "股票"
    label = item.get("label") or "价位"
    title = f"{label} {name}"
    how = "开盘跳空触及" if item.get("gap") else "触及"
    text = (
        f"### {label}\n\n"
        f"{name}（{item.get('symbol')}）现价 {_price(item.get('price'))}，"
        f"{how}{label} {_price(item.get('line'))}。\n\n"
        f"分析时间 {item.get('analyzed_at') or '未知'}。"
    )
    return title[:64], text


def drain(*, sender=None) -> int:
    """测试和后台线程共用。把队列里的提醒交出去。"""
    with _lock:
        batch = list(_queue)
        _queue.clear()
    send = sender or _dingtalk_send
    sent = 0
    for item in batch:
        try:
            send(item)
            sent += 1
        except Exception:  # noqa: BLE001 — 推送失败不能把行情线程拖住，冷却已经记下
            logger.warning("价位提醒推送失败 %s", item.get("symbol"), exc_info=True)
    return sent


def _lines_from_payload(symbol: str, payload: dict, now: datetime) -> list[dict]:
    analyzed_at = str(payload.get("analyzed_at") or now.isoformat(timespec="seconds"))
    analysis_id = str(payload.get("analysis_id") or "")[:64]
    name = str(payload.get("name") or "")[:40]
    nested = payload.get("levels") if isinstance(payload.get("levels"), dict) else {}
    rows = []
    for key, kind, label, _direction in _LEVELS:
        price = _price_value(payload.get(key) if key in payload else nested.get(key))
        if price is None:
            continue
        rows.append({
            "id": f"{symbol}:{kind}",
            "symbol": symbol,
            "name": name,
            "kind": kind,
            "label": label,
            "price": price,
            "analysis_id": analysis_id,
            "analyzed_at": analyzed_at,
        })
    return rows


def _same_analysis(lines: list[dict], symbol: str, fresh: list[dict]) -> bool:
    current = [row for row in lines if row.get("symbol") == symbol]
    if not current or not fresh:
        return False
    if current[0].get("analysis_id") != fresh[0].get("analysis_id") or not fresh[0].get("analysis_id"):
        return False
    old = {row.get("kind"): row.get("price") for row in current}
    new = {row.get("kind"): row.get("price") for row in fresh}
    return old == new


def _active(lines: list[dict], now: datetime) -> list[dict]:
    kept = []
    for row in lines:
        if not isinstance(row, dict):
            continue
        parsed = _parse_time(row.get("analyzed_at"))
        if parsed is None or now - parsed > STALE_AFTER:
            continue
        price = _price_value(row.get("price"))
        if price is None or row.get("kind") not in _LABEL:
            continue
        kept.append(row)
    return kept


def _trim(lines: list[dict], now: datetime) -> list[dict]:
    fresh = _active(lines, now)
    if len(fresh) <= MAX_LINES:
        return fresh
    fresh.sort(key=lambda row: str(row.get("analyzed_at") or ""))
    return fresh[-MAX_LINES:]


def _prior_closes(frame, symbols: set[str]) -> dict[str, float]:
    if frame is None or not symbols or getattr(frame, "is_empty", lambda: True)():
        return {}
    columns = getattr(frame, "columns", [])
    if "symbol" not in columns or "prev_close" not in columns:
        return {}
    try:
        import polars as pl

        slim = frame.filter(pl.col("symbol").is_in(list(symbols))).select("symbol", "prev_close")
    except Exception:  # noqa: BLE001 — 行情表结构异常时跳过这一轮
        logger.warning("价位提醒没能读取前收", exc_info=True)
        return {}
    found = {}
    for row in slim.to_dicts():
        symbol = canonical_symbol(str(row.get("symbol") or "")) or str(row.get("symbol") or "")
        price = _price_value(row.get("prev_close"))
        if symbol and price is not None:
            found[symbol] = price
    return found


def _prices(frame, symbols: set[str]) -> dict[str, float]:
    if not symbols or getattr(frame, "is_empty", lambda: True)():
        return {}
    columns = getattr(frame, "columns", [])
    if "symbol" not in columns:
        return {}
    price_col = "close" if "close" in columns else "last_price" if "last_price" in columns else ""
    if not price_col:
        return {}
    try:
        import polars as pl

        slim = frame.filter(pl.col("symbol").is_in(list(symbols))).select("symbol", price_col)
    except Exception:  # noqa: BLE001 — 行情表结构异常时跳过这一轮
        logger.warning("价位提醒没能读取行情表", exc_info=True)
        return {}
    found = {}
    for row in slim.to_dicts():
        symbol = canonical_symbol(str(row.get("symbol") or "")) or str(row.get("symbol") or "")
        price = _price_value(row.get(price_col))
        if symbol and price is not None:
            found[symbol] = price
    return found


def _crossed(direction: str, prev: float, price: float, line: float) -> bool:
    if direction == "down":
        return prev > line >= price
    return prev < line <= price


def _read_cached(path: Path) -> dict:
    key = str(path)
    mtime = lines_mtime(path)
    cached = _cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    state = _read(path)
    _cache[key] = (mtime, state)
    return state


def _read(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "lines": [], "fired": {}}
    except (OSError, json.JSONDecodeError):
        logger.warning("价位线文件读不了，本轮当作没有线", exc_info=True)
        return {"version": 1, "lines": [], "fired": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "lines": [], "fired": {}}
    lines = payload.get("lines") if isinstance(payload.get("lines"), list) else []
    fired = payload.get("fired") if isinstance(payload.get("fired"), dict) else {}
    return {"version": 1, "lines": lines, "fired": {str(key): str(value) for key, value in fired.items()}}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    _cache[str(path)] = (lines_mtime(path), payload)


def _ensure_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_send_loop, name="dsa-sniper-alert", daemon=True).start()


def _send_loop() -> None:
    while True:
        try:
            drain()
        except Exception:  # noqa: BLE001 — 后台线程不能退出
            logger.warning("价位提醒队列失败", exc_info=True)
        threading.Event().wait(0.5)


def _dingtalk_send(item: dict) -> None:
    from app.config import settings
    from app.news.dingtalk import send_markdown

    url = (settings.dingtalk_webhook_url or "").strip()
    if not url:
        logger.info("未配置钉钉机器人，价位提醒 %s 不推送", item.get("symbol"))
        return
    title, text = alert_text(item)
    send_markdown(url, (settings.dingtalk_secret or "").strip(), title, text)


def _local(now: datetime | None) -> datetime:
    moment = now or datetime.now(CN_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CN_TZ)
    return moment.astimezone(CN_TZ)


def _parse_time(value: Any) -> datetime | None:
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


def _price_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("元", "")
        if not text or text in {"-", "—", "无", "N/A", "null", "None"}:
            return None
        number = _first_number(text)
        if number is None:
            return None
    else:
        return None
    if number > 0 and number == number and number != float("inf"):
        return round(number, 4)
    return None


def _first_number(text: str) -> float | None:
    token = []
    started = False
    for char in text:
        if char.isdigit() or (char == "." and started):
            token.append(char)
            started = True
            continue
        if started:
            break
    if not token:
        return None
    try:
        return float("".join(token))
    except ValueError:
        return None


def _price(value: Any) -> str:
    number = _price_value(value)
    if number is None:
        return "未知"
    return f"{number:.2f}"
