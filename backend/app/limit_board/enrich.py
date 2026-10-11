"""用涨停原因核对热门事件的概念映射。

已经写在事件上的概念，如果有涨停股的题材对得上，记为已验证。
事件里的股票如果带有更细的涨停题材，补进概念列表，宽基行业名不补。
"""
from __future__ import annotations

from pathlib import Path

from app.limit_board.parse import split_themes
from app.limit_board.query import _theme_matches, reasons_for
from app.news.extract import code6_of


def apply_limit_reasons(events: list[dict], data_dir: Path, trade_date: str) -> None:
    reasons = reasons_for(data_dir, trade_date)
    if not reasons:
        return
    broad = _broad_sectors()
    for event in events:
        if not isinstance(event, dict):
            continue
        concepts = [str(name).strip() for name in (event.get("concepts") or []) if str(name).strip()]
        verified: list[str] = []
        enriched: list[str] = []
        for stock in event.get("mentioned_stocks") or []:
            if not isinstance(stock, dict):
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
            direction = str(event.get("direction") or "利好")
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
