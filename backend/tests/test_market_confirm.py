"""盘面验证只看首见之后的行情，并能把得到验证的事件排到没有验证的前面。"""
from datetime import date, datetime

from app.market_time import CN_TZ
from app.news.market_confirm import (
    _known_holiday,
    _next_open_day,
    latest_session_day,
    load_market_tape,
    score_confirmation,
)
from app.news.service import hot_event_listing, reset_store_for_tests, top_hot_events

NOW = datetime(2026, 10, 10, 8, 30, tzinfo=CN_TZ)
FRIDAY = date(2026, 10, 9)


def _bar(hour, minute, price, *, volume=20_000, kind="minute", day=FRIDAY):
    return {
        "at": datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ),
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": volume,
        "kind": kind,
    }


def _tape(stock_bars, *, flows=None, concept_flows=None, index_bars=None, live=False):
    return {
        "session": FRIDAY.isoformat(),
        "live": live,
        "members": {},
        "index_bars": index_bars or [],
        "stocks": {
            "601318.SH": {
                "name": "中国平安",
                "prev_close": 10.0,
                "prior_high": 12.0,
                "prior_low": 8.0,
                "baseline_volume": 1_000_000,
                "bars": stock_bars,
                "flows": flows or [],
            },
        },
        "concepts": {"保险": concept_flows or []},
    }


def _event(name, direction, hour, minute, *, concepts=None):
    return {
        "name": name,
        "direction": direction,
        "concepts": concepts or ["保险"],
        "mentioned_stocks": [{"key": "601318.SH", "name": "中国平安", "mentions": 1}],
        "_first": datetime(2026, 10, 9, hour, minute, tzinfo=CN_TZ),
    }


def test_latest_session_is_friday_on_saturday_morning():
    assert latest_session_day(NOW) == FRIDAY


def test_pre_event_limit_up_does_not_confirm():
    bars = [
        _bar(10, 0, 11.0, volume=500_000),
        _bar(10, 1, 11.0, volume=500_000),
        _bar(14, 30, 10.01, volume=100),
    ]
    scored = score_confirmation(_event("平安银行回购", "利好", 14, 0), _tape(bars), NOW)
    assert scored["abnormal"] is False
    assert scored["detail"]["limit_count"] == 0
    assert scored["strength"] == "无"
    assert scored["score"] == 0


def test_post_event_limit_up_inflow_and_two_windows_rank_as_strong():
    bars = [
        _bar(10, 0, 10.2),
        _bar(10, 20, 10.4),
        _bar(14, 0, 10.7),
        _bar(14, 40, 11.0, volume=80_000),
    ]
    flows = [{"at": datetime(2026, 10, 9, 10, 30, tzinfo=CN_TZ), "value": 8e7, "kind": "snapshot"}]
    concept = [{"at": datetime(2026, 10, 9, 11, 0, tzinfo=CN_TZ), "value": 2e8, "kind": "snapshot"}]
    scored = score_confirmation(
        _event("平安银行回购", "利好", 9, 0),
        _tape(bars, flows=flows, concept_flows=concept),
        NOW,
    )
    assert scored["abnormal"] is True
    assert scored["strength"] == "强"
    assert scored["persistence"] == "持续"
    assert scored["detail"]["limit_count"] == 1
    assert scored["detail"]["windows_hit"] >= 2
    assert scored["label"] == "最近交易日 10月9日"
    assert scored["live"] is False
    assert scored["score"] >= 50

    bearish = score_confirmation(
        _event("平安银行回购", "利空", 9, 0),
        _tape(bars, flows=flows, concept_flows=concept),
        NOW,
    )
    assert bearish["strength"] == "无"
    assert bearish["score"] < scored["score"]


def test_bearish_event_uses_limit_down():
    bars = [
        _bar(10, 0, 9.6),
        _bar(10, 20, 9.4),
        _bar(14, 0, 9.2),
        _bar(14, 40, 9.0, volume=80_000),
    ]
    scored = score_confirmation(_event("美国对华芯片制裁升级", "利空", 9, 0, concepts=[]), _tape(bars), NOW)
    assert scored["abnormal"] is True
    assert scored["detail"]["limit_count"] == 1
    assert scored["strength"] == "强"
    assert scored["persistence"] == "持续"


def test_flow_and_day_bar_before_the_open_count_only_after_first_seen():
    day_bar = _bar(15, 0, 11.0, volume=2_000_000, kind="day")
    early_flow = [{"at": datetime(2026, 10, 9, 15, 0, tzinfo=CN_TZ), "value": 2e8, "kind": "day"}]
    midday = score_confirmation(
        _event("平安银行回购", "利好", 11, 0),
        _tape([day_bar], flows=early_flow),
        NOW,
    )
    assert midday["score"] == 0
    opened = score_confirmation(
        _event("平安银行回购", "利好", 9, 0),
        _tape([day_bar], flows=early_flow),
        NOW,
    )
    assert opened["score"] > midday["score"]


