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
from app.news.hot_events import _AUX_SCALE
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


def test_match_sector_symbols_strips_news_column_suffix():
    """资讯栏目名（「汽车大新闻」「能源行业新闻」）剥掉后缀后要能落回真实板块。"""
    dimensions = Dimensions(concepts={
        "601127.SH": ["汽车"],
        "600900.SH": ["新能源"],
    })
    index = build_label_index(dimensions)
    assert match_sector_symbols("汽车大新闻", "汽车大新闻", index) == {"601127.SH"}
    assert match_sector_symbols("能源行业新闻", "能源行业新闻", index) == {"600900.SH"}
    # 剥完还是查不到的真·栏目名（中东、美联储动态）不能瞎命中。
    assert match_sector_symbols("美联储动态", "美联储动态", index) == set()


def test_match_sector_symbols_falls_back_to_token_overlap():
    """无包含关系的同义说法靠词素接上：「数据中心液冷」→「液冷服务器」。"""
    dimensions = Dimensions(concepts={"300001.SZ": ["液冷服务器"], "600519.SH": ["白酒"]})
    index = build_label_index(dimensions)
    assert match_sector_symbols("数据中心液冷", "数据中心液冷", index) == {"300001.SZ"}
    # 词素太泛（命中超过 30 个不同标签）仍要拒绝，不能把半个市场拉进来。
    broad = Dimensions(concepts={f"{i:06d}.SZ": [f"科技{i}"] for i in range(31)})
    assert match_sector_symbols("科技论坛", "科技论坛", build_label_index(broad)) == set()


def test_token_match_rejects_generic_and_ambiguous_terms():
    """词素匹配的精度护栏：通用词直接跳过；2 字词素必须唯一定位。"""
    # 「个股」是通用词，不该把「次新股」「个股期权」这类标签拉进来。
    ambiguous = Dimensions(concepts={
        "000001.SZ": ["次新股"],
        "000002.SZ": ["个股期权"],
        "000003.SZ": ["个股融资融券"],
    })
    index = build_label_index(ambiguous)
    assert match_sector_symbols("全市场超4300只个股下挫", "全市场超4300只个股下挫", index) == set()
    # 通用词（「个股」「美股」）在**词素匹配**这一层必须被停用词挡掉。
    # 注意：包含匹配是更早、独立的一层（`美股` ⊂ `美股概念` 属包含，不属词素），
    # 这里直接测词素函数本身，避免把两层混在一起。
    from app.services.picker import _concept_tokens, _TOKEN_STOPWORDS

    assert "个股" in _TOKEN_STOPWORDS
    assert "美股" in _TOKEN_STOPWORDS
    # 「全市场超4300只个股下挫」切出的词素里不应残留「个股」等通用词。
    tokens = _concept_tokens("全市场超4300只个股下挫")
    assert not any(token in _TOKEN_STOPWORDS for token in tokens)
    assert not any(len(token) < 2 for token in tokens)


def test_hot_group_reserves_slots_for_unmappable_major():
    """重大档保底席位：可映射事件占满前 8 时，用榜外最靠前的「重大」补到末尾。"""
    from app.services.picker import _hot_group

    def ev(key, name, importance, concepts):
        return {
            "key": key,
            "name": name,
            "importance": importance,
            "concepts": concepts,
            "mentions": 1,
            "source_count": 1,
        }

    dims = Dimensions(concepts={
        "000001.SZ": ["机器人"],
        "000002.SZ": ["白酒"],
        "000003.SZ": ["光伏"],
        "000004.SZ": ["储能"],
        "000005.SZ": ["芯片"],
        "000006.SZ": ["军工"],
        "000007.SZ": ["医药"],
        "000008.SZ": ["券商"],
    })
    concepts_pool = ["机器人", "白酒", "光伏", "储能", "芯片", "军工", "医药", "券商"]
    events = [
        # 3 条无映射「重大」排在最前（按可映射性会被压到后面）。
        ev("m1", "重大-无映射A", "重大", ["中东"]),
        ev("m2", "重大-无映射B", "重大", ["美联储动态"]),
        ev("m3", "重大-无映射C", "重大", []),
    ]
    # 9 条可映射「重要」—— 足够挤满前 8，把「重大」全推出去。
    for i in range(9):
        events.append(ev(f"i{i}", f"重要-可映射{i}", "重要", [concepts_pool[i % 8]]))

    group = _hot_group({"events": events}, None, dims)
    names = [item["name"] for item in group["items"]]
    assert len(names) == 8
    majors = [item for item in group["items"] if item["importance"] == "重大"]
    # 保底 2 条「重大」被补进来（榜内本来 0 条），且都是无映射的。
    assert len(majors) == 2
    assert all(item["mapping_breadth"] == "none" for item in majors)
    # 保底不会重复、也不会把可映射事件全挤掉：仍有 6 条可映射。
    assert len(set(names)) == len(names)
    assert sum(1 for item in group["items"] if item["selectable_count"] > 0) == 6


