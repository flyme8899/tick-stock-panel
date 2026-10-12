"""用涨停原因核对热门事件的概念映射。

已经写在事件上的概念，如果有涨停股的题材对得上，记为已验证。
事件里的股票如果带有更细的涨停题材，补进概念列表，宽基行业名不补。
"""
from __future__ import annotations

from pathlib import Path

from app.limit_board.parse import split_themes
from app.limit_board.query import _theme_matches, reasons_for
from app.news.extract import code6_of


def _event_title_blob(event: dict) -> str:
    """事件展示名 + 全部候选标题。涨停题材补概念的字面锚定用。

    成员标题只取前 3 条（``headlines``），候选 ``_candidates`` 才是全量，
    漏看成员会把该补的概念挡掉，所以两边都拼。
    """
    parts = [str(event.get("name") or ""), str(event.get("headline") or "")]
    for title in event.get("headlines") or []:
        parts.append(str(title or ""))
    for row in event.get("_candidates") or []:
        if isinstance(row, dict):
            parts.append(str(row.get("title") or ""))
    return "".join(parts)


def apply_limit_reasons(events: list[dict], data_dir: Path, trade_date: str) -> None:
    reasons = reasons_for(data_dir, trade_date)
    if not reasons:
        return
    broad = _broad_sectors()
    for event in events:
        if not isinstance(event, dict):
            continue
        concepts = [str(name).strip() for name in (event.get("concepts") or []) if str(name).strip()]
        title_blob = _event_title_blob(event)
        verified: list[str] = []
        enriched: list[str] = []
        for stock in event.get("mentioned_stocks") or []:
            if not isinstance(stock, dict):
                continue
            # 涨停题材只补「股票名在事件标题里出现过」的事件。线上实测双写
            # 残差：早报目录行的上游 stocks 混进弱相关个股（东百集团进了
            # 「证监会座谈会」簇，15 个成员没有一条讲收购），涨停题材把
            # 「光芯片收购」补进该簇，概念与成员完全脱节。股票名不在任何
            # 成员标题里，说明它对本事件只是弱关联，题材没有锚定资格。
            stock_name = str(stock.get("name") or "")
            if len(stock_name) >= 2 and stock_name not in title_blob:
                continue
            code = code6_of(str(stock.get("key") or ""))
            hit = reasons.get(code)
            if not hit:
                continue
            for theme in hit.get("limit_themes") or split_themes(str(hit.get("limit_reason") or "")):
                matched = next((name for name in concepts if _theme_matches(theme, name)), "")
                if matched:
                    if matched not in verified:
                        verified.append(matched)
                    continue
                if theme in broad or theme in concepts or theme in enriched:
                    continue
                if len(enriched) >= 3:
                    continue
                enriched.append(theme)
        if not verified and not enriched:
            continue
        if enriched:
            concepts.extend(enriched)
            event["concepts"] = concepts
            direction = str(event.get("direction") or "")
            mapping = event.get("mapping")
            if not isinstance(mapping, list):
                mapping = []
                event["mapping"] = mapping
            names = {
                str(item.get("name") or "")
                for item in mapping
                if isinstance(item, dict)
            }
            for theme in enriched:
                if theme not in names:
                    mapping.append({
                        "name": theme,
                        "kind": "concept",
                        "direction": direction,
                        "origin": "limit_reason",
                    })
        event["limit_reason"] = {"verified": verified, "enriched": enriched}


def _broad_sectors() -> frozenset[str]:
    from app.news.hot_events import BROAD_SECTORS

    return BROAD_SECTORS