def test_event_after_the_session_waits_for_the_next_one():
    bars = [_bar(10, 0, 11.0), _bar(10, 20, 11.0)]
    event = _event("美联储降息", "利好", 9, 0)
    event["_first"] = datetime(2026, 10, 10, 9, 0, tzinfo=CN_TZ)
    scored = score_confirmation(event, _tape(bars), NOW)
    assert scored["score"] == 0
    assert scored["label"] == "待开盘验证"
    assert scored["pending"] is True
    assert scored["phase"] == "auction"
    assert scored["strength"] == "无"


def test_loader_reads_minute_bars_and_concept_flow_after_first_seen(tmp_path, monkeypatch):
    import polars as pl

    from app.config import settings
    from app.fund_flow.store import write_rows

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    minute = tmp_path / "kline_minute" / "date=2026-10-09"
    minute.mkdir(parents=True)
    (tmp_path / "kline_daily_enriched" / "date=2026-10-09").mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["601318.SH", "601318.SH", "000300.SH", "000300.SH"],
        "datetime": [
            datetime(2026, 10, 9, 10, 0),
            datetime(2026, 10, 9, 10, 20),
            datetime(2026, 10, 9, 10, 0),
            datetime(2026, 10, 9, 10, 20),
        ],
        "open": [10.0, 10.4, 4000.0, 4000.0],
        "high": [10.4, 11.0, 4000.0, 4000.0],
        "low": [10.0, 10.4, 4000.0, 4000.0],
        "close": [10.4, 11.0, 4000.0, 4000.0],
        "volume": [20_000.0, 80_000.0, 1.0, 1.0],
    }).write_parquet(minute / "part.parquet")
    pl.DataFrame({
        "symbol": ["601318.SH"],
        "date": [FRIDAY],
        "open": [10.0],
        "high": [11.0],
        "low": [10.0],
        "close": [11.0],
        "volume": [100_000.0],
        "prev_close": [10.0],
        "vol_ma5": [1_000_000.0],
        "name": ["中国平安"],
    }).write_parquet(tmp_path / "kline_daily_enriched" / "date=2026-10-09" / "part.parquet")
    write_rows(tmp_path, "concept", "2026-10-09", [{
        "name": "保险",
        "trade_date": "2026-10-09",
        "captured_at": "2026-10-09T09:10:00+08:00",
        "snapshot": "am",
        "net_inflow": 2e8,
        "rank": 1,
        "source": "test",
    }])
    write_rows(tmp_path, "concept", "2026-10-09", [{
        "name": "保险",
        "trade_date": "2026-10-09",
        "captured_at": "2026-10-09T08:00:00+08:00",
        "snapshot": "pre",
        "net_inflow": 9e8,
        "rank": 1,
        "source": "test",
    }])
    event = _event("平安银行回购", "利好", 9, 5)
    tape = load_market_tape(NOW, [event])
    assert tape["session"] == "2026-10-09"
    assert tape["live"] is False
    scored = score_confirmation(event, tape, NOW)
    assert scored["detail"]["sector_net_inflow"] == 2e8
    assert scored["detail"]["limit_count"] == 1
    assert scored["label"] == "最近交易日 10月9日"


