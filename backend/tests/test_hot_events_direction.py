"""利好利空判定：无信号留空（中性），不再默认利好；概念撞车监控。

背景（2026-10-12 线上快照实测，349 事件）：全量方向分布利好 300 : 利空 49
（85.7% 偏置）。根因是 `_direction_for` / `_annotate_directions` / `_apply_llm`
/ `service` 出口共 7 处把无信号事件兜底成「利好」。投研用户拿「利空」做风控
触发器（止损/规避），默认利好让所有无明确动词的利空被标成利好 —— 方向性
误导比打分错误更危险。改为留空：前端 `item.direction &&` 不渲染标签，
`_score_event` 不给 direction 分（只有利好/利空才给）。

同时守护聚合层「概念撞车」监控：线上实测「证监会座谈会」与「上交所监管
工作函」两条无关事件共享同一组东百收购概念且成员零重叠（双写残差），
`_log_suspect_event_pairs` 对此打 warning。
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.market_time import CN_TZ
from app.news.hot_events import (
    _annotate_directions,
    _direction_for,
    _log_suspect_event_pairs,
    _score_event,
)

NOW = datetime(2026, 10, 12, 20, 0, tzinfo=CN_TZ)


def test_direction_for_returns_empty_when_no_signal() -> None:
    """无信号文本留空（中性/未知），不再默认利好。"""
    assert _direction_for("市场成交额较上一交易日基本持平", "") == ""
    assert _direction_for("多家机构调研某上市公司", "调研") == ""


def test_direction_for_still_detects_both_sides() -> None:
    """利好/利空判定本身不回退：政策整句利好、关税利空都照判。"""
    assert _direction_for("央行决定下调存款准备金率0.5个百分点", "下调") == "利好"
    assert _direction_for("美方宣布对进口钢铁加征关税", "加征") == "利空"


def test_negated_bear_word_counts_as_bull() -> None:
    """否定利空是明确利好，不是留空。

    「制裁概率低」旧逻辑只把制裁 mask 掉、不记方向，靠 `or "利好"` 兜底 ——
    去掉默认值后会变成中性，丢掉一个真信号。改为显式记利好，避免
    默认值修复把「否定利空」这一类也跟着埋掉。
    """
    assert _direction_for("全面严格制裁概率低", "制裁") == "利好"
    assert _direction_for("美方对进口钢铁加征关税的概率偏低", "加征") == "利好"


def test_annotate_directions_keeps_empty_direction() -> None:
    """无信号事件的 direction 保持空，不再被改写成利好。"""
    event = {"direction": ""}
    _annotate_directions(event, "市场成交额较上一交易日基本持平")
    assert event["direction"] == ""


def test_annotate_directions_keeps_explicit_direction() -> None:
    """显式方向不动，映射和个股继承事件方向。"""
    event = {
        "direction": "利空",
        "mapping": [{"name": "钢铁"}],
        "mentioned_stocks": [{"key": "600000.SH", "name": "示例股份"}],
    }
    _annotate_directions(event, "示例股份被立案调查")
    assert event["direction"] == "利空"
    assert event["mapping"][0]["direction"] == "利空"
    assert event["mentioned_stocks"][0]["direction"] == "利空"


def test_score_event_gives_no_direction_points_without_signal() -> None:
    """空方向不得 mapping 的 direction 分（概念+个股各 15，无方向 5）。"""
    event = {
        "importance": "重要",
        "concepts": ["某概念"],
        "mentioned_stocks": [{"key": "600000.SH", "name": "示例股份"}],
        "direction": "",
        "heat": 10,
    }
    _score_event(event, NOW)
    assert event["breakdown"]["mapping"] == 30


def test_score_event_gives_direction_points_with_signal() -> None:
    """有方向才给满 35 分，防止上一条测试因别的原因恒过。"""
    event = {
        "importance": "重要",
        "concepts": ["某概念"],
        "mentioned_stocks": [{"key": "600000.SH", "name": "示例股份"}],
        "direction": "利好",
        "heat": 10,
    }
    _score_event(event, NOW)
    assert event["breakdown"]["mapping"] == 35


def test_log_suspect_event_pairs_warns_on_disjoint_same_concepts(caplog) -> None:
    """概念集合相同、成员零重叠的事件对打 warning（双写残差监控）。"""
    events = [
        {
            "key": "ev_a",
            "concepts": ["光芯片收购", "光探测芯片", "商业零售"],
            "item_ids": ["1", "2"],
        },
        {
            "key": "ev_b",
            "concepts": ["商业零售", "光芯片收购", "光探测芯片"],
            "item_ids": ["3", "4"],
        },
        {
            "key": "ev_c",
            "concepts": ["光芯片收购", "光探测芯片", "商业零售"],
            "item_ids": ["1", "3"],
        },
    ]
    with caplog.at_level(logging.WARNING, logger="app.news.hot_events"):
        _log_suspect_event_pairs(events)
    warned = [r for r in caplog.records if "概念撞车" in r.message]
    assert len(warned) == 1
    assert "ev_a" in warned[0].getMessage() and "ev_b" in warned[0].getMessage()
    # ev_c 与 ev_a 共享成员 1、与 ev_b 共享成员 3 —— 有重叠不算撞车。
    assert "ev_c" not in warned[0].getMessage()
