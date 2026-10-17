"""DSA 价位线：替换、过期、穿越、冷却，以及买卖点叠加。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from app.custom.dsa import sniper_alerts as sniper
from app.services import preferences
from app.services.trade_marks import load_dsa_levels

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=CN)


def _payload(**kwargs):
    body = {
        "symbol": "600519",
        "name": "贵州茅台",
        "analysis_id": "a1",
        "analyzed_at": NOW.isoformat(timespec="seconds"),
        "ideal_buy": 100,
        "stop_loss": 90,
        "take_profit": 120,
    }
    body.update(kwargs)
    return body


def test_register_replaces_expires_and_keeps_previous_when_empty(tmp_path: Path):
    path = tmp_path / "lines.json"
    first = sniper.register(_payload(), now=NOW, path=path)
    assert first["replaced"] is True
    assert [row["label"] for row in first["lines"]] == ["理想买入", "止损", "目标价"]
    second = sniper.register(_payload(analysis_id="a2", ideal_buy=101, stop_loss=None, take_profit=130), now=NOW, path=path)
    prices = {row["kind"]: row["price"] for row in second["lines"]}
    assert prices == {"buy": 101, "target": 130}
    assert sniper.register(_payload(analysis_id="a2", ideal_buy=101, stop_loss=None, take_profit=130), now=NOW, path=path)["replaced"] is False
    assert sniper.register(_payload(ideal_buy=None, stop_loss=None, take_profit=None), now=NOW, path=path)["replaced"] is False
    old = NOW - timedelta(days=6)
    sniper.register(_payload(analysis_id="old", analyzed_at=old.isoformat(timespec="seconds")), now=old, path=path)
    assert sniper.chart_levels("600519.SH", now=NOW, path=path) == []


def test_cross_cooldown_and_first_print_do_not_refire():
    lines = [
        {"symbol": "600519.SH", "name": "贵州茅台", "kind": "buy", "label": "理想买入", "price": 100, "analyzed_at": NOW.isoformat()},
        {"symbol": "600519.SH", "name": "贵州茅台", "kind": "stop", "label": "止损", "price": 90, "analyzed_at": NOW.isoformat()},
        {"symbol": "600519.SH", "name": "贵州茅台", "kind": "target", "label": "目标价", "price": 120, "analyzed_at": NOW.isoformat()},
    ]
    none, previous, fired = sniper.evaluate(lines, {"600519.SH": 110}, {}, {}, NOW)
    assert none == []
    assert previous["600519.SH"] == 110
    down, previous, fired = sniper.evaluate(lines, {"600519.SH": 99}, previous, fired, NOW)
    assert [item["label"] for item in down] == ["理想买入"]
    assert "600519.SH:buy" in fired
    again, previous, fired = sniper.evaluate(
        lines, {"600519.SH": 105}, previous, fired, NOW + timedelta(minutes=1),
    )
    assert again == []
    bounced, _previous, _fired = sniper.evaluate(
        lines, {"600519.SH": 99}, {"600519.SH": 105}, fired, NOW + timedelta(minutes=10),
    )
    assert bounced == []
    later, _previous, _fired = sniper.evaluate(
        lines, {"600519.SH": 99}, {"600519.SH": 105}, fired, NOW + timedelta(minutes=31),
    )
    assert [item["label"] for item in later] == ["理想买入"]
    up, _previous, _fired = sniper.evaluate(
        lines, {"600519.SH": 121}, {"600519.SH": 110}, {}, NOW,
    )
    assert [item["label"] for item in up] == ["目标价"]
    title, text = sniper.alert_text(down[0])
    assert "理想买入" in title
    assert "贵州茅台" in text
    assert "开盘跳空" not in text
    assert "app.custom" not in text
    gap, _previous, _fired = sniper.evaluate(
        lines, {"600519.SH": 99}, {}, {}, NOW, gap_from={"600519.SH": 110},
    )
    assert [item["label"] for item in gap] == ["理想买入"]
    assert gap[0]["gap"] is True
    _gap_title, gap_text = sniper.alert_text(gap[0])
    assert "开盘跳空触及理想买入" in gap_text


def test_observe_uses_raw_close_and_skips_when_disabled(tmp_path: Path, monkeypatch):
    path = tmp_path / "lines.json"
    monkeypatch.setattr(sniper, "lines_path", lambda: path)
    monkeypatch.setattr(sniper, "alerts_enabled", lambda: True)
    sniper._previous.clear()
    sniper._previous_day.clear()
    sniper._queue.clear()
    sniper.register(_payload(), now=NOW, path=path)
    sniper._previous["600519.SH"] = 110
    frame = pl.DataFrame({"symbol": ["600519.SH", "000001.SZ"], "close": [121.0, 1.0]})
    hits = sniper.observe_frame(frame, now=NOW, enqueue=False)
    assert [item["kind"] for item in hits] == ["target"]
    assert sniper._queue == []
    monkeypatch.setattr(sniper, "alerts_enabled", lambda: False)
    sniper._previous["600519.SH"] = 100
    assert sniper.observe_frame(frame, now=NOW + timedelta(minutes=40), enqueue=False) == []
    monkeypatch.setattr(sniper, "alerts_enabled", lambda: True)
    sniper._previous.clear()
    sniper._previous_day.clear()
    opened = pl.DataFrame({"symbol": ["600519.SH"], "close": [99.0], "prev_close": [110.0]})
    gaps = sniper.observe_frame(opened, now=NOW, enqueue=False)
    assert [item["kind"] for item in gaps] == ["buy"]
    assert gaps[0]["gap"] is True


def test_chart_merges_local_lines_with_report_levels(tmp_path: Path, monkeypatch):
    # load_dsa_levels 不注入时钟，chart_levels 用北京时间墙钟，超过 STALE_AFTER
    #（5 个自然日）的线会被丢掉。模块级 NOW 钉在 2026-10-09，套件晚于 10-14 跑时
    # 本地线已过期，合并结果只剩报告价（buy 50 / stop 80），并不是本地覆盖被改掉。
    fresh = datetime.now(CN).replace(microsecond=0)
    path = tmp_path / "lines.json"
    sniper.register(_payload(analyzed_at=fresh.isoformat(timespec="seconds")), now=fresh, path=path)
    monkeypatch.setattr(sniper, "lines_path", lambda: path)
    monkeypatch.setattr("app.custom.dsa.proxy.enabled", lambda: True)

    def forward(method, path, **kwargs):
        if path == "history":
            body = '{"items":[{"id":"h1","created_at":"2026-10-09T10:00:00+08:00"}]}'
        else:
            body = (
                '{"strategy":{"ideal_buy":"50","stop_loss":"80"},'
                '"details":{"raw_result":{"dashboard":{"price":'
                '{"support_level":70,"resistance_level":"140"}}}}}'
            )
        return 200, body, "application/json", {}

    monkeypatch.setattr("app.custom.dsa.proxy.forward", forward)
    # 不给参考价: 本地线(100/90/120) + 报告线(50/80/70/140) 全部保留, 旧口径
    levels = {row["kind"]: row["price"] for row in load_dsa_levels("600519.SH")}
    assert levels == {"buy": 100, "stop": 90, "target": 120, "support": 70, "resistance": 140}

    # 现价 110: 偏离超过 25% 的丢掉 —— support 70 / resistance 140 被剔除, 本地 100/90/120 留下
    kept_band = {row["kind"]: row["price"] for row in load_dsa_levels("600519.SH", reference=110.0)}
    assert kept_band == {"buy": 100, "stop": 90, "target": 120}
    assert 140 not in kept_band.values()
    assert 70 not in kept_band.values()

    def fail(*args, **kwargs):
        raise OSError("dsa down")

    monkeypatch.setattr("app.custom.dsa.proxy.forward", fail)
    kept = [row["kind"] for row in load_dsa_levels("600519.SH", reference=110.0)]
    assert kept == ["buy", "stop", "target"]


def test_preference_defaults_on_and_quote_hook_is_wired(monkeypatch):
    monkeypatch.setattr(preferences, "load", lambda: {})
    assert preferences.get_dsa_sniper_alerts_enabled() is True
    root = Path(__file__).resolve().parents[2]
    text = (root / "backend/app/services/quote_service.py").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "observe_frame(daily_df, references=quote_extra)" in text
    assert "analysis_bridge.py:/opt/tsp/analysis_bridge.py" in compose


def test_alert_sender_stays_off_the_evaluate_result(monkeypatch):
    sniper._queue.clear()
    sent = []
    monkeypatch.setattr(sniper, "_dingtalk_send", lambda item: sent.append(item))
    sniper._queue.append({"symbol": "600519.SH", "name": "贵州茅台", "label": "止损", "line": 90, "price": 89, "analyzed_at": "t"})
    assert sniper.drain() == 1
    assert sent[0]["label"] == "止损"