def test_confirmed_move_outranks_an_unconfirmed_peer(tmp_path, monkeypatch):
    """同 importance 档内，拿到盘面验证的排在没验证的前面。

    原名是 `..._outranks_an_unconfirmed_major`，断言「已验证的『重要』压过未验证的
    『重大』」。那条断言在本轮计分改乘性后失效，**但它其实从引入那天起就是错的**
    （提交 84c8a9db）：当时 drug 136.17 已经低于 fed 138.28，测试能过只是因为
    `_event_sort_key` 里挂了 `-_IMPORTANCE_POINTS` 当第二键，把倒挂盖住了。
    本轮把排序键收敛成 `(-score, name)`（展示分已是各子项单调函数，再留子项做平局
    决胜只会让「名次」和「展示分」两套口径打架），掩盖被移除，倒挂才暴露出来。

    跨档不可达是**结构性**的，不是参数没调好：辅助项合计封顶 `_AUX_SCALE`，最多把
    分数翻一倍，于是「重要+满」上限 180 恒小于「重大」下限 200。要让验证能跨档，
    得把档位比从 2.22 压到 1.74 以下，那等于放弃「重要性主导」这条会诊结论。
    所以这里把需求重述成档内比较：验证的作用是**同一档里往前挪**，不是越级。
    """
    from app.news import market_confirm as confirm_mod

    reset_store_for_tests(tmp_path / "rank.sqlite")
    friday = datetime(2026, 10, 9, tzinfo=CN_TZ)

    def insert(source_id, hour, minute, title, sectors, stocks):
        from app.news.service import get_store
        published = friday.replace(hour=hour, minute=minute)
        mentions = [("sector", name, name, "", "structured") for name in sectors]
        mentions += [("stock", symbol, name, symbol[:6], "structured") for symbol, name in stocks]
        status = get_store().insert_item(
            source="cls",
            source_id=source_id,
            published_at=published,
            author="",
            title=title,
            clean_text=title + "。正文写长一些以免被当成短讯。",
            raw=None,
            content_hash=source_id,
            url="",
            level="",
            media_ids=[],
            extra=None,
            mentions=mentions,
        )
        assert status == "inserted"

    # 同档对照：两条都是「重要」（都靠 _IMPORTANT_ACTS 里的动作词升档），
    # 只有 drug 有映射且盘面得到验证。对照那条刻意不带降息/制裁一类重大词、
    # 也不点名 _LEADERS 龙头，否则会落进「重大」档，比的就成了档位而非验证。
    insert("order", 15, 0, "某公司公告回购", [], [])
    insert("drug", 9, 5, "创新药临床获批", ["创新药"], [("601318.SH", "中国平安")])

    def tape(_now, _events):
        return _tape([
            _bar(10, 0, 10.2),
            _bar(10, 20, 10.5),
            _bar(14, 0, 10.8),
            _bar(14, 40, 11.0, volume=80_000),
        ], flows=[{
            "at": datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ),
            "value": 8e7,
            "kind": "snapshot",
        }])

    monkeypatch.setattr(confirm_mod, "load_market_tape", tape)
    events = top_hot_events(NOW)["events"]
    names = [event["name"] for event in events]
    drug = next(event for event in events if event["name"] == "创新药临床获批")
    peer = next(event for event in events if event["name"] == "某公司公告回购")

    # 「重要」是两条共有的档位，档内比较才有意义。
    assert drug["importance"] == "重要"
    assert peer["importance"] == "重要"

    # 验证确实算进了 drug 的分：强度「强」，且这一项比热度那项重（封顶 40 vs 10）。
    # confirmation 原始分是 66，进分前按 _CONFIRMATION_CAP=40 截断 —— 旧口径下这条
    # 断言写成「breakdown 等于原始分」，只在原始分恰好等于封顶时成立，是靠数值巧合过的。
    assert drug["confirmation"]["strength"] == "强"
    assert drug["confirmation"]["persistence"] == "持续"
    assert drug["confirmation"]["score"] > 40
    assert drug["breakdown"]["confirmation"] == 40
    assert drug["breakdown"]["confirmation"] > drug["breakdown"]["heat"]

    # 没映射、没验证的那条，验证项拿 0。
    assert peer["confirmation"]["score"] == 0

    # 同档内，拿到验证的排前面 —— 这条测试真正要守的东西。
    assert drug["score"] > peer["score"]
    assert names.index("创新药临床获批") < names.index("某公司公告回购")

    listed = hot_event_listing(NOW, limit=5)
    public = listed["events"][0]
    assert public["name"] == "创新药临床获批"


def test_importance_band_dominates_any_amount_of_confirmation():
    """重要性档位主导：验证再满也跨不过一个档。

    这是乘性结构的**结构性保证**，不是调出来的巧合 —— 辅助项合计封顶 `_AUX_SCALE`，
    `_combined_score` 最多把 `importance` 翻一倍，而档位比恒 > 2。
    所以「满验证的低档」永远低于「裸着的高档」，榜单不会被一堆凑满验证的小事冲垮。
    哪天有人把档位比压到 2 以下，这条会先红。
    """
    from app.news.hot_events import _AUX_SCALE, _IMPORTANCE_POINTS, _combined_score

    order = ["琐碎", "一般", "重要", "重大"]
    for lower, upper in zip(order, order[1:]):
        base_low = _IMPORTANCE_POINTS[lower]
        base_up = _IMPORTANCE_POINTS[upper]
        if base_low == 0:
            continue
        # 低档吃满辅助，仍压不过高档的裸分。
        assert _combined_score(base_low, _AUX_SCALE) < base_up, (
            f"{lower}+满辅助 不该压过 裸{upper}"
        )
        # 档位比恒大于 2，这正是「翻倍也跨不过去」的来源。
        assert base_up / base_low > 2.0


