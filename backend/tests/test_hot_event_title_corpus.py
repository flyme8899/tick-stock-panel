"""展示门槛回归语料。#52 到 #57 的坏标题和好标题，加上词中间截断、寒暄和叙事。"""
from __future__ import annotations

from datetime import datetime

from app.market_time import CN_TZ
from app.news.hot_events import (
    _apply_llm,
    _cleaned_display_ok,
    _cleaned_headline,
    _clip_at_boundary,
    _display_clause_ok,
    _display_gate_reason,
    _display_original,
    _ensure_shown_name,
    _fallback_title_ok,
    _first_credible_title,
    _first_original_title,
    _headline_tier,
    _keyword_title,
    _llm_title_ok,
    _llm_title_reject_reason,
    _original_reject_reason,
    _parse_title_batch,
    _subtitle_reject_reason,
)
from app.news.service import get_store, reset_store_for_tests, top_hot_events

NOW = datetime(2026, 10, 10, 8, 30, tzinfo=CN_TZ)

# 不能上屏。模型标题和回退标题走同一道门槛。
_BAD = (
    "橡胶价格大涨超发布公告",
    "叠加昨夜美国针对制裁",
    "和之前结构性降息",
    "根据宏观经济运行出台务实管用的增量政",
    "事件",
    "可能还会有几次加息",
    "财政的小增量政策也出",
    "新增隐债】财政部发布20",
    "IEA下调原油供需预期...",
    "32财政部发布2026年上半年中",
    "橡胶价格大涨超60家轮胎企业集体涨价今年以来",
    "重点...",
    "TIM材料",
    "隔夜美股半导体硬件波动点评",
    "侧面佐证AI电源地位&能力",
    "持续关注1）Q3业绩增长强",
    "①Q3业绩增长强劲",
    "重申AI安全白宫《超级智能协议》签署",
    "Vicor对V",
    "PCB设备&钓针观点更新",
    "运作满一年产品近八",
    "Cybercab扩容提速",
    "某某公司发布新品天风计算机",
    "锂电行业更新-20261009#十五五政策定调",
    "风险可控从容应",
    "早上好",
    "各位",
    "各位投资者早上好",
    "利空叙事在节前已经释放",
    "光智科技",
    "央视财经",
    "美国在前财长宣布加息",
    "数据显示增长明显",
    "同比增长50%",
    "同比增长",
    "橡胶价格大涨至13",
    "结构性降息",
    "财政部发布2026年上半年中",
    "橡胶价格大涨超60家轮胎企业集体",
    "🌸固态电池",
    "高争民爆/西藏地矿/西藏铜矿资",
    "市场没有太关注到公司近期正在进行",
    "数据印证需求正在回暖",
    "板块利好继续扩散",
    "机构看好算力产业链",
    "继续关注AI视频板块",
    "建议关注算力",
    "推荐布局低空经济",
    "机构建议继续布局",
    "财政部发布",
    "招商电新称十五五政策定调固态电池产业化进入关键期",
    "招商电新称十五五…",
    "早上好考虑到",
    "早上好考虑到…",
    "早上好考虑到美联储降息",
    "腾讯&Meta对比",
    "Optimus加速落地",
    "中国巨石预告点评扣非表现好于预期（激励费环比增长）",
)