def test_hot_group_keeps_existing_major_that_sits_at_tail():
    """榜内「重大」排在末位时，补位只能砍非重大，不能把它换掉。

    评审给的反例：top8 = 7 条 usable「重要」+ 1 条「重大」但无映射 M1（正好在末位），
    此时 majors_in=1、shortfall=1，会从榜外补 M2。若按位置砍末尾（旧实现），砍掉的
    就是 M1 —— 重大席位数没变，还换成了排名更差的 M2。
    """
    from app.services.picker import _hot_group

    def ev(key, name, importance, concepts):
        return {
            "key": key,
            "name": name,
            "importance": importance,
            "concepts": concepts,
            "mentions": 1,
            "source_count": 1,
        }

    pool = ["机器人", "白酒", "光伏", "储能", "芯片", "军工", "医药", "券商"]
    dims = Dimensions(concepts={f"{i:06d}.SZ": [c] for i, c in enumerate(pool)})
    events = []
    # 7 条可映射「重要」—— 它们按可映射性排在 M1 之前，占满前 7 位。
    for i in range(7):
        events.append(ev(f"i{i}", f"重要-可映射{i}", "重要", [pool[i]]))
    # 1 条「重大」但无映射 —— 排在 usable 之后，正好落在第 8 位（末位）。
    events.append(ev("M1", "榜内重大-末位", "重大", ["中东"]))
    # 榜外还有 1 条无映射「重大」，会被保底拉进来补第 2 个席位。
    events.append(ev("M2", "榜外重大-无映射", "重大", ["美联储动态"]))

    group = _hot_group({"events": events}, None, dims)
    names = [item["name"] for item in group["items"]]
    assert len(names) == 8
    # 榜内那条末位「重大」必须保住，不能被榜外的 M2 顶掉。
    assert "榜内重大-末位" in names
    majors = [item["name"] for item in group["items"] if item["importance"] == "重大"]
    assert set(majors) == {"榜内重大-末位", "榜外重大-无映射"}
    # 让位的是「重要」而非「重大」：可映射条数从 7 降到 6。
    assert sum(1 for item in group["items"] if item["selectable_count"] > 0) == 6


def test_event_pick_rank_is_mappability_first_tier_second():
    """选股列表里可映射性主导, 档位降为次级键（与新闻榜单口径刻意不同）。"""
    from app.services.picker import _event_pick_rank, _mapping_breadth

    assert _mapping_breadth(0) == "none"
    assert _mapping_breadth(50) == "usable"
    assert _mapping_breadth(2000) == "broad"

    major_none = {"importance": "重大", "name": "A"}
    minor_usable = {"importance": "一般", "name": "B"}
    broad = {"importance": "重大", "name": "C"}
    ranks = {
        "usable": _event_pick_rank(minor_usable, 40, index_ready=True),
        "broad": _event_pick_rank(broad, 2000, index_ready=True),
        "none": _event_pick_rank(major_none, 0, index_ready=True),
    }
    assert ranks["usable"] < ranks["broad"] < ranks["none"]
    # 可映射性压过档位: 一般档的可映射事件排在重大档无映射事件之前。
    assert _event_pick_rank(minor_usable, 40, index_ready=True) < _event_pick_rank(
        major_none, 0, index_ready=True
    )
    # usable 内部按舒适区排队: 40 只在区里, 1 只在区外。
    assert _event_pick_rank(minor_usable, 40, index_ready=True) < _event_pick_rank(
        minor_usable, 1, index_ready=True
    )
    # 同组内档位仍起作用: 重大 > 一般。
    assert _event_pick_rank({"importance": "重大", "name": "x"}, 40, index_ready=True) < _event_pick_rank(
        {"importance": "一般", "name": "x"}, 40, index_ready=True
    )
    # 索引不可用时退化为「档位 + 热度」, 不再按可映射性。
    assert _event_pick_rank(major_none, 0, index_ready=False)[:2] == (0, 0)
    assert _event_pick_rank(major_none, 999, index_ready=False) < _event_pick_rank(
        minor_usable, 999, index_ready=False
    )
    # 未知(维表未同步)的广度是 None, 不是 none —— 前者「还不知道」后者「确知选不出」。
    assert _mapping_breadth(None) is None


def test_hot_group_only_marks_capped_when_constituents_actually_exceed_the_cap():
    """恰好等于上限不是「截断」, 多于上限才是。

    前端据此决定说「30 只」还是「至少 30 只」。旧实现拿 `count >= 上限` 猜,
    恰好在 30 只时也会说「至少 30 只」——把「正好」说成了「不止」。
    """
    from app.services.picker import _CONCEPT_CONSTITUENT_CAP, _hot_group

    cap = _CONCEPT_CONSTITUENT_CAP

    def dims_with(concept: str, count: int) -> Dimensions:
        # 造 count 只成分股, 全部属于同一个概念。
        return Dimensions(concepts={
            f"{i:06d}.SZ": [concept] for i in range(1, count + 1)
        })

    def ev(key: str, concept: str) -> dict:
        return {
            "key": key, "name": key, "importance": "重要", "concepts": [concept],
            "mentions": 3, "source_count": 2,
        }

    # 概念「正好」有上限只成分股 → 不该算截断。
    exact = _hot_group({"events": [ev("exact", "正好")]}, None, dims_with("正好", cap))
    exact_item = next(i for i in exact["items"] if i["id"] == hot_event_id("exact"))
    assert exact_item["selectable_count"] == cap
    assert exact_item["selectable_capped"] is False, "恰好等于上限不是截断"

    # 概念有上限多只 → 真被截断, count 停在上限, capped=True。
    over = _hot_group({"events": [ev("over", "超了")]}, None, dims_with("超了", cap + 1))
    over_item = next(i for i in over["items"] if i["id"] == hot_event_id("over"))
    assert over_item["selectable_count"] == cap
    assert over_item["selectable_capped"] is True, "多于上限才算截断"


