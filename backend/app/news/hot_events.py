"""把一个交易日的资讯收成具体事件，而不是宽行业榜。

聚类看标题里的主体、动作和细概念，再加同一时间窗。摘要只在标题没有主体时参与，
避免各条资讯共用的页脚把整天粘成一件事。宽行业名不参与聚类，也不拿去扩成分股。
"""
from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from datetime import time as dt_time

from app.market_time import CN_TZ, cn_now, current_trading_day
from app.news.collectors import parse_time
from app.news.extract import _usable_sector_name
from app.news.store import NewsStore

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 21
_CACHE_SECONDS = 600
_CLUSTER_WINDOW = timedelta(hours=6)
_HEAT_HALF_LIFE_HOURS = 12
_LLM_PER_PASS = 5
_TITLE_LIMIT = 20
_CONCEPT_LIMIT = 4

# 申万一级和口头上的大板块。出现在标题里也不够把两条资讯收成同一事件。
BROAD_SECTORS = frozenset({
    "人工智能", "机器人", "半导体", "新能源", "医药", "银行", "白酒", "军工",
    "消费", "地产", "房地产", "券商", "证券", "有色", "煤炭", "钢铁", "电力",
    "汽车", "通信", "计算机", "电子", "传媒", "农业", "化工", "机械", "建筑",
    "交运", "家电", "商贸", "纺织", "轻工", "综合", "公用事业", "非银金融",
    "国防军工", "美容护理", "社会服务", "环保", "石油石化", "基础化工",
    "有色金属", "交通运输", "建筑材料", "建筑装饰", "商贸零售", "纺织服饰",
    "轻工制造", "电力设备", "机械设备", "农林牧渔", "医药生物", "家用电器",
    "食品饮料",
})
_ACTIONS = (
    "发布", "出台", "涨价", "降价", "提价", "收购", "中标", "获批", "上市",
    "回购", "签约", "停产", "召回", "立案", "补贴",
)
_LEAD_SUFFIXES = ("宣布", "称", "表示", "指出", "消息", "传闻", "公司", "披露")
_STOP = frozenset({
    "公司", "市场", "今日", "表示", "消息", "记者", "财经", "股份", "有限",
    "集团", "板块", "概念", "龙头", "资金", "大涨", "大跌", "上涨", "下跌",
    "走强", "走弱", "午后", "盘中", "持续", "继续", "相关", "中国", "美国",
    "国内", "海外", "全球", "多家", "有关", "A股", "港股",
})
_CLAUSE = re.compile(r"[，。！？、；：:\n]")

_CACHE: dict[tuple, dict] = {}
_LLM_CACHE: dict[str, dict] = {}


def clear_hot_event_cache(*, llm: bool = True) -> None:
    _CACHE.clear()
    if llm:
        _LLM_CACHE.clear()