# 可以上屏。回退标题不要求宾语，所以「油价走高」在这里，不在模型验收里。
_GOOD = (
    "美联储降息25基点",
    "美联储宣布降息25基点",
    "美联储宣布降息二十五基点",
    "工信部出台机器人补贴",
    "存储芯片厂宣布涨价",
    "橡胶价格大涨",
    "橡胶价格大涨超预期",
    "橡胶价格大涨超60家轮胎企业集体涨价",
    "橡胶价格大涨超60%",
    "橡胶价格大涨超六成",
    "橡胶价格大涨至每吨13.1元",
    "油价走高",
    "美国制裁",
    "宏观经济运行情况出台务实管用的增量政策",
    "宏观经济运行出台务实管用的增量",
    "财政部发布2026年上半年中长期财政规划",
    "财政部发布2026年上半年中长期财政规划并明确后续增量政策",
    "财政部发布上半年财政规划",
    "财政部发布2025年一揽子化债方案",
    "IEA下调原油供需预期",
    "景旺电子Q3利润加速释放",
    "立讯精密及立讯技术涉337调查尚处立案初期",
    "立讯精密公告涉美国337调查",
    "建发股份与优必选战略合作签约",
    "建发股份与优必选签署战略合作",
    "赛力斯与宁德时代签署长期战略合作协议",
    "宁德时代回购巩固行业地位",
    "宁德时代发布固态电池",
    "宁德时代净利润同比增长50%",
    "宁德时代布局固态电池",
    "公司在前三季度净利润增长50%",
    "宁德时代在前十大股东中新增社保基金",
    "在前述公告基础上追加回购",
    "十五五政策定调",
    "十五五政策定调固态电池产业化进入关键期",
    "Vicor对VPD发起337调查",
    "某某公司发布新品",
    "特斯拉发布Cybercab扩容提速",
    "华为发布盘古新模型",
    "华为发布新模型",
    "航运公司上调运价",
    "碳酸锂报价继续上涨",
    "机构点评美联储降息",
    "证监会立案后机构建议回避",
    "国务院研究称将出台增量政策",
    "国务院布局未来产业",
    "工信部建议加快5G部署",
    "发改委布局算力枢纽",
    "国资委推荐央企试点",
    "文旅部出台演唱会补贴",
    "苹果发布智能家居新品",
    "苹果公司宣布将于10月13日在纽约发布全新iPhone",
    "黄金价格上涨",
    "欧洲多国电网在凌晨大范围熄灯",
    "沙特利雅得机场再次遭袭造成12人受伤",
    "沪指在前一交易日大涨后回调",
    "白宫签署超级智能协议",
    "优化实施财政金融协同促内需一揽子政策",
    "中国巨石预告扣非表现好于预期",
)


def test_display_gate_rejects_the_bad_corpus_and_keeps_the_good_one():
    """坏标题不能过门槛。好标题模型和回退都看这同一道。"""
    for title in _BAD:
        assert _display_gate_reason(title), title
        assert not _fallback_title_ok(title)
        assert not _llm_title_ok(title)
        assert not _cleaned_display_ok(title)
        assert not _display_clause_ok(title)
        assert _keyword_title([{"title": title}]) != title
        assert _clip_at_boundary(title) != title
    for title in _GOOD:
        assert _display_gate_reason(title) == "", title
        assert _fallback_title_ok(title)
        assert _cleaned_display_ok(title)


def test_cutoff_and_missing_party_lose_to_the_full_headline():
    """半截或丢掉合作方的标题，在簇里有完整原标题时不能上屏。"""
    apple = "苹果公司宣布将于10月13日在纽约发布"
    iphone = "苹果公司宣布将于10月13日在纽约发布全新iPhone"
    assert not _llm_title_ok(apple)
    assert _first_credible_title([("cls", apple), ("cls", iphone)]) == iphone

    short = "建发股份战略合作签约"
    paired = "建发股份与优必选战略合作签约"
    assert _display_gate_reason(short, [paired]) == "缺少合作方"
    assert _first_credible_title([("cls", short), ("reddit", paired)]) == paired

    plan = "十五五政策定调"
    full = "十五五政策定调固态电池产业化进入关键期"
    assert _display_gate_reason(plan) == ""
    assert _first_credible_title([("cls", plan), ("wscn", full)]) == full

    assert _first_credible_title([
        ("reddit", "Vicor对V"),
        ("cls", "Vicor对VPD发起337调查"),
    ]) == "Vicor对VPD发起337调查"


def test_failed_title_walks_headlines_by_source_credibility():
    """模型标题不合格时，按来源可信度试清理后的原标题。都不过就留空。"""
    event = {
        "name": "风险可控从容应",
        "_llm_title_applied": True,
        "headline": "早上好",
        "headlines": ["早上好"],
        "_candidates": [
            {"source": "reddit", "title": "美联储宣布降息25基点"},
            {"source": "cls", "title": "美联储降息25基点"},
        ],
    }
    _ensure_shown_name(event)
    assert event["name"] == "美联储降息25基点"
    assert "_llm_title_applied" not in event

    empty = {
        "name": "各位投资者早上好",
        "headline": "早上好",
        "headlines": ["早上好", "各位"],
        "_candidates": [
            {"source": "cls", "title": "早上好"},
            {"source": "wscn", "title": "各位投资者早上好"},
        ],
    }
    _ensure_shown_name(empty)
    assert empty["name"] == ""


