"""榜单排序口径回归：展示分重排 + 前 20 必有要点（任务 #50，2026-10-12）。

背景：_rank_events 在 _label_with_llm 之前按规则分排名次，_apply_llm 用模型
importance 覆盖规则 importance（内存覆盖，一次最多 ±110 分），
_resync_importance_score 只把展示分对齐、不重排 —— 榜单按旧顺序显示新分数。
服务器快照实测：全场最高分「碳酸锂期货暴跌9%」（168.55）压在 12 位、#2 只有
105.13，相邻倒挂 9/996 对。

修复：_resync 之后按展示分重排（_event_sort_key，不重跑 _score_event）；
切片外事件若被重排送进前 20，补问一轮把「前 20 必有要点」补回来。

这两个测试在修复前的代码上都会失败：旧的流程没有 _event_sort_key（属性错误），
且不重排 → 分数非单调 + 前 20 出现无要点事件。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from app.market_time import CN_TZ
from app.news import hot_events as hot_mod
from app.news import service as news_service
from app.news.hot_events import _event_sort_key, _resync_importance_score
from app.news.service import get_store, reset_store_for_tests, top_hot_events

NOW = datetime(2026, 10, 10, 8, 30, tzinfo=CN_TZ)

# 模型要点。不含数字和机构名 —— _subtitle_reject_reason 的事实核对只查数字和
# 机构名，纯文字要点恒过；措辞与新闻原标题不同，不会被判复读。
_GENERIC_SUBTITLES = (
    "市场关注度持续升温",
    "资金流入相关板块",
    "机构调研频次增加",
    "行业景气度受到关注",
    "产业链库存压力缓解",
    "海外订单增速保持平稳",
    "政策预期出现新的变化",
    "板块估值处于历史低位",
    "成交量能温和放大",
    "市场情绪偏向谨慎",
)
_LITHIUM_SUBTITLE = "期货市场担忧锂矿供应持续放量"
_US_SUBTITLE = "隔夜外围市场风险偏好回升"

# 26 条互不合并的新闻（主体各不相同：公司/指数/品种，聚类不会并簇 —— 上线前
# 用 _doc/_blocked_pairs/_can_merge 实测过 50 条候选零合并、再核对展示门槛）。
# 「美股」那条规则分最高（模型会把它降级）；碳酸锂那条故意又老又单薄，规则分
# 垫底，只有模型能把它救上来。
# 发布时间都在 15:00 前 —— 再晚的稿子会被划到下一个交易日的竞价确认时点
# （见 market_confirm._auction_deadline），跟其余事件不在同一天。
_TITLES = (
    "美股科技股领涨纳指走强",
    "碳酸锂期货价格继续下跌",
    "苹果宣布追加iPhone订单",
    "财政部宣布柴油协议触发制裁",
    "美联储宣布降息25基点",
    "工信部出台机器人补贴",
    "存储芯片厂宣布涨价",
    "航运公司上调运价",
    "IEA下调原油供需预期",
    "国务院研究称将出台增量政策",
    "证监会立案后机构建议回避",
    "宁德时代回购巩固行业地位",
    "赛力斯与宁德时代签署长期战略合作协议",
    "建发股份与优必选战略合作签约",
    "华为发布盘古新模型",
    "特斯拉发布无人驾驶新车",
    "比亚迪宣布上调电池售价",
    "蔚来宣布换电站扩容",
    "小鹏汽车宣布新车型上市",
    "中芯国际宣布扩产计划",
    "京东方宣布柔性屏量产",
    "阿里巴巴发布云计算新品",
    "腾讯宣布音乐版权续约",
    "京东物流宣布新航线开通",
    "美团宣布无人机配送扩区",
    "寒武纪发布新款推理芯片",
)


def _publish(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 9, hour, minute, tzinfo=CN_TZ)


def _insert(source_id: str, title: str, hour: int, minute: int, source: str = "cls") -> None:
    status = get_store().insert_item(
        source=source,
        source_id=source_id,
        published_at=_publish(hour, minute),
        author="",
        title=title,
        # 正文与标题不同的事实句，供 _event_body 取用；不含数字，避免事实核对找麻烦。
        clean_text=f"{title}。市场人士分析认为后续走势仍需观察供需变化。",
        raw=None,
        content_hash=source_id,
        url="",
        level="",
        media_ids=[],
        extra=None,
        mentions=[],
    )
    assert status == "inserted"


def _insert_corpus() -> None:
    """碳酸锂最老（00:30，约 32 小时前）且规则分垫底。

    两条来源是必须的：_drop_minor 会把「无映射、单源单条」的规则-一般小事在
    聚类轮就丢掉（生产里这类事件根本到不了模型），得让碳酸锂活过第一轮。
    """
    for index, title in enumerate(_TITLES):
        if "碳酸锂" in title:
            _insert(f"li-{index}", title, 0, 30)
            _insert(f"li-{index}-b", title, 0, 40, source="wscn")
        else:
            _insert(f"n-{index}", title, 8, 0)


def _fake_rule_importance(event: dict) -> str:
    """规则分级也钉死：美股=重大、碳酸锂=一般、其余=重要。

    不钉的话 _rule_importance 的真实启发式可能直接把碳酸锂判成重大，升级
    +110 就不发生了，测试退化成立。事件里哪个字段带文本不确定，整体扫一遍。
    """
    blob = json.dumps(event, ensure_ascii=False, default=str)
    if "美股" in blob:
        return "重大"
    if "碳酸锂" in blob:
        return "一般"
    return "重要"


def _make_llm_fake(calls: list[str]):
    def fake(prompt: str, **_kwargs: object) -> str:
        calls.append(prompt)
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            if "碳酸锂" in rest:
                importance, subtitle = "重大", _LITHIUM_SUBTITLE
            elif "美股" in rest:
                importance, subtitle = "一般", _US_SUBTITLE
            else:
                index = len(rows)
                importance = "一般"
                subtitle = _GENERIC_SUBTITLES[index % len(_GENERIC_SUBTITLES)]
            rows.append({"id": head, "title": subtitle, "importance": importance})
        return json.dumps({"events": rows}, ensure_ascii=False)

    return fake


def _setup(monkeypatch, calls: list[str]) -> None:
    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", _make_llm_fake(calls))
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    monkeypatch.setattr(hot_mod, "_rule_importance", _fake_rule_importance)


def _scores(events: list[dict]) -> list[float]:
    return [float(event["score"]) for event in events]


def test_resync_then_sort_puts_the_display_score_in_order():
    """纯函数层：模型改判后按展示分重排，深位「重大」压过被降级的头部。"""
    head = {
        "name": "美股三大指数集体收涨",
        "importance": "重大",
        "score": 190.0,
        "breakdown": {"importance": 130.0, "confirmation": 0.0, "mapping": 35.0,
                      "freshness": 20.0, "heat": 5.0},
        "heat": 5.0,
    }
    middle = {
        "name": "存储芯片厂宣布涨价",
        "importance": "重要",
        "score": 115.0,
        "breakdown": {"importance": 55.0, "confirmation": 0.0, "mapping": 35.0,
                      "freshness": 20.0, "heat": 5.0},
        "heat": 5.0,
    }
    deep = {
        "name": "碳酸锂期货继续下跌",
        "importance": "一般",
        "score": 30.0,
        "breakdown": {"importance": 20.0, "confirmation": 0.0, "mapping": 0.0,
                      "freshness": 10.0, "heat": 0.0},
        "heat": 0.0,
    }
    events = [head, middle, deep]
    # 模型标签：头部降两级（重大→一般），深位升三级（一般→重大）。
    head["importance"] = "一般"
    deep["importance"] = "重大"
    for event in events:
        _resync_importance_score(event)
    # 修复前：没有这一步（也没有 _event_sort_key），顺序不变、分数已变。
    events.sort(key=_event_sort_key)
    assert [event["name"] for event in events] == [
        "碳酸锂期货继续下跌", "存储芯片厂宣布涨价", "美股三大指数集体收涨",
    ]
    scores = _scores(events)
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 140.0  # 30 + (130 - 20)


def test_ranking_is_monotonic_and_top20_keeps_titles(tmp_path, monkeypatch):
    """全管线：碳酸锂（深位一般）被模型判重大后必须进榜首，榜单按展示分单调。"""
    calls: list[str] = []
    reset_store_for_tests(tmp_path / "ranking.sqlite")
    _setup(monkeypatch, calls)
    _insert_corpus()
    hot_mod.clear_hot_event_cache()
    events = top_hot_events(NOW)["events"]
    assert len(events) >= 20
    scores = _scores(events)
    # 核心不变量：顺序按展示分单调递减（修复前碳酸锂 138 分压在 20 名开外）。
    assert scores == sorted(scores, reverse=True)
    # 碳酸锂被模型救进榜首，且带要点。
    assert "碳酸锂" in str(events[0].get("name") or "")
    assert str(events[0].get("subtitle") or "").strip()
    # 前 20 必有要点：全部送过模型（本批 25 条 < 切片 80，全在切片内）。
    assert all(str(event.get("subtitle") or "").strip() for event in events[:20])


def test_unlabeled_events_pulled_into_top20_get_a_second_ask(tmp_path, monkeypatch, caplog):
    """切片漏掉的事件被重排送进前 20 时，补问一轮，保证仍然条条有要点。"""
    calls: list[str] = []
    reset_store_for_tests(tmp_path / "second-ask.sqlite")
    _setup(monkeypatch, calls)
    _insert_corpus()
    # 只让规则前 10 名送模型：碳酸锂（规则垫底）和一批「重要」事件都在切片外。
    monkeypatch.setattr(hot_mod, "_events_for_titles", lambda events: events[:10])
    hot_mod.clear_hot_event_cache()
    with caplog.at_level(logging.INFO, logger="app.news.hot_events"):
        events = top_hot_events(NOW)["events"]
    assert len(events) >= 20
    scores = _scores(events)
    assert scores == sorted(scores, reverse=True)
    assert all(str(event.get("subtitle") or "").strip() for event in events[:20])
    # 补问真的发生了：首轮切片一批 + 补问一批，至少两次调用。
    assert len(calls) >= 2
    # 补问条数记了日志，线上靠它观察裕度是否被击穿（正常情况下这条不出现）。
    asked = [r for r in caplog.records if "补问切片外未标注条目" in r.message]
    assert asked and int(str(asked[0].args[0])) >= 1
    # 被降级的美股组（重大→一般，-110）不该留在前列。
    top_names = " ".join(str(event.get("name") or "") for event in events[:10])
    assert "美股" not in top_names


def test_extra_round_empty_titles_stay_empty_without_a_title_copy(tmp_path, monkeypatch, caplog):
    """补问轮模型回空 title 时，重写一次后仍空。正文没有新事实就留空，不用标题再写一遍。"""
    calls: list[str] = []

    def fake(prompt: str, **_kwargs: object) -> str:
        calls.append(prompt)
        rows = []
        retry = "上一轮要点不合格" in prompt
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            if len(calls) == 1 and not retry:
                if "碳酸锂" in rest:
                    importance, subtitle = "重大", _LITHIUM_SUBTITLE
                elif "美股" in rest:
                    importance, subtitle = "一般", _US_SUBTITLE
                else:
                    importance = "一般"
                    subtitle = _GENERIC_SUBTITLES[len(rows) % len(_GENERIC_SUBTITLES)]
            else:
                importance, subtitle = "一般", ""
            rows.append({"id": head, "title": subtitle, "importance": importance})
        return json.dumps({"events": rows}, ensure_ascii=False)

    reset_store_for_tests(tmp_path / "empty-extra.sqlite")
    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", fake)
    monkeypatch.setattr(news_service, "reserve_hot_title_call", lambda: True)
    monkeypatch.setattr(hot_mod, "_rule_importance", _fake_rule_importance)
    monkeypatch.setattr(hot_mod, "_events_for_titles", lambda events: events[:10])
    monkeypatch.setattr(hot_mod.time, "sleep", lambda *_args, **_kwargs: None)
    _insert_corpus()
    hot_mod.clear_hot_event_cache()
    with caplog.at_level(logging.INFO, logger="app.news.hot_events"):
        events = top_hot_events(NOW)["events"]
    assert len(events) >= 20
    top = events[:20]
    empties = [event for event in top if hot_mod._point_is_empty(event)]
    written = [event for event in top if not hot_mod._point_is_empty(event)]
    assert empties
    assert written
    assert all(not str(event.get("subtitle") or "").strip() for event in empties)
    assert all(event.get("subtitle_source") != "fallback" for event in empties)
    assert all(event.get("subtitle") != event.get("name") for event in top)
    assert all(event.get("subtitle_source") == "model" for event in written)
    assert sum("上一轮要点不合格" in prompt for prompt in calls) == 1
    asked = [record for record in caplog.records if "补问切片外未标注条目" in record.message]
    assert asked and int(str(asked[0].args[0])) >= 1
    logged = [record.message for record in caplog.records if "要点为空" in record.message]
    assert logged
    assert f"要点为空 {len(empties)} 条" in logged[-1]
    assert "要点为空 0 条" not in logged[-1]
    assert "兜底前 0 条" not in logged[-1]