def build_top_hot_events(now: datetime | None, store: NewsStore) -> dict:
    now = (now or cn_now()).astimezone(CN_TZ)
    key = (str(store.path), int(now.timestamp()) // _CACHE_SECONDS)
    cached = _CACHE.get(key)
    if cached is not None:
        return copy.deepcopy(cached)
    snapshot = _compute(now, store)
    _CACHE[key] = snapshot
    return copy.deepcopy(snapshot)


def _compute(now: datetime, store: NewsStore) -> dict:
    today = now.date()
    trading = current_trading_day(now)
    start = datetime.combine(today - timedelta(days=_LOOKBACK_DAYS), dt_time.min, CN_TZ)
    end = datetime.combine(today + timedelta(days=1), dt_time.min, CN_TZ)
    by_day: dict[date, list[dict]] = defaultdict(list)
    for row in store.items_between(start, end):
        published = parse_time(row.get("published_at"))
        if published is None:
            continue
        row = dict(row)
        row["published"] = published.astimezone(CN_TZ)
        by_day[row["published"].date()].append(row)
    ranked = {day: _cluster_day(rows, now) for day, rows in by_day.items()}
    ranked = {day: events for day, events in ranked.items() if events}
    fallback = False
    if trading in ranked:
        chosen: date | None = trading
    else:
        earlier = [day for day in ranked if day <= today]
        chosen = max(earlier) if earlier else None
        fallback = chosen is not None
    if chosen is None:
        return {
            "as_of": None,
            "trading_day": trading.isoformat(),
            "fallback": False,
            "hint": None,
            "updated_at": None,
            "events": [],
        }
    events = ranked[chosen]
    _label_with_llm(events)
    latest = max(event.pop("_latest") for event in events)
    for event in events:
        event.pop("_fp", None)
    updated_hm = latest.astimezone(CN_TZ).strftime("%H:%M")
    return {
        "as_of": chosen.isoformat(),
        "trading_day": trading.isoformat(),
        "fallback": fallback,
        "hint": _hint(now, trading, chosen, fallback, updated_hm),
        "updated_at": updated_hm,
        "events": events,
    }


def _cluster_day(rows: list[dict], now: datetime) -> list[dict]:
    docs = [_doc(row) for row in rows]
    docs = [doc for doc in docs if doc is not None]
    if not docs:
        return []
    parent = list(range(len(docs)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(docs)):
        for right in range(left + 1, len(docs)):
            gap = abs(docs[left]["published"] - docs[right]["published"])
            if gap > _CLUSTER_WINDOW:
                continue
            if docs[left]["tokens"] & docs[right]["tokens"]:
                parent[find(right)] = find(left)
    groups: dict[int, list[dict]] = defaultdict(list)
    for index, doc in enumerate(docs):
        groups[find(index)].append(doc)
    events = [_event_from(members, now) for members in groups.values()]
    events.sort(key=lambda item: (-item["heat"], -item["mentions"], -item["source_count"], item["name"]))
    return events


def _doc(row: dict) -> dict | None:
    title = str(row.get("title") or "").strip()
    summary = _summary_text(row)
    if not title and not summary:
        return None
    published = row["published"]
    concepts: list[str] = []
    stocks: dict[str, str] = {}
    for mention in row.get("mentions") or []:
        kind = mention["kind"]
        key = str(mention["key"] or "").strip()
        name = str(mention["name"] or key).strip()
        if not key:
            continue
        if kind == "sector" and _fine_concept(key, name):
            concepts.append(name or key)
        elif kind == "stock":
            stocks[key] = name or key
    lead, action, obj = _parse_phrase(title)
    if not lead and not action and not obj:
        lead, action, obj = _parse_phrase(summary)
    tokens = _tokens(lead, obj, concepts)
    return {
        "item_id": row.get("id"),
        "source": str(row.get("source") or ""),
        "published": published,
        "title": title or summary[:80],
        "text": f"{title}\n{summary}",
        "lead": lead,
        "action": action,
        "object": obj,
        "concepts": concepts,
        "stocks": stocks,
        "tokens": tokens,
    }


def _summary_text(row: dict) -> str:
    raw = row.get("extra_json") or ""
    if raw:
        try:
            import json
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            zh = str(payload.get("summary_zh") or "").strip()
            if zh:
                return zh
    return str(row.get("clean_text") or "").strip()


def _fine_concept(key: str, name: str) -> bool:
    text = (name or key).strip()
    if text in BROAD_SECTORS or key in BROAD_SECTORS:
        return False
    return _usable_sector_name(text)


def _parse_phrase(text: str) -> tuple[str, str, str]:
    body = (text or "").strip()
    if not body:
        return "", "", ""
    clauses = [clause.strip() for clause in _CLAUSE.split(body) if clause.strip()]
    chosen = body
    action = ""
    for clause in clauses or [body]:
        for verb in _ACTIONS:
            if verb in clause:
                chosen = clause
                action = verb
                break
        if action:
            break
    if not action:
        return _clean_lead(chosen[:8]), "", ""
    index = chosen.find(action)
    lead = _clean_lead(chosen[:index])
    obj = _CLAUSE.split(chosen[index + len(action):])[0].strip()[:8]
    if len(obj) < 2 or obj in _STOP or obj in BROAD_SECTORS:
        obj = ""
    return lead, action, obj


def _clean_lead(text: str) -> str:
    lead = (text or "").strip()
    for suffix in _LEAD_SUFFIXES:
        if lead.endswith(suffix) and len(lead) - len(suffix) >= 2:
            lead = lead[:-len(suffix)]
    if len(lead) < 2 or lead in _STOP or lead in BROAD_SECTORS:
        return ""
    return lead[:8]


def _tokens(lead: str, obj: str, concepts: list[str]) -> set[str]:
    """聚类词来自标题主体和细概念。个股名单独出现不够把两件事粘在一起。"""
    tokens = set()
    if lead:
        tokens.add(lead)
    if obj:
        tokens.add(obj)
    tokens.update(concepts)
    return {token for token in tokens if token}


def _event_from(members: list[dict], now: datetime) -> dict:
    members = sorted(members, key=lambda item: item["published"])
    first = members[0]["published"]
    latest = members[-1]["published"]
    sources = {item["source"] for item in members if item["source"]}
    age_hours = max(0.0, (now - latest).total_seconds() / 3600)
    decay = math.exp(-math.log(2) * age_hours / _HEAT_HALF_LIFE_HOURS)
    mentions = len(members)
    source_count = len(sources) or 1
    concept_counts: Counter[str] = Counter()
    for item in members:
        concept_counts.update(dict.fromkeys(item["concepts"], 1))
    concepts = _specific_concepts(concept_counts)
    stocks: dict[str, dict] = {}
    for item in members:
        for key, name in item["stocks"].items():
            hit = stocks.get(key)
            if hit is None:
                hit = {"key": key, "name": name, "mentions": 0, "sources": set()}
                stocks[key] = hit
            hit["mentions"] += 1
            hit["sources"].add(item["source"])
            hit["name"] = name
    mentioned = [
        {
            "key": hit["key"],
            "name": hit["name"],
            "mentions": hit["mentions"],
            "source_count": len(hit["sources"]),
        }
        for hit in stocks.values()
    ]
    mentioned.sort(key=lambda item: (-item["mentions"], -item["source_count"], item["name"]))
    name = _keyword_title(members)
    item_ids = [str(item["item_id"]) for item in members]
    digest = hashlib.sha1(",".join(sorted(item_ids)).encode("utf-8")).hexdigest()[:12]
    seen = first.astimezone(CN_TZ)
    first_seen = seen.strftime("%H:%M") if seen.date() == now.date() else seen.strftime("%m-%d %H:%M")
    headline = members[0]["title"]
    return {
        "key": f"ev_{digest}",
        "name": name,
        "concepts": concepts,
        "headline": headline,
        "mentions": mentions,
        "source_count": len(sources),
        "first_seen": first_seen,
        "updated_at": first_seen,
        "heat": round(mentions * source_count * decay, 4),
        "mentioned_stocks": mentioned,
        "_latest": latest,
        "_fp": hashlib.sha1("\n".join(item["title"] for item in members).encode("utf-8")).hexdigest(),
    }


def _specific_concepts(counts: Counter[str]) -> list[str]:
    names = [name for name in counts if name not in BROAD_SECTORS]
    names.sort(key=len, reverse=True)
    kept: list[str] = []
    for name in names:
        if any(name != other and name in other for other in kept):
            continue
        kept.append(name)
    kept.sort(key=lambda name: (-counts[name], -len(name), name))
    return kept[:_CONCEPT_LIMIT]


def _keyword_title(members: list[dict]) -> str:
    leads = [item["lead"] for item in members if item["lead"]]
    actions = [item["action"] for item in members if item["action"]]
    objects = [item["object"] for item in members if item["object"]]
    lead = _mode(leads)
    action = _mode(actions)
    obj = _mode(objects)
    if lead and action:
        title = _clip(f"{lead}{action}{obj}")
        if title and title not in BROAD_SECTORS:
            return title
    headline = members[0]["title"]
    title = _clip(headline)
    if title in BROAD_SECTORS:
        title = _clip(f"{title}动态")
    return title or "事件"


def _mode(values: list[str]) -> str:
    if not values:
        return ""
    return Counter(values).most_common(1)[0][0]


def _clip(text: str) -> str:
    compact = re.sub(r"\s+", "", (text or "").strip())
    compact = compact.strip("，。！？、；：")
    return compact[:_TITLE_LIMIT]


def _hint(now: datetime, trading: date, chosen: date, fallback: bool, updated_hm: str) -> str:
    clock = f" · 更新于 {updated_hm}" if updated_hm else ""
    if fallback:
        return f"暂无{_month_day(trading)}数据，显示{_month_day(chosen)}{clock}"
    if chosen != now.date():
        return f"交易日 {_month_day(chosen)}{clock}"
    return f"更新于 {updated_hm}" if updated_hm else ""


def _month_day(day: date) -> str:
    return f"{day.month}月{day.day}日"


def _label_with_llm(events: list[dict]) -> None:
    from app.news.config import llm_extract_enabled

    if not events or not llm_extract_enabled():
        return
    from app.news.service import _llm_text, _reserve_llm_call

    used = 0
    for event in events[:_LLM_PER_PASS]:
        fingerprint = str(event.get("_fp") or "")
        cached = _LLM_CACHE.get(fingerprint)
        if cached is not None:
            _apply_llm(event, cached)
            continue
        if used >= _LLM_PER_PASS:
            break
        if not _reserve_llm_call():
            break
        used += 1
        prompt = (
            "下面几条标题是同一件财经事件。请收成一个具体事件，不要用宽泛行业名当标题。"
            "只输出 JSON："
            '{"title":"不超过20个字","concepts":["细分概念"],"stocks":[{"name":"","code":""}]}。'
            "concepts 写细概念，不要写人工智能、半导体、医药这种大行业。"
            "没有把握就留空，不要编造标题里没出现的事实。\n\n"
            f"标题：{event.get('headline') or event.get('name')}"
        )
        try:
            raw = _llm_text(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.info("热门事件标题生成失败: %s", exc)
            raw = ""
        parsed = _parse_llm_event(raw, str(event.get("headline") or ""))
        _LLM_CACHE[fingerprint] = parsed or {}
        if parsed:
            _apply_llm(event, parsed)


def _parse_llm_event(raw: str, text: str) -> dict | None:
    from app.news.extract import _llm_object

    payload = _llm_object(raw)
    if not payload:
        return None
    title = _clip(str(payload.get("title") or ""))
    if not title or title in BROAD_SECTORS:
        title = ""
    concepts = []
    for item in payload.get("concepts") or payload.get("sectors") or []:
        name = str(item or "").strip()
        if name and name in text and _fine_concept(name, name) and name not in concepts:
            concepts.append(name)
    stocks = []
    for item in payload.get("stocks") or []:
        if isinstance(item, str):
            name, code = item.strip(), ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            code = str(item.get("code") or item.get("symbol") or "").strip()
        else:
            continue
        if (name and name in text) or (code and code in text):
            stocks.append({"name": name, "code": code})
    if not title and not concepts and not stocks:
        return None
    return {"title": title, "concepts": concepts[:_CONCEPT_LIMIT], "stocks": stocks}


def _apply_llm(event: dict, parsed: dict) -> None:
    title = str(parsed.get("title") or "")
    if title:
        event["name"] = title
    for name in parsed.get("concepts") or []:
        if name not in event["concepts"] and len(event["concepts"]) < _CONCEPT_LIMIT:
            event["concepts"].append(name)
    known = {item["key"] for item in event.get("mentioned_stocks") or []}
    known_names = {item["name"] for item in event.get("mentioned_stocks") or []}
    for stock in parsed.get("stocks") or []:
        code = str(stock.get("code") or "").strip()
        name = str(stock.get("name") or "").strip()
        key = code or name
        if not key or key in known or name in known_names:
            continue
        known.add(key)
        event["mentioned_stocks"].append({
            "key": key,
            "name": name or key,
            "mentions": 1,
            "source_count": 1,
        })