def _quote_open_for_test(text: str) -> bool:
    stack: list[str] = []
    for char in text:
        if char == '"':
            if stack and stack[-1] == '"':
                stack.pop()
            else:
                stack.append('"')
    return bool(stack)


def _publish(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 9, hour, minute, tzinfo=CN_TZ)


def _insert(source_id: str, title: str, hour: int, minute: int, source: str = "cls", level: str = "") -> None:
    status = get_store().insert_item(
        source=source,
        source_id=source_id,
        published_at=_publish(hour, minute),
        author="",
        title=title,
        clean_text=title,
        raw=None,
        content_hash=source_id,
        url="",
        level=level,
        media_ids=[],
        extra=None,
        mentions=[],
    )
    assert status == "inserted"


def test_vicor_cutoff_mentions_merge_into_the_full_event(tmp_path):
    """四条 Vicor对V 超过 6 小时也要并回完整标题，提及次数留在完整那条上。"""
    reset_store_for_tests(tmp_path / "vicor-mentions.sqlite")
    _insert("v1", "Vicor对V", 0, 13)
    _insert("v2", "Vicor对V", 0, 40, source="wscn")
    _insert("v3", "Vicor对V", 1, 20, source="reuters")
    _insert("v4", "Vicor对V", 2, 0, source="bloomberg")
    _insert("v5", "Vicor对VPD发起337调查", 8, 39, source="cls")
    events = top_hot_events(NOW)["events"]
    names = [event["name"] for event in events]
    assert "Vicor对V" not in names
    vicor = [event for event in events if event["name"] == "Vicor对VPD发起337调查"]
    assert len(vicor) == 1
    assert vicor[0]["mentions"] == 5


def test_red_chatter_and_narrative_are_dropped(tmp_path):
    """加红也不能把寒暄、叙事和词中间截断放上榜。"""
    reset_store_for_tests(tmp_path / "chatter.sqlite")
    _insert("greet", "各位投资者早上好", 10, 1, level="A")
    _insert("story", "利空叙事在节前已经释放", 10, 2, source="wscn", level="A")
    _insert("cut", "风险可控从容应", 10, 3, source="reuters", level="A")
    _insert("fed", "美联储降息25基点", 10, 4)
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert "各位投资者早上好" not in names
    assert "利空叙事在节前已经释放" not in names
    assert "风险可控从容应" not in names
    assert "美联储降息25基点" in names


def test_bare_publish_loses_to_the_full_policy_line(tmp_path):
    """只有「财政部发布」时不上屏，簇里有完整政策标题就用那一条。"""
    reset_store_for_tests(tmp_path / "fiscal-object.sqlite")
    short = "财政部发布"
    full = "优化实施财政金融协同促内需一揽子政策"
    _insert("short", short, 10, 1)
    _insert("full", full, 10, 6, source="wscn")
    names = [event["name"] for event in top_hot_events(NOW)["events"]]
    assert short not in names
    assert full in names
    rows = [("cls", short), ("wscn", full)]
    assert _first_credible_title(rows) == full


def test_source_fallback_strips_the_broker_prefix():
    """回退到原标题时也要去掉招商电新称，不能把券商前缀再放上屏。"""
    raw = "招商电新称十五五政策定调固态电池产业化进入关键期"
    cleaned = "十五五政策定调固态电池产业化进入关键期"
    assert _cleaned_headline(raw) == cleaned
    assert _display_gate_reason(cleaned) == ""
    event = {
        "name": raw,
        "headline": raw,
        "headlines": [raw],
        "_candidates": [{"source": "cls", "title": raw}],
    }
    _ensure_shown_name(event)
    assert event["name"] == cleaned
    ellipsis = "招商电新称十五五…"
    assert _display_gate_reason(ellipsis)
    assert not _cleaned_headline(ellipsis).startswith("招商")


