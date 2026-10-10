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


def match_sector_symbols(name: str, key: str, index: dict[str, set[str]]) -> set[str]:
    """先精确匹配板块名，没有命中时才做有限的包含匹配。"""
    for raw in (name, key):
        exact = index.get(_norm_label(raw))
        if exact:
            return set(exact)
    for raw in (name, key):
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


def _hot_attr(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def hot_event_label(name: str, source_count: int) -> str:
    text = str(name or "").strip() or "事件"
    if len(text) > 20:
        text = text[:20]
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
    concepts = [str(item).strip() for item in (event.get("concepts") or []) if str(item).strip()]
    has_dimensions = bool(dimensions.concepts or dimensions.industry_path)
    label_index = index if index is not None else build_label_index(dimensions)
    constituents: set[str] = set()
    if has_dimensions:
        for concept in concepts:
            constituents |= match_sector_symbols(concept, concept, label_index)
    resolved: set[str] = set()
    unresolved = 0
    mention_rank: dict[str, int] = {}
    mentioned = event.get("mentioned_stocks")
    if not isinstance(mentioned, list):
        mentioned = event.get("frequent_stocks") or []
    for stock in mentioned:
        if not isinstance(stock, dict):
            continue
        key = str(stock.get("key") or "")
        name = str(stock.get("name") or "")
        if _mentioned_is_fund(key, name):
            continue
        try:
            mention_rank[key] = int(stock.get("mentions") or 0)
        except (TypeError, ValueError):
            mention_rank[key] = 0
        symbol = resolve_mentioned_stock(key, known_symbols)
        if symbol is None:
            unresolved += 1
            continue
        resolved.add(symbol)
    caps = market_caps or {}
    extra = sorted(
        constituents - resolved,
        key=lambda symbol: (-mention_rank.get(symbol, 0), -(caps.get(symbol) or 0.0), symbol),
    )
    symbols = {
        symbol for symbol in (resolved | set(extra[:_CONCEPT_CONSTITUENT_CAP]))
        if not _mentioned_is_fund(symbol)
    }
    note = None
    if concepts and not has_dimensions and resolved:
        note = "尚未同步同花顺行业/概念，本次只纳入资讯里提到的个股"
    elif concepts and not has_dimensions and not resolved:
        note = "尚未同步同花顺行业/概念，无法映射成分股"
    elif not symbols:
        note = f"「{name or key}」没有匹配到成分股"
    elif unresolved and not resolved:
        note = "热门个股无法对应本地代码"
    elif unresolved:
        note = "部分热门个股无法对应本地代码"
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


def _hot_group(snapshot: dict, error: str | None) -> dict:
    items = []
    for event in snapshot.get("events") or []:
        if len(items) >= _TOP_HOT_EVENTS:
            break
        if not isinstance(event, dict):
            continue
        key = str(event.get("key") or "").strip()
        if not key:
            continue
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
            "updated_at": first_seen,
            "first_seen": first_seen,
            "concepts": concepts,
            "headline": headline,
            "category": str(event.get("category") or ""),
            "direction": str(event.get("direction") or ""),
            "importance": str(event.get("importance") or ""),
            "confirmation": _confirmation_brief(event.get("confirmation")),
            "mapped_stocks": mapped,
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
    return {
        "strength": strength,
        "persistence": persistence,
        "label": label,
        "live": bool(raw.get("live")),
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
            _hot_group(snapshot, hot_error),
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
    except Exception:
        logger.warning("picker financial snapshot unreadable", exc_info=True)
        return pl.DataFrame(), "扣非增速"
    keep = [column for column in frame.columns if column in _FINANCIAL_COLUMNS]
    if "symbol" not in keep or "announce_date" not in keep:
        return pl.DataFrame(), "扣非增速"
    latest = point_in_time_financials(frame.select(keep), as_of)
    return with_profit_yoy(latest)


def _read_dimension_table(data_dir: Path, config_id: str, column: str, splitter: str) -> dict[str, list[str] | str]:
    path = data_dir / "ext_data" / config_id / "part.parquet"
    if not path.is_file():
        return {}
    try:
        frame = pl.read_parquet(path)
    except Exception:
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
    return out


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
    from app.news.service import top_hot_events

    return top_hot_events(now)


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
