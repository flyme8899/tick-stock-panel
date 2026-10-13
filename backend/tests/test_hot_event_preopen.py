"""盘前和隔夜事件要在价格走出来之前进热榜。

2026-10-12 的复盘：金丹科技提价从周日就在研究源里，网络安全在 07:26 晨报里，
但热榜都要等盘中财联社才出现。这里用同一天的夹具把两条路径钉住。
"""
from __future__ import annotations

from datetime import date, datetime
from datetime import time as dt_time

from app.market_time import CN_TZ
from app.news.hot_events import (
    _TITLE_SLICE,
    _can_merge,
    _doc,
    _events_for_titles,
    _expand_topic_rows,
    _first_original_title,
    _freshness_age_hours,
    _prepare_merge_doc,
    _rule_importance,
    _score_event,
    build_top_hot_events,
    clear_hot_event_cache,
)
from app.news.market_confirm import (
    _tape_days,
    confirm_session_day,
    score_confirmation,
)
from app.news.service import get_store, reset_store_for_tests

MONDAY = date(2026, 10, 12)
JINDAN = "300829.SZ"
TIANRONG = "002212.SZ"


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ)


def _insert(
    source: str,
    source_id: str,
    published: datetime,
    title: str,
    *,
    text: str | None = None,
    mentions: list[tuple[str, str, str, str, str]] | None = None,
) -> None:
    status = get_store().insert_item(
        source=source,
        source_id=source_id,
        published_at=published,
        author="",
        title=title,
        clean_text=text if text is not None else title,
        raw=None,
        content_hash=source_id,
        url="",
        level="",
        media_ids=[],
        extra=None,
        mentions=mentions or [],
    )
    assert status == "inserted"


def _stock(symbol: str, name: str) -> tuple[str, str, str, str, str]:
    return ("stock", symbol, name, symbol.split(".", 1)[0], "structured")


def _sector(name: str) -> tuple[str, str, str, str, str]:
    return ("sector", name, name, "", "structured")


def _grade(title: str) -> str:
    return _rule_importance({
        "name": title,
        "headline": title,
        "headlines": [title],
        "category": "科技与产业",
    })


def _row(
    title: str,
    published: datetime,
    *,
    source: str = "dws",
    stocks: dict[str, str] | None = None,
    concepts: list[str] | None = None,
) -> dict:
    mentions = []
    for key, name in (stocks or {}).items():
        mentions.append({"kind": "stock", "key": key, "name": name})
    for name in concepts or []:
        mentions.append({"kind": "sector", "key": name, "name": name})
    doc = _doc({
        "id": title,
        "source": source,
        "title": title,
        "clean_text": title,
        "published": published,
        "mentions": mentions,
        "level": "",
    })
    assert doc is not None
    _prepare_merge_doc(doc)
    return doc


def _names(snapshot: dict) -> list[str]:
    return [str(event["name"]) for event in snapshot["events"]]


def _find(snapshot: dict, needle: str) -> dict | None:
    for event in snapshot["events"]:
        blob = "\n".join([
            str(event.get("name") or ""),
            " ".join(str(name) for name in (event.get("concepts") or [])),
        ])
        if needle in blob:
            return event
    return None


def _replay(now: datetime) -> dict:
    clear_hot_event_cache()
    return build_top_hot_events(now, get_store(), refresh=True)


def _auction_tape(now: datetime, _events: list[dict]) -> dict:
    local = now.astimezone(CN_TZ)
    ready = local.weekday() < 5 and local.time() >= dt_time(9, 25)
    bars = []
    if ready:
        bars.append({
            "at": _at(MONDAY, 9, 25),
            "open": 10.76,
            "high": 10.76,
            "low": 10.76,
            "close": 10.76,
            "volume": 1_000_000,
            "kind": "minute",
        })
    if local.time() >= dt_time(9, 31):
        bars.append({
            "at": _at(MONDAY, 9, 31),
            "open": 10.76,
            "high": 10.9,
            "low": 10.7,
            "close": 10.9,
            "volume": 200_000,
            "kind": "minute",
        })
    session = MONDAY.isoformat() if ready else "2026-10-09"
    return {
        "session": session,
        "live": False,
        "members": {},
        "index_bars": [],
        "stocks": {
            JINDAN: {
                "name": "金丹科技",
                "prev_close": 10.0,
                "prior_high": 12.0,
                "prior_low": 8.0,
                "prior_volume": 1_000_000,
                "baseline_volume": 1_000_000,
                "bars": bars,
                "flows": [],
            },
        },
        "concepts": {},
        "changes": {},
    }