def test_original_headline_keeps_the_long_source_line():
    """主标题是清理后的原标题。要点失败不会把它换成短改写。"""
    assert _headline_tier("sec", "宁德时代公告发布固态电池新品") < _headline_tier(
        "cls", "新华社：财政部发布2026年上半年中长期财政规划",
    )
    assert _headline_tier("cls", "新华社：财政部发布2026年上半年中长期财政规划") < _headline_tier(
        "cls", "美联储降息25基点",
    )
    assert _headline_tier("cls", "美联储降息25基点") < _headline_tier("reuters", "美联储宣布降息25基点")
    assert _headline_tier("reuters", "美联储宣布降息25基点") < _headline_tier("ima", "美联储宣布降息25基点")
    assert _first_credible_title([
        ("ima", "群里说橡胶价格大涨超预期发布公告"),
        ("cls", "橡胶价格大涨超预期发布公告"),
        ("sec", "宁德时代公告发布固态电池新品"),
    ]) == "宁德时代公告发布固态电池新品"
    assert _display_original("央视财经：橡胶价格大涨超预期发布公告") == "橡胶价格大涨超预期发布公告"
    assert _display_original("【GLMS通信】橡胶价格大涨超预期发布公告") == "橡胶价格大涨超预期发布公告"
    assert _display_original("光智科技：公司发布新品") == "光智科技发布新品"
    battery = _display_original("宁德时代：发布固态电池、换电")
    assert battery == "宁德时代发布固态电池、换电"
    assert "固态电池、换电" in battery
    assert "固态电池，换电" not in battery
    assert _display_original("某某公司发布新品天风计算机") == "某某公司发布新品"
    long = "橡胶价格大涨超60家轮胎企业集体涨价，" + "后续多家企业继续跟进并上调出厂报价以及经销商报价"
    shown = _display_original(long)
    assert len(long) > 40
    assert shown == long
    assert "后续" in shown
    event = {
        "name": "短改写",
        "headline": "央视财经：橡胶价格大涨超预期发布公告",
        "headlines": ["央视财经：橡胶价格大涨超预期发布公告"],
        "_candidates": [{"source": "cls", "title": "央视财经：橡胶价格大涨超预期发布公告"}],
    }
    _apply_llm(event, {"title": "早上好"})
    assert event["subtitle"] == ""
    assert event["name"] == "短改写"
    _apply_llm(event, {"title": "橡胶价格大涨超预期"})
    assert event["subtitle"] == "橡胶价格大涨超预期"
    assert event["name"] == "短改写"
    _apply_llm(event, {"title": "美联储宣布降息二十五基点"})
    assert event["subtitle"] == "美联储宣布降息二十五基点"
    assert event["name"] == "短改写"
    _ensure_shown_name(event)
    assert event["name"] == "橡胶价格大涨超预期发布公告"
    assert event["subtitle"] == "美联储宣布降息二十五基点"


