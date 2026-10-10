"""选股合并、过滤、快照和来源降级。"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import picker as picker_api
from app.market_time import CN_TZ
from app.news.service import get_store, reset_store_for_tests, top_hot_events
from app.services.picker import (
    Dimensions,
    FilterSpec,
    PickerDeps,
    SourceSpec,
    apply_filters,
    build_catalog,
    build_label_index,
    classify_strategies,
    combine_symbol_sets,
    hot_event_id,
    load_top_events_from_news,
    map_hot_sectors,
    map_selected_hot_event,
    match_sector_symbols,
    point_in_time_financials,
    resolve_dsa_code,
    run_dsa_strategy,
    run_picker,
    safe_source_id,
    screen_fundamental,
    sync_dsa_watchlist,
    with_profit_yoy,
    write_snapshot,
)

NOW = datetime(2026, 10, 10, 8, 30, tzinfo=CN_TZ)
AS_OF = date(2026, 10, 9)


def _deps(tmp_path, **overrides) -> PickerDeps:
    deps = PickerDeps(
        data_dir=tmp_path,
        list_strategies=lambda: [],
        run_strategies=lambda ids: {sid: ([], None) for sid in ids},
        load_market=lambda: (AS_OF, []),
        load_financial=lambda as_of: (pl.DataFrame(), "扣非增速"),
        load_dimensions=lambda: Dimensions(),
        dsa_request=None,
        now=NOW,
        poll_seconds=0,
    )
    for key, value in overrides.items():
        setattr(deps, key, value)
    return deps


def _fin_row(**overrides) -> dict:
    row = {
        "symbol": "600519.SH",
        "roe": 20.0,
        "profit_yoy": 30.0,
        "revenue_yoy": 15.0,
        "debt_to_asset_ratio": 40.0,
        "pe": 18.0,
        "market_cap": 300e8,
        "operating_cash_to_revenue": 1.1,
    }
    row.update(overrides)
    return row


def test_classify_splits_builtin_factor_and_skips_fundamentals():
    technical, factor = classify_strategies([
        {"id": "macd_golden", "name": "MACD", "source": "builtin", "tags": ["趋势"], "asset_types": ["stock"], "timeframes": ["1d"]},
        {"id": "custom_factor_roe", "name": "ROE排名", "source": "custom", "tags": ["factor", "auto-generated"]},
        {"id": "fundamental_m1", "name": "不该出现", "source": "builtin", "tags": []},
        {"id": "research_only_x", "name": "研究", "source": "builtin", "research_only": True},
        {"id": "minute_only", "name": "分钟", "source": "builtin", "timeframes": ["1m"]},
        {"id": "my_custom", "name": "自定义", "source": "custom", "tags": ["趋势"]},
        {"id": "low_vol", "name": "低波动", "source": "builtin", "tags": ["factor"]},
    ])
    assert [item["id"] for item in technical] == ["macd_golden"]
    assert {item["id"] for item in factor} == {"custom_factor_roe", "low_vol"}


def test_combine_and_or():
    assert combine_symbol_sets([{"A", "B"}, {"B", "C"}], "and") == {"B"}
    assert combine_symbol_sets([{"A"}, {"B"}], "or") == {"A", "B"}
    assert combine_symbol_sets([], "and") == set()


def test_point_in_time_uses_prior_period_on_announcement_day():
    frame = pl.DataFrame({
        "symbol": ["600519.SH", "600519.SH"],
        "announce_date": ["2026-10-08", "2026-10-09"],
        "roe": [10.0, 99.0],
        "net_income_yoy": [5.0, 80.0],
    })
    latest = point_in_time_financials(frame, AS_OF)
    assert latest["roe"].to_list() == [10.0]
    labeled, label = with_profit_yoy(latest)
    assert label == "净利增速"
    assert labeled["profit_yoy"].to_list() == [5.0]

    deducted = latest.with_columns(pl.lit(33.0).alias("扣非净利润同比"))
    labeled, label = with_profit_yoy(deducted)
    assert label == "扣非增速"
    assert labeled["profit_yoy"].to_list() == [33.0]


def test_fundamental_m1_selects_quality_and_m2_needs_pe():
    frame = pl.DataFrame([
        _fin_row(),
        _fin_row(symbol="000001.SZ", roe=5.0),
        _fin_row(symbol="600000.SH", pe=None),
    ])
    scores, warning = screen_fundamental(frame, "fundamental_m1")
    assert warning is None
    assert set(scores) == {"600519.SH", "600000.SH"}

    missing_pe = frame.drop("pe")
    scores, warning = screen_fundamental(missing_pe, "fundamental_m2")
    assert scores == {}
    assert warning is not None and "pe" in warning

    scores, warning = screen_fundamental(frame, "fundamental_m2")
    assert warning is None
    assert set(scores) == {"600519.SH"}


def test_fundamental_m3_uses_cash_ratio_not_percent_guess():
    frame = pl.DataFrame([
        _fin_row(operating_cash_to_revenue=0.9),
        _fin_row(symbol="000001.SZ", operating_cash_to_revenue=0.2),
    ])
    scores, warning = screen_fundamental(frame, "fundamental_m3")
    assert warning is None
    assert set(scores) == {"600519.SH"}


def test_hot_sector_exact_match_respects_min_sources_and_broad_substring():
    dimensions = Dimensions(
        industry_label={"600519.SH": "白酒"},
        industry_path={"600519.SH": "食品饮料-白酒"},
        concepts={"000001.SZ": ["人工智能"], "600000.SH": ["银行"]},
    )
    candidates = [
        SimpleNamespace(name="人工智能", key="ai", sources=("a", "b", "c"), story_count=4),
        SimpleNamespace(name="银行", key="bank", sources=("a",), story_count=9),
    ]
    scores, events = map_hot_sectors(candidates, dimensions, min_source_count=2)
    assert set(scores) == {"000001.SZ"}
    assert events["000001.SZ"] == "人工智能 · 3源"

    broad = Dimensions(concepts={f"{index:06d}.SZ": [f"科技{index}"] for index in range(31)})
    assert match_sector_symbols("科技", "科技", build_label_index(broad)) == set()

    narrow = Dimensions(concepts={"000002.SZ": ["AI概念"]})
    assert match_sector_symbols("AI", "AI", build_label_index(narrow)) == {"000002.SZ"}


def test_filters_drop_st_financial_and_cap_outside_range():
    rows = [
        {"symbol": "600519.SH", "name": "贵州茅台", "industry": "白酒", "industry_path": "食品饮料-白酒", "score": 90, "market_cap": 300e8, "pe": 20},
        {"symbol": "000001.SZ", "name": "*ST平安", "industry": "银行", "industry_path": "银行-银行", "score": 80, "market_cap": 50e8, "pe": 5},
        {"symbol": "601318.SH", "name": "中国平安", "industry": "保险", "industry_path": "非银金融-保险", "score": 70, "market_cap": 400e8, "pe": 8},
        {"symbol": "300001.SZ", "name": "小票", "industry": "白酒", "industry_path": "食品饮料-白酒", "score": 60, "market_cap": None, "pe": None},
    ]
    filtered = apply_filters(rows, FilterSpec(
        industries=["白酒"],
        market_cap_min=100,
        market_cap_max=500,
        pe_min=10,
        pe_max=30,
        exclude_st=True,
        exclude_financial=True,
        max_per_industry=1,
    ))
    assert [row["symbol"] for row in filtered] == ["600519.SH"]


def test_failed_source_does_not_empty_intersection_but_true_miss_does(tmp_path):
    market = [{"symbol": "600519.SH", "name": "贵州茅台", "close": 100.0, "total_shares": 3e8, "market_cap": 300e8}]
    deps = _deps(
        tmp_path,
        list_strategies=lambda: [{
            "id": "macd_golden",
            "name": "MACD金叉",
            "source": "builtin",
            "tags": [],
            "asset_types": ["stock"],
            "timeframes": ["1d"],
        }],
        run_strategies=lambda ids: {"macd_golden": ([{"symbol": "600519.SH", "score": 88}], None)},
        load_market=lambda: (AS_OF, market),
        load_financial=lambda as_of: (pl.DataFrame(), "扣非增速"),
    )
    result = run_picker(
        [SourceSpec("fundamental", "fundamental_m1"), SourceSpec("technical", "macd_golden")],
        "and",
        FilterSpec(),
        deps,
    )
    assert [row["symbol"] for row in result["rows"]] == ["600519.SH"]
    assert any("缺少" in item for item in result["summary"]["warnings"])

    matched = pl.DataFrame([_fin_row(roe=1.0, profit_yoy=1.0, revenue_yoy=1.0)])
    deps.load_financial = lambda as_of: (matched, "净利增速")
    empty = run_picker(
        [SourceSpec("fundamental", "fundamental_m1"), SourceSpec("technical", "macd_golden")],
        "and",
        FilterSpec(),
        deps,
    )
    assert empty["rows"] == []
    assert empty["summary"]["warnings"] == []


def test_snapshot_diff_added_and_removed(tmp_path):
    def market(symbols: list[str]):
        return lambda: (AS_OF, [
            {"symbol": symbol, "name": symbol, "close": 10.0, "total_shares": 1e8, "market_cap": 10e8}
            for symbol in symbols
        ])

    deps = _deps(
        tmp_path,
        list_strategies=lambda: [{
            "id": "macd_golden", "name": "MACD", "source": "builtin", "tags": [],
            "asset_types": ["stock"], "timeframes": ["1d"],
        }],
    )
    deps.load_market = market(["600519.SH", "000001.SZ"])
    deps.run_strategies = lambda ids: {"macd_golden": ([
        {"symbol": "600519.SH", "score": 90},
        {"symbol": "000001.SZ", "score": 70},
    ], None)}
    first = run_picker([SourceSpec("technical", "macd_golden")], "or", FilterSpec(), deps)
    assert first["summary"]["first_snapshot"] is True
    assert first["summary"]["added"] == 2
    assert {row["change"] for row in first["rows"]} == {"new"}

    deps.load_market = market(["000001.SZ", "300001.SZ"])
    deps.run_strategies = lambda ids: {"macd_golden": ([
        {"symbol": "000001.SZ", "score": 70},
        {"symbol": "300001.SZ", "score": 60},
    ], None)}
    second = run_picker([SourceSpec("technical", "macd_golden")], "or", FilterSpec(), deps)
    assert second["summary"]["added"] == 1
    assert second["summary"]["removed"] == 1
    changes = {row["symbol"]: row["change"] for row in second["rows"]}
    assert changes == {"000001.SZ": "kept", "300001.SZ": "new"}
    assert second["summary"]["as_of"] == "2026-10-09"
    assert second["summary"]["combine_label"] == "并集"


def test_snapshot_name_stays_inside_directory(tmp_path):
    write_snapshot(tmp_path, {"as_of": None, "symbols": ["600519.SH"]}, now=NOW)
    files = list((tmp_path / "picker" / "snapshots").glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == (tmp_path / "picker" / "snapshots").resolve()


def test_dsa_unreachable_and_code_resolution():
    assert resolve_dsa_code("600519", {"600519.SH", "000001.SZ"}) == "600519.SH"
    assert resolve_dsa_code("600519", {"600519.SH", "600519.SZ"}) is None

    def boom(method, path, body):
        raise RuntimeError("connection refused")

    scores, warning = run_dsa_strategy(boom, "dual_low", {"600519.SH"}, poll_seconds=0)
    assert scores == {}
    assert warning == "决策服务未连接"

    calls = {"n": 0}

    def pending_then_done(method, path, body):
        calls["n"] += 1
        if method == "POST":
            return 202, {"task_id": "abc123", "status": "pending"}
        return 200, {"status": "completed", "result": {"candidates": [{"code": "600519", "score": 77}]}}

    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 0.2
        return clock["t"]

    scores, warning = run_dsa_strategy(
        pending_then_done,
        "dual_low",
        {"600519.SH"},
        poll_seconds=2,
        sleep=lambda _seconds: None,
        monotonic=monotonic,
    )
    assert warning is None
    assert scores == {"600519.SH": 77}


def test_dsa_sync_stops_when_upstream_drops():
    seen: list[str] = []

    def request(method, path, body):
        seen.append(body["stock_code"])
        if len(seen) == 2:
            raise RuntimeError("down")
        return 200, {"message": "ok"}

    result = sync_dsa_watchlist(["600519.SH", "000001.SZ", "300001.SZ"], request)
    assert result["ok"] is False
    assert result["synced"] == 1
    assert result["failed"] == ["000001.SZ", "300001.SZ"]
    assert result["message"] == "决策服务未连接"


def test_catalog_marks_dsa_beta_and_factor_empty_link(tmp_path):
    catalog = build_catalog(_deps(tmp_path))
    groups = {group["id"]: group for group in catalog["groups"]}
    assert [group["id"] for group in catalog["groups"]] == [
        "fundamental", "hot_events", "technical", "factor", "dsa",
    ]
    assert [item["id"] for item in groups["fundamental"]["items"]] == [
        "fundamental_m1", "fundamental_m2", "fundamental_m3",
    ]
    assert groups["hot_events"]["items"] == []
    assert groups["hot_events"]["updated_at"] is None
    assert groups["hot_events"]["fallback"] is False
    assert groups["hot_events"]["hint"] is None
    assert groups["factor"]["empty_href"] == "/factors"
    assert groups["dsa"]["beta"] is True
    assert groups["dsa"]["available"] is False
    assert groups["dsa"]["error"] == "决策服务未连接"


def test_api_validates_payload_and_runs_with_fake_deps(tmp_path, monkeypatch: pytest.MonkeyPatch):
    deps = _deps(
        tmp_path,
        list_strategies=lambda: [{
            "id": "macd_golden", "name": "MACD", "source": "builtin", "tags": [],
            "asset_types": ["stock"], "timeframes": ["1d"],
        }],
        run_strategies=lambda ids: {"macd_golden": ([{"symbol": "600519.SH", "score": 81, "name": "贵州茅台"}], None)},
        load_market=lambda: (AS_OF, [{
            "symbol": "600519.SH", "name": "贵州茅台", "close": 100.0, "total_shares": 1e8, "market_cap": 100e8,
        }]),
    )
    monkeypatch.setattr(picker_api, "deps_from_app", lambda request, **kwargs: deps)
    app = FastAPI()
    app.include_router(picker_api.router)
    client = TestClient(app)

    rejected = client.post("/api/picker/run", json={"sources": [], "combine": "or"})
    assert rejected.status_code == 422
    bad_window = client.post("/api/picker/run", json={
        "sources": [{"type": "hot_events", "id": "hot_stocks", "params": {"window": "7d"}}],
        "combine": "or",
    })
    assert bad_window.status_code == 422
    bad_range = client.post("/api/picker/run", json={
        "sources": [{"type": "technical", "id": "macd_golden"}],
        "combine": "or",
        "filters": {"market_cap_min": 80, "market_cap_max": 10},
    })
    assert bad_range.status_code == 400

    ok = client.post("/api/picker/run", json={
        "sources": [{"type": "technical", "id": "macd_golden"}],
        "combine": "or",
        "filters": {"exclude_st": True},
    })
    assert ok.status_code == 200
    body = ok.json()
    assert body["rows"][0]["symbol"] == "600519.SH"
    assert body["rows"][0]["strategies"][0]["name"] == "MACD"
    assert body["summary"]["total"] == 1

    catalog = client.get("/api/picker/sources")
    assert catalog.status_code == 200
    assert any(group["id"] == "technical" and group["items"][0]["id"] == "macd_golden" for group in catalog.json()["groups"])


def test_hot_event_id_accepts_sector_names_and_rejects_paths():
    assert safe_source_id("人工智能")
    assert hot_event_id("人工智能") == "人工智能"
    assert not safe_source_id("../etc")
    assert not safe_source_id("a/b")
    assert hot_event_id("a/b").startswith("ev_")


def _publish(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ)


def _insert_news(
    source: str,
    source_id: str,
    published: datetime,
    sectors: list[str],
    stocks: list[str],
    title: str | None = None,
) -> None:
    mentions = [("sector", name, name, "", "structured") for name in sectors]
    mentions += [("stock", symbol, symbol, symbol[:6], "structured") for symbol in stocks]
    headline = title or source_id
    status = get_store().insert_item(
        source=source,
        source_id=source_id,
        published_at=published,
        author="",
        title=headline,
        clean_text=headline,
        raw=None,
        content_hash=source_id,
        url="",
        level="",
        media_ids=[],
        extra=None,
        mentions=mentions,
    )
    assert status == "inserted"


def _seed_friday(tmp_path) -> None:
    """周五的具体事件。宽行业标签和周六闲聊都不能盖过这些标题。"""
    reset_store_for_tests(tmp_path / "news.sqlite")
    friday = date(2026, 10, 9)
    _insert_news(
        "cls", "ai-1", _publish(friday, 14, 52),
        ["人工智能", "昇腾"], ["600519.SH"],
        title="华为发布盘古新模型，昇腾链走强",
    )
    _insert_news(
        "wscn", "ai-2", _publish(friday, 14, 58),
        ["昇腾"], ["600519.SH"],
        title="华为发布盘古新模型",
    )
    _insert_news(
        "dws", "ai-3", _publish(friday, 15, 4),
        ["人工智能"], ["000001.SZ"],
        title="盘古新模型发布带动算力",
    )
    _insert_news(
        "cls", "nv-1", _publish(friday, 9, 0),
        ["人工智能"], [],
        title="英伟达追加订单传闻",
    )
    for index, minute in enumerate((10, 20, 30)):
        _insert_news(
            "cls", f"robot-{index}", _publish(friday, 11, minute),
            ["机器人", "工业机器人"],
            ["600519.SH"] if index == 0 else [],
            title="工信部出台机器人补贴政策",
        )
    _insert_news(
        "cls", "semi-1", _publish(friday, 14, 20),
        ["半导体", "存储芯片"], [],
        title="存储芯片厂宣布涨价",
    )
    _insert_news(
        "wscn", "semi-2", _publish(friday, 14, 40),
        ["半导体", "存储芯片"], [],
        title="存储芯片厂宣布涨价",
    )
    _insert_news(
        "cls", "ne-1", _publish(friday, 13, 40),
        ["新能源", "碳酸锂"], [],
        title="碳酸锂报价继续上涨",
    )
    _insert_news(
        "cls", "ne-2", _publish(friday, 13, 50),
        ["新能源", "碳酸锂"], [],
        title="碳酸锂报价继续上涨",
    )
    _insert_news(
        "wscn", "med-1", _publish(friday, 10, 10),
        ["医药", "创新药"], [],
        title="创新药临床获批",
    )
    _insert_news(
        "cls", "bank-1", _publish(friday, 9, 5),
        ["银行"], ["601318.SH"],
        title="平安银行回购股份",
    )
    _insert_news(
        "cls", "rare-1", _publish(friday, 8, 20),
        [], [],
        title="稀土出口配额收紧",
    )
    _insert_news(
        "cls", "pv-1", _publish(friday, 8, 0),
        [], [],
        title="光伏组件厂下调报价",
    )
    for index in range(4):
        _insert_news(
            "cls", f"sat-{index}", _publish(date(2026, 10, 10), 9, index),
            ["周六闲聊"], [],
            title="周六闲聊不影响交易日",
        )
    item_id = get_store()._conn.execute(
        "SELECT id FROM news_items WHERE source_id = 'ai-1'"
    ).fetchone()["id"]
    get_store()._conn.execute(
        """
        INSERT INTO news_mentions (item_id, kind, key, name, code, origin)
        VALUES (?, 'sector', '50', '50', '', 'text')
        """,
        (item_id,),
    )
    get_store()._conn.commit()


def _hot_market() -> list[dict]:
    symbols = ["600519.SH", "000001.SZ", "300001.SZ", "601318.SH", "600036.SH"]
    return [
        {"symbol": symbol, "name": symbol, "close": 10.0, "total_shares": 1e8, "market_cap": 10e8}
        for symbol in symbols
    ]


def _hot_dimensions() -> Dimensions:
    return Dimensions(concepts={
        "000001.SZ": ["人工智能"],
        "300001.SZ": ["昇腾"],
        "300002.SZ": ["昇腾"],
        "600519.SH": ["工业机器人"],
        "601318.SH": ["银行"],
        "999999.SZ": ["人工智能"],
    })


def test_top_hot_events_rank_trading_day_and_picker_uses_constituents(tmp_path, monkeypatch):
    _seed_friday(tmp_path)
    snapshot = top_hot_events(NOW)
    assert snapshot["trading_day"] == "2026-10-09"
    assert snapshot["as_of"] == "2026-10-09"
    assert snapshot["fallback"] is False
    assert snapshot["hint"] == "交易日 10月9日 · 更新于 15:04"
    names = [event["name"] for event in snapshot["events"]]
    assert names[:5] == [
        "华为发布盘古新模型",
        "存储芯片厂涨价",
        "工信部出台机器人补贴政策",
        "碳酸锂报价继续上涨",
        "创新药临床获批",
    ]
    assert "人工智能" not in names
    assert "半导体" not in names
    assert "周六闲聊不影响交易日" not in names
    assert "50" not in names
    assert "英伟达追加订单传闻" in names
    head = snapshot["events"][0]
    assert head["mentions"] == 3
    assert head["source_count"] == 3
    assert head["concepts"] == ["昇腾"]
    assert head["headline"] == "华为发布盘古新模型，昇腾链走强"
    assert head["first_seen"] == "10-09 14:52"
    assert [stock["key"] for stock in head["mentioned_stocks"]] == ["600519.SH", "000001.SZ"]
    assert head["mentioned_stocks"][0]["mentions"] == 2

    friday_afternoon = datetime(2026, 10, 9, 16, 0, tzinfo=CN_TZ)
    same_day = top_hot_events(friday_afternoon)
    assert same_day["fallback"] is False
    assert same_day["hint"] == "更新于 15:04"
    assert same_day["events"][0]["first_seen"] == "14:52"

    scores, _labels, note = map_selected_hot_event(head, Dimensions(), {"600519.SH", "000001.SZ"})
    assert note is not None and "只纳入资讯里提到的个股" in note
    assert set(scores) == {"600519.SH", "000001.SZ"}

    deps = _deps(
        tmp_path,
        load_market=lambda: (AS_OF, _hot_market()),
        load_dimensions=_hot_dimensions,
        load_top_events=load_top_events_from_news,
    )
    picked = run_picker([SourceSpec("hot_events", head["key"])], "or", FilterSpec(), deps)
    assert {row["symbol"] for row in picked["rows"]} == {
        "000001.SZ", "300001.SZ", "300002.SZ", "600519.SH",
    }
    assert "999999.SZ" not in {row["symbol"] for row in picked["rows"]}
    assert "601318.SH" not in {row["symbol"] for row in picked["rows"]}
    assert {row["event"] for row in picked["rows"]} == {"华为发布盘古新模型 · 3源"}
    assert picked["rows"][0]["score"] == 36.0
    assert picked["summary"]["hot_hint"] == "交易日 10月9日 · 更新于 15:04"

    robot = next(event for event in snapshot["events"] if event["name"].startswith("工信部"))
    overlapped = run_picker(
        [SourceSpec("hot_events", head["key"]), SourceSpec("hot_events", robot["key"])],
        "and",
        FilterSpec(),
        deps,
    )
    assert {row["symbol"] for row in overlapped["rows"]} == {"600519.SH"}

    bank = next(event for event in snapshot["events"] if event["name"] == "平安银行回购")
    assert names.index(bank["name"]) >= 5
    bought = run_picker([SourceSpec("hot_events", bank["key"])], "or", FilterSpec(), deps)
    assert [row["symbol"] for row in bought["rows"]] == ["601318.SH"]

    app = FastAPI()
    app.include_router(picker_api.router)
    monkeypatch.setattr(picker_api, "deps_from_app", lambda request, **kwargs: deps)
    client = TestClient(app)
    catalog = client.get("/api/picker/sources")
    assert catalog.status_code == 200
    group = next(item for item in catalog.json()["groups"] if item["id"] == "hot_events")
    assert [item["name"] for item in group["items"]] == names[:8]
    assert "光伏组件厂下调报价" not in {item["name"] for item in group["items"]}
    assert all("mentioned_stocks" not in item for item in group["items"])
    assert group["items"][0]["description"] == "提及 3 · 来源 3"
    assert group["items"][0]["concepts"] == ["昇腾"]
    assert group["items"][0]["headline"] == "华为发布盘古新模型，昇腾链走强"
    assert group["hint"] == "交易日 10月9日 · 更新于 15:04"
    assert group["fallback"] is False
    ran = client.post("/api/picker/run", json={
        "sources": [{"type": "hot_events", "id": head["key"]}],
        "combine": "or",
    })
    assert ran.status_code == 200
    assert {row["symbol"] for row in ran.json()["rows"]} == {
        "000001.SZ", "300001.SZ", "300002.SZ", "600519.SH",
    }
    rejected = client.post("/api/picker/run", json={
        "sources": [{"type": "hot_events", "id": "a/b"}],
        "combine": "or",
    })
    assert rejected.status_code == 422


def test_hot_events_fall_back_to_last_day_with_data(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    _insert_news(
        "cls", "thu-1", _publish(date(2026, 10, 8), 18, 12), ["白酒"], [],
        title="茅台批价上调",
    )
    snapshot = top_hot_events(NOW)
    assert snapshot["fallback"] is True
    assert snapshot["as_of"] == "2026-10-08"
    assert snapshot["trading_day"] == "2026-10-09"
    assert snapshot["hint"] == "暂无10月9日数据，显示10月8日 · 更新于 18:12"
    assert snapshot["events"][0]["first_seen"] == "10-08 18:12"
    assert snapshot["events"][0]["name"] == "茅台批价上调"
    assert snapshot["events"][0]["concepts"] == []

    reset_store_for_tests(tmp_path / "weekend.sqlite")
    _insert_news(
        "cls",
        "utc-1",
        datetime(2026, 10, 9, 16, 30, tzinfo=UTC),
        ["跨日"],
        [],
    )
    crossed = top_hot_events(NOW)
    assert crossed["as_of"] == "2026-10-10"
    assert crossed["fallback"] is True
    assert crossed["hint"].startswith("暂无10月9日数据，显示10月10日")

    reset_store_for_tests(tmp_path / "empty.sqlite")
    empty = top_hot_events(NOW)
    assert empty["events"] == []
    assert empty["fallback"] is False
    assert empty["hint"] is None
    assert empty["as_of"] is None


def test_hot_event_failure_does_not_drop_other_sources_but_true_miss_does(tmp_path):
    market = [{"symbol": "600519.SH", "name": "贵州茅台", "close": 10.0, "total_shares": 1e8, "market_cap": 10e8}]

    def strategies():
        return [{
            "id": "macd_golden", "name": "MACD", "source": "builtin", "tags": [],
            "asset_types": ["stock"], "timeframes": ["1d"],
        }]

    def hits(ids):
        del ids
        return {"macd_golden": ([{"symbol": "600519.SH", "score": 80}], None)}

    def boom(_now):
        raise RuntimeError("news down")

    deps = _deps(
        tmp_path,
        list_strategies=strategies,
        run_strategies=hits,
        load_market=lambda: (AS_OF, market),
        load_top_events=boom,
    )
    catalog = build_catalog(deps)
    hot = next(group for group in catalog["groups"] if group["id"] == "hot_events")
    assert hot["error"] == "热门事件暂时不可用"
    assert hot["items"] == []
    kept = run_picker(
        [SourceSpec("hot_events", "人工智能"), SourceSpec("technical", "macd_golden")],
        "and",
        FilterSpec(),
        deps,
    )
    assert [row["symbol"] for row in kept["rows"]] == ["600519.SH"]
    assert kept["summary"]["warnings"] == ["热门事件暂时不可用"]
    assert kept["summary"]["hot_hint"] is None

    unknown = _deps(
        tmp_path,
        list_strategies=strategies,
        run_strategies=hits,
        load_market=lambda: (AS_OF, market),
    )
    missed = run_picker(
        [SourceSpec("hot_events", "不存在"), SourceSpec("technical", "macd_golden")],
        "and",
        FilterSpec(),
        unknown,
    )
    assert [row["symbol"] for row in missed["rows"]] == ["600519.SH"]
    assert any("未识别" in item for item in missed["summary"]["warnings"])

    reset_store_for_tests(tmp_path / "miss.sqlite")
    _insert_news(
        "cls", "med-1", _publish(date(2026, 10, 9), 11, 1), ["医药"], [],
        title="医药板块午后走强",
    )
    missed_event = top_hot_events(NOW)["events"][0]
    assert missed_event["concepts"] == []
    true_miss = _deps(
        tmp_path,
        list_strategies=strategies,
        run_strategies=hits,
        load_market=lambda: (AS_OF, market),
        load_dimensions=lambda: Dimensions(concepts={"000001.SZ": ["医药"], "300001.SZ": ["医药"]}),
        load_top_events=load_top_events_from_news,
    )
    emptied = run_picker(
        [SourceSpec("hot_events", missed_event["key"]), SourceSpec("technical", "macd_golden")],
        "and",
        FilterSpec(),
        true_miss,
    )
    assert emptied["rows"] == []
    assert any("没有匹配到成分股" in item for item in emptied["summary"]["warnings"])


def test_hot_event_constituents_are_capped_by_market_cap():
    concepts = {f"{index:06d}.SZ": ["存储芯片"] for index in range(1, 36)}
    concepts["600000.SH"] = ["其他"]
    caps = {f"{index:06d}.SZ": float(index) for index in range(1, 36)}
    event = {
        "key": "ev_cap",
        "name": "存储芯片厂涨价",
        "concepts": ["存储芯片"],
        "mentions": 2,
        "source_count": 2,
        "mentioned_stocks": [{"key": "600000.SH", "name": "小市值", "mentions": 1, "source_count": 1}],
    }
    scores, _, note = map_selected_hot_event(
        event,
        Dimensions(concepts=concepts),
        set(concepts),
        market_caps=caps,
    )
    assert note is None
    assert "600000.SH" in scores
    assert len(scores) == 31
    assert "000035.SZ" in scores
    assert "000006.SZ" in scores
    assert "000005.SZ" not in scores


def test_picker_hot_event_keeps_funds_out_of_the_stock_list(monkeypatch):
    monkeypatch.setattr("app.news.service.known_asset_types", lambda: {})
    event = {
        "key": "ev_fund",
        "name": "沪深300ETF放量",
        "concepts": ["宽基"],
        "mentions": 2,
        "source_count": 1,
        "mentioned_stocks": [
            {"key": "600519.SH", "name": "贵州茅台", "mentions": 1},
            {"key": "510300.SH", "name": "沪深300ETF", "mentions": 2},
            {"key": "159915.SZ", "name": "创业板ETF易方达", "mentions": 1},
        ],
    }
    known = {"600519.SH", "510300.SH", "159915.SZ", "000001.SZ"}
    concepts = {"510300.SH": ["宽基"], "000001.SZ": ["宽基"], "600519.SH": ["白酒"]}
    scores, _, note = map_selected_hot_event(
        event,
        Dimensions(concepts=concepts),
        known,
    )
    assert note is None
    assert set(scores) == {"600519.SH", "000001.SZ"}

    monkeypatch.setattr("app.news.service.known_asset_types", lambda: {
        "510300.SH": "stock",
        "600519.SH": "etf",
    })
    scores, _, _ = map_selected_hot_event(event, Dimensions(concepts=concepts), known)
    assert "510300.SH" in scores
    assert "600519.SH" not in scores
    assert "159915.SZ" not in scores


def test_hot_events_do_not_merge_across_the_time_window(tmp_path):
    reset_store_for_tests(tmp_path / "window.sqlite")
    friday = date(2026, 10, 9)
    _insert_news(
        "cls", "early", _publish(friday, 9, 0), ["昇腾"], [],
        title="华为发布盘古新模型",
    )
    _insert_news(
        "wscn", "late", _publish(friday, 17, 30), ["昇腾"], [],
        title="华为发布盘古新模型",
    )
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert names.count("华为发布盘古新模型") == 2


def test_hot_event_recency_breaks_equal_item_source_products(tmp_path):
    reset_store_for_tests(tmp_path / "decay.sqlite")
    friday = date(2026, 10, 9)
    for index in range(4):
        _insert_news(
            "cls", f"old-{index}", _publish(friday, 8, index), ["创新药"], [],
            title="药监局出台创新药细则",
        )
    _insert_news(
        "cls", "new-1", _publish(friday, 15, 30), ["昇腾"], [],
        title="华为发布盘古新模型",
    )
    _insert_news(
        "wscn", "new-2", _publish(friday, 15, 40), ["昇腾"], [],
        title="华为发布盘古新模型",
    )
    noon = datetime(2026, 10, 9, 16, 0, tzinfo=CN_TZ)
    names = [event["name"] for event in top_hot_events(noon)["events"]]
    assert names[0] == "华为发布盘古新模型"


def test_hot_event_snapshot_is_cached_for_ten_minutes(tmp_path):
    reset_store_for_tests(tmp_path / "cache.sqlite")
    _insert_news(
        "cls", "one", _publish(date(2026, 10, 9), 11, 0), ["创新药"], [],
        title="创新药临床获批",
    )
    store = get_store()
    calls = {"n": 0}
    original = store.items_between

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    store.items_between = wrapped
    assert top_hot_events(NOW)["events"][0]["name"] == "创新药临床获批"
    assert top_hot_events(NOW)["events"][0]["name"] == "创新药临床获批"
    assert calls["n"] == 1
    assert top_hot_events(NOW + timedelta(minutes=11))["events"]
    assert calls["n"] == 2


def test_hot_event_llm_title_is_cached_and_keyword_title_remains_without_model(tmp_path, monkeypatch):
    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "llm.sqlite")
    friday = date(2026, 10, 9)
    _insert_news(
        "cls", "ai-1", _publish(friday, 14, 52), ["人工智能", "昇腾"], [],
        title="华为发布盘古新模型，昇腾链走强",
    )
    prompts = []

    def fake_text(prompt: str) -> str:
        prompts.append(prompt)
        return '{"title":"华为发布新模型","concepts":["昇腾","人工智能"],"stocks":[{"name":"不存在的公司","code":"999999"}]}'

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", fake_text)
    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    named = top_hot_events(NOW)
    assert named["events"][0]["name"] == "华为发布新模型"
    assert named["events"][0]["concepts"] == ["昇腾"]
    assert named["events"][0]["mentioned_stocks"] == []
    assert len(prompts) == 1
    assert "不超过20个字" in prompts[0]

    hot_mod.clear_hot_event_cache(llm=False)
    again = top_hot_events(NOW + timedelta(minutes=11))
    assert again["events"][0]["name"] == "华为发布新模型"
    assert len(prompts) == 1

    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: False)
    hot_mod.clear_hot_event_cache()
    fallback = top_hot_events(NOW + timedelta(minutes=22))
    assert fallback["events"][0]["name"] == "华为发布盘古新模型"
    assert len(prompts) == 1


def test_hot_event_llm_labels_at_most_eight(tmp_path, monkeypatch):
    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "llm-cap.sqlite")
    friday = date(2026, 10, 9)
    subjects = "甲乙丙丁戊己庚辛壬"
    objects = "子丑寅卯辰巳午未申"
    for index in range(9):
        _insert_news(
            "cls", f"cap-{index}", _publish(friday, 15, index), [], [],
            title=f"{subjects[index]}厂发布{objects[index]}材",
        )
    prompts: list[str] = []
    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", lambda prompt: prompts.append(prompt) or "{}")
    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    snapshot = top_hot_events(NOW)
    assert len(snapshot["events"]) == 9
    assert len(prompts) == 8
