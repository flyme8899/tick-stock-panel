"""妙想 news-search 补全当日榜首热点。

只在新热点第一次冲上榜首、开关开着、当日预算未满、且该事件还没有缓存时才请求。
缓存按快照里的交易日加事件 key，落在 ``data/news/mx_hot/<交易日>/``。
每日预算按北京时间自然日计数（妙想免费额度按自然日，不按交易日），
写在 ``data/news/mx_hot/budget.json``。这份额度和资金流的
``FUND_FLOW_MX_MAX_CALLS`` 不是同一份，也不读那份配置。

超时、非 200、空结果和解析失败只记日志，事件保持原样，异常不抛给快照和 API。
摘要、来源和方向只看北京时间近 3 个自然日（含当天）的稿：先按日期新近，再按权威度。
没有新稿时不改摘要和方向。来源在这个窗口里优先带跳转链接的条目。
方向只在权威稿（authorityLevel 以 L1 开头）给出明确利好或利空时才改，
判定复用热门事件的 ``_direction_signal``（否定句、削减订单先于「订单」）。
对不上事件、或信号不确定时，不改方向和个股。

缓存文件带 ``CACHE_VERSION``。旧版本（包括还没有版本号的文件）不再展示，
也不再为同一事件重新请求，避免部署后把今天已经查过的榜首再打一遍接口。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date, datetime
from pathlib import Path

import httpx

from app.config import settings
from app.market_time import CN_TZ, cn_now

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search"
_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MARKS = re.compile(r"[\s，。！？、；：\"'“”]+")
_TAGS = re.compile(r"<[^>]+>")
_LOCK = threading.Lock()
_MISSING_KEY_LOGGED = False
_STALE_LOGGED: set[str] = set()
# 含当天在内的 3 个北京时间自然日。取 3 天是为了盖住隔夜稿，更早的高权威旧闻不进入判断。
_RECENT_DAYS = 3
# 抬高后，旧文件既不套用也不再请求。今天已经落盘的结果会在下次快照刷新时从榜首消失。
CACHE_VERSION = 2
_FULL_DATE = re.compile(
    r"(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})"
)
_MONTH_DAY = re.compile(r"(?<!\d)(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})日?")

SUMMARY = (
    "只在新热点第一次冲上榜首时检索权威报道，补到事件来源和摘要。"
    "结果按交易日和事件缓存在 data/news/mx_hot。"
    "每日次数按北京时间自然日计算，默认 25 次，不占用资金流妙想配额。"
    "超时、失败或额度用尽时保持原事件。"
)


def api_key() -> str:
    """与资金流共用 MX_APIKEY，但调用次数分开计。"""
    return (os.environ.get("MX_APIKEY") or "").strip()


def _env_enabled() -> bool | None:
    raw = os.environ.get("NEWS_MX_HOT_ENABLED")
    if raw is None or not str(raw).strip():
        return None
    value = str(raw).strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    return None


def _saved_enabled() -> bool | None:
    from app.services import preferences

    raw = preferences.load().get("news_mx_hot")
    if isinstance(raw, dict) and "enabled" in raw:
        return bool(raw.get("enabled"))
    return None


def enabled() -> bool:
    """环境变量优先。没写环境和页面偏好时默认开启，设置页可以关掉。"""
    flag = _env_enabled()
    if flag is not None:
        return flag
    saved = _saved_enabled()
    if saved is not None:
        return saved
    return True


def locked() -> bool:
    return _env_enabled() is not None


def daily_budget() -> int:
    """自然日上限。非法或负数时回到 25。0 表示今天不再请求。"""
    raw = (os.environ.get("NEWS_MX_HOT_DAILY_BUDGET") or "").strip()
    if not raw:
        return 25
    try:
        value = int(raw)
    except ValueError:
        return 25
    return value if value >= 0 else 25


def set_enabled(value: bool) -> dict:
    """页面开关。环境变量锁定时不改偏好。"""
    if locked():
        return status()
    from app.services import preferences

    preferences.save({"news_mx_hot": {"enabled": bool(value)}})
    return status()


def status() -> dict:
    return {
        "enabled": enabled(),
        "locked": locked(),
        "configured": bool(api_key()),
        "budget": daily_budget(),
        "used": used_today(),
        "summary": SUMMARY,
    }


def cache_root(data_dir: Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else Path(settings.data_dir)
    return base / "news" / "mx_hot"


def enrich_snapshot(
    snapshot: dict,
    previous_top_key: str = "",
    *,
    searcher=None,
    data_dir: Path | None = None,
    now: datetime | None = None,
    lexicon=None,
) -> None:
    """补全快照里的榜首。失败时快照保持调用前的事件内容。"""
    try:
        _enrich(
            snapshot,
            previous_top_key,
            searcher=searcher,
            data_dir=data_dir,
            now=now,
            lexicon=lexicon,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("妙想热点补全失败: %s", type(exc).__name__)


def search_news(query: str) -> list[dict] | None:
    """请求 news-search。None 表示失败，空列表表示没有条目。"""
    key = api_key()
    text = (query or "").strip()
    if not key or not text:
        return None
    try:
        response = httpx.post(
            _SEARCH_URL,
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"query": text},
            timeout=10.0,
        )
    except httpx.HTTPError:
        logger.warning("妙想热点检索请求失败")
        return None
    if response.status_code != 200:
        logger.warning("妙想热点检索 HTTP %s", response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("妙想热点检索响应不是 JSON")
        return None
    items = parse_search_items(payload)
    if items is None:
        logger.warning("妙想热点检索结果无法解析")
        return None
    if not items:
        logger.info("妙想热点检索无结果")
    return items


def parse_search_items(payload: object) -> list[dict] | None:
    """``data.data.llmSearchResponse.data[]``。路径不在时返回 None。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    nested = data.get("data")
    if not isinstance(nested, dict):
        return None
    search = nested.get("llmSearchResponse")
    if not isinstance(search, dict):
        return None
    rows = search.get("data")
    if not isinstance(rows, list):
        return None
    items: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _normalize_item(row)
        if item is not None:
            items.append(item)
    return items