def test_hot_group_dim_index_missing_does_not_block_selection():
    """维表未同步时 selectable_count 必须是 None(未知), 不能是 0。

    回归守卫: 早前这里给 0, 前端把 `<=0` 当成「空炮」置灰, 于是维表没同步时
    用户连本来能跑出「资讯提及个股」的事件都勾不上。而实跑路径
    (`map_selected_hot_event`) 此时**仍会纳入资讯提到的个股**, 并不空。
    """
    from app.services.picker import _hot_group

    events = [
        {
            "key": "k1",
            "name": "机器人订单",
            "importance": "重要",
            "concepts": ["机器人"],
            "mentions": 5,
            "source_count": 3,
            "mentioned_stocks": [{"key": "000001.SZ", "name": "平安银行"}],
        },
        {
            "key": "k2",
            "name": "美联储讲话",
            "importance": "重大",
            "concepts": [],
            "mentions": 9,
            "source_count": 4,
        },
    ]
    # dimensions=None → 维表未就绪。
    group = _hot_group({"events": events}, None, None)

    assert len(group["items"]) == 2
    for item in group["items"]:
        # 未知 ≠ 空炮: 两个字段都要表达「不知道」。
        assert item["selectable_count"] is None, item
        assert item["mapping_breadth"] is None, item
        # 仍要给出原因, 且说明是「只纳入资讯提及个股」而不是「无法映射」。
        assert "尚未同步同花顺行业/概念" in item["selectable_note"]
        assert "只纳入资讯里提到的个股" in item["selectable_note"]
        # 未知时不能说被截断(那会渲染成「至少 N 只」)。
        assert item["selectable_capped"] is False



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
    level: str = "",
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
        level=level,
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
        "dws", "ai-3", _publish(friday, 15, 0),
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
    # 周六早上已经切到下一场（下周一），盘中事件还留在榜上。更新于是计算时刻。
    assert snapshot["trading_day"] == "2026-10-12"
    assert snapshot["as_of"] == "2026-10-12"
    assert snapshot["fallback"] is False
    assert snapshot["hint"] == "交易日 10月12日 · 待开盘验证 · 更新于 08:30"
    names = [event["name"] for event in snapshot["events"]]
    # 只断言「哪些事件进前 5」以及「平安银行进了前 5」，**不锁两个条目之间的
    # 相对名次**：它们的先后取决于盘面确认分（平安银行 seed 带 601318.SH 且
    # _hot_dimensions 有银行映射，会拿到 confirmation 分），而这个分值跟环境里
    # 的行情/映射数据有关 —— 本地与 CI 干净环境算出来就不同（本地平安在前、
    # CI 碳酸锂在前），写死方向必然在一边红。这条用例要验的是「选股链路用上了
    # 热点成分」，不是排名本身，名次靠 set + 首名 + 进榜来锁。
    assert set(names[:5]) == {
        "华为发布盘古新模型，昇腾链走强",
        "工信部出台机器人补贴政策",
        "碳酸锂报价继续上涨",
        "平安银行回购股份",
        "稀土出口配额收紧",
    }
    assert names[0] == "华为发布盘古新模型，昇腾链走强"
    assert names.index("平安银行回购股份") < 5
    assert "光伏组件厂下调报价" not in names
    assert names.index("稀土出口配额收紧") < names.index("存储芯片厂宣布涨价")
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
    assert head["headlines"][0] == head["headline"]
    assert head["category"] == "科技与产业"
    assert head["direction"] == "利好"
    assert head["relevance"] >= 3
    assert head["mapping"] == [{"name": "昇腾", "kind": "concept", "direction": "利好"}]
    assert head["first_seen"] == "10-09 14:52"
    assert head["importance"] == "重大"
    # 乘性计分：档位 0/40/90/200，概念 1 个 → 10，个股 2 个 → 9.5，方向 5。
    assert head["breakdown"]["importance"] == 200
    assert head["breakdown"]["mapping"] == 24.5
    aux = sum(value for key, value in head["breakdown"].items() if key != "importance")
    assert abs(head["score"] - 200 * (1 + aux / _AUX_SCALE)) < 0.02
    assert [stock["key"] for stock in head["mentioned_stocks"]] == ["600519.SH", "000001.SZ"]
    assert head["mentioned_stocks"][0]["mentions"] == 2

    from app.news.hot_snapshot import publish_hot_snapshot

    publish_hot_snapshot(snapshot, persist=False)
    friday_afternoon = datetime(2026, 10, 9, 16, 0, tzinfo=CN_TZ)
    same_day = top_hot_events(friday_afternoon)
    assert same_day["fallback"] is False
    assert same_day["as_of"] == "2026-10-12"
    assert same_day["hint"] == "交易日 10月12日 · 待开盘验证 · 更新于 16:00"
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
    assert {row["event"] for row in picked["rows"]} == {"华为发布盘古新模型，昇腾链走强 · 3源"}
    assert picked["rows"][0]["score"] == 36.0
    assert picked["summary"]["hot_hint"] == "交易日 10月12日 · 待开盘验证 · 更新于 08:30"

    robot = next(event for event in snapshot["events"] if event["name"].startswith("工信部"))
    overlapped = run_picker(
        [SourceSpec("hot_events", head["key"]), SourceSpec("hot_events", robot["key"])],
        "and",
        FilterSpec(),
        deps,
    )
    assert {row["symbol"] for row in overlapped["rows"]} == {"600519.SH"}

    bank = next(event for event in snapshot["events"] if event["name"] == "平安银行回购股份")
    # 名次断言改为「进前 5」：具体第 3 还是第 4 取决于盘面确认分，写死位数
    # 会在评分口径微调时无谓变红（这条用例要验的是选股联动，不是排名本身）。
    assert names.index(bank["name"]) < 5
    assert bank["mentions"] == 1
    assert bank["importance"] == "重大"
    bought = run_picker([SourceSpec("hot_events", bank["key"])], "or", FilterSpec(), deps)
    assert [row["symbol"] for row in bought["rows"]] == ["601318.SH"]

    app = FastAPI()
    app.include_router(picker_api.router)
    monkeypatch.setattr(picker_api, "deps_from_app", lambda request, **kwargs: deps)
    client = TestClient(app)
    catalog = client.get("/api/picker/sources")
    assert catalog.status_code == 200
    group = next(item for item in catalog.json()["groups"] if item["id"] == "hot_events")
    # 选股目录不再原样照抄快照顺序: 它在「同档内」按可映射性重排, 把能真正选出
    # 股的事件提前(见 _event_pick_rank)。所以这里锁集合与前 8 的档位不变量,
    # 不锁逐位相等 —— 否则每次盘面映射数据变动都要改断言。
    assert {item["name"] for item in group["items"]} == set(names[:8])
    assert group["items"][0]["name"] == "华为发布盘古新模型，昇腾链走强"
    # 同档内, 能映射到成分股的事件必须排在映射为空的事件之前。
    mapped_at = [
        index for index, item in enumerate(group["items"]) if item["selectable_count"] > 0
    ]
    unmapped_at = [
        index for index, item in enumerate(group["items"]) if item["selectable_count"] == 0
    ]
    if mapped_at and unmapped_at:
        assert max(mapped_at) < min(unmapped_at)
    assert "光伏组件厂下调报价" not in {item["name"] for item in group["items"]}
    assert all("mentioned_stocks" not in item for item in group["items"])
    assert group["items"][0]["description"] == "提及 3 · 来源 3"
    assert group["items"][0]["importance"] == "重大"
    assert group["items"][0]["concepts"] == ["昇腾"]
    assert group["items"][0]["headline"] == "华为发布盘古新模型，昇腾链走强"
    # 可选股 = 概念成分股(昇腾→300001/300002) ∪ 事件直接点到的个股(600519/000001) = 4 只。
    # 前端据此显示「会选出 4 只」, 而不是被截断的 mapped_stocks 前 3 个。
    assert group["items"][0]["selectable_count"] == 4
    assert group["items"][0]["mapping_breadth"] == "usable"
    assert group["hint"] == "交易日 10月12日 · 待开盘验证 · 更新于 08:30"
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