def test_listing_exposes_confirmation_without_internal_ids(tmp_path, monkeypatch):
    """列表接口把验证结果透出来，但不漏内部 item_ids。"""
    from app.news import market_confirm as confirm_mod

    reset_store_for_tests(tmp_path / "listing.sqlite")
    friday = datetime(2026, 10, 9, tzinfo=CN_TZ)

    def insert(source_id, hour, minute, title, sectors, stocks):
        from app.news.service import get_store
        published = friday.replace(hour=hour, minute=minute)
        mentions = [("sector", name, name, "", "structured") for name in sectors]
        mentions += [("stock", symbol, name, symbol[:6], "structured") for symbol, name in stocks]
        status = get_store().insert_item(
            source="cls",
            source_id=source_id,
            published_at=published,
            author="",
            title=title,
            clean_text=title + "。正文写长一些以免被当成短讯。",
            raw=None,
            content_hash=source_id,
            url="",
            level="",
            media_ids=[],
            extra=None,
            mentions=mentions,
        )
        assert status == "inserted"

    insert("drug", 9, 5, "创新药临床获批", ["创新药"], [("601318.SH", "中国平安")])

    def tape(_now, _events):
        return _tape([
            _bar(10, 0, 10.2),
            _bar(10, 20, 10.5),
            _bar(14, 0, 10.8),
            _bar(14, 40, 11.0, volume=80_000),
        ], flows=[{
            "at": datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ),
            "value": 8e7,
            "kind": "snapshot",
        }])

    monkeypatch.setattr(confirm_mod, "load_market_tape", tape)
    public = hot_event_listing(NOW, limit=5)["events"][0]
    assert public["confirmation"]["strength"] == "强"
    assert public["confirmation"]["label"] == "最近交易日 10月9日"
    assert "confirmation" in public["breakdown"]
    assert "item_ids" not in public


def test_overnight_auction_confirms_after_925_and_not_before():
    auction = [
        _bar(9, 20, 10.4, volume=50_000),
        _bar(9, 25, 11.0, volume=50_000),
        _bar(14, 0, 11.05, volume=1_000),
        _bar(14, 40, 11.2, volume=1_000),
    ]
    event = _event("平安银行回购", "利好", 21, 0)
    event["_first"] = datetime(2026, 10, 8, 21, 0, tzinfo=CN_TZ)
    ready = score_confirmation(event, _tape(auction), NOW)
    assert ready["phase"] == "auction"
    assert ready["strength"] == "强"
    assert ready["horizon"] == "主线"
    assert ready["detail"]["limit_count"] == 1
    assert ready["detail"]["auction_open_pct"] >= 0.09
    assert ready["score"] >= 40
    assert ready["lagged"] is False

    early = score_confirmation(
        event,
        _tape(auction),
        datetime(2026, 10, 8, 22, 0, tzinfo=CN_TZ),
    )
    assert early["score"] == 0
    assert early["label"] == "待开盘验证"
    assert early["pending"] is True
    assert early["phase"] == "auction"
    assert early["detail"]["auction_open_pct"] is None


def test_auction_gap_that_fades_in_the_afternoon_is_a_one_day_move():
    bars = [
        _bar(9, 20, 10.6, volume=40_000),
        _bar(9, 25, 11.0, volume=40_000),
        _bar(14, 0, 10.6, volume=1_000),
        _bar(14, 40, 10.2, volume=1_000),
    ]
    event = _event("平安银行回购", "利好", 21, 0)
    event["_first"] = datetime(2026, 10, 8, 21, 0, tzinfo=CN_TZ)
    scored = score_confirmation(event, _tape(bars), NOW)
    assert scored["phase"] == "auction"
    assert scored["horizon"] == "一日游"
    assert scored["strength"] == "强"


def test_intraday_windows_use_the_move_after_publish():
    bars = [
        _bar(9, 40, 10.0, volume=1_000),
        _bar(10, 4, 10.3, volume=20_000),
        _bar(10, 12, 10.6, volume=20_000),
        _bar(10, 28, 10.9, volume=20_000),
    ]
    scored = score_confirmation(_event("创新药临床获批", "利好", 10, 0), _tape(bars), NOW)
    assert scored["phase"] == "intraday"
    assert scored["lagged"] is False
    assert scored["strength"] in {"强", "中"}
    assert scored["detail"]["window_5"] > 0
    assert scored["detail"]["window_30"] > scored["detail"]["window_5"]
    assert scored["detail"]["pre_return"] < 0.01


def test_move_before_the_news_is_lagged_not_event_driven():
    bars = [
        _bar(9, 40, 10.8, volume=80_000),
        _bar(10, 6, 10.81, volume=100),
    ]
    scored = score_confirmation(_event("创新药临床获批", "利好", 10, 0), _tape(bars), NOW)
    assert scored["lagged"] is True
    assert scored["strength"] == "无"
    assert scored["score"] == 0
    assert scored["phase"] == "intraday"


