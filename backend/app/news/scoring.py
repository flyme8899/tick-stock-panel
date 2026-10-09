"""热门板块 / 个股打分。

同一内容指纹算一条故事。同来源重复只记一次；同一故事出现在多个来源时加权。
同一来源短时间刷屏按对数衰减。窗口相对前几日基线的倍数体现「突然变热」。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class MentionEvent:
    kind: str
    key: str
    name: str
    source: str
    content_hash: str
    published_at: datetime


@dataclass(frozen=True)
class Candidate:
    kind: str
    key: str
    name: str
    score: float
    story_count: int
    effective_mentions: float
    sources: tuple[str, ...]
    growth: float
    baseline_effective: float


def _dampen(count: int) -> float:
    """第一条故事记 1，同来源后续故事迅速变便宜。"""
    if count <= 0:
        return 0.0
    if count == 1:
        return 1.0
    return 1.0 + math.log1p(count - 1)


def _effective(stories_by_source: dict[str, set[str]]) -> tuple[float, int, tuple[str, ...]]:
    total = 0.0
    story_ids: set[str] = set()
    for _source, hashes in stories_by_source.items():
        total += _dampen(len(hashes))
        story_ids.update(hashes)
    sources = tuple(sorted(stories_by_source))
    return total, len(story_ids), sources


def score_candidates(
    events: list[MentionEvent],
    *,
    now: datetime,
    window_hours: int = 24,
    baseline_days: int = 4,
) -> list[Candidate]:
    window_hours = max(1, min(int(window_hours), 24 * 14))
    baseline_days = max(1, min(int(baseline_days), 30))
    window_start = now - timedelta(hours=window_hours)
    baseline_start = window_start - timedelta(days=baseline_days)

    window: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    names: dict[tuple[str, str], str] = {}
    baseline: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for event in events:
        if event.published_at > now:
            continue
        ident = (event.kind, event.key)
        names[ident] = event.name or event.key
        if event.published_at > window_start:
            window[ident][event.source].add(event.content_hash)
        elif event.published_at > baseline_start:
            baseline[ident][event.source].add(event.content_hash)

    scale = window_hours / (baseline_days * 24)
    ranked: list[Candidate] = []
    for ident, by_source in window.items():
        effective, story_count, sources = _effective(by_source)
        base_effective, _, _ = _effective(baseline.get(ident, {}))
        expected = base_effective * scale
        growth = (effective + 0.5) / (expected + 0.5)
        cross = 1.0 + 0.75 * max(0, len(sources) - 1)
        ranked.append(Candidate(
            kind=ident[0],
            key=ident[1],
            name=names.get(ident, ident[1]),
            score=round(effective * cross * growth, 4),
            story_count=story_count,
            effective_mentions=round(effective, 4),
            sources=sources,
            growth=round(growth, 4),
            baseline_effective=round(base_effective, 4),
        ))
    ranked.sort(key=lambda item: (-item.score, -item.story_count, item.name))
    return ranked
