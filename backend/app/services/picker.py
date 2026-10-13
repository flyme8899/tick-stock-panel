"""选股页：多来源合并、过滤和快照对比。

口径：
- 财务阈值沿用财务快照已有单位。ROE、增速、资产负债率是百分数（15 表示 15%）。
  经营现金/营收是比率（0.8 表示 80%），不按数值大小猜测单位。
- 利润增速优先用扣非同比列；快照没有该列时改用归母净利同比，表头随实际口径变化。
- 市盈率只用快照里的 pe/pe_ttm，或收盘价除以每股收益。不用 ROE 反推。
- 公告日当天的财报不参与选股，下一交易日才生效，与回测财务因子一致。
- 来源运行失败（例如 DSA 连不上）不参与交集/并集，避免一次失败把其他来源清空。
  来源成功但一只都没有，仍然参与合并。
- 热门事件按北京时间当前交易日的资讯标题聚类，取排序前 8。重要性与盘面验证优先，热度只作加分。
  选中后并入该事件直接提到的个股，以及细分概念的成分股（最多 30 只），不展开宽行业。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import CN_TZ, cn_now, current_trading_day
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# 板块名是中文。只拒绝路径和空白，避免把事件 id 收成另一套编码。
_SAFE_ID = re.compile(r"^[\w.:-]{1,64}$")
_ST_RE = re.compile(r"(?i)ST|\*ST|退")
_SNAPSHOT_NAME = re.compile(r"^\d{8}T\d{6}(?:-\d+)?\.json$")
_MAX_SNAPSHOTS = 40
_MAX_ROWS = 800
_FINANCIAL_KEYWORDS = ("银行", "非银金融", "保险", "证券", "多元金融")
_DEDUCTED_YOY_COLUMNS = (
    "deducted_net_income_yoy",
    "net_profit_deducted_yoy",
    "np_deducted_yoy",
    "扣非净利润同比",
)
_FINANCIAL_COLUMNS = {
    "symbol",
    "announce_date",
    "roe",
    "net_income_yoy",
    "revenue_yoy",
    "debt_to_asset_ratio",
    "operating_cash_to_revenue",
    "gross_margin",
    "net_margin",
    "basic_eps",
    "eps",
    "pe",
    "pe_ttm",
    "bps",
    *_DEDUCTED_YOY_COLUMNS,
}

FUNDAMENTAL_PRESETS: tuple[dict[str, str], ...] = (
    {
        "id": "fundamental_m1",
        "name": "成长质量精选",
        "description": "ROE≥15%，利润增速≥20%，营收增速≥10%，资产负债率≤60%。有市盈率时限制在 0–60 倍。",
    },
    {
        "id": "fundamental_m2",
        "name": "长期价值白马",
        "description": "总市值≥200亿，ROE≥12%，利润增速不为负，资产负债率≤55%，市盈率 0–25 倍。缺少市盈率时本次无结果。",
    },
    {
        "id": "fundamental_m3",
        "name": "稳健现金流",
        "description": "经营现金/营收≥0.8（比率），ROE≥8%，利润增速不为负，资产负债率≤55%。",
    },
)

_TOP_HOT_EVENTS = 8
_CONCEPT_CONSTITUENT_CAP = 30

# 宽泛板块: 命中它们等于把半个市场搬进来, 对选股没有区分度, 归为 broad 降权。
_BROAD_MAPPING_CEILING = 800
# usable 内部的「太窄」下限: 只选出 1~2 只的事件覆盖不足, 排在舒适区之后(但仍可用)。
_MAPPING_MIN_COMFORTABLE = 5
# 「重大」档在选股列表里的保底席位: 允许无映射的重大事件占位, 但不超过这个数。
# 目的: 既不把「美联储讲话」这类高关注度新闻全埋掉, 也不让一堆选不出股的空炮
# 霸占整个列表(线上实测: 档位优先时前 8 条仅 1 条能选出股)。
_MAJOR_RESERVED_SLOTS = 2

# 资讯栏/专栏名常被当成概念带进来(如「美联储动态」「汽车大新闻」「期货市场情报」),
# 这类名字在板块维表里查不到, 去掉栏目后缀后往往能还原出真正的板块词。
_COLUMN_SUFFIXES = (
    "行业新闻", "市场情报", "大新闻", "情报站", "专栏", "快报", "动态", "新闻", "播报", "报道",
)
# 出现在概念名里但纯属资讯语境的虚词, 归一化时先剥掉。
_COLUMN_INFIX = ("最新", "今日", "盘中", "早间", "午后", "隔夜", "海外", "国内")
# 词素匹配里必须直接跳过的通用词: 它们命中一片板块却毫无指向性。
# 只按 2 字以上统计, 所以「股」「涨」这类单字不会进来, 但「个股」「板块」会。
_TOKEN_STOPWORDS = frozenset({
    "个股", "板块", "行业", "概念", "指数", "基金", "龙头", "市场", "全线", "集体",
    "大涨", "大跌", "上涨", "下跌", "走强", "走低", "涨停", "跌停", "异动", "拉升",
    "公司", "集团", "股份", "相关", "新闻", "消息", "数据", "报告", "分析", "观点",
    "美股", "港股", "中概", "海外", "全球", "国内", "本周", "今日", "昨日", "盘中",
})


def _strip_column_suffix(value: str) -> str:
    """去掉资讯栏目后缀, 得到疑似板块词。改不出更短的名字就原样返回。"""
    text = str(value or "").strip()
    for suffix in _COLUMN_SUFFIXES:
        if text.endswith(suffix) and len(text) - len(suffix) >= 2:
            return text[: -len(suffix)]
    return text


def _concept_tokens(value: str) -> list[str]:
    """把概念名切成 2 字以上的中文片段, 供「任一片段命中」的宽松匹配用。

    只在精确匹配、去后缀匹配都失败时才启用 —— 「数据中心液冷」对「液冷服务器」
    这种没有包含关系、但共享「液冷」词素的组合, 靠它才能接上。

    切出来的词素要能**唯一定位**才有意义, 所以通用词(见 `_TOKEN_STOPWORDS`)
    和单字先剔除, 避免「个股」「美股」这类噪音把宽泛板块拉进来。
    """
    text = _strip_column_suffix(str(value or "").strip())
    for infix in _COLUMN_INFIX:
        text = text.replace(infix, "")
    parts: list[str] = []

    def _keep(part: str) -> bool:
        return len(part) >= 2 and part not in _TOKEN_STOPWORDS

    # 按非中文/字母数字切开, 再对纯中文串做 2-4 字滑窗
    for chunk in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", text):
        if not chunk:
            continue
        if chunk.isascii():
            # 短英文缩略词(ai/5g/pcb)大多本身就是一个板块名, 长串才值得切
            if len(chunk) >= 2 and _keep(chunk):
                parts.append(chunk)
            continue
        if len(chunk) <= 4:
            if _keep(chunk):
                parts.append(chunk)
            continue
        for size in (4, 3, 2):
            for start in range(len(chunk) - size + 1):
                part = chunk[start : start + size]
                if _keep(part):
                    parts.append(part)
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return seen


def safe_source_id(value: str) -> bool:
    return bool(_SAFE_ID.match(value)) and ".." not in value


@dataclass
class SourceSpec:
    type: str
    id: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class FilterSpec:
    industries: list[str] = field(default_factory=list)
    market_cap_min: float | None = None
    market_cap_max: float | None = None
    pe_min: float | None = None
    pe_max: float | None = None
    exclude_st: bool = False
    exclude_financial: bool = False
    max_per_industry: int | None = None


@dataclass
class Dimensions:
    industry_label: dict[str, str] = field(default_factory=dict)
    industry_path: dict[str, str] = field(default_factory=dict)
    concepts: dict[str, list[str]] = field(default_factory=dict)


def empty_hot_snapshot(now: datetime) -> dict:
    trading = current_trading_day(now)
    return {
        "as_of": None,
        "trading_day": trading.isoformat(),
        "fallback": False,
        "hint": None,
        "updated_at": None,
        "events": [],
    }


def hot_event_id(key: str) -> str:
    """板块名能直接当来源 id 时用原名，否则用短哈希，避免把斜杠送进接口。"""
    text = str(key or "").strip()
    if safe_source_id(text):
        return text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return f"ev_{digest}"


@dataclass
class PickerDeps:
    data_dir: Path
    list_strategies: Callable[[], list[dict]]
    run_strategies: Callable[[list[str]], dict[str, tuple[list[dict], str | None]]]
    load_market: Callable[[], tuple[date | None, list[dict]]]
    load_financial: Callable[[date], tuple[pl.DataFrame, str]]
    load_dimensions: Callable[[], Dimensions]
    dsa_request: Callable[[str, str, dict | None], tuple[int, dict]] | None
    now: datetime | None = None
    poll_seconds: float = 12.0
    limiter: Any = None
    load_top_events: Callable[[datetime], dict] = field(default=empty_hot_snapshot)


def classify_strategies(metas: list[dict]) -> tuple[list[dict], list[dict]]:
    """内置日线策略进技术面；因子生成的策略进因子打分。基本面 id 不进这两栏。"""
    technical: list[dict] = []
    factor: list[dict] = []
    for meta in metas:
        if meta.get("research_only"):
            continue
        sid = str(meta.get("id") or "")
        if not sid or not safe_source_id(sid):
            continue
        if "stock" not in (meta.get("asset_types") or ["stock"]):
            continue
        if "1d" not in (meta.get("timeframes") or ["1d"]):
            continue
        item = {
            "id": sid,
            "name": str(meta.get("name") or sid),
            "description": str(meta.get("description") or ""),
        }
        tags = {str(tag) for tag in (meta.get("tags") or [])}
        if sid.startswith("custom_factor_") or "factor" in tags:
            factor.append(item)
            continue
        if sid.startswith("fundamental_") or "fundamental" in tags:
            continue
        if meta.get("source") not in (None, "builtin"):
            continue
        technical.append(item)
    technical.sort(key=lambda item: item["name"])
    factor.sort(key=lambda item: item["name"])
    return technical, factor


def combine_symbol_sets(groups: list[set[str]], mode: str) -> set[str]:
    if not groups:
        return set()
    if mode == "and":
        result = set(groups[0])
        for group in groups[1:]:
            result &= group
        return result
    result: set[str] = set()
    for group in groups:
        result |= group
    return result


def industry_label(path: str) -> str:
    parts = [part.strip() for part in str(path or "").split("-") if part.strip()]
    if not parts:
        return ""
    if len(parts) >= 2:
        return parts[1]
    return parts[0]


def _norm_label(value: str) -> str:
    return "".join(str(value or "").split()).casefold()


def build_label_index(dimensions: Dimensions) -> dict[str, set[str]]:
    index: dict[str, set[str]] = defaultdict(set)
    for symbol, labels in dimensions.concepts.items():
        for label in labels:
            key = _norm_label(label)
            if key:
                index[key].add(symbol)
    for symbol, path in dimensions.industry_path.items():
        parts = [part.strip() for part in str(path or "").split("-") if part.strip()]
        for part in parts:
            key = _norm_label(part)
            if key:
                index[key].add(symbol)
        whole = _norm_label(path)
        if whole:
            index[whole].add(symbol)
    return index


def _match_exact(candidates: list[str], index: dict[str, set[str]]) -> set[str]:
    for raw in candidates:
        exact = index.get(_norm_label(raw))
        if exact:
            return set(exact)
    return set()


def _match_contained(candidates: list[str], index: dict[str, set[str]]) -> set[str]:
    """有限包含匹配: 拒绝过短词素与超过 30 个标签的过宽命中。"""
    for raw in candidates:
        needle = _norm_label(raw)
        if len(needle) < 2:
            continue
        hits: set[str] = set()
        matched = 0
        too_broad = False
        for label, symbols in index.items():
            if needle == label or (needle not in label and label not in needle):
                continue
            shorter = min(len(needle), len(label))
            longer = max(len(needle), len(label))
            if shorter * 2 < longer and shorter < 4:
                continue
            matched += 1
            if matched > 30:
                too_broad = True
                break
            hits |= symbols
        if hits and not too_broad:
            return hits
    return set()


def _match_tokens(candidates: list[str], index: dict[str, set[str]]) -> set[str]:
    """任一词素命中即算: 兜住「数据中心液冷 → 液冷服务器」这类无包含关系的同义说法。

    词素比整名短得多, 更容易撞上一堆板块, 所以这里必须比包含匹配更严。精度护栏
    (按专家意见收紧, 防止「科技」「美股」这类泛词把无关板块拉进甜区被优先展示):
      ① 命中 **不同标签数** 超过 30 个视为泛词, 直接丢弃(与包含匹配同一条线);
      ② 2 字词素**必须唯一定位**(只命中一个标签)才收 —— 2 字太短, 命中两个以上
         标签时几乎一定是巧合; 3 字以上放宽到「命中标签数 ≤ 3」;
      ③ 命中的股票总数超过上限的也丢弃(命中少数几个宽板块的情形)。
    """
    best: set[str] = set()
    for raw in candidates:
        for token in _concept_tokens(str(raw)):
            key = _norm_label(token)
            if len(key) < 2:
                continue
            hits: set[str] = set()
            labels = 0
            for label, symbols in index.items():
                if key in label or label in key:
                    labels += 1
                    if labels > 30:
                        break
                    hits |= symbols
            if labels > 30 or not hits:
                continue
            # 词素越短越容易撞车: 2 字要求唯一命中, 3 字以上最多 3 个标签。
            max_labels = 1 if len(key) <= 2 else 3
            if labels > max_labels:
                continue
            if len(hits) > _BROAD_MAPPING_CEILING:
                continue
            # 词素越具体(命中越少)越可信, 取最具体的那次结果
            if not best or len(hits) < len(best):
                best = hits
        if best:
            return best
    return best


def match_sector_symbols(name: str, key: str, index: dict[str, set[str]]) -> set[str]:
    """把事件里的概念/标题名落到板块成分股上。

    依次尝试: 精确 → 去栏目后缀 → 包含 → 词素。越靠前越可信, 命中即返回。
    """
    raw = [name, key]
    stripped = [_strip_column_suffix(name), _strip_column_suffix(key)]
    candidates: list[str] = []
    for value in raw + stripped:
        text = str(value or "").strip()
        if text and text not in candidates:
            candidates.append(text)
    for matcher in (_match_exact, _match_contained, _match_tokens):
        hits = matcher(candidates, index)
        if hits:
            return hits
    return set()


def _hot_attr(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def hot_event_label(name: str, source_count: int) -> str:
    text = str(name or "").strip() or "事件"
    return f"{text} · {source_count}源"


def hot_display_score(source_count: int, story_count: int) -> float:
    return round(min(100.0, source_count * 10 + min(max(story_count, 0), 10) * 2), 1)


def map_hot_sectors(
    candidates: list[Any],
    dimensions: Dimensions,
    *,
    min_source_count: int,
) -> tuple[dict[str, float], dict[str, str]]:
    index = build_label_index(dimensions)
    scores: dict[str, float] = {}
    events: dict[str, list[str]] = defaultdict(list)
    for item in candidates:
        sources = list(_hot_attr(item, "sources", ()) or ())
        if len(sources) < min_source_count:
            continue
        name = str(_hot_attr(item, "name", "") or "")
        key = str(_hot_attr(item, "key", "") or "")
        symbols = match_sector_symbols(name, key, index)
        if not symbols:
            continue
        label = hot_event_label(name or key, len(sources))
        score = hot_display_score(len(sources), int(_hot_attr(item, "story_count", 0) or 0))
        for symbol in symbols:
            events[symbol].append(label)
            scores[symbol] = max(scores.get(symbol, 0.0), score)
    return scores, {symbol: _join_events(labels) for symbol, labels in events.items()}


def _mentioned_is_fund(key: str, name: str = "") -> bool:
    """选股只收个股。维表优先，没有维表时再看基金名称和代码前缀。"""
    from app.news.service import asset_kind_of

    return asset_kind_of(key, name) == "etf"


def resolve_mentioned_stock(key: str, known_symbols: set[str]) -> str | None:
    text = str(key or "").strip()
    if not text:
        return None
    if known_symbols:
        if text in known_symbols:
            return text
        return resolve_dsa_code(text, known_symbols)
    if "." in text:
        return text
    return None


def resolve_event_symbols(
    event: dict,
    dimensions: Dimensions,
    label_index: dict[str, set[str]],
    *,
    known_symbols: set[str] | None = None,
    market_caps: dict[str, float] | None = None,
    constituent_cap: int = _CONCEPT_CONSTITUENT_CAP,
) -> set[str]:
    """事件 → 候选个股集合。**唯一一把尺**, 选项预览与真正跑选股都用它。

    与旧实现的差别(修掉专家指出的「预览数系统性高估」):
      - 概念成分股按 `constituent_cap` 截断, 不是全量并集;
      - 提及个股要过 `_mentioned_is_fund` 与 `resolve_mentioned_stock`;
      - 结果再整体过滤一次基金。
    `known_symbols` 为空时(选项预览阶段没有行情表)跳过代码校验, 但仍然过滤基金。
    """
    symbols, _capped = _event_symbols_and_capped(
        event,
        dimensions,
        label_index,
        known_symbols=known_symbols,
        market_caps=market_caps,
        constituent_cap=constituent_cap,
    )
    return symbols


def _event_symbols_and_capped(
    event: dict,
    dimensions: Dimensions,
    label_index: dict[str, set[str]],
    *,
    known_symbols: set[str] | None = None,
    market_caps: dict[str, float] | None = None,
    constituent_cap: int = _CONCEPT_CONSTITUENT_CAP,
) -> tuple[set[str], bool]:
    """`resolve_event_symbols` 的实现, 额外返回「成分股是否真被上限截断」。

    `selectable_count` 恰好等于上限时无法区分「正好 30 只」和「30 只是砍出来的」——
    这决定前端该说「30 只」还是「至少 30 只」。所以把「是否真截断」显式透出,
    而不是让调用方拿 `count >= cap` 去猜(恰好 30 时会误报)。
    截断 = 资讯提及之外的成分股比上限还多。
    """
    concepts = _event_concept_names(event)
    has_dimensions = bool(dimensions.concepts or dimensions.industry_path)
    constituents: set[str] = set()
    if has_dimensions:
        for concept in concepts:
            constituents |= match_sector_symbols(concept, concept, label_index)

    resolved: set[str] = set()
    mention_rank: dict[str, int] = {}
    mentioned = event.get("mentioned_stocks")
    if not isinstance(mentioned, list):
        mentioned = event.get("frequent_stocks") or []
    for stock in mentioned:
        if not isinstance(stock, dict):
            continue
        raw_key = str(stock.get("key") or "")
        stock_name = str(stock.get("name") or "")
        if _mentioned_is_fund(raw_key, stock_name):
            continue
        try:
            mention_rank[raw_key] = int(stock.get("mentions") or 0)
        except (TypeError, ValueError):
            mention_rank[raw_key] = 0
        if known_symbols:
            symbol = resolve_mentioned_stock(raw_key, known_symbols)
            if symbol is None:
                continue
        else:
            # 预览阶段拿不到行情表, 退化为「代码看起来像 A 股个股」才收。
            symbol = resolve_mentioned_stock(raw_key, set())
            if symbol is None:
                continue
        resolved.add(symbol)

    caps = market_caps or {}
    extra = sorted(
        constituents - resolved,
        key=lambda symbol: (-mention_rank.get(symbol, 0), -(caps.get(symbol) or 0.0), symbol),
    )
    picked = {
        symbol for symbol in (resolved | set(extra[:constituent_cap]))
        if not _mentioned_is_fund(symbol)
    }
    # 恰好等于上限时 extra 可能正好是 cap 只 —— 那只算「正好」不算「被砍」。
    return picked, len(extra) > constituent_cap


def _mapping_note(
    event: dict,
    *,
    symbols: set[str],
    concepts: list[str],
    has_dimensions: bool,
    known_symbols: set[str] | None,
) -> str | None:
    """事件映射的「为什么选不出/选了哪些」说明, 供实跑与选项预览共用。

    区分三种本质不同的「空」: 维表未同步 / 事件真没映射到成分股 / 提及个股对不上代码。
    前端据此给不同文案, 不能一律「暂无对应个股」。
    """
    name = str(event.get("name") or event.get("key") or "")
    key = str(event.get("key") or "")
    mentioned = event.get("mentioned_stocks")
    if not isinstance(mentioned, list):
        mentioned = event.get("frequent_stocks") or []
    unresolved = 0
    for stock in mentioned:
        if not isinstance(stock, dict):
            continue
        raw_key = str(stock.get("key") or "")
        stock_name = str(stock.get("name") or "")
        if _mentioned_is_fund(raw_key, stock_name):
            continue
        if resolve_mentioned_stock(raw_key, known_symbols or set()) is None:
            unresolved += 1
    resolved = bool(symbols)
    if concepts and not has_dimensions and resolved:
        return "尚未同步同花顺行业/概念，本次只纳入资讯里提到的个股"
    if concepts and not has_dimensions and not resolved:
        return "尚未同步同花顺行业/概念，无法映射成分股"
    if not symbols:
        return f"「{name or key}」没有匹配到成分股"
    if unresolved and not resolved:
        return "热门个股无法对应本地代码"
    if unresolved:
        return "部分热门个股无法对应本地代码"
    return None


def map_selected_hot_event(
    event: dict,
    dimensions: Dimensions,
    known_symbols: set[str],
    index: dict[str, set[str]] | None = None,
    market_caps: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, str], str | None]:
    """个股来自事件正文里点到的名字；成分股只取细分概念，并按提及和市值截断。"""
    name = str(event.get("name") or event.get("key") or "")
    key = str(event.get("key") or "")
    try:
        mentions = int(event.get("mentions") or 0)
    except (TypeError, ValueError):
        mentions = 0
    try:
        source_count = int(event.get("source_count") or 0)
    except (TypeError, ValueError):
        source_count = 0
    label = hot_event_label(name or key, source_count)
    score = hot_display_score(source_count, mentions)
    concepts = _event_concept_names(event)
    has_dimensions = bool(dimensions.concepts or dimensions.industry_path)
    label_index = index if index is not None else build_label_index(dimensions)
    symbols = resolve_event_symbols(
        event,
        dimensions,
        label_index,
        known_symbols=known_symbols,
        market_caps=market_caps,
    )
    note = _mapping_note(
        event,
        symbols=symbols,
        concepts=concepts,
        has_dimensions=has_dimensions,
        known_symbols=known_symbols,
    )
    return (
        {symbol: score for symbol in symbols},
        {symbol: label for symbol in symbols},
        note,
    )


def _join_events(labels: list[str]) -> str:
    unique: list[str] = []
    for label in labels:
        if label not in unique:
            unique.append(label)
    if len(unique) <= 2:
        return "、".join(unique)
    return "、".join(unique[:2]) + f" 等{len(unique)}个"


def resolve_dsa_code(code: str, known: set[str]) -> str | None:
    text = str(code or "").strip().upper()
    if not text:
        return None
    if text in known:
        return text
    match = re.search(r"(\d{6})", text)
    if match is None:
        return None
    digits = match.group(1)
    matches = [symbol for symbol in known if symbol == digits or symbol.startswith(digits + ".")]
    if len(matches) == 1:
        return matches[0]
    return None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def point_in_time_financials(frame: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """只保留公告日次日及之后已经生效的最新一期。"""
    if frame.is_empty() or "symbol" not in frame.columns or "announce_date" not in frame.columns:
        return frame.clear() if frame.width else frame
    work = (
        frame.with_columns(
            pl.col("announce_date").cast(pl.Utf8).str.slice(0, 10).str.to_date(strict=False).alias("_announce")
        )
        .filter(pl.col("symbol").is_not_null() & pl.col("_announce").is_not_null())
        .with_columns(pl.col("_announce").dt.offset_by("1d").alias("_effective"))
        .filter(pl.col("_effective") <= as_of)
        .sort(["symbol", "_effective"])
        .unique(subset=["symbol"], keep="last")
        .drop(["_announce", "_effective"])
    )
    return work


def with_profit_yoy(frame: pl.DataFrame) -> tuple[pl.DataFrame, str]:
    """扣非列优先。没有扣非列时用归母净利同比，并改表头，避免把两种口径标成同一个名字。"""
    for column in _DEDUCTED_YOY_COLUMNS:
        if column in frame.columns:
            return frame.with_columns(pl.col(column).alias("profit_yoy")), "扣非增速"
    if "net_income_yoy" in frame.columns:
        return frame.with_columns(pl.col("net_income_yoy").alias("profit_yoy")), "净利增速"
    return frame, "扣非增速"


def _require(columns: set[str], needed: list[str]) -> str | None:
    missing = [name for name in needed if name not in columns]
    if not missing:
        return None
    return "、".join(missing)


def screen_fundamental(frame: pl.DataFrame, preset_id: str) -> tuple[dict[str, float], str | None]:
    preset = next((item for item in FUNDAMENTAL_PRESETS if item["id"] == preset_id), None)
    if preset is None:
        return {}, f"未知基本面策略 {preset_id}"
    if frame.is_empty() or "symbol" not in frame.columns:
        return {}, f"{preset['name']}没有可用的行情或财务数据"
    columns = set(frame.columns)
    if preset_id == "fundamental_m1":
        missing = _require(columns, ["roe", "profit_yoy", "revenue_yoy", "debt_to_asset_ratio"])
        if missing:
            return {}, f"{preset['name']}缺少{missing}，本次无结果"
        expr = (
            (pl.col("roe") >= 15)
            & (pl.col("profit_yoy") >= 20)
            & (pl.col("revenue_yoy") >= 10)
            & (pl.col("debt_to_asset_ratio") <= 60)
        )
        if "pe" in columns:
            expr = expr & (pl.col("pe").is_null() | ((pl.col("pe") > 0) & (pl.col("pe") <= 60)))
    elif preset_id == "fundamental_m2":
        missing = _require(columns, ["roe", "profit_yoy", "debt_to_asset_ratio", "pe", "market_cap"])
        if missing:
            return {}, f"{preset['name']}缺少{missing}，本次无结果"
        expr = (
            (pl.col("market_cap") >= 200e8)
            & (pl.col("roe") >= 12)
            & (pl.col("profit_yoy") >= 0)
            & (pl.col("debt_to_asset_ratio") <= 55)
            & (pl.col("pe") > 0)
            & (pl.col("pe") <= 25)
        )
    else:
        missing = _require(columns, ["roe", "profit_yoy", "debt_to_asset_ratio", "operating_cash_to_revenue"])
        if missing:
            return {}, f"{preset['name']}缺少{missing}，本次无结果"
        expr = (
            (pl.col("operating_cash_to_revenue") >= 0.8)
            & (pl.col("roe") >= 8)
            & (pl.col("profit_yoy") >= 0)
            & (pl.col("debt_to_asset_ratio") <= 55)
        )
    picked = frame.filter(expr.fill_null(False)).select(
        ["symbol", "roe", "profit_yoy"] if "profit_yoy" in columns else ["symbol", "roe"]
    )
    scores: dict[str, float] = {}
    for row in picked.iter_rows(named=True):
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        roe = _num(row.get("roe")) or 0.0
        yoy = _num(row.get("profit_yoy")) or 0.0
        scores[symbol] = round(min(100.0, max(0.0, roe / 30 * 70 + min(max(yoy, 0.0), 50) / 50 * 30)), 1)
    return scores, None


def is_financial_industry(path: str) -> bool:
    text = str(path or "")
    return any(word in text for word in _FINANCIAL_KEYWORDS)


def is_st_name(name: str | None) -> bool:
    return bool(name) and _ST_RE.search(str(name)) is not None


def apply_filters(rows: list[dict], filters: FilterSpec) -> list[dict]:
    selected = {item.strip() for item in filters.industries if item and item.strip()}
    kept: list[dict] = []
    for row in rows:
        if selected:
            label = str(row.get("industry") or "")
            path = str(row.get("industry_path") or "")
            if label not in selected and not any(item in path for item in selected):
                continue
        cap = _num(row.get("market_cap"))
        if filters.market_cap_min is not None and (cap is None or cap / 1e8 < filters.market_cap_min):
            continue
        if filters.market_cap_max is not None and (cap is None or cap / 1e8 > filters.market_cap_max):
            continue
        pe = _num(row.get("pe"))
        if filters.pe_min is not None and (pe is None or pe < filters.pe_min):
            continue
        if filters.pe_max is not None and (pe is None or pe > filters.pe_max):
            continue
        if filters.exclude_st and is_st_name(row.get("name")):
            continue
        if filters.exclude_financial and is_financial_industry(str(row.get("industry_path") or row.get("industry") or "")):
            continue
        kept.append(row)
    if filters.max_per_industry is None:
        return kept
    limit = filters.max_per_industry
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in kept:
        grouped[str(row.get("industry") or "")].append(row)
    limited: list[dict] = []
    for group in grouped.values():
        group.sort(key=lambda item: (item.get("score") is None, -(item.get("score") or 0), item.get("symbol") or ""))
        limited.extend(group[:limit])
    return limited


def _snapshots_dir(data_dir: Path) -> Path:
    root = (data_dir / "picker").resolve()
    path = (root / "snapshots").resolve()
    if not path.is_relative_to(root):
        raise RuntimeError("snapshot path escaped data directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _snapshot_files(directory: Path) -> list[Path]:
    files = []
    for path in directory.glob("*.json"):
        if not _SNAPSHOT_NAME.match(path.name):
            continue
        resolved = path.resolve()
        if resolved.parent == directory.resolve():
            files.append(resolved)
    return sorted(files)


def read_previous_snapshot(data_dir: Path) -> dict | None:
    directory = _snapshots_dir(data_dir)
    files = _snapshot_files(directory)
    if not files:
        return None
    try:
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("picker snapshot unreadable: %s", files[-1].name)
        return None
    return payload if isinstance(payload, dict) else None


def write_snapshot(data_dir: Path, payload: dict, *, now: datetime) -> None:
    directory = _snapshots_dir(data_dir)
    stamp = now.astimezone(CN_TZ).strftime("%Y%m%dT%H%M%S")
    target = directory / f"{stamp}.json"
    suffix = 1
    while target.exists():
        target = directory / f"{stamp}-{suffix}.json"
        suffix += 1
    atomic_write_text(target, json.dumps(payload, ensure_ascii=False))
    files = _snapshot_files(directory)
    for old in files[:-_MAX_SNAPSHOTS]:
        try:
            old.unlink()
        except OSError:
            logger.warning("failed to prune picker snapshot %s", old.name)


def diff_against(current: set[str], previous: set[str] | None) -> tuple[dict[str, str], int, int]:
    if previous is None:
        return {symbol: "new" for symbol in current}, len(current), 0
    changes = {symbol: ("kept" if symbol in previous else "new") for symbol in current}
    removed = len(previous - current)
    added = len(current - previous)
    return changes, added, removed


def _strategy_score(row: dict) -> float | None:
    return _num(row.get("score"))


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _dsa_message(payload: dict, fallback: str) -> str:
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        text = str(detail.get("message") or detail.get("error") or fallback)
    elif isinstance(detail, str) and detail:
        text = detail
    else:
        text = str(payload.get("message") or fallback) if isinstance(payload, dict) else fallback
    return " ".join(text.split())[:80]


def _parse_dsa_strategies(payload: dict) -> list[dict]:
    raw = payload.get("strategies") if isinstance(payload, dict) else None
    if raw is None and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        raw = payload["data"].get("strategies")
    if not isinstance(raw, list):
        return []
    items = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("id") or "").strip()
        if not safe_source_id(sid):
            continue
        items.append({
            "id": sid,
            "name": str(row.get("title") or row.get("name") or sid),
            "description": str(row.get("description") or ""),
        })
    return items


def _extract_dsa_candidates(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("candidates"), list):
        return [row for row in result["candidates"] if isinstance(row, dict)]
    if isinstance(payload.get("candidates"), list):
        return [row for row in payload["candidates"] if isinstance(row, dict)]
    return []


def list_dsa_strategies(request: Callable[[str, str, dict | None], tuple[int, dict]] | None) -> tuple[list[dict], str | None]:
    if request is None:
        return [], "决策服务未连接"
    try:
        status, payload = request("GET", "screening/strategies", None)
    except Exception as exc:  # noqa: BLE001 — 上游异常转成可展示的短句
        logger.info("DSA strategy list unavailable: %s", exc)
        return [], "决策服务未连接"
    if status >= 400:
        return [], _dsa_message(payload, "决策服务未连接")
    return _parse_dsa_strategies(payload), None


def run_dsa_strategy(
    request: Callable[[str, str, dict | None], tuple[int, dict]],
    strategy_id: str,
    known_symbols: set[str],
    *,
    poll_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, float], str | None]:
    try:
        status, payload = request(
            "POST",
            "screening/screen/tasks",
            {"market": "cn", "strategy": strategy_id, "max_results": 30},
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("DSA screen submit failed: %s", exc)
        return {}, "决策服务未连接"
    if status >= 400:
        return {}, _dsa_message(payload, "DSA 选股提交失败")
    task_id = str(payload.get("task_id") or "")
    if not task_id or not safe_source_id(task_id):
        ready = _extract_dsa_candidates(payload)
        if ready:
            return _scores_from_dsa(ready, known_symbols)
        return {}, "DSA 选股没有返回任务"
    deadline = monotonic() + max(0.0, poll_seconds)
    body = payload
    while True:
        result_status = str(body.get("status") or "").lower()
        if result_status in {"completed", "succeeded", "success", "done"}:
            return _scores_from_dsa(_extract_dsa_candidates(body), known_symbols)
        if result_status in {"failed", "error", "cancelled", "canceled"}:
            return {}, _dsa_message(body, "DSA 选股失败")
        if monotonic() >= deadline:
            return {}, "DSA 选股仍在计算，本次未并入"
        sleep(0.4 if poll_seconds >= 1 else 0)
        try:
            status, body = request("GET", f"screening/screen/tasks/{task_id}", None)
        except Exception as exc:  # noqa: BLE001
            logger.info("DSA screen poll failed: %s", exc)
            return {}, "决策服务未连接"
        if status >= 400:
            return {}, _dsa_message(body, "DSA 选股失败")


def _scores_from_dsa(candidates: list[dict], known_symbols: set[str]) -> tuple[dict[str, float], str | None]:
    scores: dict[str, float] = {}
    skipped = 0
    for row in candidates:
        code = str(row.get("code") or row.get("symbol") or row.get("stock_code") or "")
        symbol = resolve_dsa_code(code, known_symbols)
        if symbol is None:
            skipped += 1
            continue
        score = _num(row.get("score"))
        if score is None and _num(row.get("rank")) is not None:
            score = None
        scores[symbol] = score if score is None or score <= 100 else round(min(score, 100.0), 1)
    warning = None
    if (candidates and not scores) or (skipped and not scores):
        warning = "DSA 结果无法对应本地代码，本次未并入"
    return scores, warning


def _item(sid: str, name: str, description: str = "") -> dict[str, str]:
    return {"id": sid, "name": name, "description": description}


def _load_hot_snapshot(deps: PickerDeps, now: datetime) -> tuple[dict, str | None]:
    try:
        raw = deps.load_top_events(now)
    except Exception:
        logger.exception("picker hot events load failed")
        return empty_hot_snapshot(now), "热门事件暂时不可用"
    if not isinstance(raw, dict) or not isinstance(raw.get("events"), list):
        return empty_hot_snapshot(now), "热门事件暂时不可用"
    return raw, None


def _event_concept_names(event: dict) -> list[str]:
    """事件里可以拿去查板块的名字, 按可信度排序。

    先取 concepts(上游已做过相关性筛选的概念), 再补 mapping 里的概念条目。
    上游 concepts 混进了栏目名, 所以调用方匹配时还要过一遍归一化。
    """
    names: list[str] = []
    for value in event.get("concepts") or []:
        text = str(value).strip()
        if text and text not in names:
            names.append(text)
    for entry in event.get("mapping") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("kind") or "") != "concept":
            continue
        text = str(entry.get("name") or "").strip()
        if text and text not in names:
            names.append(text)
    return names


def _mapping_breadth(selectable: int | None) -> str | None:
    """把可选股数折成三档语义, 供排序与前端展示共用。

    `selectable is None` 表示**还不知道** (维表没同步), 与 none(确实选不出)
    是两回事 —— 这时返回 None, 前端按「未知」处理, 不能当成「空炮」置灰。
    """
    if selectable is None:
        return None
    if selectable <= 0:
        return "none"
    if selectable > _BROAD_MAPPING_CEILING:
        return "broad"
    return "usable"


def _selectable_symbols(
    event: dict,
    dimensions: Dimensions,
    label_index: dict[str, set[str]],
) -> set[str]:
    """这个热门事件选中后会落到哪些个股上 —— 与 map_selected_hot_event 同一把尺。

    用途: ① 给选项暴露真实可选股数(而不是被截断的 mapped[:3]);
          ② 作为排序依据(可映射性 = 这条事件对选股到底有没有用)。

    直接复用 `resolve_event_symbols`, 不另写一套 —— 早前预览数与真实跑出来的
    股数对不上(预览没做 30 只截断、没过滤基金), 会系统性高估, 前端因此误导用户。

    `known_symbols=None` 仍与实跑有细微差别: 预览阶段没有行情表, 提及个股跳过
    代码校验(见 resolve_event_symbols 注释)。所以这里的数**是上限**, 不是精确值。
    """
    return resolve_event_symbols(event, dimensions, label_index, known_symbols=None)


def _selectable_detail(
    event: dict,
    dimensions: Dimensions,
    label_index: dict[str, set[str]],
) -> tuple[int, str | None, bool]:
    """可选股数 + 选不出的原因 + 是否真被上限截断。

    `resolve_event_symbols` 第三返回值是 note —— 它区分了「数据没就绪」「事件无映射」
    「代码对不上」三种本质不同的 0。前端要按原因给出不同文案, 不能一律「暂无对应个股」
    (那会把「重大政策新闻暂时映射不上」和「冷门事件真没股」画等号)。

    `capped` 单独返回而不是让调用方拿 `len >= 上限` 猜: 恰好 30 只时不该说「至少 30 只」。
    """
    symbols, capped = _event_symbols_and_capped(
        event, dimensions, label_index, known_symbols=None,
    )
    note = _mapping_note(
        event,
        symbols=symbols,
        concepts=_event_concept_names(event),
        has_dimensions=bool(dimensions.concepts or dimensions.industry_path),
        known_symbols=None,
    )
    return len(symbols), note, capped


def _event_pick_rank(event: dict, selectable: int | None, *, index_ready: bool) -> tuple:
    """热门事件在「**选股**列表」里的排序键 —— 与新闻榜单的排序口径不同。

    新闻榜单要的是「按重要性看世界」, 所以档位主导。选股列表要的是「点得动」,
    所以要的是**这条事件能不能选出股**。线上实测: 如果档位主导, 前 8 条里有
    7 条是「美联储讲话」「欧盟制裁」这类 A 股根本没有对应板块的新闻, 点进去
    一股都选不出来 —— 这是最伤体验的地方。

    因此这里改成**可映射性主导**:
      - usable(1.._BROAD_MAPPING_CEILING): 能选出有区分度的一篮子股, 最优先。
      - broad(> 上限): 命中的是「国企改革」这类半个市场的板块, 点进去等于没筛,
        排在 none 之前(好歹有股), 但不如 usable。
      - none(0): 选不出任何股, 排最后。
    usable 内部按 `_MAPPING_MIN_COMFORTABLE` 排 —— 只选出 1~2 只的事件覆盖不足,
    排在舒适区之后(但仍优于 broad/none)。上限不另设阈值: 800 以上已经归 broad,
    再划一个 400 只会制造「既非 broad 也不舒适」的模糊地带。
    broad 内部按可选数**升序**排列: 同样是宽泛, 1000 只比 1500 只稍好一点,
    不然整个 broad 段会退化成同分、只能靠名称兜底。

    档位降为「同组内」的次级键; 无映射的「重大」由 `_hot_group` 的保底席位兜住
    (见 _MAJOR_RESERVED_SLOTS), 不在这里硬顶。

    索引不可用时(离线/维表缺失)可映射性这维退化为常数, 排序回到纯档位+热度 ——
    不能因为算不出映射就退化成随机顺序。
    """
    order = {"重大": 0, "重要": 1, "一般": 2, "琐碎": 3}
    importance = str(event.get("importance") or "一般")
    if index_ready:
        if selectable <= 0:
            usefulness = (2, 0)
        elif selectable > _BROAD_MAPPING_CEILING:
            usefulness = (1, selectable)
        else:
            # 舒适区并列 0, 太窄的按「差多少只到下限」排, 越近越靠前。
            distance = max(0, _MAPPING_MIN_COMFORTABLE - selectable)
            usefulness = (0, distance)
        return (
            *usefulness,
            order.get(importance, 2),
            -int(event.get("mentions") or 0),
            -int(event.get("source_count") or 0),
            str(event.get("name") or ""),
        )
    return (
        0,
        0,
        order.get(importance, 2),
        -int(event.get("mentions") or 0),
        -int(event.get("source_count") or 0),
        str(event.get("name") or ""),
    )


def _hot_group(
    snapshot: dict,
    error: str | None,
    dimensions: Dimensions | None = None,
) -> dict:
    # 选股列表的排序 ≠ 新闻榜单的排序: 这里以「能不能选出股」为主, 档位为辅。
    # 但完全不看档位也不行 —— 全榜最重的新闻一条都不露脸会很怪。
    # 所以: 先按可映射性排序取满, 若其中「重大」档不足 _MAJOR_RESERVED_SLOTS 条,
    # 就用排名最高的无映射「重大」把它们换进来(而不是让空炮无限霸榜)。
    raw_events = [e for e in (snapshot.get("events") or []) if isinstance(e, dict) and str(e.get("key") or "").strip()]
    label_index = build_label_index(dimensions) if dimensions is not None else {}
    index_ready = bool(label_index)
    ranked: list[tuple[tuple, dict, int | None, str | None, bool]] = []
    for event in raw_events:
        if index_ready:
            selectable, note, capped = _selectable_detail(event, dimensions, label_index)
        else:
            # 行业/概念维表还没就绪: 可映射性**未知**, 不是 0。
            # 这里传 None 而不是 0 —— 用 0 会让前端把这条事件当成「空炮」拦掉勾选,
            # 但实跑 `resolve_event_symbols` 在没维表时仍会纳入资讯里提到的个股
            # (note 本身就写着「本次只纳入资讯里提到的个股」)。维表没同步 ≠ 选不出股,
            # 前者该让用户照常勾选、只在旁边说明, 后者才该拦。
            selectable, note, capped = None, "尚未同步同花顺行业/概念，本次只纳入资讯里提到的个股", False
        ranked.append(
            (
                _event_pick_rank(event, selectable, index_ready=index_ready),
                event,
                selectable,
                note,
                capped,
            )
        )
    ranked.sort(key=lambda row: row[0])

    # 保底席位: 若按可映射性取出的前 N 里「重大」档少于 _MAJOR_RESERVED_SLOTS 条,
    # 就从榜外把排名最前的「重大」补进来。
    #
    # 让位规则: **只砍榜内的「非重大」**, 且从排名末位的开始砍 —— 榜内已有的「重大」
    # 一律保留。早前的实现只按位置砍末尾(keep = picked[:-len(waiting)]), 不看末尾
    # 是不是重大, 于是会出现「榜内唯一的重大的排在末位 → 被砍掉换成榜外更差的重大」
    # 的反例: 保底席位数没变, 反而把条目换差了(评审指出)。
    if index_ready:
        picked = ranked[:_TOP_HOT_EVENTS]

        def _is_major(row: tuple) -> bool:
            return str(row[1].get("importance") or "") == "重大"

        majors_in = sum(1 for row in picked if _is_major(row))
        shortfall = _MAJOR_RESERVED_SLOTS - majors_in
        if shortfall > 0:
            picked_ids = {id(row) for row in picked}
            waiting = [
                row for row in ranked[_TOP_HOT_EVENTS:]
                if _is_major(row) and id(row) not in picked_ids
            ][:shortfall]
            if waiting:
                # 从「非重大」里挑排名最末的让位, 榜内重大全部保住。
                demotable = [row for row in picked if not _is_major(row)]
                if len(demotable) >= len(waiting):
                    drop_ids = {id(row) for row in demotable[-len(waiting):]}
                    picked = [row for row in picked if id(row) not in drop_ids]
                else:
                    # 榜内几乎全是重大(罕见), 只能砍末尾的非重大部分, 不足则放弃补位。
                    waiting = waiting[: len(demotable)]
                    drop_ids = {id(row) for row in demotable}
                    picked = [row for row in picked if id(row) not in drop_ids]
                picked = sorted(picked + waiting, key=lambda row: row[0])[:_TOP_HOT_EVENTS]
    else:
        picked = ranked[:_TOP_HOT_EVENTS]
    selected = picked

    items = []
    for _rank, event, selectable, note, capped in selected:
        if len(items) >= _TOP_HOT_EVENTS:
            break
        key = str(event.get("key") or "").strip()
        try:
            mentions = int(event.get("mentions") or 0)
            source_count = int(event.get("source_count") or 0)
        except (TypeError, ValueError):
            continue
        name = str(event.get("name") or key)
        first_seen = event.get("first_seen")
        if not isinstance(first_seen, str):
            first_seen = event.get("updated_at") if isinstance(event.get("updated_at"), str) else None
        concepts = [
            str(concept).strip()
            for concept in (event.get("concepts") or [])
            if str(concept).strip()
        ][:4]
        headline = event.get("headline") if isinstance(event.get("headline"), str) else None
        mapped = []
        for stock in event.get("mentioned_stocks") or []:
            if not isinstance(stock, dict):
                continue
            label = str(stock.get("name") or stock.get("key") or "").strip()
            if label and label not in mapped:
                mapped.append(label)
            if len(mapped) >= 3:
                break
        items.append({
            "id": hot_event_id(key),
            "name": name,
            "description": f"提及 {mentions} · 来源 {source_count}",
            "mentions": mentions,
            "source_count": source_count,
            "red_count": int(event.get("red_count") or 0),
            "updated_at": first_seen,
            "first_seen": first_seen,
            "concepts": concepts,
            "headline": headline,
            "subtitle": str(event.get("subtitle") or ""),
            "category": str(event.get("category") or ""),
            "direction": str(event.get("direction") or ""),
            "importance": str(event.get("importance") or ""),
            "confirmation": _confirmation_brief(event.get("confirmation")),
            "mapped_stocks": mapped,
            # 真实可选股数, 与实跑同一把尺(走 resolve_event_symbols: 已按
            # _CONCEPT_CONSTITUENT_CAP 截断、已过滤基金)。mapped_stocks 只有前 3 个,
            # 前端无法据此还原总数, 所以单独透出这个数。
            # **None 表示未知**(维表未同步), 与 0(确实选不出股)语义不同:
            # 前端据此决定是「照常勾选 + 说明」还是「置灰拦下」。
            "selectable_count": selectable,
            "concept_total": len(event.get("concepts") or []),
            # 映射广度: usable=有区分度(1..上限) / broad=命中过宽板块 / none=选不出股 /
            # None=未知(维表未同步)。前端据此决定这条能不能点、要不要提示「范围较大」。
            "mapping_breadth": _mapping_breadth(selectable),
            # 选不出股时的**原因**(区分「维表没同步」「事件无映射」「个股代码对不上」)。
            # 前端按原因给不同文案, 不能一律「暂无对应个股」——那会把「重大政策新闻
            # 暂时映射不上」和「冷门事件真没股」混为一谈, 藏掉重要信息。
            "selectable_note": note,
            # 是否被 `_CONCEPT_CONSTITUENT_CAP` 截断。截断了就不能说「约 N 只」,
            # 要说「至少 N 只」——N 是上限不是全集。**由计算直接给出**, 不拿
            # `count >= cap` 猜: 恰好 30 只时不是截断, 不该说「至少」。
            "selectable_capped": capped,
        })
    return {
        "id": "hot_events",
        "label": "热门事件",
        "live": True,
        "updated_at": snapshot.get("updated_at"),
        "as_of": snapshot.get("as_of"),
        "trading_day": snapshot.get("trading_day"),
        "fallback": bool(snapshot.get("fallback")),
        "hint": snapshot.get("hint") or None,
        "status": snapshot.get("status"),
        "computed_at": snapshot.get("computed_at"),
        "error": error,
        "items": items,
    }


def _confirmation_brief(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    strength = str(raw.get("strength") or "")
    persistence = str(raw.get("persistence") or "")
    label = str(raw.get("label") or "")
    if not strength and not label:
        return None
    phase = str(raw.get("phase") or "")
    horizon = str(raw.get("horizon") or "")
    return {
        "strength": strength,
        "persistence": persistence,
        "label": label,
        "live": bool(raw.get("live")),
        "phase": phase if phase in {"auction", "intraday", "session"} else "",
        "lagged": bool(raw.get("lagged")),
        "pending": bool(raw.get("pending")) or label == "待开盘验证",
        "horizon": horizon if horizon in {"主线", "一日游"} else "",
    }


def _hot_names(snapshot: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    for event in snapshot.get("events") or []:
        if not isinstance(event, dict):
            continue
        key = str(event.get("key") or "").strip()
        if not key:
            continue
        names[hot_event_id(key)] = str(event.get("name") or key)
    return names


def _event_index(snapshot: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for event in snapshot.get("events") or []:
        if not isinstance(event, dict):
            continue
        key = str(event.get("key") or "").strip()
        if key:
            index[hot_event_id(key)] = event
    return index


def build_catalog(deps: PickerDeps, snapshot: dict | None = None, hot_error: str | None = None) -> dict:
    try:
        metas = deps.list_strategies() or []
    except Exception:
        logger.exception("picker strategy catalog failed")
        metas = []
    technical, factor = classify_strategies(metas)
    dsa_items, dsa_error = list_dsa_strategies(deps.dsa_request)
    try:
        dimensions = deps.load_dimensions()
    except Exception:
        logger.exception("picker dimension catalog failed")
        dimensions = Dimensions()
    industries = sorted({label for label in dimensions.industry_label.values() if label})
    now = deps.now or cn_now()
    if snapshot is None:
        snapshot, hot_error = _load_hot_snapshot(deps, now)
    return {
        "groups": [
            {
                "id": "fundamental",
                "label": "基本面",
                "items": [
                    _item(item["id"], item["name"], item["description"])
                    for item in FUNDAMENTAL_PRESETS
                ],
            },
            _hot_group(snapshot, hot_error, dimensions),
            {"id": "technical", "label": "技术面/短线", "items": technical},
            {
                "id": "factor",
                "label": "因子打分",
                "empty_hint": "还没有因子生成的策略",
                "empty_href": "/factors",
                "items": factor,
            },
            {
                "id": "dsa",
                "label": "DSA 选股",
                "beta": True,
                "available": dsa_error is None,
                "error": dsa_error,
                "items": dsa_items,
            },
        ],
        "industries": industries,
    }


def _names_from_catalog(catalog: dict) -> dict[tuple[str, str], str]:
    names: dict[tuple[str, str], str] = {}
    for group in catalog.get("groups") or []:
        for item in group.get("items") or []:
            names[(group["id"], item["id"])] = item["name"]
    return names


def _universe_frame(
    market_rows: list[dict],
    financial: pl.DataFrame,
) -> pl.DataFrame:
    if not market_rows:
        market = pl.DataFrame({"symbol": [], "name": [], "close": [], "market_cap": []})
    else:
        market = pl.DataFrame(market_rows)
    if "market_cap" not in market.columns and {"close", "total_shares"} <= set(market.columns):
        market = market.with_columns((pl.col("close") * pl.col("total_shares")).alias("market_cap"))
    if financial.is_empty() or "symbol" not in financial.columns:
        frame = market
    else:
        overlap = [column for column in financial.columns if column in market.columns and column != "symbol"]
        right = financial.drop(overlap) if overlap else financial
        frame = market.join(right, on="symbol", how="left")
    columns = set(frame.columns)
    if "pe" not in columns and "pe_ttm" in columns:
        frame = frame.with_columns(pl.col("pe_ttm").alias("pe"))
        columns.add("pe")
    eps_col = "eps" if "eps" in columns else ("basic_eps" if "basic_eps" in columns else None)
    if "pe" not in frame.columns and eps_col and "close" in frame.columns:
        frame = frame.with_columns(
            pl.when(pl.col(eps_col) > 0).then(pl.col("close") / pl.col(eps_col)).otherwise(None).alias("pe")
        )
    return frame


def _market_caps(by_symbol: dict[str, dict]) -> dict[str, float]:
    caps: dict[str, float] = {}
    for symbol, row in by_symbol.items():
        value = row.get("market_cap")
        if value is None:
            continue
        try:
            caps[symbol] = float(value)
        except (TypeError, ValueError):
            continue
    return caps


def _lookup(frame: pl.DataFrame) -> dict[str, dict]:
    if frame.is_empty() or "symbol" not in frame.columns:
        return {}
    rows = {}
    for row in frame.iter_rows(named=True):
        symbol = str(row.get("symbol") or "")
        if symbol:
            rows[symbol] = row
    return rows


def run_picker(sources: list[SourceSpec], combine: str, filters: FilterSpec, deps: PickerDeps) -> dict:
    if combine not in {"and", "or"}:
        raise ValueError("combine 只能是 and 或 or")
    now = deps.now or cn_now()
    wants_hot = any(source.type == "hot_events" for source in sources)
    if wants_hot:
        hot_snapshot, hot_error = _load_hot_snapshot(deps, now)
    else:
        hot_snapshot, hot_error = empty_hot_snapshot(now), None
    catalog = build_catalog(deps, snapshot=hot_snapshot, hot_error=hot_error)
    names = _names_from_catalog(catalog)
    for event_id, event_name in _hot_names(hot_snapshot).items():
        names[("hot_events", event_id)] = event_name
    hot_events = _event_index(hot_snapshot)
    warnings: list[str] = []
    as_of, market_rows = deps.load_market()
    financial = pl.DataFrame()
    profit_label = "扣非增速"
    if as_of is not None:
        financial, profit_label = deps.load_financial(as_of)
    universe = _universe_frame(market_rows, financial)
    by_symbol = _lookup(universe)
    known = set(by_symbol)
    dimensions = deps.load_dimensions()
    for symbol, label in dimensions.industry_label.items():
        row = by_symbol.get(symbol)
        if row is not None:
            row["industry"] = label
            row["industry_path"] = dimensions.industry_path.get(symbol, "")

    participating: list[set[str]] = []
    hit_names: dict[str, list[dict[str, str]]] = defaultdict(list)
    hit_scores: dict[str, list[float]] = defaultdict(list)
    hit_events: dict[str, str] = {}

    def _take(source: SourceSpec, scores: dict[str, float | None], events: dict[str, str] | None, error: str | None) -> None:
        if error and not scores:
            warnings.append(error)
            return
        if error:
            warnings.append(error)
        participating.append(set(scores))
        source_name = names.get((source.type, source.id), source.id)
        for symbol, score in scores.items():
            hit_names[symbol].append({"id": source.id, "name": source_name})
            if score is not None:
                hit_scores[symbol].append(float(score))
            if events and events.get(symbol) and symbol not in hit_events:
                hit_events[symbol] = events[symbol]

    strategy_ids = [
        source.id for source in sources
        if source.type in {"technical", "factor"} and (source.type, source.id) in names
    ]
    strategy_results: dict[str, tuple[list[dict], str | None]] = {}
    if strategy_ids:
        if deps.limiter is not None:
            with deps.limiter.slot("normal", timeout=15):
                strategy_results = deps.run_strategies(strategy_ids)
        else:
            strategy_results = deps.run_strategies(strategy_ids)

    dsa_cache: tuple[list[dict], str | None] | None = None
    hot_index = build_label_index(dimensions) if wants_hot else {}
    hot_blocked = False
    for source in sources:
        if source.type == "hot_events" and hot_error:
            if not hot_blocked:
                warnings.append(hot_error)
                hot_blocked = True
            continue
        if (source.type, source.id) not in names:
            warnings.append(f"未识别的来源 {source.id}")
            continue
        if source.type == "fundamental":
            scores, error = screen_fundamental(universe, source.id)
            _take(source, scores, None, error)
        elif source.type == "hot_events":
            event = hot_events.get(source.id)
            if event is None:
                warnings.append(f"未识别的来源 {source.id}")
                continue
            scores, events, note = map_selected_hot_event(
                event, dimensions, known, hot_index, _market_caps(by_symbol),
            )
            if not scores and note in {
                "尚未同步同花顺行业/概念，无法映射成分股",
                "热门个股无法对应本地代码",
            }:
                if note not in warnings:
                    _take(source, {}, None, note)
                continue
            if note and note not in warnings:
                warnings.append(note)
            _take(source, scores, events, None)
        elif source.type in {"technical", "factor"}:
            rows, error = strategy_results.get(source.id, ([], "策略没有返回结果"))
            scores = {}
            for row in rows:
                symbol = str(row.get("symbol") or "")
                if symbol:
                    scores[symbol] = _strategy_score(row)
            _take(source, scores, None, error)
        elif source.type == "dsa":
            if deps.dsa_request is None:
                _take(source, {}, None, "决策服务未连接")
                continue
            if dsa_cache is None:
                dsa_cache = list_dsa_strategies(deps.dsa_request)
            dsa_items, dsa_error = dsa_cache
            allowed = {item["id"] for item in dsa_items}
            if dsa_error or source.id not in allowed:
                _take(source, {}, None, dsa_error or f"DSA 没有策略 {source.id}")
                continue
            scores, error = run_dsa_strategy(
                deps.dsa_request,
                source.id,
                known,
                poll_seconds=deps.poll_seconds,
            )
            _take(source, scores, None, error)

    chosen = combine_symbol_sets(participating, combine)
    rows: list[dict] = []
    for symbol in chosen:
        base = by_symbol.get(symbol, {})
        rows.append({
            "symbol": symbol,
            "name": base.get("name") or "",
            "industry": base.get("industry") or dimensions.industry_label.get(symbol) or "",
            "industry_path": base.get("industry_path") or dimensions.industry_path.get(symbol) or "",
            "score": _average(hit_scores.get(symbol, [])),
            "strategies": hit_names.get(symbol, []),
            "event": hit_events.get(symbol) or "",
            "roe": _num(base.get("roe")),
            "profit_yoy": _num(base.get("profit_yoy")),
            "pe": _num(base.get("pe")),
            "market_cap": _num(base.get("market_cap")),
        })
    rows = apply_filters(rows, filters)
    rows.sort(key=lambda item: (item.get("score") is None, -(item.get("score") or 0), item["symbol"]))
    current_symbols = {row["symbol"] for row in rows}
    previous = read_previous_snapshot(deps.data_dir)
    previous_symbols = None
    previous_as_of = None
    if isinstance(previous, dict) and isinstance(previous.get("symbols"), list):
        previous_symbols = {str(symbol) for symbol in previous["symbols"]}
        previous_as_of = previous.get("as_of")
    changes, added, removed = diff_against(current_symbols, previous_symbols)
    for row in rows:
        row["change"] = changes.get(row["symbol"], "new")
        row.pop("industry_path", None)
    write_snapshot(
        deps.data_dir,
        {
            "as_of": as_of.isoformat() if as_of else None,
            "created_at": now.astimezone(CN_TZ).isoformat(timespec="seconds"),
            "combine": combine,
            "symbols": sorted(current_symbols),
        },
        now=now,
    )
    shown = rows[:_MAX_ROWS]
    if len(rows) > _MAX_ROWS:
        warnings.append(f"结果超过 {_MAX_ROWS} 只，表格只显示前 {_MAX_ROWS} 只")
    hot_updated = hot_snapshot.get("updated_at") if wants_hot and not hot_error else None
    hot_hint = hot_snapshot.get("hint") if wants_hot and not hot_error else None
    return {
        "rows": shown,
        "summary": {
            "total": len(rows),
            "combine": combine,
            "combine_label": "交集" if combine == "and" else "并集",
            "as_of": as_of.isoformat() if as_of else None,
            "previous_as_of": previous_as_of,
            "added": added,
            "removed": removed,
            "first_snapshot": previous_symbols is None,
            "profit_yoy_label": profit_label,
            "hot_updated_at": hot_updated,
            "hot_hint": hot_hint,
            "warnings": warnings,
        },
    }


def make_strategy_runner(repo, engine):
    """复用选股引擎的单策略执行。多只策略共享一次上下文，避免重复扫全市场。"""

    def run(strategy_ids: list[str]) -> dict[str, tuple[list[dict], str | None]]:
        if engine is None:
            return {sid: ([], "策略引擎未初始化") for sid in strategy_ids}
        from dataclasses import replace

        from app.services.screener import ScreenerService
        from app.strategy import config as strategy_config

        svc = ScreenerService(repo)
        as_of = svc.latest_date()
        if not as_of:
            return {sid: ([], "无可用行情日期") for sid in strategy_ids}
        data_dir = repo.store.data_dir
        runnable: list[str] = []
        results: dict[str, tuple[list[dict], str | None]] = {}
        params_map: dict[str, dict] = {}
        overrides_map: dict[str, dict] = {}
        for sid in strategy_ids:
            if not safe_source_id(sid) or not engine.has(sid):
                results[sid] = ([], f"策略 {sid} 不存在")
                continue
            meta = engine.get(sid).meta
            if meta.get("research_only"):
                results[sid] = ([], f"策略 {sid} 不可用")
                continue
            overrides = strategy_config.load_override(data_dir, sid) or {}
            overrides_map[sid] = overrides
            params_map[sid] = dict(overrides.get("params") or {})
            runnable.append(sid)
        if not runnable:
            return results
        context = svc.build_strategy_context(
            engine,
            as_of,
            runnable,
            timeframe="1d",
            params_map=params_map,
            overrides_map=overrides_map,
        )
        if getattr(context, "market", None) is None:
            build_matrix = getattr(engine, "build_shared_matrix", None)
            if callable(build_matrix):
                try:
                    matrix = build_matrix(
                        context,
                        [(sid, engine.get(sid)) for sid in runnable],
                        params_map,
                        overrides_map,
                    )
                except Exception:
                    logger.exception("picker shared matrix skipped")
                    matrix = None
                if matrix is not None:
                    context = replace(context, market=matrix)
        for sid in runnable:
            try:
                result = engine.run(
                    sid,
                    context,
                    params=params_map[sid],
                    overrides=overrides_map[sid] or None,
                )
                results[sid] = (list(result.rows), None)
            except Exception:
                logger.exception("picker strategy %s failed", sid)
                results[sid] = ([], f"策略 {sid} 运行失败")
        return results

    return run


def load_market_from_repo(repo) -> tuple[date | None, list[dict]]:
    from app.services.screener import ScreenerService

    svc = ScreenerService(repo)
    as_of = svc.latest_date()
    if not as_of:
        return None, []
    frame = svc._load_enriched_for_date(as_of)
    if frame.is_empty() or "symbol" not in frame.columns:
        return as_of, []
    columns = ["symbol"]
    for name in ("name", "close", "total_shares"):
        if name in frame.columns:
            columns.append(name)
    rows = frame.select(columns).unique(subset=["symbol"], keep="last").to_dicts()
    for row in rows:
        close = _num(row.get("close"))
        shares = _num(row.get("total_shares"))
        row["market_cap"] = close * shares if close is not None and shares is not None else None
    return as_of, rows


def load_financial_from_dir(data_dir: Path, as_of: date) -> tuple[pl.DataFrame, str]:
    path = data_dir / "financials" / "metrics" / "part.parquet"
    if not path.is_file():
        return pl.DataFrame(), "扣非增速"
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        logger.warning("picker financial snapshot unreadable", exc_info=True)
        return pl.DataFrame(), "扣非增速"
    keep = [column for column in frame.columns if column in _FINANCIAL_COLUMNS]
    if "symbol" not in keep or "announce_date" not in keep:
        return pl.DataFrame(), "扣非增速"
    latest = point_in_time_financials(frame.select(keep), as_of)
    return with_profit_yoy(latest)


_DIMENSION_CACHE: dict[tuple, dict[str, list[str] | str]] = {}


def _read_dimension_table(data_dir: Path, config_id: str, column: str, splitter: str) -> dict[str, list[str] | str]:
    path = data_dir / "ext_data" / config_id / "part.parquet"
    if not path.is_file():
        return {}
    try:
        stat = path.stat()
    except OSError:
        return {}
    cache_key = (str(path), column, splitter, stat.st_mtime_ns, stat.st_size)
    cached = _DIMENSION_CACHE.get(cache_key)
    if cached is not None:
        return {
            symbol: (list(labels) if isinstance(labels, list) else labels)
            for symbol, labels in cached.items()
        }
    try:
        frame = pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        logger.warning("picker ext data unreadable: %s", config_id, exc_info=True)
        return {}
    symbol_col = "symbol" if "symbol" in frame.columns else ("股票代码" if "股票代码" in frame.columns else None)
    if symbol_col is None or column not in frame.columns:
        return {}
    out: dict[str, list[str] | str] = {}
    for row in frame.select([symbol_col, column]).iter_rows(named=True):
        symbol = str(row.get(symbol_col) or "").strip()
        raw = str(row.get(column) or "").strip()
        if not symbol or not raw:
            continue
        if splitter:
            out[symbol] = [part.strip() for part in raw.split(splitter) if part.strip()]
        else:
            out[symbol] = raw
    if len(_DIMENSION_CACHE) >= 8:
        _DIMENSION_CACHE.pop(next(iter(_DIMENSION_CACHE)))
    _DIMENSION_CACHE[cache_key] = out
    return {
        symbol: (list(labels) if isinstance(labels, list) else labels)
        for symbol, labels in out.items()
    }


def load_dimensions_from_dir(data_dir: Path) -> Dimensions:
    concepts = _read_dimension_table(data_dir, "ext_gn_ths", "所属概念", ";")
    paths = _read_dimension_table(data_dir, "ext_hy_ths", "所属同花顺行业", "")
    labels = {symbol: industry_label(str(path)) for symbol, path in paths.items()}
    return Dimensions(
        industry_label=labels,
        industry_path={symbol: str(path) for symbol, path in paths.items()},
        concepts={symbol: list(labels_) for symbol, labels_ in concepts.items()},
    )


def dsa_forward_request(method: str, path: str, body: dict | None) -> tuple[int, dict]:
    """走现有 DSA 代理。清单和轮询用短超时，避免选股页被上游拖住。"""
    from app.custom.dsa.proxy import UpstreamError, forward

    raw = json.dumps(body).encode("utf-8") if body is not None else None
    # 提交选股任务本身应很快返回 task_id；真正计算在 DSA 后台，由轮询预算截断。
    timeout = 20.0 if method.upper() == "POST" else 8.0
    try:
        status, payload, _media, _headers = forward(
            method,
            path,
            body=raw,
            content_type="application/json" if raw else None,
            timeout=timeout,
        )
    except UpstreamError:
        raise
    if not payload:
        return status, {}
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, {}
    return status, parsed if isinstance(parsed, dict) else {}


def sync_dsa_watchlist(
    symbols: list[str],
    request: Callable[[str, str, dict | None], tuple[int, dict]] | None,
) -> dict:
    cleaned = [str(symbol).strip() for symbol in symbols if str(symbol).strip()][:50]
    if request is None:
        return {"ok": False, "synced": 0, "failed": cleaned, "message": "决策服务未连接"}
    synced = 0
    failed: list[str] = []
    for index, symbol in enumerate(cleaned):
        try:
            status, payload = request("POST", "stocks/watchlist/add", {"stock_code": symbol})
        except Exception:  # noqa: BLE001
            failed.extend(cleaned[index:])
            return {"ok": False, "synced": synced, "failed": failed, "message": "决策服务未连接"}
        if status >= 400 or (isinstance(payload, dict) and payload.get("detail")):
            failed.append(symbol)
            continue
        synced += 1
    message = f"已同步 {synced} 只到 DSA 自选"
    if failed:
        message += f"，{len(failed)} 只失败"
    return {"ok": not failed, "synced": synced, "failed": failed, "message": message}


def load_top_events_from_news(now: datetime) -> dict:
    """选股页只读最近一次快照。now 仍留在依赖签名里，请求里不再按这个时点聚类。"""
    from app.news.hot_snapshot import read_served_snapshot

    del now
    return read_served_snapshot()


def deps_from_app(request, *, limiter=None, dsa_request=None, poll_seconds: float = 12.0) -> PickerDeps:
    repo = request.app.state.repo
    engine = getattr(request.app.state, "strategy_engine", None)
    data_dir = repo.store.data_dir

    def list_strategies() -> list[dict]:
        if engine is None:
            return []
        return engine.list_strategies()

    return PickerDeps(
        data_dir=data_dir,
        list_strategies=list_strategies,
        run_strategies=make_strategy_runner(repo, engine),
        load_market=lambda: load_market_from_repo(repo),
        load_financial=lambda as_of: load_financial_from_dir(data_dir, as_of),
        load_dimensions=lambda: load_dimensions_from_dir(data_dir),
        dsa_request=dsa_forward_request if dsa_request is None else dsa_request,
        load_top_events=load_top_events_from_news,
        poll_seconds=poll_seconds,
        limiter=limiter,
    )
