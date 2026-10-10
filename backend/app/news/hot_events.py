"""把一个交易日的资讯收成具体事件，而不是宽行业榜。

聚类看标题里的主体、动作，以及标题里出现的细概念，再加同一时间窗。正文里抽到、
但标题没写的概念不参与聚类，避免旁支概念把无关消息粘成一件事。同一主体和动作，
或标题高度相似，会在较短时间窗内合并。摘要只在标题没有主体时参与，避免各条资讯
共用的页脚把整天粘成一件事。宽行业名不参与聚类，也不拿去扩成分股。

标题优先用合格的关键词或模型说法。两者都没有时，用去掉来源前缀、按分句截断的
代表标题。没有可读标题，或标题只剩「事件」，这条就不进列表。
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
_LLM_PER_PASS = 8
_TITLE_LIMIT = 20
# 标题缓存按这个版本取。旧键里的「事件」和缺主体标题不再套用，下一轮重新生成。
_TITLE_CACHE_VERSION = 3
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
    "发布", "出台", "涨价", "降价", "提价", "上调", "下调", "报价", "收购", "中标", "获批", "上市",
    "回购", "签约", "停产", "召回", "立案", "补贴", "降准", "降息", "加息", "制裁", "收紧",
    "追加", "订单",
)
# 先看更具体的市场语境，再退回动作本身。六个分类覆盖政策、海外、产业、地缘、商品和公司。
CATEGORY_NAMES = (
    "国内政策/宏观",
    "海外市场/央行",
    "科技与产业",
    "地缘政治",
    "大宗商品/期货价格异动",
    "公司重大事项",
)
_CATEGORY_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("地缘政治", ("制裁", "关税", "冲突", "战争", "停火", "出口管制", "配额", "地缘")),
    ("海外市场/央行", ("美联储", "欧央行", "日本央行", "英央行", "加息", "降息", "美股", "纳指", "标普", "欧股", "日经")),
    ("国内政策/宏观", ("出台", "补贴", "降准", "国务院", "发改委", "工信部", "证监会", "财政部", "央行", "宏观", "政策")),
    ("大宗商品/期货价格异动", ("碳酸锂", "原油", "期货", "黄金", "铜价", "铁矿", "煤炭", "稀土", "豆粕", "螺纹", "报价", "橡胶")),
    ("公司重大事项", ("收购", "回购", "上市", "立案", "停产", "召回", "签约", "中标", "停牌", "退市")),
    ("科技与产业", ("发布", "订单", "追加", "获批", "涨价", "降价", "提价", "上调", "下调", "量产", "芯片", "模型")),
)
_ACTION_CATEGORY = {
    "出台": "国内政策/宏观",
    "补贴": "国内政策/宏观",
    "降准": "国内政策/宏观",
    "加息": "海外市场/央行",
    "降息": "海外市场/央行",
    "制裁": "地缘政治",
    "收紧": "地缘政治",
    "报价": "大宗商品/期货价格异动",
    "收购": "公司重大事项",
    "回购": "公司重大事项",
    "上市": "公司重大事项",
    "立案": "公司重大事项",
    "停产": "公司重大事项",
    "召回": "公司重大事项",
    "签约": "公司重大事项",
    "中标": "公司重大事项",
}
_BEAR_CUES = ("制裁", "立案", "停产", "召回", "下调", "降价", "收紧", "冲突", "战争", "关税", "暴跌", "下跌", "加息")
_BULL_CUES = ("补贴", "获批", "订单", "涨价", "回购", "发布", "出台", "降息", "降准", "上调", "提价", "中标", "签约", "上涨")
_NOISE_EXACT = frozenset({"图片", "广告", "视频", "转发", "分享", "推广", "赞助"})
_NOISE_HINTS = ("闲聊", "广告", "推广", "赞助", "加微信", "点击领取", "转发微博")
_RELEVANCE_MIN = 3
# 重要性高于映射、新鲜度和热度。热度封顶，避免条数把寻常涨价顶过降息或政策。
# 盘面验证另计，权重和重要性同级，不放进这个封顶。
_IMPORTANCE_POINTS = {"琐碎": 0, "一般": 20, "重要": 55, "重大": 130}
_IMPORTANCE_ORDER = ("琐碎", "一般", "重要", "重大")
_MAPPING_CONCEPT = 15
_MAPPING_ASSET = 15
_MAPPING_DIRECTION = 5
_FRESH_MAX = 20
_HEAT_BONUS_CAP = 15
_MAJOR_RATE = ("降息", "加息", "降准", "美联储", "欧央行", "日本央行", "英央行")
_MAJOR_ORGS = ("国务院", "发改委", "工信部", "证监会", "财政部", "央行")
_MAJOR_POLICY_ACTS = ("出台", "补贴", "政策", "降准")
_MAJOR_GEO = ("制裁", "战争", "关税", "出口管制", "配额")
_MAJOR_COMMODITIES = ("碳酸锂", "原油", "黄金", "铜价", "铁矿")
_PRICE_VERBS = ("报价", "涨价", "降价", "提价", "上调", "下调", "上涨", "下跌", "暴涨", "暴跌")
_LEADERS = (
    "华为", "宁德时代", "贵州茅台", "英伟达", "苹果", "特斯拉", "比亚迪",
    "平安银行", "腾讯", "阿里巴巴", "茅台",
)
# 标题里点名的机构。缺主体时从代表标题里补这些，而不是留下「可能还会」「运行情况」。
_ORG_SUBJECTS = (
    "美联储", "欧洲央行", "欧央行", "日本央行", "英格兰银行", "英央行",
    "国务院", "政治局", "发改委", "工信部", "证监会", "财政部",
    "商务部", "外交部", "住建部", "国家能源局", "能源局", "国常会",
    "央行",
)
_EXPLICIT_SUBJECTS = tuple(sorted(set(_ORG_SUBJECTS) | set(_LEADERS), key=len, reverse=True))
# 没有具体对象的词。不能单靠它们把两条资讯收成一件事。
_GENERIC_TOKENS = frozenset({
    "超预期", "不及预期", "预期", "公告", "消息", "行情", "价格", "股价",
    "政策", "市场", "公司", "股份", "板块", "概念", "产业链", "产业",
    "上涨", "下跌", "大涨", "大跌",
})
_SUBJECT_TRAIL = (
    "价格", "股价", "期货", "行情", "板块", "概念", "产业链", "产业",
    "公司", "集团", "股份", "厂商", "企业",
)
# 这些词当主语时，标题还缺一个能指认的主体。
_VAGUE_CUES = (
    "可能", "或许", "预计", "有望", "或将", "料将",
    "还会", "还将", "几次", "多次",
    "情况", "谋划", "宏观",
    "进一步", "消息称", "据悉", "传闻称",
)
_SIMILAR_WINDOW = timedelta(hours=3)
_SIMILARITY_MIN = 0.72
_REPRESENTATIVE_SIMILARITY = 0.45
_LEADER_ACTS = ("发布", "收购", "回购", "停产", "制裁", "订单", "上市", "立案")
_IMPORTANT_ACTS = ("涨价", "获批", "回购", "订单", "收购", "中标")
_IMPORTANT_CATEGORIES = frozenset({
    "国内政策/宏观", "海外市场/央行", "地缘政治", "公司重大事项",
})
_LEAD_SUFFIXES = ("宣布", "称", "表示", "指出", "消息", "传闻", "公司", "披露")
_STOP = frozenset({
    "公司", "市场", "今日", "表示", "消息", "记者", "财经", "股份", "有限",
    "集团", "板块", "概念", "龙头", "资金", "大涨", "大跌", "上涨", "下跌",
    "走强", "走弱", "午后", "盘中", "持续", "继续", "相关", "中国", "美国",
    "国内", "海外", "全球", "多家", "有关", "A股", "港股",
})
_CLAUSE = re.compile(r"[，。！？、；：:\n]")
# 半句话常见的起笔。完整标题不会从这些词开始。
_FRAGMENT_LEADS = (
    "再叠加", "叠加", "再加上", "加上",
    "和之前", "和此前", "和这次", "和本次", "和上述", "以及",
    "根据", "据悉", "据称", "据报道",
    "与此同时", "与此",
    "同时", "此外", "另外", "并且", "而且",
    "但是", "不过", "然而", "虽然", "尽管", "如果", "因为", "由于", "随着",
    "其中", "对此", "为此", "因此", "因而",
    "继而", "随后", "此后", "日前", "此前", "还有",
)
_FRAGMENT_PREFIX = re.compile("^(?:" + "|".join(_FRAGMENT_LEADS) + ")")
_TIME_PREFIX = re.compile(r"^(?:昨夜|昨晚|今日|昨日|今天|昨天|日前|此前|本次|这次|当前|目前|最近|刚刚)+")
# 涨跌叙述不是聚类动作，但标题被从中间截开时，它往往才是这件事的谓语。
_PRICE_NARRATIVE = (
    "大涨", "大跌", "上涨", "下跌", "暴涨", "暴跌",
    "走强", "走弱", "走高", "走低", "飙升", "跳水",
)
# 聚类停用词里的国别可以当标题主体。这里只挡「公司」「市场」这种没有对象的词。
_NOT_SUBJECT = frozenset({
    "公司", "市场", "今日", "表示", "消息", "记者", "财经", "股份", "有限",
    "集团", "板块", "概念", "龙头", "资金", "多家", "有关", "相关",
    "持续", "继续", "午后", "盘中",
})
# 叙述谓语后面再接这些动作，就是两件不相干的事被粘在了一起。
_GLUE_FOLLOWUPS = frozenset({
    "发布", "出台", "收购", "中标", "获批", "上市", "回购", "签约",
    "停产", "召回", "立案", "补贴", "制裁", "收紧", "降息", "加息", "降准",
})
_DANGLING_FINAL = frozenset("的了和与及等把被将已正而或但之以为于对从在超")
# 以这些字结尾时，只有最后两个字是完整词才算没截断。
_CLOSED_TAILS = {
    "政": frozenset({"新政", "政府", "财政", "行政", "市政", "内政", "党政", "军政"}),
}
_SOURCE_HEADS = (
    "财联社", "华尔街见闻", "证券时报", "上海证券报", "中国证券报",
    "第一财经", "澎湃新闻", "新浪财经", "新华社", "界面新闻",
    "路透社", "路透", "彭博", "南华早报",
)
_SOURCE_BRACKET = re.compile(r"^(?:【[^】]{1,16}】|\[[^\]]{1,16}\]|（[^）]{1,12}）|\([^)]{1,12}\))")
_SOURCE_DATELINE = re.compile(
    r"^(?:" + "|".join(_SOURCE_HEADS) + r")(?:\d{1,2}月\d{1,2}日)?(?:电|讯)[，,]?"
)
_SOURCE_LABEL = re.compile(
    r"^(?:" + "|".join(_SOURCE_HEADS) + r"|快讯|突发|独家|刚刚)[：:|｜]"
)
_REUTERS_TAIL = re.compile(r"(?:[-|｜]\s*)?(?:Reuters|路透社|路透)$")

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
    from app.news.market_confirm import attach_confirmations
    attach_confirmations(events, now)
    events = _rank_events(events, now, grade=False)
    _label_with_llm(events)
    events = [event for event in events if _display_name_ok(event.get("name"))]
    if not events:
        return {
            "as_of": None,
            "trading_day": trading.isoformat(),
            "fallback": False,
            "hint": None,
            "updated_at": None,
            "events": [],
        }
    events = _rank_events(events, now, grade=False)
    latest = max(event.pop("_latest") for event in events)
    for event in events:
        event.pop("_fp", None)
        event.pop("_first", None)
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
            if _can_merge(docs[left], docs[right]):
                parent[find(right)] = find(left)
    groups: dict[int, list[dict]] = defaultdict(list)
    for index, doc in enumerate(docs):
        groups[find(index)].append(doc)
    events = [
        event for event in (_event_from(members, now) for members in groups.values())
        if _display_name_ok(event.get("name"))
    ]
    return _rank_events(events, now, grade=True)


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
    if not lead or not action:
        lead, action, obj = _parse_phrase(summary)
    shown = title or summary[:80]
    if _relevance(shown, lead, action, concepts, stocks) < _RELEVANCE_MIN:
        return None
    entity_text = title
    if summary and not _explicit_entities(title):
        entity_text = f"{title}\n{summary[:160]}"
    entities = _explicit_entities(title)
    if not entities and _subject_is_vague(lead):
        entities = _explicit_entities(summary[:200])
    tokens = _title_tokens(lead, obj, concepts, title or summary[:80], entities)
    return {
        "item_id": row.get("id"),
        "source": str(row.get("source") or ""),
        "published": published,
        "title": title or summary[:80],
        "entity_text": entity_text,
        "text": f"{title}\n{summary}",
        "lead": lead,
        "action": action,
        "object": obj,
        "entities": entities,
        "relevance": _relevance(shown, lead, action, concepts, stocks),
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
        for clause in clauses or [body]:
            index, verb = _narrative_action(clause)
            if verb and _subject_ok(clause[:index]):
                chosen = clause
                action = verb
                break
    if not action:
        return _clean_lead(chosen[:8]), "", ""
    index = chosen.find(action)
    lead = _clean_lead(chosen[:index])
    obj = _CLAUSE.split(chosen[index + len(action):])[0].strip()[:8]
    if len(obj) < 2 or obj in _STOP or obj in BROAD_SECTORS or obj in _GENERIC_TOKENS:
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


def _title_tokens(
    lead: str,
    obj: str,
    concepts: list[str],
    title: str,
    entities: list[str] | None = None,
) -> set[str]:
    """聚类词只来自标题里的主体、宾语和标题中出现的细概念。

    正文提及的概念不进聚类词，否则一条快讯带上的苹果、黄金会把橡胶新闻粘进去。
    """
    tokens = set()
    for value in (lead, obj, _subject_core(lead)):
        if (
            value
            and len(value) >= 2
            and value not in _STOP
            and value not in BROAD_SECTORS
            and value not in _GENERIC_TOKENS
        ):
            tokens.add(value)
    for entity in entities or []:
        if entity:
            tokens.add(entity)
    for concept in concepts:
        if concept and concept in (title or "") and concept not in BROAD_SECTORS:
            tokens.add(concept)
    return tokens


def _event_from(members: list[dict], now: datetime) -> dict:
    members = sorted(members, key=lambda item: item["published"])
    first = members[0]["published"]
    latest = members[-1]["published"]
    sources = {item["source"] for item in members if item["source"]}
    age_hours = max(0.0, (now - latest).total_seconds() / 3600)
    decay = math.exp(-math.log(2) * age_hours / _HEAT_HALF_LIFE_HOURS)
    mentions = len(members)
    source_count = len(sources) or 1
    name = _keyword_title(members)
    focused = _representative_members(members, name)
    concept_counts: Counter[str] = Counter()
    for item in focused:
        concept_counts.update(dict.fromkeys(item["concepts"], 1))
    focus_titles = [str(item.get("title") or "") for item in focused]
    subjects = _subject_set(name, focused)
    concepts = _relevant_concepts(concept_counts, focus_titles, subjects)
    rejected = [label for label in concept_counts if label not in concepts and label not in BROAD_SECTORS]
    stocks: dict[str, dict] = {}
    for item in focused:
        for key, stock_name in item["stocks"].items():
            if not _stock_relevant(stock_name, key, focus_titles, subjects, concepts, rejected):
                continue
            hit = stocks.get(key)
            if hit is None:
                hit = {"key": key, "name": stock_name, "mentions": 0, "sources": set()}
                stocks[key] = hit
            hit["mentions"] += 1
            hit["sources"].add(item["source"])
            hit["name"] = stock_name
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
    blob = "\n".join(focus_titles)
    action = _mode([item["action"] for item in focused if item["action"]])
    category = _category_for(blob, action)
    direction = _direction_for(blob, action)
    for hit in mentioned:
        hit["direction"] = direction
    item_ids = [str(item["item_id"]) for item in members]
    digest = hashlib.sha1(",".join(sorted(item_ids)).encode("utf-8")).hexdigest()[:12]
    seen = first.astimezone(CN_TZ)
    first_seen = seen.strftime("%H:%M") if seen.date() == now.date() else seen.strftime("%m-%d %H:%M")
    headlines: list[str] = []
    for item in members:
        title = str(item["title"] or "").strip()
        if title and title not in headlines:
            headlines.append(title)
        if len(headlines) >= 3:
            break
    headline = headlines[0] if headlines else ""
    return {
        "key": f"ev_{digest}",
        "name": name,
        "category": category,
        "direction": direction,
        "relevance": max(int(item.get("relevance") or 0) for item in members),
        "concepts": concepts,
        "mapping": [{"name": concept, "kind": "concept", "direction": direction} for concept in concepts],
        "headline": headline,
        "headlines": headlines,
        "mentions": mentions,
        "source_count": len(sources),
        "first_seen": first_seen,
        "updated_at": first_seen,
        "heat": round(mentions * source_count * decay, 4),
        "mentioned_stocks": mentioned,
        "item_ids": item_ids,
        "_latest": latest,
        "_first": first,
        "_fp": hashlib.sha1("\n".join(item["title"] for item in members).encode("utf-8")).hexdigest(),
    }


def _rank_events(events: list[dict], now: datetime, *, grade: bool) -> list[dict]:
    """按重要性、映射、新鲜度、热度排序。只有第一次按规则分级时丢掉琐碎小事。"""
    kept = []
    for event in events:
        if grade:
            event["importance"] = _rule_importance(event)
        _score_event(event, now)
        if grade and _drop_minor(event):
            continue
        kept.append(event)
    kept.sort(key=lambda item: (
        -float(item["score"]),
        -_IMPORTANCE_POINTS.get(str(item.get("importance")), 0),
        -float(item["breakdown"]["mapping"]),
        -float(item["breakdown"]["freshness"]),
        -float(item["heat"]),
        str(item["name"]),
    ))
    return kept


def _score_event(event: dict, now: datetime) -> None:
    label = str(event.get("importance") or "一般")
    if label not in _IMPORTANCE_POINTS:
        label = "一般"
        event["importance"] = label
    importance = _IMPORTANCE_POINTS[label]
    concepts = [str(name).strip() for name in (event.get("concepts") or []) if str(name).strip()]
    stocks = [item for item in (event.get("mentioned_stocks") or []) if isinstance(item, dict)]
    mapping = 0
    if concepts:
        mapping += _MAPPING_CONCEPT
    if stocks:
        mapping += _MAPPING_ASSET
    direction = str(event.get("direction") or "")
    if direction in {"利好", "利空"} and (concepts or stocks):
        mapping += _MAPPING_DIRECTION
    first = event.get("_first")
    if isinstance(first, datetime):
        age_hours = max(0.0, (now - first).total_seconds() / 3600)
    else:
        age_hours = _HEAT_HALF_LIFE_HOURS
    freshness = _FRESH_MAX * math.exp(-math.log(2) * age_hours / _HEAT_HALF_LIFE_HOURS)
    try:
        heat = float(event.get("heat") or 0)
    except (TypeError, ValueError):
        heat = 0.0
    heat_bonus = min(_HEAT_BONUS_CAP, max(0.0, heat))
    confirmation = _confirmation_points(event.get("confirmation"))
    event["breakdown"] = {
        "importance": importance,
        "confirmation": confirmation,
        "mapping": mapping,
        "freshness": round(freshness, 4),
        "heat": round(heat_bonus, 4),
    }
    event["score"] = round(importance + confirmation + mapping + freshness + heat_bonus, 4)


def _confirmation_points(raw) -> float:
    if not isinstance(raw, dict):
        return 0.0
    try:
        points = float(raw.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0
    if points != points:
        return 0.0
    return round(min(80.0, max(0.0, points)), 4)


def _event_text(event: dict) -> str:
    parts = [str(event.get("name") or ""), str(event.get("headline") or "")]
    parts.extend(str(item) for item in (event.get("headlines") or []))
    return "\n".join(part for part in parts if part)


def _rule_importance(event: dict) -> str:
    text = _event_text(event)
    if _is_major(text):
        label = "重大"
    elif _is_important(text, str(event.get("category") or "")):
        label = "重要"
    else:
        label = "一般"
    if "传闻" in text:
        label = _IMPORTANCE_ORDER[max(0, _IMPORTANCE_ORDER.index(label) - 1)]
    return label


def _is_major(text: str) -> bool:
    if any(cue in text for cue in _MAJOR_RATE):
        return True
    if any(org in text for org in _MAJOR_ORGS) and any(act in text for act in _MAJOR_POLICY_ACTS):
        return True
    if any(cue in text for cue in _MAJOR_GEO):
        return True
    if any(name in text for name in _MAJOR_COMMODITIES) and any(verb in text for verb in _PRICE_VERBS):
        return True
    return any(name in text for name in _LEADERS) and any(act in text for act in _LEADER_ACTS)


def _is_important(text: str, category: str) -> bool:
    if any(act in text for act in _IMPORTANT_ACTS):
        return True
    if category in _IMPORTANT_CATEGORIES:
        return True
    return any(name in text for name in _LEADERS) and any(verb in text for verb in _PRICE_VERBS)


def _drop_minor(event: dict) -> bool:
    """没有映射、只有一条来源的寻常小事不进榜。重大和重要事件留下。"""
    label = str(event.get("importance") or "")
    if label == "琐碎":
        return True
    if label != "一般":
        return False
    if float(event["breakdown"]["mapping"]) > 0:
        return False
    return int(event.get("mentions") or 0) <= 1 and int(event.get("source_count") or 0) <= 1


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


def _display_name_ok(name: object) -> bool:
    text = _compact(str(name or ""))
    return bool(text) and text != "事件"


def _subject_core(lead: str) -> str:
    core = _compact(lead)
    original = core
    changed = True
    while changed and len(core) > 2:
        changed = False
        for suffix in _SUBJECT_TRAIL:
            if core.endswith(suffix) and len(core) - len(suffix) >= 2:
                core = core[:-len(suffix)]
                changed = True
                break
    if not core or core == original or len(core) < 2:
        return ""
    if core in _STOP or core in _GENERIC_TOKENS or core in BROAD_SECTORS:
        return ""
    return core


def _explicit_entities(text: str) -> list[str]:
    if not text:
        return []
    occupied = [False] * len(text)
    spans: list[tuple[int, str]] = []
    for name in _EXPLICIT_SUBJECTS:
        start = 0
        while True:
            index = text.find(name, start)
            if index < 0:
                break
            end = index + len(name)
            if not any(occupied[index:end]):
                spans.append((index, name))
                for pos in range(index, end):
                    occupied[pos] = True
            start = end
    spans.sort()
    seen: list[str] = []
    for _index, name in spans:
        if name not in seen:
            seen.append(name)
    return seen


def _subject_is_vague(lead: str) -> bool:
    text = _normalize_subject(lead) or _subject_text(lead)
    if len(text) < 2:
        return True
    if _explicit_entities(text):
        return False
    if text in _NOT_SUBJECT or text in BROAD_SECTORS or text in _STOP:
        return True
    if text.endswith(("性", "的", "地")):
        return True
    return any(cue in text for cue in _VAGUE_CUES)


def _norm_title(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", _strip_source_prefix(text))


def _headline_similarity(left: str, right: str) -> float:
    aa = _norm_title(left)
    bb = _norm_title(right)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0

    def grams(text: str) -> set[str]:
        if len(text) < 2:
            return {text}
        return {text[i:i + 2] for i in range(len(text) - 1)}

    ga, gb = grams(aa), grams(bb)
    union = len(ga | gb)
    if not union:
        return 0.0
    return len(ga & gb) / union


def _subject_keys(doc: dict) -> set[str]:
    keys = {entity for entity in (doc.get("entities") or []) if entity}
    lead = str(doc.get("lead") or "")
    if lead and not _subject_is_vague(lead):
        if len(lead) <= 8:
            keys.add(lead)
        core = _subject_core(lead)
        if core:
            keys.add(core)
    return keys


def _same_subject_action(left: dict, right: dict) -> bool:
    if not left.get("action") or left.get("action") != right.get("action"):
        return False
    return bool(_subject_keys(left) & _subject_keys(right))


def _can_merge(left: dict, right: dict) -> bool:
    gap = abs(left["published"] - right["published"])
    if gap > _CLUSTER_WINDOW:
        return False
    if _same_subject_action(left, right):
        return True
    if (
        gap <= _SIMILAR_WINDOW
        and _headline_similarity(str(left.get("title") or ""), str(right.get("title") or "")) >= _SIMILARITY_MIN
    ):
        return True
    shared = set(left.get("tokens") or ()) & set(right.get("tokens") or ())
    shared = {token for token in shared if len(token) >= 3 and token not in _GENERIC_TOKENS}
    if not shared:
        return False
    return not (left.get("action") and right.get("action") and left.get("action") != right.get("action"))


def _subject_set(name: str, members: list[dict]) -> set[str]:
    subjects = set(_explicit_entities(name))
    lead, _action, _obj = _split_headline(name)
    if lead and not _subject_is_vague(lead):
        if len(lead) <= 8:
            subjects.add(lead)
        core = _subject_core(lead)
        if core:
            subjects.add(core)
    for item in members:
        for entity in item.get("entities") or []:
            if entity:
                subjects.add(entity)
        item_lead = str(item.get("lead") or "")
        if item_lead and not _subject_is_vague(item_lead):
            if len(item_lead) <= 8:
                subjects.add(item_lead)
            core = _subject_core(item_lead)
            if core:
                subjects.add(core)
    return {subject for subject in subjects if subject and len(subject) >= 2}


def _matches_event(item: dict, name: str, subjects: set[str]) -> bool:
    title = str(item.get("title") or "")
    if not title:
        return False
    if any(len(subject) >= 2 and subject in title for subject in subjects):
        return True
    if name and _headline_similarity(title, name) >= _REPRESENTATIVE_SIMILARITY:
        return True
    norm_name = _norm_title(name)
    for token in item.get("tokens") or ():
        if len(str(token)) >= 3 and str(token) in norm_name:
            return True
    return False


def _representative_members(members: list[dict], name: str) -> list[dict]:
    subjects = _subject_set(name, [])
    chosen = [item for item in members if _matches_event(item, name, subjects)]
    return chosen or list(members)


def _concept_relevant(name: str, blob: str, subjects: set[str]) -> bool:
    if name and name in blob:
        return True
    # 细概念延伸标题里已经出现的词，例如标题写「机器人」、提及是「工业机器人」。
    if _headline_maps_concept(name, blob):
        return True
    checked: list[str] = []
    for subject in subjects:
        checked.append(subject)
        core = _subject_core(subject)
        if core:
            checked.append(core)
    for token in checked:
        if len(token) < 2 or token in _GENERIC_TOKENS or token in _STOP:
            continue
        if token in name:
            return True
        if len(token) <= 8 and name in token:
            return True
    return False


def _headline_maps_concept(name: str, blob: str) -> bool:
    if len(name) < 3 or not blob or name in blob:
        return False
    for size in range(min(len(name) - 1, 6), 1, -1):
        for start in range(0, len(name) - size + 1):
            token = name[start:start + size]
            if token in _GENERIC_TOKENS or token in _STOP:
                continue
            if token in blob:
                return True
    return False


def _relevant_concepts(counts: Counter[str], titles: list[str], subjects: set[str]) -> list[str]:
    blob = "\n".join(titles)
    filtered: Counter[str] = Counter({
        name: count
        for name, count in counts.items()
        if _concept_relevant(str(name), blob, subjects)
    })
    return _specific_concepts(filtered)


def _stock_relevant(
    name: str,
    key: str,
    titles: list[str],
    subjects: set[str],
    kept_concepts: list[str],
    rejected: list[str],
) -> bool:
    """代表成员上的个股默认留下。名字只贴着被丢掉的概念时才剔除。"""
    label = str(name or "").strip()
    blob = "\n".join(titles)
    if label and label in blob:
        return True
    if key and str(key) in blob:
        return True
    tokens: list[str] = list(kept_concepts)
    for subject in subjects:
        tokens.append(subject)
        core = _subject_core(subject)
        if core:
            tokens.append(core)
    if label and any(len(token) >= 2 and token not in _GENERIC_TOKENS and token in label for token in tokens):
        return True
    tied_to_rejected = bool(label) and any(
        len(concept) >= 2 and (concept in label or label in concept) for concept in rejected
    )
    return not tied_to_rejected


def _cleaned_headline(text: str) -> str:
    """去掉来源前缀，取第一个有内容的分句，过长时停在分句内部的自然断点。"""
    body = _strip_source_prefix(text)
    if not body or body == "事件":
        return ""
    parts = [part.strip() for part in _CLAUSE.split(body) if part.strip()] or [body]
    for part in parts:
        cleaned = _TIME_PREFIX.sub("", _drop_leading_fragment(part)).strip("，。！？、；：\"'“”")
        if cleaned and cleaned != "事件":
            clipped = _clip_at_boundary(cleaned)
            if clipped and clipped != "事件":
                return clipped
    return ""


def _clip_at_boundary(text: str) -> str:
    if not text or text == "事件":
        return ""
    if len(text) <= _TITLE_LIMIT and not _ends_abruptly(text):
        return text
    window = text[:_TITLE_LIMIT]
    cut = -1
    for sep in ("以及", "并且", "同时", "和", "与", "及"):
        index = window.rfind(sep)
        if index >= 4:
            cut = max(cut, index)
    if cut >= 4:
        window = window[:cut]
    while window and (_ends_abruptly(window) or window[-1] in "的和与及"):
        window = window[:-1]
    if len(window) >= 4 and window != "事件" and not _ends_abruptly(window):
        return window
    if text != "事件" and not _ends_abruptly(text):
        return text[:_TITLE_LIMIT] if len(text) > _TITLE_LIMIT else text
    return ""


def _ensure_subject(title: str, source: str) -> str:
    text = _compact(title)
    if not _display_name_ok(text):
        return ""
    lead, action, _obj = _split_headline(text)
    if action and lead and not _subject_is_vague(lead):
        return text
    entities = _explicit_entities(source)
    if not entities:
        return text
    if lead and any(entity in lead for entity in entities) and not _subject_is_vague(lead):
        return text
    attached = _attach_entity(text, entities[0])
    return attached if _display_name_ok(attached) else text


def _attach_entity(title: str, entity: str) -> str:
    if not entity:
        return title
    lead, action, obj = _split_headline(title)
    if entity in title and lead and not _subject_is_vague(lead):
        return title
    prefixed = title if entity in title else f"{entity}{title}"
    if len(prefixed) <= _TITLE_LIMIT and _title_ok(prefixed):
        prefixed_lead, _prefixed_action, _prefixed_obj = _split_headline(prefixed)
        if prefixed_lead and not _subject_is_vague(prefixed_lead):
            return prefixed
    if action:
        fitted = _fit_object(f"{entity}{action}", obj)
        if _title_ok(fitted):
            return fitted
    if len(prefixed) <= _TITLE_LIMIT and _title_ok(prefixed):
        return prefixed
    return title


def _keyword_title(members: list[dict]) -> str:
    """用代表标题的第一分句，不再把各条的主体、动作、宾语拼在一起。

    合格分句优先。没有合格分句时，退回去掉来源前缀、按分句截断的原标题。
    仍然没有可读文本时返回空字符串，调用方把这条事件丢掉，不再显示「事件」。
    """
    clauses: list[str] = []
    for item in members:
        clause = _representative_clause(str(item.get("title") or ""))
        if clause and clause != "事件":
            clauses.append(clause)
    blob = "\n".join(
        str(item.get("entity_text") or item.get("title") or "")
        for item in members
    )
    if clauses:
        counts = Counter(clauses)
        best = max(counts.values())
        for clause in clauses:
            if counts[clause] != best:
                continue
            ensured = _ensure_subject(clause, blob)
            if _display_name_ok(ensured):
                return ensured
    for item in members:
        cleaned = _cleaned_headline(str(item.get("title") or ""))
        if not cleaned:
            continue
        ensured = _ensure_subject(cleaned, blob)
        if _display_name_ok(ensured):
            return ensured
    return ""


def _is_noise(title: str) -> bool:
    text = re.sub(r"\s+", "", (title or "").strip())
    if not text or text in _NOISE_EXACT or len(text) < 4:
        return True
    if sum(1 for char in text if "\u4e00" <= char <= "\u9fff") < 2:
        return True
    return any(hint in text for hint in _NOISE_HINTS)


def _relevance(title: str, lead: str, action: str, concepts: list[str], stocks: dict) -> int:
    """有主体也有动作才算一条能进榜的资讯。图片、闲聊和广告是 0。"""
    if _is_noise(title) or not lead or not action:
        return 0
    score = 3
    if concepts:
        score += 1
    if stocks:
        score += 1
    return score


def _category_for(text: str, action: str) -> str:
    blob = text or ""
    for name, cues in _CATEGORY_CUES:
        if any(cue in blob for cue in cues):
            return name
    return _ACTION_CATEGORY.get(action, "科技与产业")


def _direction_for(text: str, action: str) -> str:
    blob = text or ""
    if any(cue in blob for cue in _BEAR_CUES):
        return "利空"
    if any(cue in blob for cue in _BULL_CUES):
        return "利好"
    if action in {"降价", "下调", "立案", "停产", "召回", "制裁", "收紧", "加息"}:
        return "利空"
    return "利好"


def _mode(values: list[str]) -> str:
    if not values:
        return ""
    return Counter(values).most_common(1)[0][0]


def _compact(text: str) -> str:
    body = re.sub(r"\s+", "", (text or "").strip())
    return body.strip("，。！？、；：\"'“”")


def _strip_source_prefix(text: str) -> str:
    body = _compact(text)
    changed = True
    while body and changed:
        changed = False
        for pattern in (_SOURCE_BRACKET, _SOURCE_DATELINE, _SOURCE_LABEL):
            updated = pattern.sub("", body, count=1)
            if updated != body:
                body = updated.lstrip("，。！？、；：")
                changed = True
    return _REUTERS_TAIL.sub("", body).strip("，。！？、；：")


def _starts_fragment(text: str) -> bool:
    if not text:
        return False
    if _FRAGMENT_PREFIX.match(text):
        return True
    return text.startswith("与")


def _drop_leading_fragment(text: str) -> str:
    body = text
    while body:
        match = _FRAGMENT_PREFIX.match(body)
        if match:
            body = body[match.end():]
            continue
        if body.startswith("与"):
            body = body[1:]
            continue
        break
    return body


def _ends_abruptly(text: str) -> bool:
    if not text:
        return True
    last = text[-1]
    if last in _DANGLING_FINAL:
        return True
    allowed = _CLOSED_TAILS.get(last)
    return allowed is not None and text[-2:] not in allowed


def _subject_text(before: str) -> str:
    lead = _compact(before)
    changed = True
    while changed and lead:
        changed = False
        for suffix in _LEAD_SUFFIXES:
            if lead.endswith(suffix) and len(lead) - len(suffix) >= 2:
                lead = lead[:-len(suffix)]
                changed = True
                break
    return lead


def _subject_ok(before: str) -> bool:
    lead = _subject_text(before)
    if len(lead) < 2 or lead in _NOT_SUBJECT or lead in BROAD_SECTORS:
        return False
    if _starts_fragment(lead) or lead.endswith(("性", "的", "地", "超")):
        return False
    if lead.endswith(("针对", "对于", "关于")):
        return False
    if any(word in lead for word in _PRICE_NARRATIVE):
        return False
    return _tuple_action(lead)[0] < 0


def _normalize_subject(before: str) -> str:
    lead = _drop_leading_fragment(_subject_text(before))
    lead = _TIME_PREFIX.sub("", lead)
    for suffix in ("针对", "对于", "关于"):
        if lead.endswith(suffix) and len(lead) - len(suffix) >= 2:
            lead = lead[:-len(suffix)]
    return lead


def _tuple_action(text: str) -> tuple[int, str]:
    for verb in _ACTIONS:
        index = text.find(verb)
        if index >= 0:
            return index, verb
    return -1, ""


def _narrative_action(text: str) -> tuple[int, str]:
    found: tuple[int, str] | None = None
    for verb in _PRICE_NARRATIVE:
        index = text.find(verb)
        if index < 0:
            continue
        if found is None or index < found[0] or (index == found[0] and len(verb) > len(found[1])):
            found = (index, verb)
    return found if found else (-1, "")


def _split_headline(text: str) -> tuple[str, str, str]:
    """选出主体说得通的谓语。叙述后面再接发布、出台这类动作时，宾语停在那之前。"""
    options: list[tuple[int, int, str, str]] = []
    for index, verb in (_tuple_action(text), _narrative_action(text)):
        if index < 0 or not verb or not _subject_ok(text[:index]):
            continue
        options.append((index, -len(verb), verb, text[:index]))
    if not options:
        return "", "", ""
    options.sort()
    index, _neg, verb, lead = options[0]
    rest = text[index + len(verb):]
    obj = rest
    if verb in _PRICE_NARRATIVE:
        later, later_verb = _tuple_action(rest)
        if later > 0 and later_verb in _GLUE_FOLLOWUPS:
            obj = rest[:later]
    return lead, verb, obj.strip("，。！？、；：")


def _title_ok(title: str) -> bool:
    """完整、有主体和动作、不超过 20 字，且不是半句话或词中间截断。"""
    text = _compact(title)
    if not text or len(text) > _TITLE_LIMIT or text in BROAD_SECTORS:
        return False
    if _starts_fragment(text) or _ends_abruptly(text):
        return False
    if re.search(r"超(?!过|预|标|额|出|市)", text):
        return False
    lead, action, obj = _split_headline(text)
    if not action or not _subject_ok(lead):
        return False
    tail = text[len(lead) + len(action) + len(obj):].strip("，。！？、；：")
    return not tail


def _fit_object(base: str, obj: str) -> str:
    obj = obj.strip("，。！？、；：")
    if obj and len(base) + len(obj) <= _TITLE_LIMIT and not _ends_abruptly(obj):
        return base + obj
    head = re.split(r"[和与及]", obj, maxsplit=1)[0] if obj else ""
    if head and head != obj and len(base) + len(head) <= _TITLE_LIMIT and not _ends_abruptly(head):
        return base + head
    return base


def _repair_headline(text: str) -> str:
    body = _TIME_PREFIX.sub("", _drop_leading_fragment(_compact(text)))
    if not body:
        return ""
    options: list[tuple[int, int, str, str]] = []
    for index, verb in (_tuple_action(body), _narrative_action(body)):
        if index < 0 or not verb:
            continue
        subject = _normalize_subject(body[:index])
        if _subject_ok(subject):
            options.append((index, -len(verb), subject, verb))
    if not options:
        return ""
    options.sort()
    index, _neg, subject, verb = options[0]
    rest = body[index + len(verb):]
    obj = rest
    if verb in _PRICE_NARRATIVE:
        later, later_verb = _tuple_action(rest)
        if later > 0 and later_verb in _GLUE_FOLLOWUPS:
            obj = rest[:later]
    base = f"{subject}{verb}"
    if len(base) > _TITLE_LIMIT or _ends_abruptly(base):
        return ""
    return _fit_object(base, obj)


def _fit_clause(clause: str) -> str:
    compact = _drop_leading_fragment(_strip_source_prefix(clause))
    if not compact:
        return ""
    if _title_ok(compact):
        return compact
    repaired = _repair_headline(compact)
    if repaired and _title_ok(repaired):
        return repaired
    return ""


def _representative_clause(text: str) -> str:
    body = _strip_source_prefix(text)
    if not body:
        return ""
    for part in _CLAUSE.split(body):
        fitted = _fit_clause(part)
        if fitted:
            return fitted
    return ""


def _title_cache_key(fingerprint: str) -> str:
    return f"title-v{_TITLE_CACHE_VERSION}:{fingerprint}"


def _purge_stale_title_cache() -> None:
    prefix = f"title-v{_TITLE_CACHE_VERSION}:"
    for key in [key for key in _LLM_CACHE if not str(key).startswith(prefix)]:
        _LLM_CACHE.pop(key, None)


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

    _purge_stale_title_cache()
    used = 0
    for event in events[:_LLM_PER_PASS]:
        fingerprint = str(event.get("_fp") or "")
        cache_key = _title_cache_key(fingerprint)
        cached = _LLM_CACHE.get(cache_key)
        cached_title = str((cached or {}).get("title") or "")
        if cached is not None and (not cached_title or _title_ok(cached_title)):
            _apply_llm(event, cached)
            continue
        if cached is not None:
            _LLM_CACHE.pop(cache_key, None)
        if used >= _LLM_PER_PASS:
            break
        if not _reserve_llm_call():
            break
        used += 1
        headlines = "\n".join(str(item) for item in (event.get("headlines") or [event.get("headline") or ""]))
        prompt = (
            "下面几条标题是同一件与资本市场有关的资讯。收成一个具体事件，不要用宽泛行业名当标题。"
            "只输出 JSON："
            '{"title":"不超过20个字","category":"","direction":"利好或利空",'
            '"importance":"重大或重要或一般或琐碎",'
            '"concepts":[{"name":"细分概念","direction":"利好或利空"}],'
            '"stocks":[{"name":"","code":"","direction":"利好或利空"}]}。'
            "title 必须是完整具体的事件标题，不超过20个字，包含主体和动作，"
            "例如美联储降息25基点。不要半句话，不要以叠加、和之前、根据、与开头，不要在词中间截断。"
            "不要用「事件」当标题。主体必须写明，例如美联储、国务院、政治局，"
            "不要只写可能还会、宏观经济运行情况这种没有主体的说法。"
            f"category 只能是：{'、'.join(CATEGORY_NAMES)}。"
            "importance 按分量：央行利率、国家级政策、战争制裁、大宗商品冲击、龙头公司重大事项是重大；"
            "寻常涨价、获批、订单是重要。"
            "concepts 写受影响的细概念，不要写人工智能、半导体、医药这种大行业。"
            "direction 表示对 A 股相关方向是利好还是利空。"
            "没有把握就留空，不要编造标题里没出现的事实。\n\n"
            f"标题：\n{headlines}"
        )
        try:
            raw = _llm_text(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.info("热门事件标题生成失败: %s", exc)
            raw = ""
        parsed = _parse_llm_event(raw, str(event.get("headline") or ""))
        _LLM_CACHE[cache_key] = parsed or {}
        if parsed:
            _apply_llm(event, parsed)


def _parse_llm_event(raw: str, text: str) -> dict | None:
    from app.news.extract import _llm_object

    payload = _llm_object(raw)
    if not payload:
        return None
    title = _compact(str(payload.get("title") or ""))
    if not _title_ok(title):
        title = ""
    concepts = []
    concept_directions: dict[str, str] = {}
    for item in payload.get("concepts") or payload.get("sectors") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            direct = str(item.get("direction") or "")
        else:
            name, direct = str(item or "").strip(), ""
        if name and name in text and _fine_concept(name, name) and name not in concepts:
            concepts.append(name)
            if direct in {"利好", "利空"}:
                concept_directions[name] = direct
    stocks = []
    for item in payload.get("stocks") or []:
        if isinstance(item, str):
            name, code, direct = item.strip(), "", ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            code = str(item.get("code") or item.get("symbol") or "").strip()
            direct = str(item.get("direction") or "")
        else:
            continue
        if (name and name in text) or (code and code in text):
            row = {"name": name, "code": code}
            if direct in {"利好", "利空"}:
                row["direction"] = direct
            stocks.append(row)
    category = str(payload.get("category") or "").strip()
    if category not in CATEGORY_NAMES:
        category = ""
    direction = str(payload.get("direction") or "").strip()
    if direction not in {"利好", "利空"}:
        direction = ""
    importance = str(payload.get("importance") or "").strip()
    if importance not in _IMPORTANCE_POINTS:
        importance = ""
    if not title and not concepts and not stocks and not category and not importance:
        return None
    return {
        "title": title,
        "category": category,
        "direction": direction,
        "importance": importance,
        "concepts": concepts[:_CONCEPT_LIMIT],
        "concept_directions": concept_directions,
        "stocks": stocks,
    }


def _apply_llm(event: dict, parsed: dict) -> None:
    title = str(parsed.get("title") or "")
    if _title_ok(title):
        blob = "\n".join(
            [str(event.get("headline") or "")]
            + [str(item) for item in (event.get("headlines") or [])]
        )
        ensured = _ensure_subject(title, blob)
        if _title_ok(ensured) and _display_name_ok(ensured):
            event["name"] = ensured
    category = str(parsed.get("category") or "")
    if category in CATEGORY_NAMES:
        event["category"] = category
    direction = str(parsed.get("direction") or "")
    if direction in {"利好", "利空"}:
        event["direction"] = direction
    importance = str(parsed.get("importance") or "")
    if importance == "琐碎":
        importance = "一般"
    if importance in _IMPORTANCE_POINTS:
        event["importance"] = importance
    for name in parsed.get("concepts") or []:
        if name not in event["concepts"] and len(event["concepts"]) < _CONCEPT_LIMIT:
            event["concepts"].append(name)
    texts = [str(event.get("name") or ""), str(event.get("headline") or "")]
    texts.extend(str(item) for item in (event.get("headlines") or []))
    event["concepts"] = _relevant_concepts(
        Counter({str(name): 1 for name in event.get("concepts") or []}),
        texts,
        _subject_set(str(event.get("name") or ""), []),
    )
    directions = parsed.get("concept_directions") or {}
    event["mapping"] = [
        {
            "name": name,
            "kind": "concept",
            "direction": directions.get(name) or event.get("direction") or "利好",
        }
        for name in event["concepts"]
    ]
    if direction in {"利好", "利空"}:
        for stock in event.get("mentioned_stocks") or []:
            stock["direction"] = direction
    known = {item["key"] for item in event.get("mentioned_stocks") or []}
    known_names = {item["name"] for item in event.get("mentioned_stocks") or []}
    for stock in parsed.get("stocks") or []:
        code = str(stock.get("code") or "").strip()
        name = str(stock.get("name") or "").strip()
        key = code or name
        direct = str(stock.get("direction") or event.get("direction") or "利好")
        if direct not in {"利好", "利空"}:
            direct = "利好"
        if not key or key in known or name in known_names:
            continue
        known.add(key)
        event["mentioned_stocks"].append({
            "key": key,
            "name": name or key,
            "mentions": 1,
            "source_count": 1,
            "direction": direct,
        })