def test_shared_symbol_is_split_by_time_then_by_mapping_weight():
    bars = [
        _bar(9, 50, 10.0, volume=1_000),
        _bar(10, 10, 10.4, volume=20_000),
        _bar(10, 30, 11.0, volume=20_000),
    ]
    tape = _tape(bars)
    earlier = _event("先发布", "利好", 10, 0)
    later = _event("后发布", "利好", 10, 20)
    first = score_confirmation(earlier, tape, NOW, peers=[earlier, later])
    second = score_confirmation(later, tape, NOW, peers=[earlier, later])
    assert first["detail"]["excess_pct"] < 0.05
    assert second["detail"]["excess_pct"] > first["detail"]["excess_pct"]
    assert first["detail"]["window_30"] < 0.05

    same_time = _event("直接映射", "利好", 10, 0)
    concept_only = {
        "name": "只映射概念",
        "direction": "利好",
        "concepts": ["保险"],
        "mentioned_stocks": [],
        "_first": datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ),
    }
    shared = _tape([
        _bar(9, 50, 10.0, volume=1_000),
        _bar(10, 10, 11.0, volume=20_000),
    ])
    shared["members"] = {"保险": ["601318.SH"]}
    direct = score_confirmation(same_time, shared, NOW, peers=[same_time, concept_only])
    indirect = score_confirmation(concept_only, shared, NOW, peers=[same_time, concept_only])
    assert direct["detail"]["share"] == 0.75
    assert indirect["detail"]["share"] == 0.25
    assert direct["detail"]["excess_pct"] > indirect["detail"]["excess_pct"] * 2


def test_new_event_is_confirmed_in_the_same_snapshot(tmp_path, monkeypatch):
    """新事件进榜时，个股映射要等要点补上。行情已经支持时，这一轮快照就要验证。"""
    from app.news.hot_events import build_top_hot_events

    store = reset_store_for_tests(tmp_path / "same-cycle.sqlite")
    friday = datetime(2026, 10, 9, tzinfo=CN_TZ)
    published = friday.replace(hour=9, minute=5)
    status = store.insert_item(
        source="cls",
        source_id="drug-late",
        published_at=published,
        author="",
        title="创新药临床获批",
        clean_text="创新药临床获批。正文写长一些以免被当成短讯。",
        raw=None,
        content_hash="drug-late",
        url="",
        level="",
        media_ids=[],
        extra=None,
        mentions=[],
    )
    assert status == "inserted"

    def label(events):
        for event in events:
            blob = " ".join([
                str(event.get("name") or ""),
                str(event.get("headline") or ""),
                * [str(item) for item in (event.get("headlines") or [])],
            ])
            if "临床获批" not in blob:
                continue
            event["mentioned_stocks"] = [{
                "key": "601318.SH",
                "name": "中国平安",
                "mentions": 1,
                "source_count": 1,
                "direction": "利好",
            }]
            event["concepts"] = ["创新药"]
            event["direction"] = "利好"

    monkeypatch.setattr("app.news.hot_events._label_with_llm", label)
    monkeypatch.setattr(
        "app.news.market_confirm.load_market_tape",
        lambda _now, _events: _tape([
            _bar(10, 0, 10.2),
            _bar(10, 20, 10.5),
            _bar(14, 0, 10.8),
            _bar(14, 40, 11.0, volume=80_000),
        ], flows=[{
            "at": datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ),
            "value": 8e7,
            "kind": "snapshot",
        }]),
    )
    snapshot = build_top_hot_events(NOW, store, refresh=True)
    assert snapshot["events"]
    drug = snapshot["events"][0]
    assert any(item.get("key") == "601318.SH" for item in drug["mentioned_stocks"])
    assert drug["confirmation"]["strength"] == "强"


def test_next_open_day_after_the_calendar_uses_weekdays(monkeypatch):
    """日历在年底截断后，更晚的工作日不当休市，下一开市日也不再连跳 16 天。"""
    monkeypatch.setattr(
        "app.news.market_confirm._fuyao_trading_days",
        lambda: frozenset({date(2026, 12, 31)}),
    )
    assert _known_holiday(date(2026, 12, 30)) is True
    assert _known_holiday(date(2027, 1, 1)) is False
    assert _next_open_day(date(2026, 12, 31)) == date(2027, 1, 1)
    assert _next_open_day(date(2027, 1, 2)) == date(2027, 1, 4)


def test_state_separates_unverifiable_from_unconfirmed():
    """`state` 把「无标可验」与「验证未兑现」分开 —— 二者都是 score=0 + strength="无"。

    规格来源：会诊裁定 A。线上实测（16:09 快照，`score=0` 共 381 条）两者同时存在：
    `unverifiable` 185 条（48.6%）、`unconfirmed` 40 条（10.5%）
    （如「奥克股份终止重组复牌」，excess=-4.96%、看过 4 个窗口仍不认）。
    在此之前只能靠 score 猜，是异质簇。
    """
    # ① 有标的、有观测，但盘面不认（消息前已涨停，事后不涨）→ 验证未兑现。
    bars = [
        _bar(10, 0, 11.0, volume=500_000),
        _bar(10, 1, 11.0, volume=500_000),
        _bar(14, 30, 10.01, volume=100),
    ]
    unconfirmed = score_confirmation(_event("平安银行回购", "利好", 14, 0), _tape(bars), NOW)
    assert unconfirmed["score"] == 0
    assert unconfirmed["strength"] == "无"
    assert unconfirmed["state"] == "unconfirmed"

    # ② 标的为空的事件 → 无标可验。
    no_symbol = dict(_event("欧盟制裁俄军工", "利好", 9, 0))
    no_symbol["mentioned_stocks"] = []
    no_symbol["concepts"] = []
    unverifiable = score_confirmation(no_symbol, _tape(bars), NOW)
    assert unverifiable["score"] == 0
    assert unverifiable["strength"] == "无"
    assert unverifiable["state"] == "unverifiable"

    # 两者 score/strength 完全同形，只有 state 能区分 —— 这正是本改动存在的理由。
    assert (unconfirmed["score"], unconfirmed["strength"]) == (unverifiable["score"], unverifiable["strength"])
    assert unconfirmed["state"] != unverifiable["state"]