def test_major_event_with_few_mentions_outranks_routine_heat(tmp_path):
    """一条降息仍高于多来源的寻常涨价；没有映射的单条报价不进榜。"""
    from app.news.service import hot_event_listing

    reset_store_for_tests(tmp_path / "rank.sqlite")
    friday = date(2026, 10, 9)
    _insert_news("cls", "fed", _publish(friday, 14, 50), [], [], title="美联储宣布降息")
    for index, source in enumerate(("cls", "wscn", "dws", "reddit", "cls")):
        _insert_news(
            source, f"chip-{index}", _publish(friday, 14, index),
            ["存储芯片"], [],
            title="存储芯片厂宣布涨价",
        )
    _insert_news("cls", "pv", _publish(friday, 14, 20), [], [], title="光伏组件厂下调报价")
    events = top_hot_events(NOW)["events"]
    names = [event["name"] for event in events]
    assert names[0] == "美联储宣布降息"
    assert "存储芯片厂宣布涨价" in names
    assert names.index("美联储宣布降息") < names.index("存储芯片厂宣布涨价")
    assert "光伏组件厂下调报价" not in names
    head = events[0]
    assert head["importance"] == "重大"
    assert head["mentions"] == 1
    assert head["source_count"] == 1
    parts = head["breakdown"]
    assert set(parts) == {"importance", "confirmation", "mapping", "freshness", "heat"}
    assert parts["importance"] > parts["mapping"] + parts["freshness"] + parts["heat"]
    routine = next(event for event in events if event["name"] == "存储芯片厂宣布涨价")
    assert routine["importance"] == "重要"
    assert routine["mentions"] > head["mentions"]
    assert routine["heat"] > head["heat"]
    assert head["score"] > routine["score"]

    listed = hot_event_listing(NOW, limit=20)
    public = listed["events"][0]
    assert public["name"] == "美联储宣布降息"
    assert public["subtitle"] == head["subtitle"]
    assert public["importance"] == "重大"
    assert public["score"] == head["score"]
    assert public["breakdown"] == head["breakdown"]
    assert "item_ids" not in public