def _seed_oct12() -> None:
    """10 条更重的隔夜事件把没验证的提价压到第 11，竞价分再把它抬进前 10。"""
    fillers = (
        ("美联储宣布降息二十五基点", "美联储"),
        ("欧央行维持观察并讨论降息", "欧央行"),
        ("日本央行宣布加息并缩表", "日本央行"),
        ("国务院出台家电以旧换新补贴", "以旧换新"),
        ("发改委出台算力基建补贴政策", "算力基建"),
        ("工信部出台人形机器人采购补贴", "人形机器人"),
        ("财政部出台设备更新补贴方案", "设备更新"),
        ("美国扩大对华芯片出口管制", "出口管制"),
        ("碳酸锂报价继续上涨", "碳酸锂"),
        ("原油期货报价大幅上涨", "原油期货"),
    )
    sources = ("cls", "wscn", "reuters", "bloomberg")
    for index, (title, concept) in enumerate(fillers):
        symbol = f"601{index + 1:03d}.SH"
        for offset, source in enumerate(sources):
            _insert(
                source,
                f"fill-{index}-{source}",
                _at(MONDAY, 8, 30 + offset),
                title,
                mentions=[_stock(symbol, title[:4]), _sector(concept)],
            )
    _insert(
        "dws",
        "jindan-sun",
        _at(date(2026, 10, 11), 9, 0),
        "金丹科技发布涨价函，产品提价10%",
        mentions=[_stock(JINDAN, "金丹科技")],
    )
    for minute, source_id, title in (
        (1, "jindan-1", "金丹科技涨价函已经发出，产品提价10%"),
        (26, "jindan-2", "金丹科技再次提价10%，乳酸系列上调价格"),
        (54, "jindan-3", "金丹科技提价函显示幅度达到10%"),
        (58, "jindan-4", "金丹科技上调产品价格10%"),
    ):
        _insert(
            "zsxq",
            source_id,
            _at(MONDAY, 8, minute),
            title,
            mentions=[_stock(JINDAN, "金丹科技")],
        )
    brief = "\n".join([
        "晨会纪要",
        "1、网络安全：天融信中标政务云安全项目",
        "2、机器人：工信部出台人形机器人补贴",
        "3、存储芯片：厂商宣布涨价",
    ])
    _insert(
        "dws",
        "brief-0726",
        _at(MONDAY, 7, 26),
        "晨会纪要",
        text=brief,
        mentions=[
            _sector("网络安全"),
            _stock(TIANRONG, "天融信"),
            _sector("存储芯片"),
        ],
    )


def test_clean_research_headline_is_used_and_chatter_is_not():
    price = "金丹科技发布涨价函，产品提价10%"
    assert "金丹科技" in _first_original_title([("dws", price)])
    assert "金丹科技" in _first_original_title([("zsxq", "金丹科技上调产品价格10%")])
    assert "天融信" in _first_original_title([("ima", "天融信中标政务云安全项目")])
    clinical = "J26完成RCT临床验证1基本面更新"
    pcb = "小PCB公司们的8月单月业绩崇达8月单月1e+利润，超声"
    assert _first_original_title([("zsxq", clinical)]) == ""
    assert _first_original_title([("dws", pcb)]) == ""
    assert _first_original_title([("ima", clinical)]) == ""


def test_numbered_tail_does_not_split_a_clean_research_headline():
    row = {
        "source": "zsxq",
        "title": "金丹科技发布涨价函，产品提价10%",
        "clean_text": "金丹科技发布涨价函，产品提价10%\n1、乳酸提价10%\n2、柠檬酸同步上调价格",
        "mentions": [],
    }
    assert _expand_topic_rows(row) == [row]


def test_company_price_hike_grades():
    assert _grade("金丹科技发布涨价函") == "重要"
    assert _grade("金丹科技上调产品价格") == "重要"
    assert _grade("金丹科技发出提价函") == "重要"
    assert _grade("金丹科技产品提价10%") == "重大"
    assert _grade("金丹科技再次提价") == "重大"
    assert _grade("存储芯片厂宣布涨价") == "重要"
    assert _grade("存储芯片厂宣布涨价10%") == "重要"
    assert _grade("光伏组件厂下调报价") == "一般"


def test_overnight_same_stock_and_action_merges_across_the_weekend():
    sunday = _row(
        "金丹科技发布涨价函，产品提价10%",
        _at(date(2026, 10, 11), 9, 0),
        stocks={JINDAN: "金丹科技"},
    )
    morning = _row(
        "金丹科技上调产品价格10%",
        _at(MONDAY, 8, 58),
        source="zsxq",
        stocks={JINDAN: "金丹科技"},
    )
    buyback = _row(
        "金丹科技宣布回购股份",
        _at(MONDAY, 8, 40),
        source="zsxq",
        stocks={JINDAN: "金丹科技"},
    )
    assert _can_merge(sunday, morning)
    assert not _can_merge(sunday, buyback)
    friday_open = _row(
        "金丹科技发布涨价函，产品提价10%",
        _at(date(2026, 10, 9), 10, 5),
        source="cls",
        stocks={JINDAN: "金丹科技"},
    )
    friday_late = _row(
        "金丹科技发布涨价函，产品提价10%",
        _at(date(2026, 10, 9), 18, 30),
        source="cls",
        stocks={JINDAN: "金丹科技"},
    )
    assert not _can_merge(friday_open, friday_late)