def test_live_sentences_subtitles_and_research_chatter():
    """线上这批：要点留下，第一句不截断，转述和研报聊天不当主标题。"""
    kept = (
        "WTI原油期货收涨至91.85美元",
        "财政部实施更加积极财政政策",
        "纽约联储称特朗普关税推高商品价格",
        "苹果iPhoneDuo加单30%",
        "英伟达洽谈收购AI初创Reflection",
    )
    for line in kept:
        assert _subtitle_reject_reason(line) == "", line
    assert _subtitle_reject_reason("") == "空标题"
    assert _subtitle_reject_reason("早上好") == "寒暄"
    assert _subtitle_reject_reason("各位投资者请看") == "寒暄"
    assert _subtitle_reject_reason("财联社：美联储降息") == "来源前缀"
    assert _subtitle_reject_reason("叠加昨夜美国针对制裁") == ""
    live_subtitles = (
        "WTI11月原油期货收涨0.36美元报91.85美元/桶",
        "苹果iPhoneDuo加单30%，iPhone18Pro系列总订单量未变",
        "特朗普与普京达成的柴油协议被指违反俄罗斯制裁法",
    )
    for line in live_subtitles:
        assert _subtitle_reject_reason(line) == "", line
    assert _subtitle_reject_reason("\ufffd") == "乱码"
    assert _subtitle_reject_reason("锟斤拷") == "乱码"
    assert _subtitle_reject_reason("Ã¤Â¸Â­文") == "乱码"
    assert _subtitle_reject_reason("!!!@@@###") == "乱码"

    price = "统一涨价10%！光刻机巨头ASML将上调服务价格"
    shown_price = _display_original(price)
    assert "统一涨价10%" in shown_price
    assert "ASML将上调" in shown_price
    assert shown_price != "统一涨价10%"
    assert _display_original("华为发布盘古新模型！昇腾链走强") == "华为发布盘古新模型"

    quoted = '深圳公积金拟新增"装修住房提取。额度上调"'
    shown_quote = _display_original(quoted)
    assert "额度上调" in shown_quote
    assert shown_quote != '深圳公积金拟新增"装修住房提取'
    assert not _quote_open_for_test(shown_quote)
    closed = '深圳公积金拟新增"装修住房提取"'
    assert _display_original(closed) == closed

    broker = "招商电新锂电行业更新称十五五政策定调固态电池产业化进入关键期"
    assert _display_original(broker) == "十五五政策定调固态电池产业化进入关键期"
    assert _display_original("经济日报深圳公积金拟新增装修住房提取额度") == "深圳公积金拟新增装修住房提取额度"
    assert _display_original("经济日报：深圳公积金拟新增装修住房提取额度") == "深圳公积金拟新增装修住房提取额度"
    assert _display_original("经济日报") == ""

    demand = "国家发改委出台一揽子增量政策更好鼓励居民消费、撬动社会投资并扩大有效需求持续释放内需潜力"
    industry = "国家把十五五政策定调固态电池产业化进入关键期并带动上下游设备与材料订单集中释放落地"
    plan = '多部门共同推动"十五五"规划落地并明确未来五年的产业支持方向、配套资金安排和年度项目清单'
    ai4s = "AI4S产业加速从实验室走向中试并带动科研仪器与算力需求以及交叉学科平台持续建设落地"
    for sentence in (demand, industry, plan, ai4s):
        assert len(sentence) > 40
        assert _display_original(sentence) == sentence
    assert _display_original(demand).endswith("撬动") is False
    assert "产业化进入关键期" in _display_original(industry)
    assert _display_original(plan) != '推动"十五五"规划'
    assert _display_original(ai4s) != "AI4S产业"

    nvidia = "英伟达洽谈收购AI初创Reflection"
    assert _display_original("报道" + nvidia) == nvidia
    assert _display_original("据报道，" + nvidia) == nvidia
    assert _display_original("消息称" + nvidia) == nvidia

    clinical = "J26完成RCT临床验证1基本面更新"
    pcb = "小PCB公司们的8月单月业绩崇达8月单月1e+利润，超声"
    tip = "调整建议把握机会！"
    assert _first_credible_title([("zsxq", clinical), ("cls", nvidia)]) == nvidia
    assert _first_credible_title([("ima", clinical)]) == ""
    assert _first_credible_title([("dws", pcb)]) == ""
    assert _first_credible_title([("cls", tip), ("wscn", nvidia)]) == nvidia
    assert _first_credible_title([("cls", tip)]) == ""

    chat = {
        "name": "旧",
        "headline": clinical,
        "headlines": [clinical],
        "_candidates": [{"source": "zsxq", "title": clinical}],
        "subtitle": nvidia,
    }
    _ensure_shown_name(chat)
    assert chat["name"] == nvidia
    assert chat["subtitle"] == ""
    assert _llm_title_reject_reason(nvidia) == ""

    weak = {
        "name": "旧",
        "headline": clinical,
        "headlines": [clinical],
        "_candidates": [{"source": "ima", "title": clinical}],
        "subtitle": "WTI原油期货收涨至91.85美元",
    }
    _ensure_shown_name(weak)
    assert weak["name"] == ""


def test_realistic_batch_keeps_subtitles(tmp_path, monkeypatch):
    """线上这三条要点含数字、拉丁、百分号和斜杠，一批下来不能是空的。"""
    import json

    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "subtitle-batch.sqlite")
    samples = (
        (
            "wti",
            "WTI原油期货上涨报91.85美元",
            "WTI11月原油期货收涨0.36美元报91.85美元/桶",
        ),
        (
            "apple",
            "苹果宣布追加iPhone订单",
            "苹果iPhoneDuo加单30%，iPhone18Pro系列总订单量未变",
        ),
        (
            "diesel",
            "财政部宣布柴油协议触发制裁",
            "特朗普与普京达成的柴油协议被指违反俄罗斯制裁法",
        ),
    )
    for index, (source_id, title, _subtitle) in enumerate(samples):
        _insert(source_id, title, 9, 10 + index)
        _insert(source_id + "-b", title, 9, 20 + index, source="wscn")

    def fake(prompt: str, **_kwargs: object) -> str:
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            title = ""
            if "原油" in rest:
                title = samples[0][2]
            elif "苹果" in rest or "iPhone" in rest:
                title = samples[1][2]
            elif "制裁" in rest or "柴油" in rest:
                title = samples[2][2]
            rows.append({"id": head, "title": title, "importance": "重要"})
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", fake)
    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    events = top_hot_events(NOW)["events"]
    subtitles = [str(event.get("subtitle") or "") for event in events]
    assert events
    assert all(subtitles)
    for _source_id, _title, subtitle in samples:
        assert subtitle in subtitles