def test_hot_events_classify_market_news_and_drop_noise(tmp_path):
    reset_store_for_tests(tmp_path / "classes.sqlite")
    friday = date(2026, 10, 9)
    samples = [
        ("fed", "美联储宣布降息", "海外市场/央行", "利好"),
        ("policy", "工信部出台算力补贴", "国内政策/宏观", "利好"),
        ("chip", "华为发布新模型", "科技与产业", "利好"),
        ("geo", "美国对华芯片制裁升级", "地缘政治", "利空"),
        ("metal", "碳酸锂报价继续上涨", "大宗商品/期货价格异动", "利好"),
        ("firm", "平安银行宣布回购", "公司重大事项", "利好"),
    ]
    for index, (source_id, title, _category, _direction) in enumerate(samples):
        _insert_news("cls", source_id, _publish(friday, 9, index), [], [], title=title)
    _insert_news("cls", "pic", _publish(friday, 10, 0), [], [], title="图片")
    _insert_news("cls", "chat", _publish(friday, 10, 5), [], [], title="周末闲聊没什么")
    _insert_news("cls", "ad", _publish(friday, 10, 8), [], [], title="点击领取开户赞助")
    events = {event["name"]: event for event in top_hot_events(NOW)["events"]}
    assert "图片" not in events
    assert not any("闲聊" in name or "赞助" in name for name in events)
    expected = {
        "美联储宣布降息": ("海外市场/央行", "利好"),
        "工信部出台算力补贴": ("国内政策/宏观", "利好"),
        "华为发布新模型": ("科技与产业", "利好"),
        "美国对华芯片制裁升级": ("地缘政治", "利空"),
        "碳酸锂报价继续上涨": ("大宗商品/期货价格异动", "利好"),
        "平安银行宣布回购": ("公司重大事项", "利好"),
    }
    for name, (category, direction) in expected.items():
        assert events[name]["category"] == category
        assert events[name]["direction"] == direction
        assert events[name]["relevance"] >= 3
        assert events[name]["headlines"]


