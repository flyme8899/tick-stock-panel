"""热门事件没有点名个股时，按「事件 → 申万行业 → 个股」补候选个股。

只读仓库内的静态数据（``app/news/data/``）和本地日线 / 证券信息，不联网。

- 成员资格：申万行业（每只股票只属一个三级行业）。不用概念：概念「沾边就算」，
  选出来的一半不是主营相关。
- 板块识别（置信度从高到低）：三级行业产品名 / 别名 / 产业词（光伏、锂电等对应一组
  三级行业）0.90 → 一级行业名 0.70 → 主题词典（宏观、海外、地缘、产业主题 → 一级行业）0.40。
- 排序：宏观 / 海外 / 地缘事件按总市值（系统性影响先落在权重股，避免按换手选出
  小城商行），其余按近 20 个交易日日均成交额。每个事件最多 ``MAX_SECTOR_STOCKS`` 只。
- 补出来的个股单独放在 ``sector_stocks``，不写进 ``mentioned_stocks``：不进打分、
  不进推送，只给页面和研究区分「点名」与「板块推导」。
- 本地行情读不到时不补（不给按代码顺序乱排的结果）。
- 外国机构名里的行业词不算（「古巴国际商业银行」「法国农业信贷银行」不映射到 A 股银行）：
  国家 / 地区前缀 + 机构后缀，或已知外企名，先从文本里抹掉再匹配三级行业和一级行业名。
- 宏观 / 主题词指向国防军工时，正文里还得有军事 / 国防词（军工、导弹、军演等）。
  只有「制裁」「俄罗斯」这类地缘词不映射到军工。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_SECTOR_STOCKS = 5
LIQUIDITY_DAYS = 20
SECTOR_SOURCE = "sector"
CONF_INDUSTRY3 = 0.90
CONF_INDUSTRY1 = 0.70
CONF_THEME = 0.40
# 这些事件分类的传导是系统性的，按市值排序。
MACRO_CATEGORIES = frozenset({"海外市场/央行", "地缘政治", "国内政策/宏观"})

_DATA_DIR = Path(__file__).resolve().parent / "data"
_ranking_lock = threading.Lock()
_ranking_cache: dict[str, object] = {"key": None, "value": None}


@lru_cache(maxsize=1)
def _static_tables() -> dict:
    sw = json.loads((_DATA_DIR / "sw_industry.json").read_text(encoding="utf-8"))
    kw = json.loads((_DATA_DIR / "sector_keywords.json").read_text(encoding="utf-8"))
    members = {code: tuple(items) for code, items in sw["members"].items()}
    l1_members: dict[str, list[str]] = {}
    for code, items in members.items():
        name = sw["l1"].get(code[:2])
        if name:
            l1_members.setdefault(name, []).extend(items)
    industry_kw = sorted(
        ((word, tuple(codes)) for word, codes in kw["industry"].items() if codes),
        key=lambda pair: -len(pair[0]),
    )
    blocked = set(kw.get("l1_block") or [])
    l1_kw = sorted((name for name in l1_members if name not in blocked), key=len, reverse=True)
    return {
        "industries": sw["industries"],
        "l1": sw["l1"],
        "members": members,
        "l1_members": {name: tuple(items) for name, items in l1_members.items()},
        "names": sw["names"],
        "industry_kw": industry_kw,
        "l1_kw": l1_kw,
        "macro": {word: tuple(targets) for word, targets in kw["macro"].items()},
        "theme": {word: tuple(targets) for word, targets in kw["theme"].items()},
        "foreign_re": _foreign_entity_re(kw.get("foreign_entity") or {}),
        "military_targets": frozenset((kw.get("military_gate") or {}).get("targets") or ()),
        "military_words": tuple((kw.get("military_gate") or {}).get("require") or ()),
    }


# 外国机构名中间不该出现的字：标点和「将 / 对 / 与」这类连接词，出现就说明不是一个名字。
_ENTITY_BODY = r"[^\s，。、；：:,.!?！？“”\"'（）()《》·\-—对将与和的及在把向从为被让给称或宣表说拟已再要会]"


def _foreign_entity_re(table: dict) -> re.Pattern | None:
    countries = sorted(table.get("countries") or (), key=len, reverse=True)
    suffixes = sorted(table.get("suffixes") or (), key=len, reverse=True)
    names = sorted(table.get("names") or (), key=len, reverse=True)
    parts: list[str] = []
    if countries and suffixes:
        parts.append(
            "(?:" + "|".join(map(re.escape, countries)) + ")"
            + _ENTITY_BODY + "{0,4}?"
            + "(?:" + "|".join(map(re.escape, suffixes)) + ")"
        )
    if names:
        parts.append("(?:" + "|".join(map(re.escape, names)) + ")")
    return re.compile("|".join(parts)) if parts else None


def _mask_foreign_entities(text: str) -> str:
    """把外国机构名换成空格，里面的行业词（银行、钢铁、汽车……）就不会被匹配。"""
    pattern = _static_tables()["foreign_re"]
    if pattern is None:
        return text
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def _military_ok(text: str) -> bool:
    return any(word in text for word in _static_tables()["military_words"])


def find_sectors(text: str) -> list[dict]:
    """文本命中的板块，按置信度降序。每层只取最长 / 最先命中的一项，避免叠加。"""
    tables = _static_tables()
    hits: list[dict] = []
    # 三级行业词和一级行业名在抹掉外国机构名的文本上匹配；宏观 / 主题词仍看原文
    # （「俄罗斯天然气工业公司」里的「俄罗斯」仍算地缘信号）。
    local = _mask_foreign_entities(text)
    for word, codes in tables["industry_kw"]:
        if word in local:
            if len(codes) == 1:
                name = tables["industries"].get(codes[0], {}).get("name", codes[0]).split("-")[-1]
            else:
                name = word
            hits.append({
                "kind": "industry3", "codes": codes, "name": name,
                "confidence": CONF_INDUSTRY3, "why": f"行业关键词:{word}", "macro": False,
            })
            break
    for name in tables["l1_kw"]:
        if name in local:
            hits.append({
                "kind": "industry1", "codes": (), "name": name,
                "confidence": CONF_INDUSTRY1, "why": f"一级行业:{name}", "macro": False,
            })
            break
    military = _military_ok(text)
    for table, macro in ((tables["macro"], True), (tables["theme"], False)):
        # 按词表顺序取第一个还有可用目标的词：「制裁伊朗」里「制裁」被军工门槛拦下后，
        # 仍由「伊朗」映射到石油石化。
        layer: list[dict] = []
        for word, targets in table.items():
            if word not in text:
                continue
            for name in targets:
                if name not in tables["l1_members"]:
                    continue
                if name in tables["military_targets"] and not military:
                    continue
                layer.append({
                    "kind": "theme", "codes": (), "name": name,
                    "confidence": CONF_THEME, "why": f"主题词:{word}", "macro": macro,
                })
            if layer:
                break
        if layer:
            hits.extend(layer)
            break
    return hits


def _sector_members(hit: dict) -> tuple[str, ...]:
    tables = _static_tables()
    if hit["kind"] == "industry3":
        return tuple(symbol for code in hit["codes"] for symbol in tables["members"].get(code, ()))
    return tables["l1_members"].get(hit["name"], ())


def _load_ranking(data_dir: Path) -> dict | None:
    """近 N 日日均成交额与总市值（总股本 × 最新收盘）。读不到返回 None。"""
    import polars as pl

    root = data_dir / "kline_daily"
    if not root.is_dir():
        return None
    days = sorted(p for p in root.iterdir() if p.name.startswith("date=") and (p / "part.parquet").is_file())
    if not days:
        return None
    recent = days[-LIQUIDITY_DAYS:]
    key = f"{recent[-1].name}:{len(recent)}"
    with _ranking_lock:
        if _ranking_cache["key"] == key:
            return _ranking_cache["value"]  # type: ignore[return-value]
        frames = [
            pl.read_parquet(day / "part.parquet", columns=["symbol", "close", "amount"]).with_columns(
                pl.lit(day.name).alias("_day"),
            )
            for day in recent
        ]
        bars = pl.concat(frames, how="vertical_relaxed")
        amount = bars.group_by("symbol").agg(pl.col("amount").mean().alias("amount"))
        last_close = bars.sort("_day").group_by("symbol").agg(pl.col("close").last().alias("close"))
        amounts = {row[0]: float(row[1] or 0.0) for row in amount.iter_rows()}
        caps: dict[str, float] = {}
        inst = data_dir / "instruments" / "instruments.parquet"
        if inst.is_file():
            shares = pl.read_parquet(inst, columns=["symbol", "total_shares"])
            joined = last_close.join(shares, on="symbol", how="inner")
            caps = {
                row[0]: float(row[1] or 0.0) * float(row[2] or 0.0)
                for row in joined.select("symbol", "close", "total_shares").iter_rows()
            }
        value = {"amount": amounts, "cap": caps, "as_of": recent[-1].name.removeprefix("date=")}
        _ranking_cache["key"] = key
        _ranking_cache["value"] = value
        return value


def clear_ranking_cache_for_tests() -> None:
    with _ranking_lock:
        _ranking_cache["key"] = None
        _ranking_cache["value"] = None


def _tradable(symbol: str, name: str, ranking: dict) -> bool:
    if "ST" in name.upper() or "退" in name:
        return False
    return ranking["amount"].get(symbol, 0.0) > 0


def map_text(text: str, *, category: str = "", ranking: dict, limit: int = MAX_SECTOR_STOCKS) -> tuple[dict | None, list[dict]]:
    """文本 → (最主要的板块, 候选个股)。没有命中或没有可交易成分时返回 (None, [])。"""
    tables = _static_tables()
    hits = find_sectors(text)
    picked: list[dict] = []
    used: set[str] = set()
    primary: dict | None = None
    macro_event = category in MACRO_CATEGORIES
    for hit in hits:
        by_cap = macro_event or hit["macro"]
        metric = ranking["cap"] if by_cap and ranking["cap"] else ranking["amount"]
        candidates = [
            symbol for symbol in _sector_members(hit)
            if symbol not in used and _tradable(symbol, tables["names"].get(symbol, ""), ranking)
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda symbol: (-metric.get(symbol, 0.0), symbol))
        if primary is None:
            primary = hit
        for symbol in candidates[: limit - len(picked)]:
            used.add(symbol)
            picked.append({
                "key": symbol,
                "name": tables["names"].get(symbol, symbol),
                "source": SECTOR_SOURCE,
                "sector": hit["name"],
                "sector_kind": hit["kind"],
                "confidence": hit["confidence"],
                "rank_by": "market_cap" if metric is ranking["cap"] else "amount",
                "why": hit["why"],
            })
        if len(picked) >= limit:
            break
    return primary, picked


def _event_text(event: dict) -> str:
    parts = [str(event.get("name") or "")]
    parts.extend(str(item) for item in (event.get("headlines") or []) if item)
    headline = str(event.get("headline") or "")
    if headline:
        parts.append(headline)
    return "\n".join(part for part in parts if part.strip())


def _has_direct_stock(event: dict) -> bool:
    from app.news.service import asset_kind_of

    for item in event.get("mentioned_stocks") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if key and asset_kind_of(key, str(item.get("name") or "")) != "etf":
            return True
    return False


def attach_sector_stocks(events: list[dict], data_dir: Path | str | None = None) -> int:
    """给没有点名个股的事件写 ``sector_stocks`` / ``sector_mapping``。返回补到的事件数。

    不改 ``mentioned_stocks``、分数和顺序。任何异常只记日志，不影响热榜。
    """
    try:
        if data_dir is None:
            from app.config import settings

            data_dir = settings.data_dir
        ranking = _load_ranking(Path(data_dir))
    except Exception:  # noqa: BLE001
        logger.warning("板块补个股：读取本地行情失败，本轮不补", exc_info=True)
        return 0
    if not ranking or not ranking["amount"]:
        return 0
    filled = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        event.pop("sector_stocks", None)
        event.pop("sector_mapping", None)
        if _has_direct_stock(event):
            continue
        try:
            primary, stocks = map_text(_event_text(event), category=str(event.get("category") or ""), ranking=ranking)
        except Exception:  # noqa: BLE001
            logger.warning("板块补个股失败: %s", event.get("key"), exc_info=True)
            continue
        if not stocks or primary is None:
            continue
        event["sector_stocks"] = stocks
        event["sector_mapping"] = {
            "name": primary["name"],
            "kind": primary["kind"],
            "confidence": primary["confidence"],
            "why": primary["why"],
        }
        filled += 1
    return filled
