"""DSA 个股上下文：题材、竞价、加红、资金，以及注入时的长度和失败隔离。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.custom.dsa import stock_context
from app.custom.dsa.analysis_bridge import (
    _CONTEXT_TIMEOUT,
    merge_news,
    sniper_payload,
    wrap_context_loader,
    wrap_history_save,
)
from app.custom.dsa.stock_context import (
    MAX_TEXT,
    market_phase,
    match_events,
    quote_from_rows,
    render_context,
)

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=CN)


def _event(**kwargs):
    base = {
        "name": "固态电池放量",
        "headline": "固态电池中试线投产",
        "category": "产业",
        "direction": "利好",
        "heat": 12.5,
        "red_count": 2,
        "first_seen": "09:20",
        "concepts": ["固态电池", "锂电"],
        "stocks": [{"key": "600519.SH", "name": "贵州茅台"}],
    }
    base.update(kwargs)
    return base


def test_match_uses_code_and_keeps_four_hottest():
    events = [
        _event(heat=index, headline=f"事件{index}", stocks=[{"key": "600519"}])
        for index in range(6)
    ]
    events.append(_event(headline="别的股票", stocks=[{"key": "000001.SZ"}], heat=99))
    matched = match_events(events, "SH600519")
    assert len(matched) == 4
    assert matched[0]["headline"] == "事件5"
    assert all("别的" not in item["headline"] for item in matched)


def test_auction_change_uses_raw_prices_and_marks_unconfirmed_before_match():
    rows = [
        {"date": "2026-10-08", "open": 20, "close": 20, "raw_close": 10, "volume": 240_000},
        {"date": "2026-10-09", "open": 22, "close": 24, "raw_close": 12, "volume": 30_000},
    ]
    quote = quote_from_rows(rows, NOW)
    assert quote["auction_price"] == 11
    assert quote["prev_close"] == 10
    pre = datetime(2026, 10, 9, 9, 20, tzinfo=CN)
    text = render_context(symbol="600519.SH", now=pre, events=[], red_news=[], flow_rows=[], quote=quote)["text"]
    assert "竞价涨跌幅: +10.00%" in text
    assert "已确认: 否" in text
    assert market_phase(pre) == "集合竞价"
    opened = render_context(symbol="600519.SH", now=NOW, events=[], red_news=[], flow_rows=[], quote=quote)
    assert "已确认: 是" in opened["text"]
    assert "量比: 1.00" in opened["text"]


def test_missing_raw_price_is_not_replaced_with_adjusted_close():
    quote = quote_from_rows(
        [{"date": "2026-10-09", "open": 22, "close": 24, "volume": 1}],
        NOW,
    )
    assert quote["auction_price"] is None
    text = render_context(
        symbol="600519.SH", now=NOW, events=[], red_news=[], flow_rows=[], quote=quote,
    )["text"]
    assert "竞价涨跌幅: 缺失" in text
    assert "22" not in text.split("盘口确认")[1].split("加红")[0]


def test_context_blocks_instructions_caps_and_staleness():
    events = [_event(headline="固" * 80, heat=index, first_seen="09:20") for index in range(8)]
    news = [
        {"title": f"加红标题{index}", "published_at": "2026-10-09T09:18:00+08:00", "red": True}
        for index in range(6)
    ]
    news[0]["title"] = "固态电池中试线投产公告"
    flow = [
        {"trade_date": f"2026-09-{day:02d}", "main_net": day * 1e8}
        for day in range(1, 12)
    ]
    payload = render_context(
        symbol="600519.SH",
        now=NOW,
        events=events,
        events_as_of="2026-10-09T09:40:00+08:00",
        red_news=news,
        flow_rows=flow,
        sector_rank={"name": "白酒", "rank": 4, "net_inflow": 1.2e8, "trade_date": "2026-09-01"},
        concept_ranks=[{"name": "固态电池", "rank": 2, "net_inflow": -5e7, "trade_date": "2026-10-09"}],
        quote=None,
    )
    text = payload["text"]
    assert len(text) <= MAX_TEXT
    assert "【TSP个股上下文】" in text
    assert "相关热点" in text and "板块行业分析" in text
    assert "高优先级" in text
    assert "利好催化" in text and "风险警报" in text
    assert text.count("加红标题") == 3
    assert "固态电池中试线投产公告" in text
    assert "方向未标" in text or "利好" in text
    assert "近10日主力净流入" in text
    assert "2026-09-01" not in text or "过期" in text
    assert "行业资金排名: 白酒 第4" in text
    assert "过期" in text
    assert "盘口确认" in text and "状态: 缺失" in text
    assert "auction" in payload["missing"]
    assert "fund_flow" in payload["stale"]


def test_red_item_linked_to_event_keeps_direction():
    payload = render_context(
        symbol="600519.SH",
        now=NOW,
        events=[_event()],
        red_news=[{
            "title": "固态电池中试线投产",
            "published_at": "09:18",
            "red": True,
        }],
        flow_rows=[
            {"trade_date": "2026-10-08", "main_net": None},
            {"trade_date": "2026-10-09", "main_net": 1e8},
        ],
        quote=None,
    )
    assert "09:18 | 利好 | 固态电池中试线投产" in payload["text"]
    assert "不足5日" in payload["text"]
    assert "10-09 +1.00亿" in payload["text"]
    assert "10-08" not in payload["text"]


def test_failed_section_does_not_drop_the_others(monkeypatch):
    def boom(symbol, now):
        raise RuntimeError("hot down")

    monkeypatch.setattr(stock_context, "_load_events", boom)
    monkeypatch.setattr(stock_context, "_load_red_news", lambda symbol: [{
        "title": "业绩预增", "published_at": "09:01", "red": True,
    }])
    monkeypatch.setattr(stock_context, "_load_flow", lambda symbol, events: {"rows": [], "sector": None, "concepts": []})
    monkeypatch.setattr(stock_context, "_load_bars", lambda repo, symbol, now: None)
    payload = stock_context.load_context("600519", now=NOW, repo=None)
    assert "题材催化" in payload["text"] and "状态: 缺失" in payload["text"]
    assert "业绩预增" in payload["text"]
    assert payload["symbol"] == "600519.SH"


def test_bridge_prepends_context_and_posts_sniper_without_blocking_save():
    assert merge_news("旧情报", "【TSP个股上下文】\n题材") .startswith("【TSP个股上下文】")
    assert merge_news(None, "") is None

    class Pipeline:
        def _load(self, *, code, stock_name, market, limit=6):
            return "旧情报"

    wrapped = wrap_context_loader(Pipeline._load, lambda code: "【TSP个股上下文】")
    assert wrapped(Pipeline(), code="600519", stock_name="茅台", market="cn").startswith("【TSP")

    class Result:
        success = True
        code = "600519"
        name = "贵州茅台"

        def get_sniper_points(self):
            return {"ideal_buy": "理想买入点：1800元", "stop_loss": "1700", "take_profit": None}

    body = sniper_payload(Result(), query_id="q1", history_id=0)
    assert body["ideal_buy"] == 1800
    assert body["stop_loss"] == 1700
    assert body["take_profit"] is None
    assert body["analysis_id"] == "q1"
    assert sniper_payload(SimpleNamespace(success=False, code="600519")) is None

    posted = []

    class Store:
        def save(self, result, query_id, report_type, news_content, context_snapshot=None, save_snapshot=True):
            return 7

    def fail(body):
        posted.append(body)
        raise RuntimeError("tsp down")

    saver = wrap_history_save(Store.save, fail)
    assert saver(Store(), Result(), "q1", "full", None) == 7
    assert posted and posted[0]["analysis_id"] == "7"


def _request(path: str, header: str = "") -> Request:
    headers = []
    if header:
        headers.append((b"x-news-feed-token", header.encode()))
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    })


async def _ok(_request):
    return JSONResponse({"ok": True})


def test_context_cache_lasts_about_thirty_seconds(monkeypatch):
    calls = []
    clock = {"now": 100.0}

    def fake(symbol, now=None, repo=None):
        calls.append(symbol)
        return {"symbol": "600519.SH", "text": "cached", "missing": [], "stale": []}

    monkeypatch.setattr(stock_context, "load_context", fake)
    monkeypatch.setattr(stock_context.time, "monotonic", lambda: clock["now"])
    stock_context.clear_context_cache()
    first = stock_context.load_context_cached("600519", now=NOW)
    second = stock_context.load_context_cached("600519.SH", now=NOW)
    assert second == first
    assert calls == ["600519"]
    clock["now"] += stock_context.CONTEXT_CACHE_TTL + 1
    stock_context.load_context_cached("600519", now=NOW)
    assert calls == ["600519", "600519"]
    assert _CONTEXT_TIMEOUT == 3


def test_stock_context_and_sniper_routes_use_feed_token(monkeypatch):
    monkeypatch.setattr("app.news.config.feed_token", lambda: "feed-secret")
    from app.main import auth_middleware

    for path in ("/api/dsa/stock-context", "/api/dsa/sniper-lines"):
        allowed = asyncio.run(auth_middleware(_request(path, header="feed-secret"), _ok))
        denied = asyncio.run(auth_middleware(_request(path, header="nope"), _ok))
        assert allowed.status_code == 200
        assert denied.status_code == 404
