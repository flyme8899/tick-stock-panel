"""热门事件「事件→申万行业→个股」补候选：板块识别、排序口径、字段隔离。"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.news import sector_map
from app.news.hot_rank import archive_hot_rank, load_hot_event_rank
from app.news.sector_map import attach_sector_stocks, find_sectors, map_text
from app.news.service import public_hot_event

DATA = Path(sector_map.__file__).resolve().parent / "data"
SW = json.loads((DATA / "sw_industry.json").read_text(encoding="utf-8"))
BEER = "340601"
BIG_BANKS = ("601398.SH", "601939.SH", "601288.SH", "601988.SH")
SMALL_BANKS = ("002807.SZ", "603323.SH", "001227.SZ")


def _bank_members() -> list[str]:
    return [s for code, items in SW["members"].items() if code.startswith("48") for s in items]


def _ranking(amount: dict[str, float], cap: dict[str, float] | None = None) -> dict:
    return {"amount": amount, "cap": cap or {}, "as_of": "2026-10-16"}


@pytest.fixture(autouse=True)
def _clear_cache():
    sector_map.clear_ranking_cache_for_tests()
    yield
    sector_map.clear_ranking_cache_for_tests()


def test_static_data_is_consistent_and_small():
    keywords = json.loads((DATA / "sector_keywords.json").read_text(encoding="utf-8"))
    for word, codes in keywords["industry"].items():
        for code in codes:
            assert code in SW["industries"], (word, code)
    l1 = set(SW["l1"].values())
    for table in ("macro", "theme"):
        for word, targets in keywords[table].items():
            assert set(targets) <= l1, word
    members = [s for items in SW["members"].values() for s in items]
    assert len(members) == len(set(members)), "一只股票只能属一个三级行业"
    assert all(s in SW["names"] for s in members)
    assert (DATA / "sw_industry.json").stat().st_size < 400_000
    for symbol in (*BIG_BANKS, *SMALL_BANKS):
        assert symbol in members


def test_macro_event_maps_to_banks_by_market_cap_not_turnover():
    banks = _bank_members()
    # 小行成交额更大、市值更小：按市值才会排出大行。
    amount = {s: 1.0 for s in banks}
    cap = {s: 1.0 for s in banks}
    for i, s in enumerate(SMALL_BANKS):
        amount[s] = 100.0 + i
    for i, s in enumerate(BIG_BANKS):
        cap[s] = 1000.0 - i
    primary, stocks = map_text(
        "美联储进入10月会议静默期前美债市场紧盯沃什讲话",
        category="海外市场/央行",
        ranking=_ranking(amount, cap),
    )
    assert primary is not None and primary["name"] == "银行" and primary["kind"] == "theme"
    keys = [row["key"] for row in stocks]
    assert keys[:4] == list(BIG_BANKS)
    assert not set(keys) & set(SMALL_BANKS)
    assert len(keys) == sector_map.MAX_SECTOR_STOCKS
    assert {row["rank_by"] for row in stocks} == {"market_cap"}
    assert {row["source"] for row in stocks} == {"sector"}


def test_industry_keyword_uses_sw_members_ranked_by_amount_and_capped():
    members = SW["members"][BEER]
    amount = {s: float(i + 1) for i, s in enumerate(members)}
    primary, stocks = map_text("啤酒依靠6-8元产品扩容", category="科技与产业", ranking=_ranking(amount))
    assert primary["kind"] == "industry3" and primary["codes"] == (BEER,)
    assert primary["confidence"] == pytest.approx(0.90)
    expected = sorted(members, key=lambda s: -amount[s])[: sector_map.MAX_SECTOR_STOCKS]
    assert [row["key"] for row in stocks] == expected
    assert all(row["rank_by"] == "amount" for row in stocks)


def test_industry_theme_word_maps_to_a_group_of_sw_level3_industries():
    pv = {"630501", "630502", "630503", "630504", "630505"}
    members = [s for code in pv for s in SW["members"][code]]
    amount = {s: float(i + 1) for i, s in enumerate(members)}
    amount["300750.SZ"] = 1e12  # 宁德时代成交额再大也不属光伏
    primary, stocks = map_text("通威、协鑫等光伏企业举行反内卷闭门会议", ranking=_ranking(amount))
    assert primary["name"] == "光伏" and set(primary["codes"]) == pv
    assert {row["key"] for row in stocks} <= set(members)
    assert "300750.SZ" not in {row["key"] for row in stocks}


def test_suspended_and_st_members_are_skipped():
    members = SW["members"][BEER]
    amount = {s: 0.0 for s in members}
    amount[members[0]] = 5.0
    _primary, stocks = map_text("啤酒销量回暖", ranking=_ranking(amount))
    assert [row["key"] for row in stocks] == [members[0]]
    st = next(s for s, n in SW["names"].items() if "ST" in n.upper())
    assert not sector_map._tradable(st, SW["names"][st], _ranking({st: 1.0}))


def test_text_without_sector_keywords_maps_nothing():
    assert find_sectors("美国SEC批准KPI合约") == []
    assert map_text("美国SEC批准KPI合约", ranking=_ranking({"601398.SH": 1.0})) == (None, [])


def _names(text: str) -> list[str]:
    return [hit["name"] for hit in find_sectors(text)]


@pytest.mark.parametrize("text", [
    "美国将古巴国际商业银行列入制裁名单",
    "法国农业信贷银行三季度净利同比增长",
    "德国工业银行宣布裁员",
    "汇丰银行下调全年指引",
    "丰田汽车宣布在美召回",
    "美国钢铁被收购",
])
def test_industry_words_inside_foreign_institution_names_are_ignored(text):
    names = _names(text)
    assert "银行" not in names and "汽车" not in names and "钢铁" not in names, names


@pytest.mark.parametrize("text, expected", [
    ("中国银行下调存款利率", "银行"),
    ("今日银行板块大涨", "银行"),
    ("公积金降息对商业银行净息差的直接冲击较小", "银行"),
    ("美股盘前，存储芯片股普跌", "存储"),
    ("美国国际贸易委员会作出晶体硅光伏电池双反产业损害终裁", "光伏"),
])
def test_domestic_and_industry_mentions_still_map(text, expected):
    assert _names(text)[0] == expected


def test_foreign_entity_mask_does_not_swallow_verbs():
    # 「宣布制裁」不是机构名的一部分：只抹掉「伊朗石油」，伊朗仍映射石油石化。
    assert sector_map._mask_foreign_entities("美国宣布制裁伊朗石油出口").startswith("美国宣布制裁")
    assert _names("美国宣布制裁伊朗石油出口") == ["石油石化"]


@pytest.mark.parametrize("text", [
    "特朗普宣布暂缓实施新一轮对俄制裁",
    "特朗普豁免《格雷厄姆法案》中多项对俄制裁、限制措施及关税",
    "欧盟通过第20轮制裁方案",
])
def test_sanctions_alone_do_not_map_to_defense(text):
    assert "国防军工" not in _names(text)


def test_geopolitical_words_need_military_terms_for_defense():
    # 只有「俄罗斯」：只留石油石化。
    assert _names("泽连斯基：俄罗斯已将放松制裁的信号视为继续袭击的许可") == ["石油石化"]
    # 有军事词才映射军工，置信度语义不变（主题词 0.4）。
    hits = find_sectors("美国宣布对俄制裁并扩大对乌克兰武器援助")
    assert hits[0]["name"] == "国防军工"
    assert hits[0]["confidence"] == pytest.approx(sector_map.CONF_THEME)
    assert _names("台海附近举行联合军演")[0] == "国防军工"
    assert _names("军工板块午后拉升")[0] == "国防军工"


def test_military_gate_and_foreign_tables_are_consistent():
    keywords = json.loads((DATA / "sector_keywords.json").read_text(encoding="utf-8"))
    assert set(keywords["military_gate"]["targets"]) <= set(SW["l1"].values())
    assert keywords["military_gate"]["require"]
    # 单字国名（「日」「加」）会把「今日银行」「增加银行」误当外国机构，不收。
    assert all(len(name) >= 2 for name in keywords["foreign_entity"]["countries"])


def _write_market(data_dir: Path, amount: dict[str, float], close: float = 10.0) -> None:
    day = data_dir / "kline_daily" / "date=2026-10-16"
    day.mkdir(parents=True)
    pl.DataFrame({
        "symbol": list(amount),
        "close": [close] * len(amount),
        "amount": list(amount.values()),
    }).write_parquet(day / "part.parquet")
    inst = data_dir / "instruments"
    inst.mkdir()
    pl.DataFrame({
        "symbol": list(amount),
        "total_shares": [1e9] * len(amount),
    }).write_parquet(inst / "instruments.parquet")


def test_attach_only_fills_events_without_direct_stocks(tmp_path):
    _write_market(tmp_path, {s: 1e8 for s in SW["members"][BEER]})
    direct = {
        "key": "ev1", "name": "啤酒企业青岛啤酒提价", "category": "公司重大事项",
        "mentioned_stocks": [{"key": "600600.SH", "name": "青岛啤酒", "mentions": 1}],
    }
    etf_only = {
        "key": "ev2", "name": "啤酒依靠6-8元产品扩容", "category": "科技与产业",
        "mentioned_stocks": [{"key": "510300.SH", "name": "沪深300ETF", "mentions": 1}],
        "score": 50.0,
    }
    none = {"key": "ev3", "name": "美国SEC批准KPI合约", "category": "海外市场/央行"}
    events = [direct, etf_only, none]
    assert attach_sector_stocks(events, data_dir=tmp_path) == 1
    assert "sector_stocks" not in direct
    assert direct["mentioned_stocks"] == [{"key": "600600.SH", "name": "青岛啤酒", "mentions": 1}]
    assert etf_only["mentioned_stocks"] == [{"key": "510300.SH", "name": "沪深300ETF", "mentions": 1}]
    assert etf_only["score"] == 50.0
    assert len(etf_only["sector_stocks"]) == min(5, len(SW["members"][BEER]))
    assert etf_only["sector_mapping"]["kind"] == "industry3"
    assert "sector_stocks" not in none


def test_attach_without_local_market_data_adds_nothing(tmp_path):
    event = {"key": "ev", "name": "啤酒依靠6-8元产品扩容"}
    assert attach_sector_stocks([event], data_dir=tmp_path) == 0
    assert "sector_stocks" not in event


def test_public_event_keeps_sector_stocks_apart_from_direct_stocks(tmp_path):
    _write_market(tmp_path, {s: 1e8 for s in SW["members"][BEER]})
    event = {"key": "ev", "name": "啤酒依靠6-8元产品扩容", "category": "科技与产业"}
    attach_sector_stocks([event], data_dir=tmp_path)
    public = public_hot_event(event)
    assert public["stocks"] == []
    assert public["stock_source"] == "sector"
    assert public["sector_stocks"][0]["source"] == "sector"
    assert public["sector_mapping"]["kind"] == "industry3"
    assert public["sector_mapping"]["confidence"] == pytest.approx(0.90)
    direct = public_hot_event({"key": "d", "name": "x", "mentioned_stocks": [{"key": "600519.SH", "name": "贵州茅台"}]})
    assert direct["stock_source"] == "direct"
    assert "sector_stocks" not in direct
    assert public_hot_event({"key": "n", "name": "x"})["stock_source"] == ""


def test_hot_rank_archive_records_sector_columns_and_reads_old_partitions(tmp_path):
    now = datetime(2026, 10, 10, 11, 0, tzinfo=CN_TZ)  # 周六，写下一开市日档
    snapshot = {
        "as_of": "2026-10-09", "trading_day": "2026-10-12", "computed_at": now.isoformat(),
        "events": [{
            "key": "ev", "name": "啤酒依靠6-8元产品扩容",
            "sector_stocks": [{"key": "600600.SH", "name": "青岛啤酒", "sector": "啤酒", "sector_kind": "industry3", "confidence": 0.9}],
            "sector_mapping": {"name": "啤酒", "kind": "industry3", "confidence": 0.9, "why": "行业关键词:啤酒"},
        }],
    }
    archive_hot_rank(snapshot, now=now, data_dir=tmp_path)
    frame = load_hot_event_rank(tmp_path, date(2026, 10, 12))
    assert frame["stocks"].to_list() == [[]]
    assert frame["sector_stocks"].to_list() == [["600600.SH"]]
    assert frame["sector_name"].to_list() == ["啤酒"]
    assert frame["sector_confidence"].to_list() == [pytest.approx(0.9)]
    # 旧分区（没有板块列）和新分区拼在一起读不报错。
    old_dir = tmp_path / "news" / "hot_event_rank" / "date=2026-10-12"
    pl.DataFrame({"session_date": [date(2026, 10, 12)], "slot": ["preopen"], "event_key": ["old"]}).write_parquet(old_dir / "preopen.parquet")
    both = load_hot_event_rank(tmp_path, date(2026, 10, 12))
    assert set(both["event_key"].to_list()) == {"ev", "old"}
