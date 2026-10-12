"""打板解析、落盘、同花顺降级，以及热门事件核对。

合成夹具按 simonlin1212/a-stock-data SKILL.md 的字段写成。
``recorded_*.json`` 是 2026-10-09 从东财和同花顺裁到 1 条的真实响应。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.limit_board import jobs, store
from app.limit_board.enrich import apply_limit_reasons
from app.limit_board.parse import parse_ths_pool, parse_zt_pool, sentiment_row
from app.limit_board.query import overlay_stock, reasons_for, sentiment_for, theme_symbols
from app.market_feeds.scheduler import due_tasks
from app.market_time import CN_TZ
from app.news.market_confirm import _limit_reason_hits, _members

_FIXTURES = Path(__file__).parent / "fixtures" / "limit_board"
_DAY = "2026-10-09"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, payload, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload
        self.url = "https://example.eastmoney.com/pool"

    def json(self):
        return self._payload


def test_zt_pool_prices_are_divided_and_times_are_clocks() -> None:
    rows = parse_zt_pool(_load("zt_pool.json"), _DAY)
    assert rows is not None
    first = rows[0]
    assert first["code"] == "600519"
    assert first["symbol"] == "600519.SH"
    assert first["price"] == 1680.0
    assert first["pct"] == 10.01
    assert first["first_seal"] == "09:25:00"
    assert first["last_seal"] == "09:30:12"
    assert first["limit_days"] == 2
    assert first["zt_stat"] == "2天2板"
    assert rows[1]["price"] == 250.0
    assert rows[1]["symbol"] == "300750.SZ"


def test_sentiment_break_rate_is_a_ratio() -> None:
    zt = parse_zt_pool(_load("zt_pool.json"), _DAY) or []
    zb_payload = _load("zb_pool.json")
    from app.limit_board.parse import parse_zb_pool

    zb = parse_zb_pool(zb_payload, _DAY) or []
    row = sentiment_row(_DAY, zt, zb, [], None)
    assert row["zt_count"] == 2
    assert row["zb_count"] == 2
    assert row["break_rate"] == 0.5
    assert row["max_height"] == 2
    assert json.loads(row["ladder_json"]) == {"1": 1, "2": 1}
    assert row["advance_count"] is None


def test_recorded_live_payloads_match_the_parser() -> None:
    zt = parse_zt_pool(_load("recorded_zt_pool.json"), _DAY)
    assert zt is not None and zt[0]["code"] == "002058"
    assert zt[0]["price"] == 24.44
    assert zt[0]["pct"] == 9.99
    assert zt[0]["first_seal"] == "09:25:00"
    from app.limit_board.parse import parse_zb_pool

    zb = parse_zb_pool(_load("recorded_zb_pool.json"), _DAY)
    assert zb is not None and zb[0]["limit_price"] == 34.68
    ths = parse_ths_pool(_load("recorded_ths_pool.json"), _DAY)
    assert ths is not None
    assert ths[0]["themes"] == "数据中心、AI营销、体育营销"
    assert ths[0]["first_time"] == "14:27:01"
    assert ths[0]["seal_rate"] == pytest.approx(0.8571428571428571)


def test_ths_themes_and_beijing_time() -> None:
    rows = parse_ths_pool(_load("ths_pool.json"), _DAY)
    assert rows is not None
    assert rows[0]["reason"] == "固态电池+白酒"
    assert rows[0]["themes"] == "固态电池、白酒"
    assert rows[0]["seal_rate"] == 0.86
    assert rows[0]["first_time"] == "09:31:02"
    assert rows[1]["themes"] == "固态电池概念"


def test_sync_writes_pools_and_skips_unreachable_ths(tmp_path: Path) -> None:
    pools = {
        "getTopicZTPool": _load("zt_pool.json"),
        "getTopicZBPool": _load("zb_pool.json"),
        "getTopicDTPool": _load("dt_pool.json"),
        "getYesterdayZTPool": _load("yzt_pool.json"),
    }
    seen: list[str] = []

    def eastmoney(url, **_kwargs):
        seen.append(url)
        for name, payload in pools.items():
            if name in url:
                return _Resp(payload)
        raise AssertionError(url)

    def ths(*_args, **_kwargs):
        raise TimeoutError("10jqka unreachable")

    result = jobs.sync_day(tmp_path, _DAY, get=eastmoney, ths_get=ths)
    assert result["ths"] == "unavailable"
    assert result["wrote"]["zt"] == 2
    assert result["wrote"]["sentiment"] == 1
    saved = store.read_partition(tmp_path, "zt", _DAY)
    assert saved["price"].to_list()[0] == 1680.0
    sentiment = sentiment_for(tmp_path, _DAY)
    assert sentiment is not None
    assert sentiment["break_rate"] == 0.5
    assert sentiment["advance_count"] == 1
    assert sentiment["ladder"] == {"1": 1, "2": 1}
    assert "pdf." not in "".join(seen)


def test_ths_health_failure_does_not_block_a_later_success(tmp_path: Path) -> None:
    calls = {"n": 0}

    def ths(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp({"status_code": 0}, status=200)
        return _Resp(_load("ths_pool.json"))

    result = jobs.sync_day(
        tmp_path,
        _DAY,
        eastmoney=False,
        get=lambda *_a, **_k: pytest.fail("东财应关闭"),
        ths_get=ths,
    )
    assert result["ths"] == "unavailable"
    assert calls["n"] == 1
    assert store.read_partition(tmp_path, "ths", _DAY).is_empty()


def test_null_pool_is_not_written_and_empty_pool_replaces(tmp_path: Path) -> None:
    store.write_rows(tmp_path, "zt", _DAY, parse_zt_pool(_load("zt_pool.json"), _DAY) or [])
    assert store.read_partition(tmp_path, "zt", _DAY).height == 2

    def eastmoney(url, **_kwargs):
        if "getTopicZTPool" in url:
            return _Resp({"data": {"pool": []}})
        if "getTopicZBPool" in url:
            return _Resp({"data": None})
        if "getTopicDTPool" in url:
            return _Resp(_load("dt_pool.json"))
        return _Resp(_load("yzt_pool.json"))

    result = jobs.sync_day(tmp_path, _DAY, ths=False, get=eastmoney)
    assert store.read_partition(tmp_path, "zt", _DAY).is_empty()
    assert "zb: 无有效数据" in result["errors"]
    assert "sentiment" not in result["wrote"]
    assert not store.partition_file(tmp_path, "sentiment", _DAY).exists()


def test_partition_replace_drops_opened_boards(tmp_path: Path) -> None:
    first = parse_zt_pool(_load("zt_pool.json"), _DAY) or []
    store.write_rows(tmp_path, "zt", _DAY, first)
    store.write_rows(tmp_path, "zt", _DAY, first[:1])
    codes = store.read_partition(tmp_path, "zt", _DAY)["code"].to_list()
    assert codes == ["600519"]


def test_overlay_and_theme_symbols(tmp_path: Path) -> None:
    rows = parse_ths_pool(_load("ths_pool.json"), _DAY) or []
    store.write_rows(tmp_path, "ths", _DAY, rows)
    reasons = reasons_for(tmp_path, _DAY)
    stock = {"symbol": "600519.SH", "name": "贵州茅台"}
    overlay_stock(stock, reasons)
    assert stock["limit_reason"] == "固态电池+白酒"
    assert stock["limit_themes"] == ["固态电池", "白酒"]
    assert stock["seal_rate"] == 0.86
    found = theme_symbols(tmp_path, _DAY, ["固态电池概念", "银行"])
    assert found["固态电池概念"] == ["600519.SH", "300750.SZ"]
    assert "银行" not in found


def test_enrich_verifies_and_skips_new_broad_names(tmp_path: Path) -> None:
    store.write_rows(tmp_path, "ths", _DAY, parse_ths_pool(_load("ths_pool.json"), _DAY) or [])
    events = [
        {
            "concepts": ["固态电池概念", "白酒"],
            "direction": "利好",
            "name": "贵州茅台股价创历史新高",
            "headlines": ["贵州茅台：前三季度净利润同比增长"],
            "mentioned_stocks": [{"key": "600519.SH", "name": "贵州茅台"}],
            "mapping": [{"name": "固态电池概念", "kind": "concept", "direction": "利好"}],
        },
        {
            "concepts": [],
            "direction": "利好",
            "name": "宁德时代概念股集体大涨",
            "headlines": ["宁德时代放量拉升"],
            "mentioned_stocks": [{"key": "300750.SZ", "name": "宁德时代"}],
        },
    ]
    apply_limit_reasons(events, tmp_path, _DAY)
    assert events[0]["limit_reason"]["verified"] == ["固态电池概念", "白酒"]
    assert events[0]["limit_reason"]["enriched"] == []
    assert events[1]["limit_reason"]["enriched"] == ["固态电池概念"]
    assert events[1]["concepts"] == ["固态电池概念"]
    assert events[1]["mapping"][0]["origin"] == "limit_reason"
    assert _limit_reason_hits(events[0]) == 2
    assert _limit_reason_hits({}) == 0

    members = _members(tmp_path, ["固态电池概念"], date.fromisoformat(_DAY))
    assert members["固态电池概念"][0] == "600519.SH"
    assert "300750.SZ" in members["固态电池概念"]


def test_enrich_skips_limit_theme_for_stock_absent_from_titles(tmp_path: Path) -> None:
    """涨停题材只补股票名在事件标题里出现过的事件。

    线上实测双写残差：早报目录行的上游 stocks 混进弱相关个股（东百集团进了
    「证监会座谈会」簇，15 个成员没有一条讲收购），涨停题材把「光芯片收购」
    补进该簇，概念与成员完全脱节。股票名不在任何成员标题里，题材就没有
    锚定资格 —— 本例用 fixture 里的宁德时代：涨停题材命中，但股票名不在
    事件标题里，enriched 必须为空。
    """
    store.write_rows(tmp_path, "ths", _DAY, parse_ths_pool(_load("ths_pool.json"), _DAY) or [])
    events = [
        {
            "concepts": [],
            "direction": "",
            "name": "证监会召开资本市场和金融形势专家座谈会，听取意见建议",
            "headlines": ["证监会召开资本市场和金融形势专家座谈会"],
            "mentioned_stocks": [{"key": "300750.SZ", "name": "宁德时代"}],
        },
    ]
    apply_limit_reasons(events, tmp_path, _DAY)
    # 股票被锚定校验挡在最外面，verified/enriched 都空，事件压根不写
    # limit_reason 键 —— 概念列表保持原样。
    assert "limit_reason" not in events[0]
    assert events[0]["concepts"] == []


def test_enrich_anchors_stock_by_short_name_or_code(tmp_path: Path) -> None:
    """锚定校验支持简称与 6 位代码，别把该补的题材误挡。

    标题常写简称（「威领」而非「*ST威领」）或代码（「300750」）。只看全名会把
    这些正确场景挡掉，所以去掉 ST/XD 前缀的正名和 6 位代码也算锚定。
    """
    store.write_rows(tmp_path, "ths", _DAY, parse_ths_pool(_load("ths_pool.json"), _DAY) or [])
    # 简称命中：股票带 *ST 前缀，标题只写去前缀的名字。
    short_hit = {
        "concepts": [],
        "direction": "",
        "name": "威领股份要约收购结果出炉",
        "headlines": ["威领股份要约收购结果出炉"],
        "mentioned_stocks": [{"key": "300750.SZ", "name": "*ST威领"}],
    }
    # 代码命中：标题只写 6 位代码。
    code_hit = {
        "concepts": [],
        "direction": "",
        "name": "300750盘中涨停",
        "headlines": ["300750盘中涨停"],
        "mentioned_stocks": [{"key": "300750.SZ", "name": "宁德时代"}],
    }
    apply_limit_reasons([short_hit, code_hit], tmp_path, _DAY)
    # 两只都锚定成功，涨停题材（fixture 里宁德时代对应固态电池概念）应被补上。
    assert short_hit["limit_reason"]["enriched"] == ["固态电池概念"]
    assert code_hit["limit_reason"]["enriched"] == ["固态电池概念"]


def test_enrich_code_anchor_requires_digit_boundary(tmp_path: Path) -> None:
    """6 位代码要前后非数字，「600000 吨」这类金额数量不该被当成代码锚定。"""
    store.write_rows(tmp_path, "ths", _DAY, parse_ths_pool(_load("ths_pool.json"), _DAY) or [])
    # 标题里的 7 位数字（6000001）包含 6 位子串，但没有数字边界 → 不该锚定。
    digits = {
        "concepts": [],
        "direction": "",
        "name": "某公司成交额达3007500万元",
        "headlines": ["某公司成交额达3007500万元"],
        "mentioned_stocks": [{"key": "300750.SZ", "name": "宁德时代"}],
    }
    apply_limit_reasons([digits], tmp_path, _DAY)
    assert "limit_reason" not in digits


def test_limit_board_sentiment_route(tmp_path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.limit_board import router

    zt = parse_zt_pool(_load("zt_pool.json"), _DAY) or []
    from app.limit_board.parse import parse_dt_pool, parse_zb_pool

    zb = parse_zb_pool(_load("zb_pool.json"), _DAY) or []
    dt = parse_dt_pool(_load("dt_pool.json"), _DAY) or []
    store.write_rows(tmp_path, "sentiment", _DAY, [sentiment_row(_DAY, zt, zb, dt, None)])
    monkeypatch.setattr("app.api.limit_board._dir", lambda: tmp_path)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    payload = client.get("/api/limit-board/sentiment").json()
    assert payload["trade_date"] == _DAY
    assert payload["sentiment"]["break_rate"] == 0.5
    assert payload["sentiment"]["dt_count"] == 1


def test_scheduler_close_then_research_and_weekend_idle() -> None:
    flags = {"limit_board": True, "research": True}
    morning = datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ)
    assert due_tasks(morning, trading=True, state={}, flags=flags) == ["limit_intraday"]
    close = datetime(2026, 10, 9, 15, 20, tzinfo=CN_TZ)
    assert due_tasks(close, trading=True, state={}, flags=flags) == ["limit_close"]
    # 收盘快照落定后（research 窗口 16:00 起）才轮到研报
    after = datetime(2026, 10, 9, 16, 5, tzinfo=CN_TZ)
    state = {"limit_close_date": _DAY}
    assert due_tasks(after, trading=True, state=state, flags=flags) == ["research"]
    # 周末/非交易日要补上一交易日的收盘快照（原实现把非交易日整个排除，导致
    # 上线后首个周末之后 limit_board 目录一直没有数据）。收盘快照优先，
    # 补到位后这一轮只剩研报。run_once 每轮只跑 tasks[0]，所以两次调用都要看。
    weekend = datetime(2026, 10, 10, 11, 0, tzinfo=CN_TZ)
    assert due_tasks(weekend, trading=False, state={}, flags=flags) == ["limit_close"]
    assert due_tasks(weekend, trading=False,
                     state={"limit_close_date": _DAY}, flags=flags) == ["research"]
    monday = datetime(2026, 10, 12, 8, 0, tzinfo=CN_TZ)  # 周一 08:00 盘前
    # 交易日盘前 session_day 还是上一交易日（09:35 才切当天），快照没补 → 先补快照
    assert due_tasks(monday, trading=True, state={}, flags=flags) == ["limit_close"]
    assert due_tasks(monday, trading=True,
                     state={"limit_close_date": _DAY}, flags=flags) == []

def test_non_trading_holiday_catches_up_previous_session() -> None:
    """长假期间（周三，非交易日）也要回补上一交易日，不能整个假期空转。"""
    flags = {"limit_board": True, "research": True}
    holiday = datetime(2026, 10, 7, 11, 0, tzinfo=CN_TZ)  # 国庆假期中的周三
    due = due_tasks(holiday, trading=False, state={}, flags=flags)
    assert due[0] == "limit_close"
    # 收盘快照补到 session_day 后，同一窗口继续补研报（run_once 每轮只跑 tasks[0]）
    session = date(2026, 10, 6).isoformat()
    assert due_tasks(holiday, trading=False,
                     state={"limit_close_date": session}, flags=flags) == ["research"]


def test_non_close_day_retry_is_gap_limited() -> None:
    """补跑受 30 分钟冷却保护，不会每 30 秒一轮疯狂重试。"""
    flags = {"limit_board": True, "research": True}
    sat = datetime(2026, 10, 10, 11, 0, tzinfo=CN_TZ)
    just_ran = {"limit_close_at": (sat - timedelta(minutes=5)).isoformat()}
    assert "limit_close" not in due_tasks(sat, trading=False, state=just_ran, flags=flags)
    cooled = {"limit_close_at": (sat - timedelta(minutes=45)).isoformat()}
    assert "limit_close" in due_tasks(sat, trading=False, state=cooled, flags=flags)
