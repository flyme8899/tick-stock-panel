"""钉钉推送：加签、文案、冷却和触发。不访问外网。"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.market_time import CN_TZ
from app.news.dingtalk import signed_url
from app.news.host_collector import send_dingtalk
from app.news.push import (
    PushState,
    _cached_live_rows,
    _live_rows,
    _load_abnormal,
    _load_t,
    abnormal_signals,
    count_rule_edges,
    filter_prev_close,
    format_hot_markdown,
    format_symbol_markdown,
    format_test_markdown,
    hot_material,
    limit_price,
    new_edges,
    pick_refs,
    session_signal_sets,
    t_conditions,
    t_universe,
    tick,
)


def test_sign_matches_frozen_vector_and_skips_without_secret():
    url = "https://oapi.dingtalk.com/robot/send?access_token=test"
    signed = signed_url(url, "SECtest", timestamp_ms=1700000000000)
    assert signed.endswith("timestamp=1700000000000&sign=aZLLrriXgn05YbwaGR7knYsLeJADjr9NwLaNNKpxh4g%3D")
    assert signed_url(url, "") == url


def test_hot_markdown_keeps_private_sources_as_names_only():
    secret = "钦钉正文不应出现在推送里"
    sectors = [{
        "name": "半导体",
        "key": "半导体",
        "story_count": 3,
        "source_count": 2,
        "growth": 1.8,
        "refs": pick_refs([
            {
                "source": "dws",
                "source_label": "钉钉作文实时",
                "title": secret,
                "excerpt": secret,
                "url": "https://private.example/secret",
            },
            {
                "source": "cls",
                "source_label": "财联社",
                "title": "电报标题",
                "url": "https://www.cls.cn/detail/1",
                "excerpt": "电报正文也不应整段复制",
            },
            {
                "source": "zsxq",
                "source_label": "知识星球纳指星球调研",
                "title": "星球标题",
                "excerpt": "星球正文",
            },
        ]),
    }]
    packed = format_hot_markdown(sectors, [], heading="【热点候选】盘前")
    assert packed is not None
    _title, text = packed
    assert "【热点候选】盘前" in text
    assert "提及 3" in text
    assert "来源 2" in text
    assert "相对基线 1.8 倍" in text
    assert "[电报标题](https://www.cls.cn/detail/1)" in text
    assert "钉钉作文实时" in text
    assert secret not in text
    assert "星球标题" not in text
    assert "星球正文" not in text
    assert "https://private.example" not in text
    assert "电报正文" not in text


def test_verified_confirmation_labels_auction_and_lag():
    from app.news.push import _confirm_label, event_is_verified

    assert _confirm_label({"phase": "auction", "strength": "强", "horizon": "主线"}) == "竞价验证 强·主线"
    assert _confirm_label({"lagged": True, "strength": "无"}) == "消息滞后确认"
    assert event_is_verified({"strength": "中"}) is True
    assert event_is_verified({"strength": "无"}) is False
    packed = format_hot_markdown([], [], heading="【热点候选】盘前", events=[{
        "name": "平安银行回购",
        "story_count": 1,
        "source_count": 1,
        "confirmation": {"phase": "auction", "strength": "强", "horizon": "主线"},
    }])
    assert packed is not None
    assert "竞价验证 强·主线" in packed[1]


def test_symbol_messages_are_labeled_by_type():
    abnormal = format_symbol_markdown("abnormal", [{
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "detail": "涨停",
    }])
    assert abnormal is not None
    assert abnormal[0] == "【异动监控】"
    assert "涨停" in abnormal[1]
    trade = format_symbol_markdown("t_trade", [{
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "detail": "高于分时均价",
    }])
    assert trade is not None
    assert "【做T提醒】" in trade[1]
    title, text = format_test_markdown()
    assert title.startswith("【测试】")
    assert "登录失效" in text
    assert "资讯正文" in text


def test_hot_trigger_needs_support_or_a_real_jump():
    current = [
        {"kind": "stock", "key": "600519.SH", "score": 3, "story_count": 2, "rank": 1},
        {"kind": "stock", "key": "300037.SZ", "score": 2, "story_count": 1, "rank": 2},
    ]
    assert hot_material(None, current, top_n=5, min_stories=2, jump=0.5) == []
    previous = [{"kind": "stock", "key": "000001.SZ", "score": 1, "story_count": 2, "rank": 1}]
    entered = hot_material(previous, current, top_n=5, min_stories=2, jump=0.5)
    assert [item["key"] for item in entered] == ["600519.SH"]
    assert entered[0]["reason"] == "new"
    jumped = hot_material(
        [{"kind": "stock", "key": "600519.SH", "score": 2, "story_count": 2, "rank": 1}],
        current,
        top_n=5,
        min_stories=2,
        jump=0.5,
    )
    assert jumped[0]["reason"] == "jump"
    quiet = hot_material(
        [{"kind": "stock", "key": "600519.SH", "score": 2.9, "story_count": 2, "rank": 1}],
        current,
        top_n=5,
        min_stories=2,
        jump=0.5,
    )
    assert quiet == []


def test_edges_seed_once_and_prev_close_needs_a_prior_move():
    assert new_edges(None, {"600519.SH": {"limit_up"}}) == []
    assert new_edges({"600519.SH": set()}, {"600519.SH": {"limit_up", "volume_surge"}}) == [
        ("600519.SH", "limit_up"),
        ("600519.SH", "volume_surge"),
    ]
    above, pct = t_conditions({
        "close": 103, "high": 104, "low": 100, "volume": 1000, "amount": 1000 * 100 * 100, "prev_close": 100,
    })
    assert "above_vwap" in above
    assert pct == pytest.approx(0.03)
    flat, _flat_pct = t_conditions({"close": 10, "high": 10, "low": 10, "volume": 1, "amount": 1000, "prev_close": 10})
    assert "near_high" not in flat
    assert "near_prev_close" in flat
    kept = filter_prev_close(
        [("600519.SH", "near_prev_close"), ("600519.SH", "above_vwap")],
        {"600519.SH": 0.02},
        {"600519.SH": 0.001},
    )
    assert kept == [("600519.SH", "near_prev_close"), ("600519.SH", "above_vwap")]
    assert filter_prev_close(
        [("600519.SH", "near_prev_close")],
        {"600519.SH": 0.002},
        {"600519.SH": 0.001},
    ) == []


def test_cooldown_and_rate_limit(tmp_path):
    state = PushState(tmp_path / "push_state.json")
    now = 1_700_000_000.0
    assert state.rate_ok(now)
    state.cool_until("hot", now, 1800)
    assert state.cooling("hot", now + 10)
    assert not state.cooling("hot", now + 1801)
    for _ in range(20):
        state.mark_sent(now)
    assert not state.rate_ok(now + 1)
    assert state.rate_ok(now + 61)


def test_scheduled_push_once_and_change_respects_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr("app.news.push.push_master_enabled", lambda: True)
    monkeypatch.setattr("app.news.push.push_type_enabled", lambda kind: kind == "hot")
    monkeypatch.setattr("app.config.settings.dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=test")
    monkeypatch.setattr("app.config.settings.dingtalk_secret", "SECtest")
    monkeypatch.setattr("app.config.settings.news_push_top_n", 5)
    monkeypatch.setattr("app.config.settings.news_push_min_stories", 2)
    monkeypatch.setattr("app.config.settings.news_push_score_jump", 0.5)
    monkeypatch.setattr("app.config.settings.news_push_hot_cooldown_min", 30)
    state = PushState(tmp_path / "push_state.json")
    sent = []

    def opener(request, timeout=10):
        sent.append(json.loads(request.data.decode()))
        return None

    pre = datetime(2026, 10, 9, 8, 46, tzinfo=CN_TZ)
    rows = [{
        "kind": "sector",
        "key": "半导体",
        "name": "半导体",
        "score": 2,
        "story_count": 3,
        "sources": ["cls"],
        "growth": 1.8,
    }]
    assert tick(pre, state=state, opener=opener, trading=True, hot_loader=lambda: rows) == ["hot"]
    assert tick(pre + timedelta(minutes=5), state=state, opener=opener, trading=True, hot_loader=lambda: rows) == []
    assert sent[0]["msgtype"] == "markdown"
    assert "【热点候选】盘前" in sent[0]["markdown"]["text"]
    assert "登录已失效" not in sent[0]["markdown"]["text"]

    still_cooling = datetime(2026, 10, 9, 9, 10, tzinfo=CN_TZ)
    grown = [{**rows[0], "score": 4, "story_count": 4}]
    assert tick(still_cooling, state=state, opener=opener, trading=True, hot_loader=lambda: grown) == []
    later = datetime(2026, 10, 9, 10, 30, tzinfo=CN_TZ)
    assert tick(later, state=state, opener=opener, trading=True, hot_loader=lambda: grown) == ["hot"]
    assert tick(later, state=state, opener=opener, trading=True, hot_loader=lambda: grown) == []
    assert "【热点候选】变化" in sent[1]["markdown"]["text"]


def test_abnormal_and_t_only_after_baseline(tmp_path, monkeypatch):
    enabled = {"abnormal"}
    monkeypatch.setattr("app.news.push.push_master_enabled", lambda: True)
    monkeypatch.setattr("app.news.push.push_type_enabled", lambda kind: kind in enabled)
    monkeypatch.setattr("app.config.settings.dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=test")
    monkeypatch.setattr("app.config.settings.dingtalk_secret", "")
    monkeypatch.setattr("app.config.settings.news_push_symbol_cooldown_min", 30)
    monkeypatch.setattr("app.config.settings.news_push_t_cooldown_min", 20)
    state = PushState(tmp_path / "push_state.json")
    sent = []

    def opener(request, timeout=10):
        sent.append(json.loads(request.data.decode()))
        return None

    def empty_edges():
        return ({}, {}, {})

    morning = datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ)
    abnormal = ({"600519.SH": {"limit_up"}}, {}, {"600519.SH": "贵州茅台"})
    lunch = datetime(2026, 10, 9, 12, 0, tzinfo=CN_TZ)
    assert tick(
        lunch, state=state, opener=opener, trading=True,
        abnormal_loader=lambda: abnormal, t_loader=empty_edges,
    ) == []
    assert tick(
        morning, state=state, opener=opener, trading=False,
        abnormal_loader=lambda: abnormal, t_loader=empty_edges,
    ) == []
    assert tick(
        morning, state=state, opener=opener, trading=True,
        abnormal_loader=lambda: abnormal, t_loader=empty_edges,
    ) == []
    assert sent == []
    same = morning + timedelta(seconds=181)
    assert tick(
        same, state=state, opener=opener, trading=True,
        abnormal_loader=lambda: abnormal, t_loader=empty_edges,
    ) == []
    broken = ({"600519.SH": {"limit_up", "broken"}}, {}, {"600519.SH": "贵州茅台"})
    fresh = same + timedelta(seconds=181)
    assert tick(
        fresh, state=state, opener=opener, trading=True,
        abnormal_loader=lambda: broken, t_loader=empty_edges,
    ) == ["abnormal"]
    assert sent[0]["markdown"]["title"] == "【异动监控】"
    assert "炸板" in sent[0]["markdown"]["text"]
    assert "涨停" not in sent[0]["markdown"]["text"]

    enabled.add("t_trade")
    away = (
        {"600519.SH": {"above_vwap"}},
        {"600519.SH": 0.02},
        {"600519.SH": "贵州茅台"},
    )
    back = (
        {"600519.SH": {"above_vwap", "near_prev_close"}},
        {"600519.SH": 0.001},
        {"600519.SH": "贵州茅台"},
    )
    t_seed = fresh + timedelta(seconds=181)
    assert tick(
        t_seed, state=state, opener=opener, trading=True,
        abnormal_loader=lambda: broken, t_loader=lambda: away,
    ) == []
    pushed = tick(
        t_seed + timedelta(seconds=181), state=state, opener=opener, trading=True,
        abnormal_loader=lambda: broken, t_loader=lambda: back,
    )
    assert pushed == ["t_trade"]
    assert "【做T提醒】" in sent[-1]["markdown"]["text"]
    assert "回到昨收附近" in sent[-1]["markdown"]["text"]
    assert "高于分时均价" not in sent[-1]["markdown"]["text"]


def test_auth_alert_stays_plain_text():
    captured = []

    def opener(request, timeout=10):
        captured.append(json.loads(request.data.decode()))
        return None

    send_dingtalk(
        "https://oapi.dingtalk.com/robot/send?access_token=test",
        "SECtest",
        "TSP 资讯采集：钉钉登录已失效，请在宿主机重新登录。",
        opener=opener,
    )
    assert captured[0]["msgtype"] == "text"
    assert "登录已失效" in captured[0]["text"]["content"]
    assert "【热点候选】" not in captured[0]["text"]["content"]


def test_test_message_requires_confirm(monkeypatch):
    from app.news.push import send_test
    monkeypatch.setattr("app.config.settings.dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=test")
    with pytest.raises(ValueError, match="确认"):
        send_test(confirm=False)


def test_limit_price_matches_polars_integer_cents():
    import polars as pl

    from app.price_limits import polars_limit_price
    previous = [10.0, 10.03, 7.77, 20.0]
    pct = [0.10, 0.10, 0.20, 0.05]
    frame = pl.DataFrame({"prev": previous, "pct": pct}).with_columns(
        polars_limit_price(pl.col("prev"), pl.col("pct"), up=True).alias("up"),
        polars_limit_price(pl.col("prev"), pl.col("pct"), up=False).alias("down"),
    )
    for row in frame.to_dicts():
        assert limit_price(row["prev"], row["pct"], up=True) == row["up"]
        assert limit_price(row["prev"], row["pct"], up=False) == row["down"]


def test_abnormal_signals_use_price_against_prior_levels():
    when = date(2026, 10, 9)
    base = {"symbol": "600519.SH", "name": "贵州茅台", "trade_date": when, "open": 10.0}
    assert abnormal_signals({**base, "close": 11.0, "high": 11.0, "low": 10.0, "prev_close": 10.0}) == {"limit_up"}
    broken = abnormal_signals({
        **base, "close": 10.5, "high": 11.0, "low": 10.2, "prev_close": 10.0,
    })
    assert broken == {"broken"}
    recovery = abnormal_signals({
        **base, "close": 9.5, "open": 9.2, "high": 9.6, "low": 9.0, "prev_close": 10.0,
    })
    assert recovery == {"recovery"}
    assert "new_high" not in abnormal_signals({**base, "close": 10.2, "high": 10.2, "low": 10.0})
    assert abnormal_signals({
        **base, "close": 10.2, "qfq_close": 10.2, "high": 10.2, "low": 10.0, "prior_high": 10.0,
    }) == {"new_high"}
    # 前复权价创新高，原始价没有封板。两套口径不能混用。
    split = abnormal_signals({
        **base,
        "qfq_close": 21.0,
        "close": 10.5,
        "raw_close": 10.5,
        "high": 10.5,
        "raw_high": 10.5,
        "low": 10.0,
        "raw_low": 10.0,
        "prev_close": 10.0,
        "prior_high": 20.5,
    })
    assert split == {"new_high"}


def test_t_universe_is_watchlist_until_positions_are_requested():
    assert t_universe(["600519.SH", "600519.SH", ""], ["000001.SZ"], include_positions=False, cap=40) == ["600519.SH"]
    assert t_universe(["600519.SH"], ["000001.SZ", "600519.SH"], include_positions=True, cap=40) == [
        "600519.SH", "000001.SZ",
    ]
    assert t_universe(["A", "B", "C"], ["D"], include_positions=True, cap=2) == ["A", "B"]


def test_count_rule_edges_baselines_and_respects_cooldown():
    stayed = [
        set(),
        {"above_vwap"},
        set(),
        {"above_vwap"},
        {"above_vwap"},
    ]
    assert count_rule_edges(stayed, 10) == {"above_vwap": 1}
    returned = [set(), {"above_vwap"}] + [set()] * 10 + [{"above_vwap"}]
    assert count_rule_edges(returned, 10) == {"above_vwap": 2}


def test_session_signal_sets_follow_the_same_bands():
    bars = [
        {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100.0, "amount": 100000.0},
        {"open": 10.2, "high": 10.2, "low": 9.9, "close": 10.2, "volume": 10.0, "amount": 10200.0},
        {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1.0, "amount": 1000.0},
    ]
    sets = session_signal_sets(
        bars, mode="t", symbol="600519.SH", prev_close=10.0, vwap_band=0.015,
    )
    assert sets[0] == set()
    assert sets[1] == {"above_vwap", "near_high"}
    assert sets[2] == {"near_prev_close"}
    default = session_signal_sets(bars, mode="t", symbol="600519.SH", prev_close=10.0)
    assert default[1] == {"above_vwap", "near_high"}
    wider_default = session_signal_sets(
        bars, mode="t", symbol="600519.SH", prev_close=10.0, vwap_band=0.020,
    )
    assert "above_vwap" not in wider_default[1]
    wider = session_signal_sets(
        bars, mode="t", symbol="600519.SH", prev_close=10.0, vwap_band=0.025,
    )
    assert "above_vwap" not in wider[1]
    assert "near_high" in wider[1]


def test_t_loader_starts_from_watchlist(monkeypatch):
    monkeypatch.setattr("app.services.watchlist.list_symbols", lambda: [{"symbol": "600519.SH"}])
    seen = {}

    def live(symbols):
        seen["symbols"] = list(symbols)
        return []

    monkeypatch.setattr("app.news.push._live_rows", live)
    monkeypatch.setattr("app.news.push._flag_on", lambda field: False)
    assert _load_t() == ({}, {}, {})
    assert seen["symbols"] == ["600519.SH"]

    monkeypatch.setattr(
        "app.news.push._flag_on",
        lambda field: field == "news_push_t_include_positions",
    )
    monkeypatch.setattr("app.news.push._position_symbols", lambda: ["000001.SZ"])
    _load_t()
    assert seen["symbols"] == ["600519.SH", "000001.SZ"]


def test_abnormal_loader_computes_from_live_price(monkeypatch):
    monkeypatch.setattr("app.services.watchlist.list_symbols", lambda: [{"symbol": "600519.SH"}])
    monkeypatch.setattr("app.news.push._flag_on", lambda field: False)
    monkeypatch.setattr("app.news.push._live_rows", lambda symbols: [{
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "close": 11.0,
        "high": 11.0,
        "low": 10.0,
        "open": 10.0,
        "prev_close": 10.0,
        "trade_date": date(2026, 10, 9),
        "signal_limit_up": False,
    }])
    current, pct, names = _load_abnormal()
    assert current["600519.SH"] == {"limit_up"}
    assert pct == {}
    assert names["600519.SH"] == "贵州茅台"


def test_fresh_quote_cache_keeps_raw_and_qfq_apart(monkeypatch):
    import polars as pl

    from app.main import app
    from app.market_time import cn_today

    frame = pl.DataFrame({
        "symbol": ["600519.SH"],
        "name": ["贵州茅台"],
        "close": [21.0],
        "open": [20.0],
        "high": [21.0],
        "low": [20.0],
        "raw_close": [10.5],
        "raw_high": [10.5],
        "raw_low": [10.0],
        "volume": [100.0],
        "amount": [105000.0],
        "prev_close": [20.0],
        "signal_limit_up": [True],
    })

    class Service:
        def status(self):
            return {"enabled": True, "quote_age_ms": 1_000}

        def get_enriched_today(self):
            return frame, cn_today()

    monkeypatch.setattr(app.state, "quote_service", Service(), raising=False)
    monkeypatch.setattr("app.news.push._prior_levels", lambda symbols: {
        "600519.SH": {"prior_high": 20.5, "prior_low": 8.0, "prev_close": 20.0, "adj_factor": 2.0},
    })
    rows = _live_rows(["600519.SH"])
    assert "signal_limit_up" not in rows[0]
    assert rows[0]["close"] == 10.5
    assert rows[0]["prev_close"] == 10.0
    assert rows[0]["qfq_close"] == 21.0
    assert rows[0]["prior_high"] == 20.5
    signals = abnormal_signals({**rows[0], "trade_date": date(2026, 10, 9), "symbol": "600519.SH"})
    assert signals == {"new_high"}


def test_stale_or_unknown_quote_age_does_not_read_the_cache(monkeypatch):
    from app.main import app

    class Stale:
        def status(self):
            return {"enabled": True, "quote_age_ms": 181_000}

        def get_enriched_today(self):
            raise AssertionError("过期缓存不能当实时报价")

    monkeypatch.setattr(app.state, "quote_service", Stale(), raising=False)
    assert _cached_live_rows(["600519.SH"]) is None

    class Unknown:
        def status(self):
            return {"enabled": True, "quote_age_ms": -1}

        def get_enriched_today(self):
            raise AssertionError("没有拉取时间不能当新鲜缓存")

    monkeypatch.setattr(app.state, "quote_service", Unknown(), raising=False)
    assert _cached_live_rows(["600519.SH"]) is None


def test_quote_batch_is_used_when_cache_is_cold(monkeypatch):
    from app.main import app
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    class Off:
        def status(self):
            return {"enabled": False}

    monkeypatch.setattr(app.state, "quote_service", Off(), raising=False)
    monkeypatch.setattr("app.news.push._prior_levels", lambda symbols: {
        "600519.SH": {"prior_high": 12.0, "prior_low": 8.0, "prev_close": None, "adj_factor": 1.0},
        "000001.SZ": {"prior_high": 12.0, "prior_low": 8.0, "prev_close": None, "adj_factor": 1.0},
        "601318.SH": {"prior_high": 12.0, "prior_low": 8.0, "prev_close": None, "adj_factor": 1.0},
    })
    calls = []

    def get(symbols):
        calls.append(list(symbols))
        return [
            {
                "symbol": symbol,
                "last_price": 11.0,
                "prev_close": 10.0,
                "open": 10.0,
                "high": 11.0,
                "low": 10.0,
                "volume": 100,
                "amount": 110000,
                "ext": {"name": "测试"},
            }
            for symbol in symbols
        ]

    monkeypatch.setattr(
        "app.tickflow.client.get_client",
        lambda: SimpleNamespace(quotes=SimpleNamespace(get=get)),
    )
    monkeypatch.setattr(
        app.state,
        "capabilities",
        CapabilitySet({Cap.QUOTE_BATCH: CapabilityLimits(rpm=300, batch=2)}),
        raising=False,
    )
    rows = _live_rows(["600519.SH", "000001.SZ", "601318.SH"])
    assert calls == [["600519.SH", "000001.SZ"], ["601318.SH"]]
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["600519.SH"]["name"] == "测试"
    assert by_symbol["600519.SH"]["qfq_close"] == 11.0
    signals = abnormal_signals({**by_symbol["600519.SH"], "trade_date": date(2026, 10, 9)})
    assert "limit_up" in signals
    assert "new_high" not in signals

    calls.clear()
    monkeypatch.setattr(app.state, "capabilities", CapabilitySet({}), raising=False)
    assert _live_rows(["600519.SH"]) == []
    assert calls == []


def _replay_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "replay_push_rules.py"
    spec = importlib.util.spec_from_file_location("replay_push_rules", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_counts_edges_on_a_threshold_grid(tmp_path):
    import polars as pl
    module = _replay_module()
    bars = [
        (datetime(2026, 10, 8, 9, 31), 10.0, 10.0, 10.0, 10.0, 100.0, 100000.0),
        (datetime(2026, 10, 9, 9, 31), 10.0, 10.0, 10.0, 10.0, 100.0, 100000.0),
        (datetime(2026, 10, 9, 9, 32), 10.2, 10.2, 9.9, 10.2, 10.0, 10200.0),
        (datetime(2026, 10, 9, 9, 33), 10.0, 10.0, 10.0, 10.0, 1.0, 1000.0),
    ]
    rows = [
        {
            "symbol": symbol,
            "datetime": moment,
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
        for symbol in ("600519.SH", "000001.SZ")
        for moment, opened, high, low, close, volume, amount in bars
    ]
    for day in ("2026-10-08", "2026-10-09"):
        part = tmp_path / f"date={day}"
        part.mkdir()
        pl.DataFrame(rows).filter(
            pl.col("datetime").dt.date() == date.fromisoformat(day),
        ).write_parquet(part / "part.parquet")
    frame = module.load_minutes(tmp_path, ["600519.SH"], date(2026, 10, 8), date(2026, 10, 9))
    assert set(frame["symbol"].unique().to_list()) == {"600519.SH"}
    counted = module.replay(frame)
    picked = [
        row for row in counted
        if row["date"] == "2026-10-09"
        and row["vwap_pct"] == 0.015
        and row["range_pct"] == 0.003
        and row["cooldown_min"] == 10
    ]
    triggers = {(row["kind"], row["rule"]): row["triggers"] for row in picked}
    assert triggers[("t_trade", "above_vwap")] == 1
    assert triggers[("t_trade", "near_high")] == 1
    assert triggers[("t_trade", "near_prev_close")] == 1
    wide = [
        row for row in counted
        if row["rule"] == "above_vwap" and row["vwap_pct"] == 0.025 and row["cooldown_min"] == 30
    ]
    assert wide == []
    assert module.main(["--symbols", "600519.SH", "--days", "1"]) == 2
    assert module.main(["--symbols", "600519.SH", "--days", "2", "--path", str(tmp_path / "missing")]) == 2


def _t_push(monkeypatch, tmp_path, *, once="", cap=20, vwap_min=1, range_min=120):
    monkeypatch.setattr("app.news.push.push_master_enabled", lambda: True)
    monkeypatch.setattr("app.news.push.push_type_enabled", lambda kind: kind == "t_trade")
    monkeypatch.setattr("app.config.settings.dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=test")
    monkeypatch.setattr("app.config.settings.dingtalk_secret", "")
    monkeypatch.setattr("app.config.settings.news_push_t_cooldown_min", vwap_min)
    monkeypatch.setattr("app.config.settings.news_push_t_range_cooldown_min", range_min)
    monkeypatch.setattr("app.config.settings.news_push_t_range_once_per_day", once)
    monkeypatch.setattr("app.config.settings.news_push_t_daily_cap", cap)
    state = PushState(tmp_path / "push_state.json")
    sent = []

    def opener(request, timeout=10):
        sent.append(json.loads(request.data.decode()))
        return None

    def run(moment, signals, pct=0.02, symbol="600519.SH"):
        loaded = ({symbol: set(signals)}, {symbol: pct}, {symbol: "贵州茅台"})
        return tick(moment, state=state, opener=opener, trading=True, t_loader=lambda: loaded)

    return sent, run


def test_near_high_waits_until_after_the_open_and_uses_its_own_cooldown(tmp_path, monkeypatch):
    sent, run = _t_push(monkeypatch, tmp_path)
    early = datetime(2026, 10, 9, 9, 40, tzinfo=CN_TZ)
    assert run(early, set()) == []
    assert run(early + timedelta(seconds=181), {"near_high"}) == []
    assert sent == []
    later = datetime(2026, 10, 9, 10, 5, tzinfo=CN_TZ)
    assert run(later, {"near_high"}) == []
    fired = later + timedelta(seconds=181)
    assert run(fired, {"above_vwap", "near_low"}) == ["t_trade"]
    assert "高于分时均价" in sent[-1]["markdown"]["text"]
    assert "接近日内低点" in sent[-1]["markdown"]["text"]
    mid = fired + timedelta(seconds=181)
    assert run(mid, set()) == []
    assert run(mid + timedelta(seconds=181), {"above_vwap", "near_low"}) == ["t_trade"]
    assert "高于分时均价" in sent[-1]["markdown"]["text"]
    assert "接近日内低点" not in sent[-1]["markdown"]["text"]


def test_range_once_per_stock_per_day_still_allows_vwap(tmp_path, monkeypatch):
    sent, run = _t_push(monkeypatch, tmp_path, once="true", cap=20, range_min=1)
    start = datetime(2026, 10, 9, 10, 20, tzinfo=CN_TZ)
    assert run(start, set()) == []
    assert run(start + timedelta(seconds=181), {"near_high"}) == ["t_trade"]
    assert run(start + timedelta(seconds=362), set()) == []
    assert run(start + timedelta(seconds=543), {"near_low"}) == []
    assert run(start + timedelta(seconds=724), {"above_vwap"}) == ["t_trade"]
    assert "接近日内高点" in sent[0]["markdown"]["text"]
    assert "高于分时均价" in sent[1]["markdown"]["text"]
    assert len(sent) == 2


def test_t_daily_cap_counts_messages_across_the_watchlist(tmp_path, monkeypatch):
    sent, run = _t_push(monkeypatch, tmp_path, cap=1)
    moment = datetime(2026, 10, 9, 13, 10, tzinfo=CN_TZ)
    assert run(moment, set(), symbol="600519.SH") == []
    assert run(moment + timedelta(seconds=181), {"above_vwap"}, symbol="600519.SH") == ["t_trade"]
    assert run(moment + timedelta(seconds=362), {"above_vwap"}, symbol="000001.SZ") == []
    assert len(sent) == 1