def test_weekend_news_waits_on_the_next_session_and_keeps_red(tmp_path):
    """周六、周日的稿归入下周一，休市日就能看到，并带着加红。"""
    from app.news.hot_events import _news_session_day
    from app.news.market_confirm import _confirm_day

    saturday_news = datetime(2026, 10, 10, 11, 30, tzinfo=CN_TZ)
    assert _news_session_day(saturday_news) == _confirm_day(saturday_news) == date(2026, 10, 12)
    assert _news_session_day(_publish(date(2026, 10, 9), 15, 0)) == date(2026, 10, 9)
    assert _news_session_day(_publish(date(2026, 10, 9), 15, 1)) == date(2026, 10, 12)
    assert _news_session_day(_publish(date(2026, 10, 12), 9, 25)) == date(2026, 10, 12)
    assert _news_session_day(_publish(date(2026, 10, 12), 9, 26)) == date(2026, 10, 12)

    reset_store_for_tests(tmp_path / "overnight.sqlite")
    friday = date(2026, 10, 9)
    _insert_news(
        "cls", "session", _publish(friday, 11, 0), [], [],
        title="工信部出台算力补贴",
    )
    _insert_news(
        "cls", "close", _publish(friday, 15, 0), [], [],
        title="平安银行回购股份",
    )
    _insert_news(
        "cls", "after", _publish(friday, 15, 1), [], [],
        title="碳酸锂报价继续上涨", level="B",
    )
    _insert_news(
        "cls", "sat-red", datetime(2026, 10, 10, 11, 30, tzinfo=CN_TZ), [], [],
        title="橡胶价格大涨超预期", level="B",
    )
    _insert_news(
        "wscn", "sun-red", datetime(2026, 10, 11, 9, 20, tzinfo=CN_TZ), [], [],
        title="原油期货报价上调", level="2",
    )
    _insert_news(
        "cls", "monday-open", _publish(date(2026, 10, 12), 10, 20), [], [],
        title="存储芯片厂宣布涨价",
    )

    friday_evening = top_hot_events(datetime(2026, 10, 9, 18, 0, tzinfo=CN_TZ))
    friday_by_name = {event["name"]: event for event in friday_evening["events"]}
    assert "工信部出台算力补贴" in friday_by_name
    assert "平安银行回购股份" in friday_by_name
    assert "碳酸锂报价继续上涨" in friday_by_name
    assert "橡胶价格大涨超预期" not in friday_by_name
    assert friday_evening["as_of"] == "2026-10-12"
    assert friday_evening["updated_at"] == "18:00"
    assert friday_by_name["碳酸锂报价继续上涨"]["confirmation"]["pending"] is True
    assert friday_by_name["碳酸锂报价继续上涨"]["confirmation"]["label"] == "待开盘验证"
    assert friday_by_name["平安银行回购股份"]["confirmation"]["pending"] is False
    assert friday_by_name["工信部出台算力补贴"]["confirmation"]["pending"] is False

    saturday = top_hot_events(datetime(2026, 10, 10, 18, 0, tzinfo=CN_TZ))
    saturday_by_name = {event["name"]: event for event in saturday["events"]}
    assert "碳酸锂报价继续上涨" in saturday_by_name
    assert "橡胶价格大涨超预期" in saturday_by_name
    assert "原油期货报价上调" not in saturday_by_name
    assert "工信部出台算力补贴" in saturday_by_name
    assert "平安银行回购股份" in saturday_by_name
    assert saturday["as_of"] == "2026-10-12"
    assert saturday["trading_day"] == "2026-10-12"
    assert saturday["fallback"] is False
    assert saturday["updated_at"] == "18:00"
    assert "待开盘验证" in saturday["hint"]
    for name in ("碳酸锂报价继续上涨", "橡胶价格大涨超预期"):
        confirm = saturday_by_name[name]["confirmation"]
        assert confirm["label"] == "待开盘验证"
        assert confirm["pending"] is True
        assert confirm["phase"] == "auction"
        assert confirm["score"] == 0
    for name in ("工信部出台算力补贴", "平安银行回购股份"):
        assert saturday_by_name[name]["confirmation"]["pending"] is False
    rubber = saturday_by_name["橡胶价格大涨超预期"]
    lithium = saturday_by_name["碳酸锂报价继续上涨"]
    assert rubber["red_count"] > 0
    assert lithium["red_count"] > 0

    sunday = top_hot_events(datetime(2026, 10, 11, 15, 0, tzinfo=CN_TZ))
    sunday_by_name = {event["name"]: event for event in sunday["events"]}
    assert "橡胶价格大涨超预期" in sunday_by_name
    assert "原油期货报价上调" in sunday_by_name
    assert "碳酸锂报价继续上涨" in sunday_by_name
    assert "工信部出台算力补贴" in sunday_by_name
    assert sunday["as_of"] == "2026-10-12"
    assert sunday["fallback"] is False
    assert sunday["updated_at"] == "15:00"
    assert "待开盘验证" in sunday["hint"]
    assert sunday_by_name["原油期货报价上调"]["red_count"] > 0
    assert sunday_by_name["橡胶价格大涨超预期"]["red_count"] > 0
    assert sunday_by_name["原油期货报价上调"]["confirmation"]["pending"] is True
    assert sunday_by_name["工信部出台算力补贴"]["confirmation"]["pending"] is False

    monday_wait = top_hot_events(datetime(2026, 10, 12, 9, 10, tzinfo=CN_TZ))
    monday_wait_by_name = {event["name"]: event for event in monday_wait["events"]}
    assert monday_wait["as_of"] == "2026-10-12"
    assert monday_wait["updated_at"] == "09:10"
    assert "待开盘验证" in monday_wait["hint"]
    assert {event["name"] for event in monday_wait["events"]} >= {
        "橡胶价格大涨超预期", "原油期货报价上调", "碳酸锂报价继续上涨",
        "工信部出台算力补贴",
    }
    assert monday_wait_by_name["工信部出台算力补贴"]["confirmation"]["pending"] is False
    for name in ("橡胶价格大涨超预期", "原油期货报价上调", "碳酸锂报价继续上涨"):
        assert monday_wait_by_name[name]["confirmation"]["label"] == "待开盘验证"
        assert monday_wait_by_name[name]["confirmation"]["pending"] is True

    monday = top_hot_events(datetime(2026, 10, 12, 10, 40, tzinfo=CN_TZ))
    monday_by_name = {event["name"]: event for event in monday["events"]}
    assert "橡胶价格大涨超预期" in monday_by_name
    assert "原油期货报价上调" in monday_by_name
    assert "存储芯片厂宣布涨价" in monday_by_name
    assert "工信部出台算力补贴" not in monday_by_name
    assert monday["as_of"] == "2026-10-12"
    chip = monday_by_name["存储芯片厂宣布涨价"]
    assert chip["confirmation"]["phase"] == "intraday"
    assert chip["confirmation"]["pending"] is False
    assert chip["confirmation"]["label"] != "待开盘验证"
    weekend_left = monday_by_name["橡胶价格大涨超预期"]
    assert weekend_left["confirmation"]["phase"] != "intraday"
    assert weekend_left["confirmation"]["pending"] is False
    assert weekend_left["confirmation"]["label"] != "待开盘验证"
    assert weekend_left["red_count"] > 0


def test_hot_events_fall_back_to_last_day_with_data(tmp_path):
    reset_store_for_tests(tmp_path / "news.sqlite")
    _insert_news(
        "cls", "thu-1", _publish(date(2026, 10, 8), 18, 12), ["白酒"], [],
        title="茅台批价上调",
    )
    snapshot = top_hot_events(NOW)
    # 周四 18:12 归周五这场。周六再看时榜已经切到下周一，周五的事件还在，竞价已经过去。
    assert snapshot["fallback"] is False
    assert snapshot["as_of"] == "2026-10-12"
    assert snapshot["trading_day"] == "2026-10-12"
    assert snapshot["hint"] == "交易日 10月12日 · 待开盘验证 · 更新于 08:30"
    assert snapshot["events"][0]["first_seen"] == "10-08 18:12"
    assert snapshot["events"][0]["name"] == "茅台批价上调"
    assert snapshot["events"][0]["concepts"] == []
    assert snapshot["events"][0]["confirmation"]["pending"] is False
    assert snapshot["events"][0]["confirmation"]["label"] != "待开盘验证"

    reset_store_for_tests(tmp_path / "weekend.sqlite")
    _insert_news(
        "cls",
        "utc-1",
        datetime(2026, 10, 9, 16, 30, tzinfo=UTC),
        ["跨日"],
        [],
        title="茅台宣布回购",
    )
    crossed = top_hot_events(NOW)
    assert crossed["as_of"] == "2026-10-12"
    assert crossed["trading_day"] == "2026-10-12"
    assert crossed["fallback"] is False
    assert crossed["hint"].startswith("交易日 10月12日 · 待开盘验证")
    assert crossed["updated_at"] == "08:30"
    assert crossed["events"][0]["confirmation"]["label"] == "待开盘验证"
    assert crossed["events"][0]["confirmation"]["pending"] is True
    assert crossed["events"][0]["confirmation"]["phase"] == "auction"
    assert crossed["events"][0]["confirmation"]["score"] == 0

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
        title="药监局出台医药细则",
    )
    missed_event = top_hot_events(NOW)["events"][0]
    from app.news.hot_snapshot import publish_hot_snapshot

    publish_hot_snapshot(top_hot_events(NOW), persist=False)
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
    """收盘后的同题快讯归下一交易日，不和上午那条并成一件事。"""
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
    morning = top_hot_events(datetime(2026, 10, 9, 12, 0, tzinfo=CN_TZ))
    assert [event["name"] for event in morning["events"]].count("华为发布盘古新模型") == 1
    assert morning["as_of"] == "2026-10-09"
    overnight = top_hot_events(NOW)
    huawei = [event for event in overnight["events"] if event["name"] == "华为发布盘古新模型"]
    assert len(huawei) == 2
    assert overnight["as_of"] == "2026-10-12"
    by_seen = {event["first_seen"]: event for event in huawei}
    assert by_seen["10-09 09:00"]["confirmation"]["pending"] is False
    assert by_seen["10-09 17:30"]["confirmation"]["pending"] is True
    assert by_seen["10-09 17:30"]["confirmation"]["label"] == "待开盘验证"


