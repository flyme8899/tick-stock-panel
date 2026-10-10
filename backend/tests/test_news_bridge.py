"""DSA 资讯桥的纯函数。不导入 vendor。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.custom.dsa.news_bridge import (
    _SOURCES,
    _fetch_tsp_source,
    expand_item,
    hot_news_rows,
    install,
    is_tsp_feed_url,
)
from app.news.config import SOURCE_LABELS, SOURCE_ORDER


def test_bridge_sources_follow_the_news_catalog():
    keys = [row[0] for row in _SOURCES]
    assert keys == [*SOURCE_ORDER, "hot"]
    assert [row[1] for row in _SOURCES] == [SOURCE_LABELS[key] for key in keys]


def test_feed_url_allowlist(monkeypatch):
    monkeypatch.setenv("TSP_NEWS_BASE_URL", "http://app:3018")
    assert is_tsp_feed_url("http://app:3018/api/news/dsa-feed?source=cls")
    assert is_tsp_feed_url("http://127.0.0.1:3018/api/news/dsa-feed")
    assert not is_tsp_feed_url("http://app:3018/api/news/hot")
    assert not is_tsp_feed_url("http://evil.example/api/news/dsa-feed")


def test_expand_item_keeps_stock_and_sector_tags():
    rows = expand_item(
        {
            "source_id": "1",
            "title": "茅台电报",
            "summary": "摘录",
            "published_at": "2026-10-09T10:00:00+08:00",
            "symbols": ["600519.SH", "600519.SH", ""],
            "sectors": ["白酒"],
        },
        source_id=7,
        source_name="财联社",
        now=datetime(2026, 10, 9, 10, 5),
    )
    scopes = {(row["scope_type"], row["scope_value"]) for row in rows}
    assert ("market", None) in scopes
    assert ("symbol", "600519.SH") in scopes
    assert ("sector", "白酒") in scopes
    assert all(row["source_type"] == "tsp" for row in rows)
    assert expand_item({"title": "  "}, source_id=1, source_name="财联社", now=datetime.now()) == []

    wide = expand_item(
        {
            "title": "很多标签",
            "symbols": [f"{i:06d}.SZ" for i in range(12)],
            "sectors": [f"板块{i}" for i in range(10)],
        },
        source_id=1,
        source_name="华尔街见闻",
        now=datetime(2026, 10, 9, 10, 5),
    )
    assert sum(row["scope_type"] == "symbol" for row in wide) == 8
    assert sum(row["scope_type"] == "sector" for row in wide) == 6


def test_hot_rows_are_bounded():
    rows = hot_news_rows([
        {"title": f"候选{i}", "summary": "摘录", "published_at": "2026-10-09"}
        for i in range(6)
    ])
    assert len(rows) == 4
    assert rows[0]["source"] == "TSP热门候选"


def test_install_degrades_without_token(monkeypatch):
    monkeypatch.delenv("NEWS_DSA_FEED_TOKEN", raising=False)
    install()


class _Source:
    def __init__(self, *, enabled: bool):
        self.id = 3
        self.name = "财联社"
        self.url = "http://127.0.0.1:3018/api/news/dsa-feed?source=cls"
        self.enabled = enabled
        self.source_type = "tsp"


class _Repo:
    def __init__(self):
        self.calls: list[tuple] = []

    def upsert_items(self, rows):
        self.calls.append(("upsert", len(rows)))
        return len(rows)

    def apply_retention(self, days):
        self.calls.append(("retention", days))
        return 4

    def update_source_status(self, source_id, **kwargs):
        self.calls.append(("status", kwargs.get("status"), source_id))


class _Service:
    def __init__(self):
        self.repo = _Repo()
        self.config = type("Config", (), {"news_intel_retention_days": 30})()


def test_fetch_keeps_retention_enabled_and_samples(monkeypatch):
    monkeypatch.setattr(
        "app.custom.dsa.news_bridge._fetch_json",
        lambda key: {
            "items": [{
                "title": "茅台电报",
                "summary": "摘录",
                "url": "https://example.test/a",
                "source_id": "1",
                "published_at": "2026-10-09T10:00:00+08:00",
                "symbols": ["600519.SH"],
            }],
        },
    )
    service = _Service()
    result = _fetch_tsp_source(service, _Source(enabled=True), dry_run=False)
    assert ("retention", 30) in service.repo.calls
    assert result["retention_deleted"] == 4
    assert result["saved_count"] == 2
    sample = result["sample_items"][0]
    assert sample["title"] == "茅台电报"
    assert sample["summary"] == "摘录"
    assert sample["url"] == "https://example.test/a"
    assert sample["source"] == "财联社"
    assert sample["published_at"]

    service.repo.calls.clear()
    dry = _fetch_tsp_source(service, _Source(enabled=True), dry_run=True)
    assert dry["retention_deleted"] == 0
    assert dry["sample_items"][0]["title"] == "茅台电报"
    assert not any(call[0] == "retention" for call in service.repo.calls)
    assert not any(call[0] == "upsert" for call in service.repo.calls)

    service.repo.calls.clear()
    with pytest.raises(Exception, match="disabled"):
        _fetch_tsp_source(service, _Source(enabled=False), dry_run=False)
    assert service.repo.calls == []