def test_state_scored_when_confirmation_lands():
    """打出分时 state='scored'，与两条 0 分支区分开。"""
    bars = [
        _bar(10, 0, 10.2),
        _bar(10, 20, 10.4),
        _bar(14, 0, 10.7),
        _bar(14, 40, 11.0, volume=80_000),
    ]
    flows = [{"at": datetime(2026, 10, 9, 10, 30, tzinfo=CN_TZ), "value": 8e7, "kind": "snapshot"}]
    scored = score_confirmation(_event("平安银行回购", "利好", 9, 0), _tape(bars, flows=flows), NOW)
    assert scored["score"] > 0
    assert scored["state"] == "scored"


def test_public_confirmation_exposes_state_and_backfills_legacy_snapshots():
    """对外接口透出 state；老快照缺 state 时按 strength/pending 反推，不返回空串。"""
    from app.news.service import _public_confirmation

    # 新数据：直接透传。
    assert _public_confirmation({"strength": "无", "state": "unverifiable"})["state"] == "unverifiable"
    assert _public_confirmation({"strength": "强", "state": "scored"})["state"] == "scored"
    # 老快照没有 state：反推。
    assert _public_confirmation({"strength": "无", "pending": True})["state"] == "pending"
    assert _public_confirmation({"strength": "强"})["state"] == "scored"
    assert _public_confirmation({"strength": "无"})["state"] == "unverifiable"


def test_unverifiable_state_keeps_legacy_score_and_strength():
    """核心向后兼容保证：state='unverifiable' 时 score 仍是 0、strength 仍是「无」、pending 仍为 False。

    这是本次改动「加字段不改值域」承诺的可执行断言 —— 只要这条成立，
    `_confirmation_points`、`service._public_confirmation` 的 strength 白名单、
    前端既有的 `strength === '无'` 分支就全部无需改动、行为不变。
    """
    from app.news.market_confirm import _result

    r = _result(0, False, "无", "无", "暂无行情", None, False, {}, state="unverifiable")
    assert r["state"] == "unverifiable"
    assert r["score"] == 0
    assert r["strength"] == "无"
    assert r["pending"] is False

    # 未显式传 state 时按旧字段兜底推断，不返回空串。
    # `(score=0, strength="无", pending=False)` 的兜底结果**只能**是 unverifiable ——
    # 断言具体值而非 `in {...}`，否则将来兜底逻辑漂移（例如误判成 scored）也不会有测试报警。
    legacy = _result(0, False, "无", "无", "暂无行情", None, False, {})
    assert legacy["state"] == "unverifiable"
    assert _result(50, True, "强", "持续", "最近交易日", None, False, {},
                   state="scored")["state"] == "scored"


def test_scored_state_requires_nonempty_strength_end_to_end():
    """评审必修 1（回归）：`state="scored"` 与 `strength="无"` 不得共存 —— 端到端。

    `total = move + strength_points + persist_points`，而两个打分函数的档位阈值不同：
        excess ∈ [0.3%, 0.5%) → `_move_points` 给 3 分，`_strength_points` 给 0 分。
    若又无持续性（`persist_points == 0`），就得到 `total=3` 而 `strength="无"`。
    旧写法按 `total > 0` 判 scored → 这类事件被 `event_is_verified` 判为「已验证」
    进钉钉推送，而前端仍按 `strength === "无"` 显示无验证，两边结论相反。
    现统一为 `strength != "无"`。

    构造：消息 10/9(周五) 收盘后首见 → 确认时点推到 10/12(周一) 9:25 竞价。
    标的竞价相对前收 +0.4%，指数竞价持平 → `excess = +0.4%`，落入该档位。
    """
    from app.market_time import CN_TZ as _CN_TZ

    def _qbar(day, hour, minute, price, volume=20_000):
        return {
            "at": datetime(2026, 10, day, hour, minute, tzinfo=_CN_TZ),
            "open": price, "high": price, "low": price, "close": price,
            "volume": volume, "kind": "minute",
        }

    event = _event("平安银行回购", "利好", 15, 30)
    tape = {
        "session": "2026-10-12",
        "live": False,
        "members": {},
        "index_bars": [
            {"at": datetime(2026, 10, 12, 9, 15, tzinfo=_CN_TZ), "open": 4000.0, "close": 4000.0, "kind": "minute"},
            {"at": datetime(2026, 10, 12, 9, 25, tzinfo=_CN_TZ), "open": 4000.0, "close": 4000.0, "kind": "minute"},
        ],
        "stocks": {
            "601318.SH": {
                "name": "中国平安",
                "prev_close": 10.0,
                "baseline_volume": 1_000_000,
                # 竞价收 10.04 → 相对前收 +0.4%，恰在「move>0 但 strength_points==0」档
                "bars": [_qbar(12, 9, 20, 10.0), _qbar(12, 9, 25, 10.04)],
                "flows": [],
            },
        },
        "concepts": {"保险": []},
    }
    now = datetime(2026, 10, 12, 10, 0, tzinfo=_CN_TZ)
    result = score_confirmation(event, tape, now)

    # 前提：确实命中了「有异动分、无强度分」的那个档位（否则这条测试没测到东西）。
    assert result["strength"] == "无", f"场景未进入目标档位：strength={result['strength']}"
    assert result["score"] > 0, "该场景应有 move 分（否则无需区分 state）"
    # 核心断言：不得把「只有异动、没有强度」说成「已验证」。
    assert result["state"] != "scored", (
        f"strength=无 却标 scored，会被 event_is_verified 误判为已验证进推送："
        f"state={result['state']} score={result['score']}"
    )
    assert result["state"] == "unconfirmed"


