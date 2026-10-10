"""盘面验证只看首见之后的行情，并能把得到验证的事件排到没有验证的前面。"""
from datetime import date, datetime

from app.market_time import CN_TZ
from app.news.market_confirm import latest_session_day, load_market_tape, score_confirmation
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
    assert scored["label"] == "尚无首见之后的盘面"
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


def test_confirmed_move_outranks_an_unconfirmed_major(tmp_path, monkeypatch):
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

    insert("fed", 15, 0, "美联储宣布降息", [], [])
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
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert names[0] == "创新药临床获批"
    assert names.index("创新药临床获批") < names.index("美联储降息")
    drug = top_hot_events(NOW)["events"][0]
    assert drug["importance"] == "重要"
    assert drug["confirmation"]["strength"] == "强"
    assert drug["confirmation"]["persistence"] == "持续"
    assert drug["breakdown"]["confirmation"] == drug["confirmation"]["score"]
    assert drug["breakdown"]["confirmation"] > drug["breakdown"]["heat"]
    fed = next(event for event in top_hot_events(NOW)["events"] if event["name"] == "美联储降息")
    assert fed["importance"] == "重大"
    assert fed["confirmation"]["score"] == 0
    assert drug["score"] > fed["score"]

    listed = hot_event_listing(NOW, limit=5)
    public = listed["events"][0]
    assert public["name"] == "创新药临床获批"
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
    assert early["label"] == "等待竞价"
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
