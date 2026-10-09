"""钉钉推送：加签、文案、冷却和触发。不访问外网。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.market_time import CN_TZ
from app.news.dingtalk import signed_url
from app.news.host_collector import send_dingtalk
from app.news.push import (
    PushState,
    filter_prev_close,
    format_hot_markdown,
    format_symbol_markdown,
    format_test_markdown,
    hot_material,
    new_edges,
    pick_refs,
    t_conditions,
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