def test_subjectless_media_headline_uses_the_next_source():
    """去掉报名后没有主体的口号，改用下一条来源，不把「经济日报评论」留在主标题上。"""
    slogan = "经济日报评论：培育赛事经济新增长极"
    nxt = "财政部出台赛事经济扶持政策"
    assert _display_original("经济日报：培育赛事经济新增长极") == "培育赛事经济新增长极"
    assert _original_reject_reason("培育赛事经济新增长极") == "缺少主体或动作"
    assert _first_original_title([("cls", slogan), ("wscn", nxt)]) == nxt
    assert _first_original_title([("cls", slogan)]) == ""


def test_subtitle_is_keyed_by_event_id_not_list_order():
    """模型把 WTI 写在数组前面时，仍按 id 对上，不能安到纳斯达克那条上。"""
    nasdaq = "纳斯达克金龙中国指数上涨"
    oil = "WTI原油期货上涨报91.85美元"
    events = [
        {"_batch_id": "1", "name": nasdaq, "headline": nasdaq, "headlines": [nasdaq]},
        {"_batch_id": "2", "name": oil, "headline": oil, "headlines": [oil]},
    ]
    swapped = (
        '{"events":['
        '{"id":"2","title":"WTI原油期货收涨至91.85美元"},'
        '{"id":"1","title":"纳斯达克金龙中国指数收涨"}'
        ']}'
    )
    parsed = _parse_title_batch(swapped, events)
    assert parsed["1"]["title"] == "纳斯达克金龙中国指数收涨"
    assert parsed["2"]["title"] == "WTI原油期货收涨至91.85美元"
    assert "WTI" not in parsed["1"]["title"]
    shifted = (
        '{"events":['
        '{"title":"WTI原油期货收涨至91.85美元"},'
        '{"title":"纳斯达克金龙中国指数收涨"}'
        ']}'
    )
    assert _parse_title_batch(shifted, events) == {}


def test_subtitle_repeats_are_dropped_and_listing_keeps_the_field(tmp_path, monkeypatch):
    """要点和主标题重复就留空。页面接口要把剩下的要点带出去。漏掉的编号不要记成永久未采用。"""
    import json

    from app.news import hot_events as hot_mod
    from app.news import service as news_service
    from app.news.service import hot_event_listing

    same = {
        "name": "航运公司上调运价",
        "headline": "航运公司上调运价",
        "headlines": ["航运公司上调运价"],
        "_candidates": [{"source": "cls", "title": "航运公司上调运价"}],
        "subtitle": "上调运价",
    }
    _ensure_shown_name(same)
    assert same["name"] == "航运公司上调运价"
    assert same["subtitle"] == ""
    distinct = {
        "name": "旧",
        "headline": "航运公司上调运价",
        "headlines": ["航运公司上调运价"],
        "_candidates": [{"source": "cls", "title": "航运公司上调运价"}],
        "subtitle": "运价上调影响集运公司",
    }
    _ensure_shown_name(distinct)
    assert distinct["subtitle"] == "运价上调影响集运公司"

    reset_store_for_tests(tmp_path / "subtitle-id.sqlite")
    oil = "WTI原油期货上涨报91.85美元"
    nasdaq = "纳斯达克金龙中国指数上涨"
    _insert("oil", oil, 9, 10)
    _insert("oil-b", oil, 9, 12, source="wscn")
    _insert("nasdaq", nasdaq, 9, 11)
    _insert("nasdaq-b", nasdaq, 9, 13, source="wscn")
    prompts: list[str] = []
    flags: list[bool] = []

    def first(prompt: str, **kwargs: object) -> str:
        prompts.append(prompt)
        flags.append(bool(kwargs.get("prefer_final_answer")))
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            if "原油" in rest:
                rows.append({"id": head, "title": "WTI原油期货收涨至91.85美元"})
            elif "纳斯达克" in rest:
                rows.append({"id": head, "title": "纳斯达克金龙中国指数收涨"})
        rows.reverse()
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", first)
    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    listed = hot_event_listing(NOW)
    by_name = {event["name"]: event for event in listed["events"]}
    assert by_name[oil]["subtitle"] == "WTI原油期货收涨至91.85美元"
    assert by_name[nasdaq]["subtitle"] == "纳斯达克金龙中国指数收涨"
    assert "WTI" not in by_name[nasdaq]["subtitle"]
    assert flags and all(flags)
    assert hot_mod._LLM_TITLE_TOKENS >= 4096
    assert not any(item.get("_miss") for item in hot_mod._LLM_CACHE.values() if isinstance(item, dict))


