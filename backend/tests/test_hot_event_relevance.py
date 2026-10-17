"""热门事件：A 股相关度展示分组、「重要」不能只凭分类、海外分类收窄、国际组织不映射 A 股银行。"""
from __future__ import annotations

import pytest

from app.news import hot_events as he
from app.news.hot_snapshot import (
    clear_hot_snapshot_for_tests,
    publish_hot_snapshot,
    wait_hot_refresh,
)
from app.news.sector_map import find_sectors
from app.news.service import (
    a_share_relevance,
    group_hot_events,
    hot_event_listing,
    public_hot_event,
)


@pytest.fixture(autouse=True)
def _isolate_hot_snapshot():
    clear_hot_snapshot_for_tests()
    yield
    wait_hot_refresh(2)
    clear_hot_snapshot_for_tests()


# ---- (b) 「重要」不能只凭分类 ----

@pytest.mark.parametrize("category", sorted(he._IMPORTANT_CATEGORIES))
def test_category_alone_is_not_important(category):
    assert not he._is_important("某某事项进展情况通报", category)


def test_category_plus_category_act_is_important():
    assert he._is_important("某公司拟筹划重大资产重组并停牌", "公司重大事项")
    assert he._is_important("两部门印发新一轮设备更新方案", "国内政策/宏观")
    assert he._is_important("对伊朗实施新一轮制裁", "地缘政治")


def test_category_plus_stock_mapping_is_important():
    assert he._is_important("某某事项进展情况通报", "公司重大事项", mapped=True)


def test_mapping_without_important_category_stays_general():
    assert not he._is_important("某某事项进展情况通报", "科技与产业", mapped=True)


def test_broad_verbs_do_not_count_as_category_act():
    # 「增长」「新增」「上市」是几乎每条稿都有的宽动词，线上「重要」虚高主要来自它们。
    assert not he._is_important("公司三季度营收同比增长", "公司重大事项")
    assert not he._is_important("新品上市首日销量新增", "公司重大事项")


def test_important_acts_still_important_without_category():
    assert he._is_important("某公司获批新药上市", "科技与产业")
    assert he._is_important("龙头企业发布涨价函", "")


def test_rule_importance_uses_stock_mapping():
    base = {"name": "某某事项进展情况通报", "headline": "", "headlines": [], "category": "公司重大事项"}
    assert he._rule_importance(dict(base)) == "一般"
    mapped = dict(base, mentioned_stocks=[{"key": "600000.SH", "name": "浦发银行"}])
    assert he._rule_importance(mapped) == "重要"


# ---- (c) 海外分类收窄 ----

@pytest.mark.parametrize("text", [
    "央行宣布下调政策利率 降息10个基点",
    "多家银行跟进降息 下调存款挂牌利率",
    "公积金降息对商业银行净息差的直接冲击较小",
    "联储证券：降息周期下关注高股息",
])
def test_domestic_rate_news_is_not_overseas(text):
    assert he._category_for(text, "降息") != "海外市场/央行"
    assert he._category_for(text, "降息") == "国内政策/宏观"


@pytest.mark.parametrize("text", [
    "美联储宣布降息25个基点",
    "美国降息预期升温 美元走弱",
    "欧洲央行年内第三次降息",
    "鲍威尔暗示年内不再加息",
    "美股三大指数集体收涨 纳指创新高",
    "欧洲央行拉加德：财政可持续性是经济稳定的关键",
])
def test_overseas_rate_news_stays_overseas(text):
    assert he._category_for(text, "") == "海外市场/央行"


def test_bare_rate_action_falls_back_to_domestic():
    assert he._ACTION_CATEGORY["降息"] == "国内政策/宏观"
    assert he._ACTION_CATEGORY["加息"] == "国内政策/宏观"


def test_model_overseas_category_needs_foreign_cue():
    # 模型给「海外市场/央行」时，标题里得有海外主体；国内降息不接受。
    assert not he._category_allowed("海外市场/央行", "多家银行跟进降息", "降息")
    assert he._category_allowed("海外市场/央行", "美国降息预期升温", "降息")


# ---- (d) 国际组织不映射到 A 股银行 ----

@pytest.mark.parametrize("text", [
    "世界银行上调中国经济增长预期",
    "世行报告：全球经济增速放缓",
    "国际货币基金组织下调全球经济增速预测",
    "IMF下调全球经济增速预测",
    "亚投行批准新的贷款项目",
    "亚洲开发银行发布亚洲经济展望",
    "国际清算银行警示债务风险",
])
def test_international_orgs_do_not_map_to_banks(text):
    assert all(hit["name"] != "银行" for hit in find_sectors(text)), find_sectors(text)


def test_domestic_bank_text_still_maps_to_banks():
    assert any(hit["name"] == "银行" for hit in find_sectors("银行板块午后拉升，多家银行净息差企稳"))


# ---- (a) A 股相关度：只用于展示 ----