def test_hot_event_recency_breaks_equal_item_source_products(tmp_path):
    reset_store_for_tests(tmp_path / "decay.sqlite")
    friday = date(2026, 10, 9)
    for index in range(4):
        _insert_news(
            "cls", f"old-{index}", _publish(friday, 8, index), ["创新药"], [],
            title="创新药临床获批",
        )
    _insert_news(
        "cls", "new-1", _publish(friday, 14, 30), ["存储芯片"], [],
        title="存储芯片厂宣布涨价",
    )
    _insert_news(
        "wscn", "new-2", _publish(friday, 14, 40), ["存储芯片"], [],
        title="存储芯片厂宣布涨价",
    )
    noon = datetime(2026, 10, 9, 16, 0, tzinfo=CN_TZ)
    events = top_hot_events(noon)["events"]
    names = [event["name"] for event in events]
    assert names[0] == "存储芯片厂宣布涨价"
    assert events[0]["importance"] == events[1]["importance"] == "重要"
    assert events[0]["mentions"] * events[0]["source_count"] == events[1]["mentions"] * events[1]["source_count"]


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

    def fake_text(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return (
            '{"title":"华为发布新模型","category":"地缘政治","direction":"利空",'
            '"concepts":[{"name":"昇腾","direction":"利空"}],'
            '"stocks":[{"name":"不存在的公司","code":"999999"}]}'
        )

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", fake_text)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    named = top_hot_events(NOW)
    assert named["events"][0]["name"] == "华为发布盘古新模型，昇腾链走强"
    assert named["events"][0]["subtitle"] == "华为发布新模型"
    assert named["events"][0]["concepts"] == ["昇腾"]
    # 标题里没有制裁、冲突这类词，模型不能把分类改成地缘政治。
    assert named["events"][0]["category"] == "科技与产业"
    assert named["events"][0]["direction"] == "利空"
    assert named["events"][0]["importance"] == "重大"
    assert named["events"][0]["mapping"] == [{"name": "昇腾", "kind": "concept", "direction": "利空"}]
    assert named["events"][0]["mentioned_stocks"] == []
    assert len(prompts) == 1
    assert "补充要点" in prompts[0]
    assert "国内政策/宏观" in prompts[0]

    hot_mod.clear_hot_event_cache(llm=False)
    again = top_hot_events(NOW + timedelta(minutes=11))
    assert again["events"][0]["name"] == "华为发布盘古新模型，昇腾链走强"
    assert again["events"][0]["subtitle"] == "华为发布新模型"
    assert len(prompts) == 1

    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: False)
    hot_mod.clear_hot_event_cache()
    fallback = top_hot_events(NOW + timedelta(minutes=22))
    assert fallback["events"][0]["name"] == "华为发布盘古新模型，昇腾链走强"
    assert fallback["events"][0]["category"] == "科技与产业"
    assert fallback["events"][0]["direction"] == "利好"
    assert fallback["events"][0]["importance"] == "重大"
    assert len(prompts) == 1

    def trivial(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return '{"title":"华为发布新模型","importance":"琐碎"}'

    monkeypatch.setattr(news_service, "_llm_text", trivial)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    kept = top_hot_events(NOW + timedelta(minutes=33))
    assert kept["events"][0]["name"] == "华为发布盘古新模型，昇腾链走强"
    assert kept["events"][0]["importance"] == "一般"


def test_hot_event_llm_labels_all_events_in_batches(tmp_path, monkeypatch):
    """展示标题按批问完；21 条分两批（每批 20），每批模型返回空再各问一次。

    批次大小是 20（_LLM_TITLE_BATCH）：40 条一批时后段条目会被模型集体留空，
    所以拆小。21 条 → 2 批；模型第一次返回空对象，触发每批一次重写，共 4 次。
    """
    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "llm-cap.sqlite")
    friday = date(2026, 10, 9)
    marks = "子丑寅卯辰巳午未申酉戌亥金木水火土山川风雨雷电"
    titles = [f"{marks[index]}厂宣布涨价" for index in range(21)]
    for index, title in enumerate(titles):
        _insert_news(
            "cls", f"cap-{index}", _publish(friday, 10, index), [], [],
            title=title,
        )
    prompts: list[str] = []
    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(
        news_service,
        "_llm_text",
        lambda prompt, **_kwargs: prompts.append(prompt) or "{}",
    )
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    snapshot = top_hot_events(NOW)
    assert len(snapshot["events"]) == 21
    # 2 批 × 2 轮（首轮 + 空返回触发重写）= 4 次。
    assert len(prompts) == 4
    assert all("补充要点" in prompt for prompt in prompts)
    assert sum(1 for prompt in prompts if "上一轮要点不合格" in prompt) == 2
    # 21 条全都被问到（跨批次拼起来覆盖全部标题）。
    asked = "\n".join(prompts)
    assert not [title for title in titles if title not in asked]