def test_no_stock_observation_does_not_borrow_index_move_auction():
    """评审必修 2（回归）：无标的观测时，不得把指数涨跌当成标的超额收益。

    盘面验证里 `basket = sum(opens)/len(opens) if opens else 0.0`，`_score_auction`
    随后 `excess = (basket - index_ret)`。**标的没有竞价观测时 `opens` 为空 → `basket=0`**，
    于是 `excess = -index_ret`（利好时）—— 指数竞价跌 1.5% 就让一个 `windows=0`、
    `auction_open_pct=0` 的事件拿到 `score=16, strength="中", state="scored",
    abnormal=True`，并因 `event_is_verified` 进钉钉推送。指数一下跌就"给利好事件发验证"。

    修复后按 `not opens and not flows` 直接判 `unconfirmed`（score=0）。

    这条走的是**竞价路径**（收盘后消息的确认时点被推到下一开市 9:25），
    是实测中该 bug 真正可达的路径。
    """
    from app.market_time import CN_TZ as _CN_TZ

    def _qbar(day, hour, minute, price, volume=20_000):
        return {
            "at": datetime(2026, 10, day, hour, minute, tzinfo=_CN_TZ),
            "open": price, "high": price, "low": price, "close": price,
            "volume": volume, "kind": "minute",
        }

    # 消息 10/9(周五) 15:30 首见 → 确认时点被推到 10/12(周一) 9:25 竞价。
    event = _event("平安银行回购", "利好", 15, 30)
    # 标的有 10/12 的竞价 bar（借以路由到竞价路径），但 prev_close 缺失
    # → `opens` 为空 → 无任何标的观测。指数竞价下跌 1.5%。
    tape = {
        "session": "2026-10-12",
        "live": False,
        "members": {},
        "index_bars": [
            {"at": datetime(2026, 10, 12, 9, 15, tzinfo=_CN_TZ), "open": 4000.0, "close": 4000.0, "kind": "minute"},
            {"at": datetime(2026, 10, 12, 9, 25, tzinfo=_CN_TZ), "open": 4000.0, "close": 3940.0, "kind": "minute"},
        ],
        "stocks": {
            "601318.SH": {
                "name": "中国平安",
                "prev_close": None,  # 缺前收 → 算不出 opens
                "baseline_volume": 1_000_000,
                "bars": [_qbar(12, 9, 20, 10.0), _qbar(12, 9, 25, 10.1)],
                "flows": [],
            },
        },
        "concepts": {"保险": []},
    }
    now = datetime(2026, 10, 12, 10, 0, tzinfo=_CN_TZ)  # gate(9:25) 已过
    result = score_confirmation(event, tape, now)

    assert result["score"] == 0, f"无标的观测却得了分：score={result['score']}"
    assert result["strength"] == "无", f"无标的观测却拿到强度：strength={result['strength']}"
    assert result["abnormal"] is False, "无标的观测不该判异动"
    assert result["state"] != "scored", f"无标的观测却标 scored：state={result['state']}"
    # 评审 C：**完全没有任何观测**（无价格 bar、无资金流、无盘口窗口）→ 按 docstring
    # 定义应为 `unverifiable`（无标可验），而不是 `unconfirmed`（那是「有观测未兑现」）。
    assert result["state"] == "unverifiable"