def test_omitted_subtitle_row_is_not_cached_as_miss(tmp_path, monkeypatch):
    """模型没返回某个编号时，不要记成未采用，下一轮还能补上要点。"""
    import json
    from datetime import timedelta

    from app.news import hot_events as hot_mod
    from app.news import service as news_service

    reset_store_for_tests(tmp_path / "subtitle-miss.sqlite")
    oil = "WTI原油期货上涨报91.85美元"
    nasdaq = "纳斯达克金龙中国指数上涨"
    _insert("oil", oil, 9, 10)
    _insert("oil-b", oil, 9, 12, source="wscn")
    _insert("nasdaq", nasdaq, 9, 11)
    _insert("nasdaq-b", nasdaq, 9, 13, source="wscn")

    def partial(prompt: str, **_kwargs: object) -> str:
        if "上一轮" in prompt:
            return "{}"
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or "原油" not in rest:
                continue
            rows.append({"id": head, "title": "WTI原油期货收涨至91.85美元"})
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr("app.news.config.llm_extract_enabled", lambda: True)
    monkeypatch.setattr(news_service, "_llm_text", partial)
    monkeypatch.setattr(news_service, "_reserve_llm_call", lambda: True)
    hot_mod.clear_hot_event_cache()
    first = {event["name"]: event for event in top_hot_events(NOW)["events"]}
    assert first[oil]["subtitle"] == "WTI原油期货收涨至91.85美元"
    assert first[nasdaq]["subtitle"] == ""
    assert not any(item.get("_miss") for item in hot_mod._LLM_CACHE.values() if isinstance(item, dict))

    def complete(prompt: str, **_kwargs: object) -> str:
        rows = []
        for line in prompt.splitlines():
            head, sep, rest = line.partition(". ")
            if not sep or not head.isdigit() or not rest.strip():
                continue
            if "纳斯达克" in rest:
                rows.append({"id": head, "title": "纳斯达克金龙中国指数收涨"})
            elif "原油" in rest:
                rows.append({"id": head, "title": "WTI原油期货收涨至91.85美元"})
        return json.dumps({"events": rows}, ensure_ascii=False)

    monkeypatch.setattr(news_service, "_llm_text", complete)
    hot_mod.clear_hot_event_cache(llm=False)
    second = {event["name"]: event for event in top_hot_events(NOW + timedelta(minutes=11))["events"]}
    assert second[nasdaq]["subtitle"] == "纳斯达克金龙中国指数收涨"


def test_same_company_earnings_notes_are_one_event(tmp_path):
    """同一家公司的业绩预告并成一件。主标题用更可信来源的完整原标题。"""
    reset_store_for_tests(tmp_path / "jushi.sqlite")
    raw = "中国巨石预告点评扣非表现好于预期（激励费环比增长）"
    other = "中国巨石扣非净利润同比增长"
    assert _cleaned_headline(raw) == "中国巨石预告扣非表现好于预期"
    _insert("note", raw, 9, 10)
    _insert("result", other, 11, 40, source="wscn")
    events = [event for event in top_hot_events(NOW)["events"] if "巨石" in event["name"]]
    assert len(events) == 1
    assert events[0]["mentions"] == 2
    assert events[0]["name"] == raw