def test_weekend_freshness_starts_at_the_session_open():
    first = _at(date(2026, 10, 11), 9, 0)
    event = {
        "importance": "重要",
        "concepts": [],
        "mentioned_stocks": [],
        "direction": "",
        "heat": 1,
        "confirmation": {},
        "_first": first,
        "_latest": first,
    }
    open_clock = _at(MONDAY, 9, 25)
    assert _freshness_age_hours(event, open_clock) < 1
    _score_event(event, open_clock)
    # 新鲜度封顶从 20 降到 15（乘性计分：封顶之和必须等于 _AUX_SCALE）。
    assert event["breakdown"]["freshness"] > 11
    event["_latest"] = _at(MONDAY, 9, 20)
    assert _freshness_age_hours(event, open_clock) < 0.2


def test_auction_session_includes_today_at_925():
    auction = _at(MONDAY, 9, 25)
    early = _at(MONDAY, 9, 10)
    event = {"_first": _at(date(2026, 10, 11), 9, 0)}
    assert confirm_session_day(auction) == MONDAY
    assert MONDAY in _tape_days(auction, [event])
    assert confirm_session_day(early) == date(2026, 10, 9)
    assert MONDAY not in _tape_days(early, [event])


def test_untitled_stock_cluster_still_scores_the_auction():
    event = {
        "name": "",
        "headline": "",
        "headlines": [],
        "direction": "利好",
        "concepts": [],
        "mentioned_stocks": [{"key": JINDAN, "name": "金丹科技"}],
        "_first": _at(date(2026, 10, 11), 9, 0),
    }
    scored = score_confirmation(event, _auction_tape(_at(MONDAY, 9, 25), []), _at(MONDAY, 9, 25))
    assert scored["phase"] == "auction"
    assert scored["score"] > 0
    assert scored["detail"]["auction_open_pct"] >= 0.07
    assert scored["detail"]["auction_vol_ratio"] >= 20

    opening = dict(event)
    tape = _auction_tape(_at(MONDAY, 9, 31), [])
    tape["stocks"][JINDAN]["bars"] = [
        {
            "at": _at(MONDAY, 9, 31),
            "open": 10.76,
            "high": 10.76,
            "low": 10.76,
            "close": 10.76,
            "volume": 2_000_000,
            "kind": "minute",
        },
    ]
    opened = score_confirmation(opening, tape, _at(MONDAY, 9, 31))
    assert opened["pending"] is False
    assert opened["label"] != "待开盘验证"
    assert opened["score"] > 0


def test_stock_mapped_cluster_is_titled_ahead_of_the_slice_cut():
    titled = []
    for index in range(_TITLE_SLICE):
        title = f"美联储宣布降息{index:02d}基点"
        titled.append({
            "name": title,
            "importance": "一般",
            "headline": title,
            "headlines": [title],
            "mentioned_stocks": [],
            "concepts": [],
            "_candidates": [{"source": "cls", "title": title}],
        })
    buried = {
        "name": "",
        "importance": "重要",
        "headline": "J26完成RCT临床验证1基本面更新",
        "headlines": ["J26完成RCT临床验证1基本面更新"],
        "mentioned_stocks": [{"key": JINDAN, "name": "金丹科技"}],
        "concepts": [],
        "_candidates": [{"source": "zsxq", "title": "J26完成RCT临床验证1基本面更新"}],
    }
    chosen = _events_for_titles([*titled, buried])
    assert len(chosen) == len(titled)
    assert buried in chosen


def test_oct12_replay_shows_cyber_after_the_brief_and_jindan_by_the_open(tmp_path, monkeypatch):
    reset_store_for_tests(tmp_path / "oct12.sqlite")
    monkeypatch.setattr("app.news.market_confirm.load_market_tape", _auction_tape)
    _seed_oct12()

    after_brief = _replay(_at(MONDAY, 7, 30))
    cyber = _find(after_brief, "网络安全")
    assert cyber is not None
    assert "天融信" in str(cyber["name"])
    assert "网络安全" in (cyber.get("concepts") or [])
    assert "晨会纪要" not in _names(after_brief)

    before_move = _replay(_at(MONDAY, 9, 0))
    waiting = _find(before_move, "金丹")
    assert waiting is not None
    assert waiting["importance"] == "重大"
    assert waiting["mentions"] >= 2
    assert waiting["confirmation"]["label"] == "待开盘验证"
    assert waiting["confirmation"]["pending"] is True
    assert waiting["confirmation"]["score"] == 0
    before_rank = _names(before_move).index(waiting["name"])

    for hour, minute in ((9, 25), (9, 31)):
        opened = _replay(_at(MONDAY, hour, minute))
        priced = _find(opened, "金丹")
        assert priced is not None
        rank = _names(opened).index(priced["name"])
        assert rank < 10
        assert rank < before_rank
        assert priced["confirmation"]["score"] > 0
        assert priced["confirmation"]["phase"] == "auction"
        assert priced["breakdown"]["freshness"] > 11
        assert priced["confirmation"]["detail"]["auction_open_pct"] >= 0.07