def event_query(event: dict) -> str:
    """展示标题去掉来源前缀。标题空了才退回主体加动作。"""
    from app.news.hot_events import _parse_phrase, _strip_source_prefix

    name = str(event.get("name") or "").strip()
    headline = str(event.get("headline") or "").strip()
    raw = name or headline
    cleaned = _strip_source_prefix(raw)
    if len(cleaned) >= 2:
        return cleaned[:80]
    lead, action, _obj = _parse_phrase(raw)
    return f"{lead}{action}".strip()[:80]


def apply_articles(
    event: dict,
    items: list[dict],
    *,
    lexicon=None,
    now: datetime | None = None,
) -> None:
    """近 3 日稿写入摘要和来源。只有 L1 且方向明确时才改方向和个股。"""
    ranked = _select_recent(items, now)
    if not ranked:
        return
    _write_summary_and_sources(event, ranked)
    signal, evidence = _matched_signal(event, ranked)
    if signal not in {"利好", "利空"}:
        return
    _apply_signal(event, signal, evidence, ranked, lexicon)


def used_today(data_dir: Path | None = None, now: datetime | None = None) -> int:
    payload = _read_budget(cache_root(data_dir))
    if payload.get("day") != _calendar_day(now):
        return 0
    return _as_int(payload.get("used"))