def test_flows_only_does_not_borrow_index_move_and_stays_unconfirmed():
    """评审 B（回归）：只有资金流、没有价格 bar 时，不得借指数涨跌给分。

    `opens`/`returns` 为空 → `basket = 0` → `excess = -index_ret`。若只靠 `flows` 存在
    就放行继续计分，`_move_points`/`_strength_points` 仍会读这个被指数污染的值。
    现清零 `excess` 后再计分：资金流事件只能由资金面（`_flow_confirms`）得分。

    同时确认 state 口径：有资金流观测但未兑现 → `unconfirmed`（与「完全没观测」区分）。
    """
    from app.market_time import CN_TZ as _CN_TZ

    def _qbar(day, hour, minute, price, volume=20_000):
        return {
            "at": datetime(2026, 10, day, hour, minute, tzinfo=_CN_TZ),
            "open": price, "high": price, "low": price, "close": price,
            "volume": volume, "kind": "minute",
        }

    event = _event("平安银行回购", "利好", 15, 30)
    # 指数竞价跌 1.5%；标的有竞价 bar 但缺 prev_close（算不出 opens），另有概念资金流。
    concept_flows = [
        {"at": datetime(2026, 10, 12, 9, 20, tzinfo=_CN_TZ), "value": 1e8, "kind": "snapshot"},
    ]
    tape = {
        "session": "2026-10-12",
        "live": False,
        "members": {},
        "index_bars": [
            {"at": datetime(2026, 10, 12, 9, 15, tzinfo=_CN_TZ), "open": 4000.0, "close": 4000.0, "kind": "minute"},
            {"at": datetime(2026, 10, 12, 9, 25, tzinfo=_CN_TZ), "open": 4000.0, "close": 3940.0, "kind": "minute"},
        ],
        "stocks": {
            "601318.SH": {
                "name": "中国平安",
                "prev_close": None,
                "baseline_volume": 1_000_000,
                "bars": [_qbar(12, 9, 20, 10.0), _qbar(12, 9, 25, 10.1)],
                "flows": [],
            },
        },
        "concepts": {"保险": concept_flows},
    }
    now = datetime(2026, 10, 12, 10, 0, tzinfo=_CN_TZ)
    result = score_confirmation(event, tape, now)

    detail = result.get("detail") or {}
    # 核心：excess 不得等于「指数跌幅」（-index_ret = +1.5%）。
    assert (detail.get("excess_pct") or 0) == 0.0, (
        f"无价格观测却借了指数的涨跌：excess_pct={detail.get('excess_pct')}"
    )
    # 有资金流观测 → unconfirmed（而非 unverifiable）。
    assert result["state"] == "unconfirmed", f"state={result['state']}"


def test_verified_flag_still_feeds_horizon_after_scored_decoupling():
    """评审 A（回归）：`state` 用 `scored`(=strength!="无") 后，`verified` 必须保持原口径。

    `verified` 不只喂 state，还喂 `_horizon()` 与「一日游」封顶。若把它也改成
    `strength != "无"`，只有异动（涨停/放量/资金流，但 `strength_points == 0`）的事件会：
      - 丢掉「主线 / 一日游」标签；
      - 不再触发 `persist_points = min(persist_points, 6)`，**total 被抬高、排序改变** —— 这是
        本次 PR 最不该引入的那种「顺带改分」的行为回归。

    守护方式：三条计分路径传给 `_horizon` 的第 4 个实参**必须**含 `abnormal`。
    主路径经 `verified` 间接传（该路径端到端难以构造：正常盘中/盘后消息会被路由到
    intraday/auction），故此处直接做源码级断言，锁定 wiring 不被再次改窄。
    """
    import inspect

    from app.news import market_confirm as mc

    src = inspect.getsource(mc)
    # 主路径：verified 必须保留 `or abnormal`。
    assert 'verified = strength != "无" or abnormal' in src, (
        "主路径 `verified` 被改窄，会导致 _horizon 丢标签并抬高 total"
    )
    # 竞价 / 盘中路径：内联实参也必须含 abnormal。
    assert src.count('_horizon(stock_rows, first, bull, strength != "无" or abnormal)') == 2, (
        "竞价/盘中路径的 _horizon 实参被改窄"
    )
    # 同时确认 state 用的是 scored 而非 verified（两者已解耦）。
    assert 'scored = strength != "无"' in src

    # 行为侧：verified=True 时 _horizon 能给出标签；False 时按设计返回空。
    def _bar(hour, minute, price):
        return {
            "at": datetime(2026, 10, 9, hour, minute, tzinfo=CN_TZ),
            "open": price, "high": price, "low": price, "close": price,
            "volume": 20_000, "kind": "minute",
        }

    first = datetime(2026, 10, 9, 13, 0, tzinfo=CN_TZ)
    rows = [("601318.SH", {"bars": [_bar(13, 0, 10.0), _bar(14, 0, 10.05)]})]
    assert mc._horizon(rows, first, True, True) != ""
    assert mc._horizon(rows, first, True, False) == ""