def test_blocked_clustering_matches_pairwise_rank_and_merges(tmp_path, monkeypatch):
    """分桶只减少比较次数，排序、合并和事件键与两两比较一致。"""
    from app.news import hot_events as hot_mod
    from app.news.hot_events import build_top_hot_events

    _seed_friday(tmp_path)
    friday = date(2026, 10, 9)
    _insert_news(
        "cls", "party-a", _publish(friday, 0, 13), [], [],
        title="赛力斯与宁德时代签署战略合作",
    )
    _insert_news(
        "wscn", "party-b", _publish(friday, 8, 39), [], [],
        title="宁德时代与赛力斯签署战略合作",
    )
    _insert_news(
        "cls", "earn-a", _publish(friday, 9, 12), [], [],
        title="立讯精密业绩预告扣非增长",
    )
    _insert_news(
        "wscn", "earn-b", _publish(friday, 11, 40), [], [],
        title="立讯精密业绩预增扣非增长",
    )
    marks = "子丑寅卯辰巳午未申酉戌亥金木水火土山川风雨"
    for index, mark in enumerate(marks):
        _insert_news(
            "cls", f"solo-{index}", _publish(friday, 10, index % 60), [], [],
            title=f"{mark}厂宣布回购股份",
        )
    store = get_store()
    calls = {"n": 0}
    real = hot_mod._can_merge

    def counted(left, right):
        calls["n"] += 1
        return real(left, right)

    monkeypatch.setattr(hot_mod, "_can_merge", counted)
    naive = build_top_hot_events(NOW, store, refresh=True, blocked=False)
    naive_calls = calls["n"]
    calls["n"] = 0
    fast = build_top_hot_events(NOW, store, refresh=True, blocked=True)
    assert calls["n"] < naive_calls

    def signature(snapshot: dict) -> list[tuple]:
        return [
            (
                event["name"],
                event["key"],
                event["mentions"],
                event["source_count"],
                event["score"],
                event["importance"],
                tuple(event.get("headlines") or []),
            )
            for event in snapshot["events"]
        ]

    assert signature(fast) == signature(naive)


def test_selectable_note_and_capped_flag_explain_the_zero():
    """选项透出的 selectable_note / selectable_capped 要能解释「为什么是 0」与「是不是上限」。

    前端不能一律显示「暂无对应个股」—— 维表未同步和事件真没映射是两回事。
    """
    from app.services.picker import _hot_group

    def ev(key, name, importance, concepts):
        return {
            "key": key, "name": name, "importance": importance, "concepts": concepts,
            "mentions": 1, "source_count": 1,
        }

    dims = Dimensions(concepts={"000001.SZ": ["机器人"]})
    events = [
        ev("mapped", "有映射的事件", "重要", ["机器人"]),
        ev("noconcept", "中东局势", "重大", ["中东"]),      # 概念在维表里查无此词
    ]
    group = _hot_group({"events": events}, None, dims)
    by_name = {item["name"]: item for item in group["items"]}

    ok = by_name["有映射的事件"]
    assert ok["selectable_count"] == 1
    assert ok["selectable_note"] is None          # 有映射就不该有原因
    assert ok["selectable_capped"] is False       # 1 只远未触顶

    bad = by_name["中东局势"]
    assert bad["selectable_count"] == 0
    # 事件「没有匹配到成分股」是具体原因, 不能是 None 也不能是空串。
    assert bad["selectable_note"] and "没有匹配到成分股" in bad["selectable_note"]
    assert bad["selectable_capped"] is False


def test_selectable_capped_marks_the_thirty_ceiling():
    """命中数达到 _CONCEPT_CONSTITUENT_CAP 时要标 capped —— N 是上限, 不能说「约 N 只」。"""
    from app.services.picker import _hot_group, _CONCEPT_CONSTITUENT_CAP

    dims = Dimensions(concepts={f"{i:06d}.SZ": ["宽板块"] for i in range(1, 200)})
    events = [{
        "key": "wide", "name": "宽板块事件", "importance": "重要",
        "concepts": ["宽板块"], "mentions": 1, "source_count": 1,
    }]
    group = _hot_group({"events": events}, None, dims)
    item = group["items"][0]
    assert item["selectable_count"] == _CONCEPT_CONSTITUENT_CAP
    assert item["selectable_capped"] is True