def _enrich(
    snapshot: dict,
    previous_top_key: str,
    *,
    searcher,
    data_dir: Path | None,
    now: datetime | None,
    lexicon,
) -> None:
    event = _top_event(snapshot)
    if event is None:
        return
    key = str(event.get("key") or "").strip()
    if not key:
        return
    root = cache_root(data_dir)
    day = _trading_day(snapshot, now)
    state, cached = _read_cache(root, day, key)
    if state == "stale":
        _note_stale(day, key)
        return
    if state == "hit":
        apply_articles(event, cached, lexicon=lexicon, now=now)
        return
    if key == str(previous_top_key or "").strip():
        return
    if not enabled():
        return
    query = event_query(event)
    if not query:
        return
    if searcher is None and not api_key():
        _note_missing_key()
        return
    if not _reserve_call(root, _calendar_day(now)):
        logger.info("妙想热点补全当日预算已用完")
        return
    found = searcher(query) if searcher is not None else search_news(query)
    if found is None:
        return
    items = _coerce_items(found)
    _write_cache(root, day, key, query, items)
    apply_articles(event, items, lexicon=lexicon, now=now)


def _top_event(snapshot: dict) -> dict | None:
    events = snapshot.get("events") if isinstance(snapshot, dict) else None
    if not isinstance(events, list) or not events or not isinstance(events[0], dict):
        return None
    return events[0]


def _trading_day(snapshot: dict, now: datetime | None) -> str:
    raw = str(snapshot.get("trading_day") or "").strip()
    if _DAY.fullmatch(raw):
        return raw
    moment = now or cn_now()
    from app.market_time import current_trading_day

    return current_trading_day(moment).isoformat()


def _calendar_day(now: datetime | None) -> str:
    """预算用北京时间自然日。妙想免费档按自然日计次，不跟交易日走。"""
    moment = now or cn_now()
    return moment.astimezone(CN_TZ).date().isoformat()


def _note_missing_key() -> None:
    global _MISSING_KEY_LOGGED
    if _MISSING_KEY_LOGGED:
        return
    _MISSING_KEY_LOGGED = True
    logger.info("妙想热点补全未配置 MX_APIKEY，已跳过")


def _note_stale(day: str, key: str) -> None:
    token = f"{day}:{key}"
    if token in _STALE_LOGGED:
        return
    _STALE_LOGGED.add(token)
    logger.info("妙想热点缓存版本已过期，不再展示也不再请求")


def _budget_path(root: Path) -> Path:
    return root / "budget.json"


