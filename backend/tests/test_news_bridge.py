"""DSA 资讯桥的纯函数。不导入 vendor。"""
from __future__ import annotations

from datetime import datetime

from app.custom.dsa.news_bridge import expand_item, hot_news_rows, install, is_tsp_feed_url


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
