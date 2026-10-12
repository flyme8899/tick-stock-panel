"""利好利空判定回归：无信号留空（中性）+ 反向语义修正。

## 一、无信号留空（PR #109）

背景（2026-10-12 线上快照实测，349 事件）：全量方向分布利好 300 : 利空 49
（85.7% 偏置）。根因是 `_direction_for` / `_annotate_directions` / `_apply_llm`
/ `service` 出口共 7 处把无信号事件兜底成「利好」。投研用户拿「利空」做风控
触发器（止损/规避），默认利好让所有无明确动词的利空被标成利好 —— 方向性
误导比打分错误更危险。改为留空：前端 `item.direction &&` 不渲染标签，
`_score_event` 不给 direction 分（只有利好/利空才给）。

同时守护聚合层「概念撞车」监控：线上实测「证监会座谈会」与「上交所监管
工作函」两条无关事件共享同一组东百收购概念且成员零重叠（双写残差），
`_log_suspect_event_pairs` 对此记一条 info，并且至少要两个共同概念。

## 二、反向语义修正（PR #110）

`_direction_signal` 只认词面，利空词表里「加息」「关税」「制裁」「收紧」
命中就判利空，不识别后面的修饰翻转。线上实测 4 例全部误判：

    '…非农不及预期加息预期退潮，短期金属估值压力缓和'  -> 利空（应为利好）
    '…10月加息预期回落，稀土钨钼望迎节后补库'          -> 利空（应为利好）
    '美联储加息预期降温'                                -> 利空（应为利好）
    '加息预期下修'                                      -> 利空（应为利好）

而真加息 '央行加息25个基点' 判利空是对的，修复不能把它翻过来。

修复：极性 = 词根极性 × 修饰极性。预期类利空词根（加息/关税/制裁/收紧）后
紧跟预期/概率/几率/可能性再跟退潮、回落、降温、减弱、放缓、趋缓、消退等修饰时
记为利好；纯真动作不受影响。中间至多夹 3 个非主体字，不容许「美股/股指/油价」
这类主体词——否则「美联储加息美股回落」会被误翻。
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.market_time import CN_TZ
from app.news.hot_events import (
    _annotate_directions,
    _direction_for,
    _direction_signal,
    _log_suspect_event_pairs,
    _score_event,
)

NOW = datetime(2026, 10, 12, 20, 0, tzinfo=CN_TZ)


# --------------------------------------------------------------------------
# 一、无信号留空（PR #109）
# --------------------------------------------------------------------------


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
    """至少两个概念完全相同、成员零重叠才记 info。只共享一个概念不记。"""
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
        {
            "key": "ev_d",
            "concepts": ["银行"],
            "item_ids": ["8"],
        },
        {
            "key": "ev_e",
            "concepts": ["银行"],
            "item_ids": ["9"],
        },
    ]
    with caplog.at_level(logging.INFO, logger="app.news.hot_events"):
        _log_suspect_event_pairs(events)
    warned = [r for r in caplog.records if "概念撞车" in r.message]
    assert len(warned) == 1
    assert warned[0].levelno == logging.INFO
    assert "ev_a" in warned[0].getMessage() and "ev_b" in warned[0].getMessage()
    # ev_c 与 ev_a 共享成员 1、与 ev_b 共享成员 3 —— 有重叠不算撞车。
    assert "ev_c" not in warned[0].getMessage()
    assert "ev_d" not in warned[0].getMessage()
    assert "ev_e" not in warned[0].getMessage()


# --------------------------------------------------------------------------
# 二、反向语义修正（PR #110）
# --------------------------------------------------------------------------


def test_expectation_weakening_flips_bear_to_bull() -> None:
    """加息预期退潮/回落/降温/减弱/放缓 一律判利好。"""
    bull = [
        "有色金属行业周报：非农不及预期加息预期退潮，短期金属估值压力缓和",
        "有色金属行业周报：10月加息预期回落，稀土钨钼望迎节后补库",
        "美联储加息预期降温",
        "美联储加息预期减弱",
        "美联储加息预期放缓",
        "关税预期降温",
        "制裁预期消退，风险偏好回升",
        "加息预期有所回落",
        "加息预期大幅降温",
        "加息预期情绪降温",
    ]
    for text in bull:
        assert _direction_signal(text, "") == "利好", text


def test_expect_root_with_subject_word_does_not_flip() -> None:
    """「加息 + 美股/股指回落」不是预期退潮，不能翻成利好。"""
    bear = [
        "美联储加息美股回落",
        "美联储加息美股放缓",
        "加息股指走弱",
    ]
    for text in bear:
        assert _direction_signal(text, "") == "利空", text


def test_real_rate_hike_still_bearish() -> None:
    """真加息没有预期修饰，必须仍是利空。"""
    bear = [
        "央行加息25个基点",
        "加息",
        "美联储宣布加息",
        "央行加息，但经济增速放缓",
    ]
    for text in bear:
        assert _direction_signal(text, "") == "利空", text


def test_true_tariff_still_bearish() -> None:
    """真关税加征不能被当成预期退潮。"""
    for text in ["美国宣布对华关税加征", "对进口钢铁加征关税", "关税"]:
        assert _direction_signal(text, "") == "利空", text


def test_intensifying_expectation_stays_bearish() -> None:
    """强化/升温不在翻转词表内，仍判利空。"""
    for text in ["美联储加息预期强化", "加息预期升温"]:
        assert _direction_signal(text, "") == "利空", text


def test_weakening_only_applies_to_expect_roots() -> None:
    """非预期词根（走低/下跌）后面的放缓不改极性。"""
    # 「放缓」紧跟在非预期词根后，但词根不是预期式 -> 不翻转，仍是利空
    assert _direction_signal("业绩增速放缓", "") == ""
    assert _direction_signal("营收走低", "") == "利空"