def _read_budget(root: Path) -> dict:
    path = _budget_path(root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _reserve_call(root: Path, day: str) -> bool:
    limit = daily_budget()
    if limit <= 0:
        return False
    with _LOCK:
        payload = _read_budget(root)
        used = _as_int(payload.get("used")) if payload.get("day") == day else 0
        if used >= limit:
            return False
        try:
            _atomic_json(_budget_path(root), {"day": day, "used": used + 1})
        except OSError:
            logger.warning("妙想热点预算未能落盘")
            return False
        return True


def _cache_path(root: Path, day: str, key: str) -> Path:
    safe = key if _KEY.fullmatch(key) else "invalid-key"
    return root / day / f"{safe}.json"


def _read_cache(root: Path, day: str, key: str) -> tuple[str, list[dict]]:
    """返回 hit、stale 或 miss。stale 表示旧版本，调用方不得再请求。"""
    path = _cache_path(root, day, key)
    if not path.is_file():
        return "miss", []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("妙想热点缓存无法读取")
        return "miss", []
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        logger.warning("妙想热点缓存格式无效")
        return "miss", []
    if payload.get("v") != CACHE_VERSION:
        return "stale", []
    items = [item for item in payload["items"] if isinstance(item, dict)]
    return "hit", items


def _write_cache(root: Path, day: str, key: str, query: str, items: list[dict]) -> None:
    try:
        _atomic_json(
            _cache_path(root, day, key),
            {"v": CACHE_VERSION, "query": query, "items": items},
        )
    except OSError:
        logger.warning("妙想热点缓存未能落盘")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        number = int(value)
    except ValueError:
        return 0
    return number if number >= 0 else 0


def _coerce_items(items: list) -> list[dict]:
    coerced: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_item(item)
        if normalized is not None:
            coerced.append(normalized)
    return coerced


def _normalize_item(row: dict) -> dict | None:
    title = str(row.get("title") or "").strip()
    content = str(row.get("content") or "").strip()
    if not title and not content:
        return None
    return {
        "title": title[:300],
        "content": content[:2000],
        "date": str(row.get("date") or "").strip()[:40],
        "source": str(row.get("source") or "").strip()[:80],
        "authority": str(row.get("authorityLevel") or row.get("authority") or "").strip()[:40],
        "url": str(row.get("jumpUrl") or row.get("url") or "").strip()[:500],
    }


def _authority_rank(item: dict) -> tuple[int, str]:
    level = str(item.get("authority") or "").upper()
    if len(level) >= 2 and level[0] == "L" and level[1].isdigit():
        return (int(level[1]), level)
    return (9, level)


def _beijing_today(now: datetime | None) -> date:
    moment = now or cn_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CN_TZ)
    return moment.astimezone(CN_TZ).date()


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _article_day(raw: str, today: date) -> date | None:
    """稿件日期按北京时间自然日。带时区的时间先换到北京时间再取日期。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if "T" in text or text.endswith("Z"):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=CN_TZ)
            return parsed.astimezone(CN_TZ).date()
    found = _FULL_DATE.search(text)
    if found:
        return _safe_date(int(found["y"]), int(found["m"]), int(found["d"]))
    found = _MONTH_DAY.search(text)
    if not found:
        return None
    month = int(found["m"])
    day = int(found["d"])
    candidate = _safe_date(today.year, month, day)
    if candidate is None:
        return None
    # 只有月日、且落在今天之后时，当作上一年，避免 1 月把去年 12 月稿当成未来。
    if (candidate - today).days > 0:
        return _safe_date(today.year - 1, month, day)
    return candidate


def _select_recent(items: list[dict], now: datetime | None) -> list[dict]:
    """丢掉窗口外和无法识别日期的稿，先新后旧，同一天再比权威度。"""
    today = _beijing_today(now)
    ranked: list[tuple[int, tuple[int, str], dict]] = []
    for item in _coerce_items(items):
        day = _article_day(str(item.get("date") or ""), today)
        if day is None:
            continue
        age = (today - day).days
        if age < 0 or age >= _RECENT_DAYS:
            continue
        ranked.append((age, _authority_rank(item), item))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked]


def _has_jump_url(item: dict) -> bool:
    url = str(item.get("url") or "").strip()
    return url.startswith("https://") or url.startswith("http://")


def _is_l1(item: dict) -> bool:
    return str(item.get("authority") or "").upper().startswith("L1")


def _plain(text: str, limit: int) -> str:
    body = _TAGS.sub("", text or "")
    body = re.sub(r"\s+", " ", body).strip()
    return body[:limit]


def _flat(text: str) -> str:
    return _MARKS.sub("", text or "")


def _shares_span(query: str, text: str, length: int = 4) -> bool:
    """改写过的标题对不上整句时，连续四个字对得上才认为是同一件事。"""
    left = _flat(query)
    right = _flat(text)
    if len(left) < 2 or not right:
        return False
    if len(left) <= length:
        return left in right
    return any(left[index:index + length] in right for index in range(len(left) - length + 1))


def _write_summary_and_sources(event: dict, ranked: list[dict]) -> None:
    best = ranked[0]
    summary = _plain(str(best.get("content") or ""), 160) or _plain(str(best.get("title") or ""), 160)
    name = _flat(str(event.get("name") or ""))
    if summary and _flat(summary) != name:
        event["summary"] = summary
    # 摘要仍按日期和权威度。来源另把带链接的排前面，无链接的只用来补满三条。
    ordered = [item for item in ranked if _has_jump_url(item)]
    ordered.extend(item for item in ranked if not _has_jump_url(item))
    sources = []
    for item in ordered[:3]:
        title = str(item.get("title") or "").strip()
        source = str(item.get("source") or "").strip()
        if not title and not source:
            continue
        sources.append({
            "title": title,
            "source": source,
            "url": str(item.get("url") or "").strip(),
            "authority": str(item.get("authority") or "").strip(),
            "date": str(item.get("date") or "").strip(),
        })
    if sources:
        event["mx_sources"] = sources


def _matched_signal(event: dict, ranked: list[dict]) -> tuple[str, str]:
    """先看 L1 标题。标题没有明确方向时，才看正文开头。"""
    from app.news.hot_events import _direction_signal, _parse_phrase

    query = event_query(event)
    action = _parse_phrase(query)[1]
    l1 = [item for item in ranked if _is_l1(item)]
    for item in l1:
        title = str(item.get("title") or "").strip()
        signal = _direction_signal(title, action) if title else ""
        if signal and _shares_span(query, title):
            return signal, title
    for item in l1:
        lead = _plain(str(item.get("content") or ""), 180)
        signal = _direction_signal(lead, action) if lead else ""
        if signal and _shares_span(query, lead):
            return signal, lead
    return "", ""


def _apply_signal(
    event: dict,
    signal: str,
    evidence: str,
    ranked: list[dict],
    lexicon,
) -> None:
    from app.news.hot_events import _explicit_target_direction

    old = str(event.get("direction") or "")
    event["direction"] = signal
    for item in event.get("mapping") or []:
        if isinstance(item, dict):
            _assign_direction(item, evidence, signal, old, _explicit_target_direction)
    for stock in event.get("mentioned_stocks") or []:
        if isinstance(stock, dict):
            _assign_direction(stock, evidence, signal, old, _explicit_target_direction)
    _add_stocks(event, ranked, signal, lexicon, _explicit_target_direction)


def _assign_direction(item: dict, evidence: str, signal: str, old: str, explicit_for) -> None:
    explicit = explicit_for(str(item.get("name") or ""), evidence)
    if explicit in {"利好", "利空"}:
        item["direction"] = explicit
    elif str(item.get("direction") or "") in {"", old}:
        item["direction"] = signal


def _add_stocks(event: dict, ranked: list[dict], signal: str, lexicon, explicit_for) -> None:
    book = lexicon if lexicon is not None else _load_lexicon()
    if book is None:
        return
    stocks = event.setdefault("mentioned_stocks", [])
    if not isinstance(stocks, list):
        return
    known_keys = {str(item.get("key") or "") for item in stocks if isinstance(item, dict)}
    known_names = {str(item.get("name") or "") for item in stocks if isinstance(item, dict)}
    query = event_query(event)
    added = 0
    for item in ranked:
        if not _is_l1(item) or added >= 3:
            break
        title = str(item.get("title") or "").strip()
        if not title or not _shares_span(query, title):
            continue
        for mention in book.extract(title):
            if mention.kind != "stock" or added >= 3:
                continue
            name = str(mention.name or "").strip()
            key = str(mention.key or "").strip()
            if not key or key in known_keys or (name and name in known_names):
                continue
            if name and name not in title:
                continue
            explicit = explicit_for(name, title)
            direction = explicit if explicit in {"利好", "利空"} else signal
            stocks.append({
                "key": key,
                "name": name or key,
                "mentions": 1,
                "source_count": 1,
                "direction": direction,
            })
            known_keys.add(key)
            if name:
                known_names.add(name)
            added += 1


def _load_lexicon():
    try:
        from app.news.service import get_lexicon

        return get_lexicon()
    except Exception as exc:  # noqa: BLE001
        logger.warning("妙想热点补全未能读取股票词典: %s", type(exc).__name__)
        return None


def reset_for_tests() -> None:
    global _MISSING_KEY_LOGGED
    _MISSING_KEY_LOGGED = False
    _STALE_LOGGED.clear()