def _raw(key: str, importance: str, *, stocks=(), concepts=(), sector=None, score=100.0) -> dict:
    event = {
        "key": key,
        "name": f"事件{key}",
        "headline": f"事件{key}",
        "importance": importance,
        "score": score,
        "category": "海外市场/央行",
        "mentioned_stocks": [{"key": code, "name": code} for code in stocks],
        "concepts": list(concepts),
        "breakdown": {"importance": 1, "confirmation": 0, "mapping": 0, "freshness": 0, "heat": 0},
    }
    if sector is not None:
        event["sector_stocks"] = [{
            "key": "601398.SH", "name": "工商银行", "sector": "银行",
            "sector_kind": "theme" if sector < 0.7 else "industry1", "confidence": sector,
        }]
        event["sector_mapping"] = {"name": "银行", "kind": "theme", "confidence": sector, "why": "主题词:美联储"}
    return event


def test_relevance_levels():
    assert public_hot_event(_raw("a", "重大", stocks=["600000.SH"]))["a_share_relevance"] == "高"
    assert public_hot_event(_raw("b", "重大", concepts=["降息受益"]))["a_share_relevance"] == "中"
    assert public_hot_event(_raw("c", "重大", sector=0.9))["a_share_relevance"] == "中"
    assert public_hot_event(_raw("d", "重大", sector=0.7))["a_share_relevance"] == "中"
    # 只靠主题词推出来的（美联储 → 银行市值前几）不算相关。
    assert public_hot_event(_raw("e", "重大", sector=0.4))["a_share_relevance"] == "无"
    assert public_hot_event(_raw("f", "重大"))["a_share_relevance"] == "无"


def test_etf_only_is_medium():
    assert a_share_relevance({"stocks": [], "etfs": [{"key": "510300.SH"}], "concepts": []}) == "中"


def test_relevance_does_not_touch_score():
    raw = _raw("a", "重大", stocks=["600000.SH"], score=123.4)
    before = he._IMPORTANCE_POINTS.copy()
    public = public_hot_event(raw)
    assert public["score"] == 123.4
    assert raw["score"] == 123.4
    assert before == he._IMPORTANCE_POINTS


def test_group_orders_by_tier_then_relevance_and_splits_reference():
    events = [public_hot_event(item) for item in [
        _raw("major_none", "重大", score=300),
        _raw("imp_mid", "重要", concepts=["x"], score=250),
        _raw("major_mid", "重大", concepts=["x"], score=240),
        _raw("imp_high", "重要", stocks=["600000.SH"], score=200),
        _raw("major_high", "重大", stocks=["600000.SH"], score=190),
        _raw("gen_high", "一般", stocks=["600000.SH"], score=180),
        _raw("imp_none", "重要", score=170),
    ]]
    main, reference = group_hot_events(events)
    assert [item["key"] for item in main] == ["major_high", "major_mid", "imp_high", "imp_mid", "gen_high"]
    assert [item["key"] for item in reference] == ["major_none", "imp_none"]
    assert {item["display_group"] for item in main} == {"main"}
    assert {item["display_group"] for item in reference} == {"reference"}
    # 分数原样保留。
    assert next(item for item in main if item["key"] == "imp_mid")["score"] == 250


def test_same_tier_same_relevance_keeps_score_order():
    events = [public_hot_event(item) for item in [
        _raw("m1", "重要", stocks=["600000.SH"], score=200),
        _raw("m2", "重要", stocks=["600000.SH"], score=150),
        _raw("m3", "重要", stocks=["600000.SH"], score=100),
    ]]
    main, _ = group_hot_events(events)
    assert [item["key"] for item in main] == ["m1", "m2", "m3"]


def _publish(events):
    publish_hot_snapshot({
        "events": events, "as_of": "2026-10-16", "trading_day": "2026-10-19",
        "fallback": False, "hint": "更新于 00:10", "updated_at": "00:10",
    }, persist=False)


def test_listing_default_keeps_original_order_for_push_and_dsa():
    _publish([_raw("none", "重大", score=300), _raw("high", "重要", stocks=["600000.SH"], score=200)])
    listing = hot_event_listing(limit=20)
    assert [item["key"] for item in listing["events"]] == ["none", "high"]
    assert "reference_events" not in listing


def test_listing_grouped_for_page():
    _publish([_raw("none", "重大", score=300), _raw("high", "重要", stocks=["600000.SH"], score=200)])
    listing = hot_event_listing(limit=20, grouped=True)
    assert [item["key"] for item in listing["events"]] == ["high"]
    assert [item["key"] for item in listing["reference_events"]] == ["none"]
    assert listing["reference_group"] == "海外/宏观参考"


def test_api_hot_returns_reference_group():
    from app.api.news import hot

    _publish([_raw("none", "重大", score=300), _raw("high", "重要", stocks=["600000.SH"], score=200)])
    payload = hot(kind="event", window_hours=24, baseline_days=4, limit=20)
    assert [item["key"] for item in payload["events"]] == ["high"]
    assert [item["key"] for item in payload["reference_events"]] == ["none"]
    assert payload["reference_group"] == "海外/宏观参考"
    assert payload["events"][0]["a_share_relevance"] == "高"


def test_category_only_general_events_are_not_dropped():
    # 只凭分类不再给「重要」，但降成「一般」后仍留在榜上，进榜与否和改档前一致。
    event = {
        "importance": "一般", "category": "国内政策/宏观", "mentions": 1, "source_count": 1,
        "red_count": 0, "breakdown": {"mapping": 0},
    }
    assert not he._drop_minor(event)
    assert he._drop_minor(dict(event, category="科技与产业"))
