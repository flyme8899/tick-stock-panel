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
from app.news.hot_events import HOT_RULES_VERSION
from app.news.push import (
    HOT_NEW_BURST,
    PushState,
    _cached_live_rows,
    _live_rows,
    _load_abnormal,
    _load_hot,
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
    # state 优先：只有 scored 才算已验证，「无标可验」绝不能被当成有效信号。
    # 这正是把黑名单改成白名单要防的 fail-open：早前 `strength not in {"","无"}`
    # 会让任何新增取值默认落进「已验证」，把无标事件映射进钉钉推送。
    assert event_is_verified({"strength": "无", "state": "unverifiable"}) is False
    assert event_is_verified({"strength": "无", "state": "unconfirmed"}) is False
    assert event_is_verified({"strength": "无", "state": "pending"}) is False
    assert event_is_verified({"strength": "强", "state": "scored"}) is True
    # 未知/未来新增的 strength 取值一律不算已验证（白名单语义）。
    assert event_is_verified({"strength": "无标"}) is False
    assert event_is_verified({"strength": "某种新档位"}) is False
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
    # 9:25 会换视图签名。升温比较留在同一份签名里，避免被基线重置吃掉。
    later = datetime(2026, 10, 9, 9, 20, tzinfo=CN_TZ)
    assert tick(later, state=state, opener=opener, trading=True, hot_loader=lambda: grown) == ["hot"]
    assert tick(later, state=state, opener=opener, trading=True, hot_loader=lambda: grown) == []
    assert "【热点候选】变化" in sent[1]["markdown"]["text"]
    assert "本次变化" in sent[1]["markdown"]["text"]
    assert "升温" in sent[1]["markdown"]["text"]
    assert "半导体" in sent[1]["markdown"]["text"]


def test_hot_rules_version_mismatch_drops_the_previous_snapshot(tmp_path, monkeypatch):
    """口径版本不匹配时，上一轮快照只建基线、不比较分数（PR #97 评审）。

    升级计分/准入规则后，旧快照的分数是新口径算不出来的。直接比较会把
    importance 20→130 这类口径差报成「升温」。版本不匹配 → previous 作废；
    版本一致后同样的跳变正常识别。
    """
    monkeypatch.setattr("app.news.push.push_master_enabled", lambda: True)
    monkeypatch.setattr("app.news.push.push_type_enabled", lambda kind: kind == "hot")
    monkeypatch.setattr("app.config.settings.dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=test")
    monkeypatch.setattr("app.config.settings.dingtalk_secret", "")
    monkeypatch.setattr("app.config.settings.news_push_top_n", 5)
    monkeypatch.setattr("app.config.settings.news_push_min_stories", 2)
    monkeypatch.setattr("app.config.settings.news_push_score_jump", 0.5)
    monkeypatch.setattr("app.config.settings.news_push_hot_cooldown_min", 30)
    state = PushState(tmp_path / "push_state.json")
    sent = []

    def opener(request, timeout=10):
        sent.append(json.loads(request.data.decode()))
        return None

    rows = [{
        "kind": "sector",
        "key": "半导体",
        "name": "半导体",
        "score": 2,
        "story_count": 3,
        "sources": ["cls"],
        "growth": 1.8,
    }]
    # 9:25 之后视图签名不再变，版本不匹配才是这次不推的原因。
    pre = datetime(2026, 10, 9, 9, 30, tzinfo=CN_TZ)
    assert tick(pre, state=state, opener=opener, trading=True, hot_loader=lambda: rows) == []
    assert state.data["hot_rules_version"] == HOT_RULES_VERSION
    # 模拟升级：上一轮快照的规则版本是旧值，分数按旧口径算过。
    state.data["hot_rules_version"] = HOT_RULES_VERSION - 1
    grown = [{**rows[0], "score": 20, "story_count": 5}]
    later = datetime(2026, 10, 9, 9, 40, tzinfo=CN_TZ)
    assert tick(later, state=state, opener=opener, trading=True, hot_loader=lambda: grown) == []
    assert sent == []  # 口径差没有报成升温
    # 版本一致后，10 倍跳变正常识别。
    again = datetime(2026, 10, 9, 9, 50, tzinfo=CN_TZ)
    jumped = [{**rows[0], "score": 40, "story_count": 6}]
    assert tick(again, state=state, opener=opener, trading=True, hot_loader=lambda: jumped) == ["hot"]
    assert "升温" in sent[-1]["markdown"]["text"]


def _sector(key: str, score: float, stories: int) -> dict:
    return {
        "kind": "sector",
        "key": key,
        "name": key,
        "score": score,
        "story_count": stories,
        "sources": ["cls"],
        "growth": 1.5,
    }


def test_hot_view_switch_at_the_close_does_not_flood(tmp_path, monkeypatch):
    """15:00 榜单从当天切到下一场时只建基线，不把整批名次变化报成新热点。"""
    monkeypatch.setattr("app.news.push.push_master_enabled", lambda: True)
    monkeypatch.setattr("app.news.push.push_type_enabled", lambda kind: kind == "hot")
    monkeypatch.setattr("app.config.settings.dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=test")
    monkeypatch.setattr("app.config.settings.dingtalk_secret", "")
    monkeypatch.setattr("app.config.settings.news_push_top_n", 5)
    monkeypatch.setattr("app.config.settings.news_push_min_stories", 2)
    monkeypatch.setattr("app.config.settings.news_push_score_jump", 0.5)
    monkeypatch.setattr("app.config.settings.news_push_hot_cooldown_min", 30)
    state = PushState(tmp_path / "push_state.json")
    sent = []

    def opener(request, timeout=10):
        sent.append(json.loads(request.data.decode()))
        return None

    morning = datetime(2026, 10, 9, 14, 50, tzinfo=CN_TZ)
    assert tick(
        morning, state=state, opener=opener, trading=True,
        hot_loader=lambda: [_sector("半导体", 2, 3)],
    ) == []
    assert state.data["hot_view"] == "2026-10-09|2026-10-09"
    # 升级前的快照没有 hot_view。缺字段也只建基线。
    state.data.pop("hot_view")
    assert tick(
        morning + timedelta(minutes=6), state=state, opener=opener, trading=True,
        hot_loader=lambda: [_sector("银行", 8, 4)],
    ) == []
    assert sent == []
    assert state.data["hot_view"] == "2026-10-09|2026-10-09"

    at_close = datetime(2026, 10, 9, 15, 6, tzinfo=CN_TZ)
    assert tick(
        at_close, state=state, opener=opener, trading=True,
        hot_loader=lambda: [_sector("碳酸锂", 9, 4)],
    ) == []
    assert sent == []
    assert state.data["hot_view"] == "2026-10-12|2026-10-09"

    later = datetime(2026, 10, 9, 15, 12, tzinfo=CN_TZ)
    assert tick(
        later, state=state, opener=opener, trading=True,
        hot_loader=lambda: [_sector("碳酸锂", 9, 4), _sector("机器人", 3, 3)],
    ) == ["hot"]
    assert "【热点候选】变化" in sent[-1]["markdown"]["text"]
    assert "机器人" in sent[-1]["markdown"]["text"]
    assert state.data["hot_view"] == "2026-10-12|2026-10-09"


def test_auction_open_drops_prior_intraday_without_a_change_flood(tmp_path, monkeypatch):
    """9:25 去掉上一场盘中时视图签名变了，只建基线，不把剩下的条目报成新热点。"""
    monkeypatch.setattr("app.news.push.push_master_enabled", lambda: True)
    monkeypatch.setattr("app.news.push.push_type_enabled", lambda kind: kind == "hot")
    monkeypatch.setattr("app.config.settings.dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=test")
    monkeypatch.setattr("app.config.settings.dingtalk_secret", "")
    monkeypatch.setattr("app.config.settings.news_push_top_n", 5)
    monkeypatch.setattr("app.config.settings.news_push_min_stories", 2)
    monkeypatch.setattr("app.config.settings.news_push_score_jump", 0.5)
    monkeypatch.setattr("app.config.settings.news_push_hot_cooldown_min", 30)
    state = PushState(tmp_path / "push_state.json")
    sent = []

    def opener(request, timeout=10):
        sent.append(json.loads(request.data.decode()))
        return None

    before = datetime(2026, 10, 12, 9, 10, tzinfo=CN_TZ)
    assert tick(
        before, state=state, opener=opener, trading=True,
        hot_loader=lambda: [_sector("半导体", 4, 3), _sector("银行", 3, 3)],
    ) == []
    assert state.data["hot_view"] == "2026-10-12|2026-10-09"

    opened = datetime(2026, 10, 12, 9, 26, tzinfo=CN_TZ)
    assert tick(
        opened, state=state, opener=opener, trading=True,
        hot_loader=lambda: [_sector("碳酸锂", 9, 4)],
    ) == []
    assert sent == []
    assert state.data["hot_view"] == "2026-10-12|2026-10-12"

    later = datetime(2026, 10, 12, 9, 40, tzinfo=CN_TZ)
    assert tick(
        later, state=state, opener=opener, trading=True,
        hot_loader=lambda: [_sector("碳酸锂", 9, 4), _sector("机器人", 3, 3)],
    ) == ["hot"]
    assert "机器人" in sent[-1]["markdown"]["text"]
    assert state.data["hot_view"] == "2026-10-12|2026-10-12"


def _enable_hot(monkeypatch) -> None:
    monkeypatch.setattr("app.news.push.push_master_enabled", lambda: True)
    monkeypatch.setattr("app.news.push.push_type_enabled", lambda kind: kind == "hot")
    monkeypatch.setattr(
        "app.config.settings.dingtalk_webhook_url",
        "https://oapi.dingtalk.com/robot/send?access_token=test",
    )
    monkeypatch.setattr("app.config.settings.dingtalk_secret", "")
    monkeypatch.setattr("app.config.settings.news_push_top_n", 5)
    monkeypatch.setattr("app.config.settings.news_push_min_stories", 2)
    monkeypatch.setattr("app.config.settings.news_push_score_jump", 0.5)
    monkeypatch.setattr("app.config.settings.news_push_hot_cooldown_min", 30)


def _collect(sent: list):
    def opener(request, timeout=10):
        sent.append(json.loads(request.data.decode()))
        return None

    return opener


def test_restart_empty_snapshot_then_full_board_does_not_push(tmp_path, monkeypatch):
    """重启后先遇到空快照，再遇到第一份完整榜，不能推「变化」。

    空结果或正在计算的半截榜若被写成基线，下一轮完整榜会整批变成新进。
    """
    _enable_hot(monkeypatch)
    state = PushState(tmp_path / "push_state.json")
    sent = []
    board = [_sector("半导体", 2, 3), _sector("银行", 3, 3)]
    restarted = datetime(2026, 10, 12, 9, 30, tzinfo=CN_TZ)
    assert tick(
        restarted, state=state, opener=_collect(sent), trading=True,
        hot_loader=lambda: {"rows": [], "status": "computing"},
    ) == []
    assert "hot_snapshot" not in state.data
    assert sent == []

    later = restarted + timedelta(minutes=6)
    assert tick(
        later, state=state, opener=_collect(sent), trading=True,
        hot_loader=lambda: board,
    ) == []
    assert sent == []
    assert [row["key"] for row in state.data["hot_snapshot"]] == ["半导体", "银行"]

    # 已经有基线之后，计算中的半截榜（只有板块）也不能覆盖它。
    partial = restarted + timedelta(minutes=12)
    assert tick(
        partial, state=state, opener=_collect(sent), trading=True,
        hot_loader=lambda: {"rows": [_sector("机器人", 9, 4)], "status": "computing"},
    ) == []
    assert [row["key"] for row in state.data["hot_snapshot"]] == ["半导体", "银行"]
    again = restarted + timedelta(minutes=18)
    assert tick(
        again, state=state, opener=_collect(sent), trading=True,
        hot_loader=lambda: board,
    ) == []
    assert sent == []


def test_load_hot_marks_computing_snapshot_partial(monkeypatch):
    """事件快照还在算时，板块候选有数也要标成 computing，不能当完整榜。"""
    monkeypatch.setattr(
        "app.news.service.hot_event_listing",
        lambda limit=20: {"events": [], "status": "computing"},
    )
    monkeypatch.setattr(
        "app.news.service.hot_candidates",
        lambda kind, limit: [SimpleNamespace(
            kind=kind, key="半导体", name="半导体", score=2, story_count=3,
            sources=["cls"], growth=1.5,
        )],
    )
    loaded = _load_hot()
    assert loaded["status"] == "computing"
    assert loaded["rows"]
    assert {row["kind"] for row in loaded["rows"]} == {"sector", "stock", "etf"}


def test_hot_new_burst_sets_baseline_without_push(tmp_path, monkeypatch):
    """一次新进超过上限时只换基线，不把整批名单推出去。单独一条新进仍会推。"""
    _enable_hot(monkeypatch)
    monkeypatch.setattr("app.config.settings.news_push_top_n", 20)
    state = PushState(tmp_path / "push_state.json")
    sent = []
    start = datetime(2026, 10, 12, 9, 30, tzinfo=CN_TZ)
    assert tick(
        start, state=state, opener=_collect(sent), trading=True,
        hot_loader=lambda: [_sector("半导体", 2, 3)],
    ) == []
    burst = [_sector(f"板块{index}", 2, 3) for index in range(HOT_NEW_BURST + 1)]
    assert tick(
        start + timedelta(minutes=6), state=state, opener=_collect(sent), trading=True,
        hot_loader=lambda: burst,
    ) == []
    assert sent == []
    assert [row["key"] for row in state.data["hot_snapshot"]] == [row["key"] for row in burst]

    added = [*burst, _sector("新能源", 3, 3)]
    assert tick(
        start + timedelta(minutes=12), state=state, opener=_collect(sent), trading=True,
        hot_loader=lambda: added,
    ) == ["hot"]
    assert "新能源" in sent[-1]["markdown"]["text"]
    assert "【热点候选】变化" in sent[-1]["markdown"]["text"]


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


def _enable_hot_push(monkeypatch, tmp_path, *, verified: bool = False):
    monkeypatch.setattr("app.news.push.push_master_enabled", lambda: True)
    monkeypatch.setattr("app.news.push.push_type_enabled", lambda kind: kind == "hot")
    monkeypatch.setattr("app.news.push.push_type_saved", lambda type_id: type_id == "hot_verified" and verified)
    monkeypatch.setattr(
        "app.config.settings.dingtalk_webhook_url",
        "https://oapi.dingtalk.com/robot/send?access_token=test",
    )
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

    return state, sent, opener


def _event_row(key: str, name: str, *, score: float, strength: str) -> dict:
    return {
        "kind": "event",
        "key": key,
        "name": name,
        "score": score,
        "story_count": 3,
        "sources": ["cls", "wscn"],
        "growth": 0,
        "confirmation": {"phase": "intraday", "strength": strength},
    }


def test_unverified_new_event_does_not_trigger_hot_push(tmp_path, monkeypatch):
    state, sent, opener = _enable_hot_push(monkeypatch, tmp_path, verified=True)
    old = _event_row("ev_old", "旧事件", score=3, strength="强")
    fresh = _event_row("ev_new", "尚未验证的新事件", score=4, strength="无")
    morning = datetime(2026, 10, 12, 10, 0, tzinfo=CN_TZ)
    assert tick(morning, state=state, opener=opener, trading=True, hot_loader=lambda: [old]) == []
    later = morning + timedelta(seconds=301)
    assert tick(
        later, state=state, opener=opener, trading=True, hot_loader=lambda: [fresh, old],
    ) == []
    assert sent == []


def test_verified_change_names_what_changed_and_is_logged(tmp_path, monkeypatch, caplog):
    state, sent, opener = _enable_hot_push(monkeypatch, tmp_path, verified=True)
    old = _event_row("ev_old", "旧事件", score=3, strength="强")
    fresh = _event_row("ev_new", "竞价新事件", score=4, strength="中")
    morning = datetime(2026, 10, 12, 10, 0, tzinfo=CN_TZ)
    assert tick(morning, state=state, opener=opener, trading=True, hot_loader=lambda: [old]) == []
    later = morning + timedelta(seconds=301)
    caplog.set_level("INFO", logger="app.news.push")
    assert tick(
        later, state=state, opener=opener, trading=True, hot_loader=lambda: [fresh, old],
    ) == ["hot"]
    text = sent[0]["markdown"]["text"]
    assert "【热点候选】变化" in text
    assert "本次变化" in text
    assert "新进" in text
    assert "竞价新事件" in text
    assert "尚未验证" not in text
    assert text.index("本次变化") < text.index("具体事件")
    assert "钉钉热点候选已推送" in caplog.text
    assert "【热点候选】变化" in caplog.text
    assert "竞价新事件" in caplog.text
    assert state.data.get("hot_digest")


def test_identical_hot_content_is_not_sent_again(tmp_path, monkeypatch):
    state, sent, opener = _enable_hot_push(monkeypatch, tmp_path)
    baseline = [{
        "kind": "sector",
        "key": "半导体",
        "name": "半导体",
        "score": 2,
        "story_count": 3,
        "sources": ["cls"],
        "growth": 1.8,
    }]
    grown = [{**baseline[0], "score": 4, "story_count": 4}]
    morning = datetime(2026, 10, 12, 10, 0, tzinfo=CN_TZ)
    assert tick(morning, state=state, opener=opener, trading=True, hot_loader=lambda: baseline) == []
    saved = json.loads(json.dumps(state.data["hot_snapshot"]))
    later = morning + timedelta(seconds=301)
    assert tick(later, state=state, opener=opener, trading=True, hot_loader=lambda: grown) == ["hot"]
    assert "升温" in sent[0]["markdown"]["text"]
    state.data["hot_snapshot"] = saved
    state.data["hot_day"] = later.date().isoformat()
    state.data["cooldowns"]["hot"] = 0
    state.data["last_hot_check"] = 0
    again = later + timedelta(seconds=301)
    assert tick(again, state=state, opener=opener, trading=True, hot_loader=lambda: grown) == []
    assert len(sent) == 1


def test_t_daily_cap_counts_messages_across_the_watchlist(tmp_path, monkeypatch):
    sent, run = _t_push(monkeypatch, tmp_path, cap=1)
    moment = datetime(2026, 10, 9, 13, 10, tzinfo=CN_TZ)
    assert run(moment, set(), symbol="600519.SH") == []
    assert run(moment + timedelta(seconds=181), {"above_vwap"}, symbol="600519.SH") == ["t_trade"]
    assert run(moment + timedelta(seconds=362), {"above_vwap"}, symbol="000001.SZ") == []
    assert len(sent) == 1


# ================================================================ 做T文案三件套
# 批次 20261028gtpush · P-t-push-copy。只改文案, 不改边沿/冷却/频率/日封顶。


def _t_text(rows, now=None):
    packed = format_symbol_markdown("t_trade", rows, now=now)
    assert packed is not None
    return packed[1]


def test_g_t1_trigger_timestamp_is_shanghai_and_formatted():
    """G-T1: 触发时刻按上海时区、`YYYY-MM-DD HH:MM:SS` 写进正文。"""
    moment = datetime(2026, 10, 28, 14, 52, 7, tzinfo=CN_TZ)
    text = _t_text([{"symbol": "600519.SH", "name": "贵州茅台", "reason": "below_vwap"}], now=moment)
    assert "触发时刻 2026-10-28 14:52:07" in text


def test_g_t1_timestamp_converts_to_shanghai_from_another_zone():
    """G-T1: 非上海时区的 `now` 会被换算过去, 不是直接格式化。

    UTC 06:52 与上海 14:52 是同一时刻。若只 `strftime` 不换算, 文本会写成 06:52,
    读的人完全看不出这是「同一时刻的不同写法」, 会以为是清晨触发的。
    """
    from datetime import timezone
    utc_moment = datetime(2026, 10, 28, 6, 52, 7, tzinfo=timezone.utc)
    text = _t_text([{"symbol": "600519.SH", "name": "贵州茅台", "reason": "below_vwap"}], now=utc_moment)
    assert "触发时刻 2026-10-28 14:52:07" in text
    assert "06:52" not in text


def test_g_t1_timestamp_defaults_to_now_when_absent():
    """G-T1: 不传 `now` 时用当前时刻(不崩、不写空)。"""
    import re as _re
    text = _t_text([{"symbol": "600519.SH", "name": "贵州茅台", "reason": "below_vwap"}])
    assert _re.search(r"触发时刻 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)


def test_g_t2_buy_reasons_map_to_buy_label():
    """G-T2: `below_vwap` / `near_low` → 建议T买入。"""
    for reason in ("below_vwap", "near_low"):
        text = _t_text([{"symbol": "600519.SH", "name": "贵州茅台", "reason": reason}])
        assert "建议T买入" in text, reason


def test_g_t2_sell_reasons_map_to_sell_label():
    """G-T2: `above_vwap` / `near_high` → 建议T卖出。"""
    for reason in ("above_vwap", "near_high"):
        text = _t_text([{"symbol": "600519.SH", "name": "贵州茅台", "reason": reason}])
        assert "建议T卖出" in text, reason


def test_g_t2_prev_close_maps_to_watch_not_a_direction():
    """G-T2: `near_prev_close` → 建议T观察。

    回到昨收既不是「便宜了」也不是「贵了」, 给它一个方向就是把中性信号说成方向信号。
    """
    text = _t_text([{"symbol": "600519.SH", "name": "贵州茅台", "reason": "near_prev_close"}])
    assert "建议T观察" in text
    assert "建议T买入" not in text
    assert "建议T卖出" not in text


def test_g_t2_labels_are_a_partition_of_known_reasons():
    """G-T2: 三个标签集合两两不交, 且覆盖 `T_LABELS` 的全部键。

    覆盖性保证「新加一个 reason 却忘了分类」会红 —— 否则它会静默落进「观察」,
    而「观察」是有语义的, 不该是默认垃圾桶。
    """
    from app.news.push import T_LABELS, _T_BUY_REASONS, _T_SELL_REASONS, _T_WATCH_REASONS
    assert not (_T_BUY_REASONS & _T_SELL_REASONS)
    assert not (_T_BUY_REASONS & _T_WATCH_REASONS)
    assert not (_T_SELL_REASONS & _T_WATCH_REASONS)
    assert set(_T_BUY_REASONS | _T_SELL_REASONS | _T_WATCH_REASONS) == set(T_LABELS)
    assert _T_BUY_REASONS == frozenset({"below_vwap", "near_low"})
    assert _T_SELL_REASONS == frozenset({"above_vwap", "near_high"})


def test_g_t2_multiple_rows_each_get_their_own_label():
    """G-T2: 一行里买/卖/观察并存时, 每条各带各的标签, 不会串。"""
    text = _t_text([
        {"symbol": "600519.SH", "name": "贵州茅台", "reason": "below_vwap"},
        {"symbol": "000001.SZ", "name": "平安银行", "reason": "above_vwap"},
        {"symbol": "300750.SZ", "name": "宁德时代", "reason": "near_prev_close"},
    ])
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == 3
    assert "建议T买入" in lines[0] and "600519.SH" in lines[0]
    assert "建议T卖出" in lines[1] and "000001.SZ" in lines[1]
    assert "建议T观察" in lines[2] and "300750.SZ" in lines[2]


def test_g_t3_reason_line_carries_chinese_kind_and_quotes():
    """G-T3: 理由字段 = 种类中文 + 现价/均价/高/低。"""
    text = _t_text([{
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "reason": "below_vwap",
        "price": 1500.5,
        "vwap": 1520.123,
        "high": 1530.0,
        "low": 1490.0,
    }])
    assert "低于分时均价" in text
    assert "现价 1500.50" in text
    assert "均价 1520.12" in text
    assert "高 1530.00" in text
    assert "低 1490.00" in text


def test_g_t3_missing_quotes_are_omitted_not_placeheld():
    """G-T3: 缺报价的项**不出现**, 不写 `--` / `0.00` 占位。

    占位会被读成「有这个数但很小」; 0.00 更糟, 它看起来像一个真实价格。
    """
    text = _t_text([{"symbol": "600519.SH", "name": "贵州茅台", "reason": "near_low"}])
    assert "接近日内低点" in text
    for token in ("现价", "均价", "高 ", "低 "):
        assert token not in text, token
    assert "--" not in text
    assert "0.00" not in text


def test_g_t3_unknown_reason_falls_back_to_raw_code():
    """G-T3: 拿不到中文时退回**原始码**, 不退成「未知」。

    原始码至少能搜到、能追到规则名; 「未知」等于把线索丢了。
    """
    text = _t_text([{"symbol": "600519.SH", "name": "贵州茅台", "reason": "some_new_rule"}])
    assert "some_new_rule" in text
    assert "未知" not in text


def test_g_t3_zero_and_negative_quotes_are_treated_as_missing():
    """G-T3: 0 / 负数价格不渲染 —— 那不是价格。"""
    text = _t_text([{
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "reason": "near_low",
        "price": 0,
        "high": -1,
    }])
    assert "现价" not in text
    assert "0.00" not in text


def test_g_t4_abnormal_markdown_is_byte_identical_to_old_shape():
    """G-T4: 异动文案**一个字没改** —— 没有触发时刻行, 没有建议标签。"""
    packed = format_symbol_markdown("abnormal", [{
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "detail": "涨停",
    }])
    assert packed == ("【异动监控】", "### 【异动监控】\n- 贵州茅台 600519.SH · 涨停\n\n仅供内部研究。")


def test_g_t4_abnormal_ignores_now_keyword():
    """G-T4: 即使传了 `now`, 异动文案也不写时刻(它没有这个概念)。"""
    moment = datetime(2026, 10, 28, 14, 52, 7, tzinfo=CN_TZ)
    with_now = format_symbol_markdown("abnormal", [{"symbol": "600519.SH", "name": "茅台", "detail": "涨停"}], now=moment)
    without = format_symbol_markdown("abnormal", [{"symbol": "600519.SH", "name": "茅台", "detail": "涨停"}])
    assert with_now == without


def test_g_t5_empty_rows_still_returns_none():
    """G-T5: 没有行就返回 `None`(调用方据此不发), 空文案不能被当成「有内容」。"""
    assert format_symbol_markdown("t_trade", []) is None
    assert format_symbol_markdown("abnormal", []) is None


def test_g_t6_cooldown_and_cap_constants_unchanged():
    """G-T6: 冷却/频率/日封顶/阈值常量**一个都没动**。

    本批次只改文案。这些数字若被顺手调过, 文案再好也是另一件事了。
    """
    from app.news import push as P
    assert P.INTRADAY_CHECK_S == 180
    assert P.T_CAP == 40
    assert P.VWAP_BAND == 0.015
    assert P.RANGE_BAND == 0.003
    assert P.PREV_BAND == 0.003
    assert P.RANGE_RULES == frozenset({"near_high", "near_low"})


def test_g_t7_t_push_still_cools_down_after_sending(tmp_path, monkeypatch):
    """G-T7: 加了文案之后**冷却照旧** —— 同一 (符号, 理由) 不会连发。"""
    sent, run = _t_push(monkeypatch, tmp_path)
    first = datetime(2026, 10, 9, 10, 5, tzinfo=CN_TZ)
    assert run(first, set()) == []
    fired = first + timedelta(seconds=181)
    assert run(fired, {"below_vwap"}) == ["t_trade"]
    # 立刻再跑同一理由: 冷却中, 不发
    assert run(fired + timedelta(seconds=181), {"below_vwap"}) == []
    assert len(sent) == 1


def test_g_t7_t_daily_cap_still_enforced(tmp_path, monkeypatch):
    """G-T7: 日封顶照旧 —— 文案变长不影响计数。"""
    sent, run = _t_push(monkeypatch, tmp_path, cap=1)
    moment = datetime(2026, 10, 9, 13, 10, tzinfo=CN_TZ)
    assert run(moment, set()) == []
    assert run(moment + timedelta(seconds=181), {"above_vwap"}, symbol="600519.SH") == ["t_trade"]
    assert run(moment + timedelta(seconds=362), {"above_vwap"}, symbol="000001.SZ") == []
    assert len(sent) == 1


def test_g_t8_pushed_text_carries_all_three_pieces(tmp_path, monkeypatch):
    """G-T8: 真走一遍 `tick`, 发出去的正文里三件套**都在**。"""
    sent, run = _t_push(monkeypatch, tmp_path)
    moment = datetime(2026, 10, 9, 10, 5, tzinfo=CN_TZ)
    assert run(moment, set()) == []
    fired = moment + timedelta(seconds=181)
    assert run(fired, {"below_vwap"}) == ["t_trade"]
    text = sent[-1]["markdown"]["text"]
    assert "触发时刻 2026-10-09 10:08:01" in text
    assert "建议T买入" in text
    assert "低于分时均价" in text


def test_g_t8_quote_lookup_failure_does_not_block_the_push(tmp_path, monkeypatch):
    """G-T8: 取报价失败时**仍然发**, 只是文案里没有报价数字。

    文案是锦上添花。为了一段好看的文案让一条本该发出的提醒发不出去, 是把
    优先级搞反了。
    """
    sent, run = _t_push(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.news.push._live_quote_snapshot",
        lambda symbols: (_ for _ in ()).throw(RuntimeError("quote down")),
    )
    moment = datetime(2026, 10, 9, 10, 5, tzinfo=CN_TZ)
    assert run(moment, set()) == []
    fired = moment + timedelta(seconds=181)
    assert run(fired, {"below_vwap"}) == ["t_trade"]
    text = sent[-1]["markdown"]["text"]
    assert "建议T买入" in text
    assert "现价" not in text


def test_g_t9_quote_snapshot_computes_vwap_from_amount_and_volume():
    """G-T9: 均价 = amount / (volume * 100), 与 `t_conditions` 同一口径。

    volume 是「手」, amount 是「元」—— 不乘 100 会算出 100 倍的均价,
    而那个数看起来仍然像一个价格。
    """
    import app.news.push as _p
    row = {
        "symbol": "600519.SH",
        "close": 100.0,
        "high": 102.0,
        "low": 98.0,
        "volume": 10000,
        "amount": 10000 * 100 * 100.0,  # vwap = 100.0
    }
    orig = _p._live_rows
    try:
        _p._live_rows = lambda symbols: [row]
        snap = _p._live_quote_snapshot(["600519.SH"])
        quotes = _p._quote_lookup("t_trade", [("600519.SH", "below_vwap")])
    finally:
        _p._live_rows = orig
    assert snap["600519.SH"]["close"] == 100.0
    got = quotes[("600519.SH", "below_vwap")]
    assert got["price"] == pytest.approx(100.0)
    assert got["vwap"] == pytest.approx(100.0), "volume 单位是手, 不乘 100 会差 100 倍"


def test_g_t9_quote_lookup_passes_only_fresh_symbols():
    """G-T9: 只给**要推的那几条**取报价, 不整池拉。"""
    import app.news.push as _p
    seen = {}
    orig = _p._live_quote_snapshot

    def fake(symbols):
        seen["symbols"] = list(symbols)
        return {}

    try:
        _p._live_quote_snapshot = fake
        _p._quote_lookup("t_trade", [("000001.SZ", "below_vwap"), ("600519.SH", "near_low")])
    finally:
        _p._live_quote_snapshot = orig
    assert seen["symbols"] == ["000001.SZ", "600519.SH"]


def test_g_t10_abnormal_never_asks_for_quotes():
    """G-T10: 异动**不取**报价 —— 它的文案不用这些数字, 多取一次是多一次风险。"""
    import app.news.push as _p
    orig = _p._live_quote_snapshot

    def boom(symbols):
        raise AssertionError("异动不该取报价")

    try:
        _p._live_quote_snapshot = boom
        assert _p._quote_lookup("abnormal", [("600519.SH", "limit_up")]) == {}
    finally:
        _p._live_quote_snapshot = orig


def test_g_t11_dedupe_key_fields_untouched():
    """G-T11: 去重/冷却用的键没被改 —— 文案层不得顺手动提醒层。"""
    from app.buy_sell_signals.constants import HITS_DINGTALK_DEDUPE_COOLDOWN_MIN
    assert HITS_DINGTALK_DEDUPE_COOLDOWN_MIN == 30
    from app.news import push as P
    assert callable(P._cooldown_seconds)


def test_g_t12_body_keeps_compliance_footer():
    """G-T12: 做T正文带合规脚注, 且明确「须人工确认」。"""
    text = _t_text([{"symbol": "600519.SH", "name": "贵州茅台", "reason": "below_vwap"}])
    assert "仅供内部研究" in text
    assert "人工确认" in text
