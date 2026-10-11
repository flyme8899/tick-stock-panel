"""第二批东财数据：解析、保留上次结果、增量、超时不重开线程。不出网。"""
from __future__ import annotations

import json
import threading
from datetime import datetime

from app.event_context import jobs, parse, query, scheduler, store
from app.event_context.scheduler import EventWorker
from app.market_time import CN_TZ

DAY = "2026-10-09"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _dc(rows, *, pages=1, code=None):
    if code is not None:
        return _Resp({"success": False, "code": code, "message": "返回数据为空"})
    return _Resp({"success": True, "result": {"pages": pages, "data": rows}})


def test_money_stays_in_yuan_and_suffixes_convert() -> None:
    assert parse.money_yuan(7149757278) == 7149757278
    assert parse.money_yuan("1.5亿") == 1.5e8
    assert parse.money_yuan("20万") == 200000
    assert parse.market_of("000001", "深交所") == "szse"
    assert parse.market_of("600519", "") == "sse"


def test_parsers_cover_lhb_margin_qa_notice_and_changes() -> None:
    summary = parse.parse_lhb_summary([{
        "SECURITY_CODE": "600519",
        "SECURITY_NAME_ABBR": "贵州茅台",
        "TRADE_DATE": "2026-10-09 00:00:00",
        "BILLBOARD_NET_AMT": 120000000,
        "CHANGE_RATE": 3.2,
        "EXPLANATION": "日涨幅偏离",
    }])
    assert summary[0]["net_buy"] == 120000000
    seats = parse.parse_lhb_seats([{
        "SECURITY_CODE": "600519",
        "TRADE_DATE": DAY,
        "OPERATEDEPT_NAME": "中信证券上海分公司",
        "NET": 80000000,
    }], side="buy")
    assert seats[0]["broker"] == "中信证券上海分公司"
    margin = parse.parse_margin_detail([{
        "SCODE": "600519",
        "SECNAME": "贵州茅台",
        "TRADE_DATE": DAY,
        "TRADE_MARKET": "上交所",
        "RZYE": 7149757278,
        "RQYE": 1000,
        "RZMRE": 2000,
    }])
    assert margin[0]["margin_balance"] == 7149757278
    assert margin[0]["market"] == "sse"
    qa = parse.parse_qa_payload(
        'jQuery({"result":{"wenDongMiWeb":[{"questionId":"q1","securityCode":"600519",'
        '"questionContent":"分红吗","answerContent":"已披露","questionTime":"2026-10-09 10:00:00"}]}})',
        code="600519",
    )
    assert qa[0]["question_id"] == "q1"
    notices = parse.parse_notices({"data": {"list": [{
        "art_code": "AN1",
        "title": "关于回购的公告",
        "notice_date": "2026-10-09 00:00:00",
        "columns": [{"column_name": "回购"}],
        "codes": [{"stock_code": "600519", "short_name": "贵州茅台"}],
    }]}})
    assert notices[0]["url"].endswith("/600519/AN1.html")
    changes = parse.parse_changes(
        {"data": {"allstock": [{"c": "000001", "n": "平安银行", "tm": 93501, "i": "1000"}]}},
        kind="大笔买入",
        trade_date=DAY,
    )
    assert changes[0]["at"] == "09:35:01"
    assert changes[0]["kind"] == "大笔买入"


def test_empty_lhb_keeps_the_previous_partition(tmp_path) -> None:
    store.write_rows(tmp_path, "lhb", DAY, [{
        "code": "600519",
        "name": "贵州茅台",
        "trade_date": DAY,
        "net_buy": 10,
        "broker": "旧营业部",
        "side": "buy",
        "source": "eastmoney_lhb",
    }])

    def get(url, **_kwargs):
        assert "eastmoney.com" in url
        assert _kwargs["timeout"] == (10.0, 30.0)
        return _dc([], code=9201)

    result = jobs.sync_lhb(tmp_path, DAY, get=get)
    assert result["kept"] is True
    frame = store.read_partition(tmp_path, "lhb", DAY)
    assert frame.height == 1
    assert frame["broker"][0] == "旧营业部"


def test_lhb_keeps_net_buy_when_seats_are_empty(tmp_path) -> None:
    calls = {"n": 0}

    def get(url, *, params, headers=None, timeout=None):
        calls["n"] += 1
        assert timeout == (10.0, 30.0)
        if params["reportName"] == "RPT_DAILYBILLBOARD_DETAILSNEW":
            return _dc([{
                "SECURITY_CODE": "600519",
                "SECURITY_NAME_ABBR": "贵州茅台",
                "TRADE_DATE": DAY,
                "BILLBOARD_NET_AMT": 99,
                "CHANGE_RATE": 1,
                "EXPLANATION": "偏离",
            }])
        return _dc([], code=9201)

    result = jobs.sync_lhb(tmp_path, DAY, get=get)
    assert result["ok"] is True
    assert "营业部" in result["note"]
    row = store.read_partition(tmp_path, "lhb", DAY).to_dicts()[0]
    assert row["net_buy"] == 99
    assert row["broker"] == ""


def test_margin_sz_gap_keeps_previous_balances(tmp_path) -> None:
    store.write_rows(tmp_path, "margin", "2026-10-08", [{
        "code": "000001",
        "name": "平安银行",
        "trade_date": "2026-10-08",
        "market": "szse",
        "margin_balance": 111,
        "short_balance": 2,
        "margin_buy": 3,
        "source": "eastmoney_margin",
    }])

    def get(url, *, params, headers=None, timeout=None):
        assert "datacenter" in url
        assert timeout == (10.0, 30.0)
        return _dc([{
            "SCODE": "600519",
            "SECNAME": "贵州茅台",
            "TRADE_DATE": DAY,
            "TRADE_MARKET": "上交所",
            "RZYE": 7149757278,
            "RQYE": 10,
            "RZMRE": 20,
        }])

    result = jobs.sync_margin(tmp_path, DAY, get=get)
    assert result["ok"] is True
    today = store.read_partition(tmp_path, "margin", DAY).to_dicts()
    assert [row["code"] for row in today] == ["600519"]
    assert today[0]["margin_balance"] == 7149757278
    previous = store.read_partition(tmp_path, "margin", "2026-10-08").to_dicts()[0]
    assert previous["margin_balance"] == 111
    note = store.load_state(tmp_path)["margin"]["note"]
    assert "深市明细不可用" in note
    assert "2026-10-08" in note
    text = " ".join(query.context_lines(tmp_path, "000001"))
    assert "111" in text or "元" in text
    assert "沿用" in text


def test_qa_is_incremental_and_limited_to_given_symbols(tmp_path) -> None:
    asked: list[str] = []

    def get(url, *, params, headers=None, timeout=None):
        assert "search-api-web.eastmoney.com" in url
        body = json.loads(params["param"])
        asked.append(body["keyword"])
        payload = {
            "result": {
                "wenDongMiWeb": [
                    {
                        "questionId": "new",
                        "securityCode": body["keyword"],
                        "questionContent": "新问题",
                        "questionTime": "2026-10-09 11:00:00",
                    },
                    {
                        "questionId": "old",
                        "securityCode": body["keyword"],
                        "questionContent": "旧问题",
                        "questionTime": "2026-10-08 11:00:00",
                    },
                ],
            },
        }
        return json.dumps(payload, ensure_ascii=False)

    first = jobs.sync_qa(tmp_path, get=get, symbols=["600519.SH", "000001.SZ", "300750.SZ"], batch=1)
    assert first["symbols"] == ["600519"]
    assert asked == ["600519"]
    second = jobs.sync_qa(tmp_path, get=get, symbols=["600519.SH", "000001.SZ", "300750.SZ"], batch=1)
    assert second["symbols"] == ["000001"]
    rows = store.read_partition(tmp_path, "qa", DAY).to_dicts()
    assert {row["question_id"] for row in rows} == {"new"}
    again = jobs.sync_qa(tmp_path, get=get, symbols=["600519.SH"], batch=1)
    assert again["rows"] == 0


def test_notice_is_incremental_and_ingested(tmp_path, monkeypatch) -> None:
    ingested: list[str] = []

    def get(url, *, params, headers=None, timeout=None):
        assert "np-anotice-stock.eastmoney.com" in url
        assert params["f_node"] == "0"
        return _Resp({"data": {"list": [{
            "art_code": "AN1",
            "title": "回购公告",
            "notice_date": f"{DAY} 00:00:00",
            "columns": [{"column_name": "回购"}],
            "codes": [{"stock_code": "600519", "short_name": "贵州茅台"}],
        }]}})

    def ingest(items):
        ingested.extend(item.source_id for item in items)
        return {"inserted": len(items)}

    monkeypatch.setattr("app.news.service.ingest_items", ingest)
    monkeypatch.setattr("app.news.service.get_store", lambda: type("Store", (), {
        "mark_health": lambda *args, **kwargs: None,
    })())
    first = jobs.sync_notice(tmp_path, get=get)
    assert first["rows"] == 1
    assert ingested == ["AN1"]
    second = jobs.sync_notice(tmp_path, get=get)
    assert second["rows"] == 0
    assert store.read_partition(tmp_path, "notice", DAY).height == 1


def test_changes_merge_is_idempotent(tmp_path) -> None:
    def get(url, *, params, headers=None, timeout=None):
        assert "push2ex.eastmoney.com" in url
        kind = params["type"]
        return _Resp({"data": {"allstock": [{"c": "600519", "n": "贵州茅台", "tm": 93501, "i": kind}]}})

    assert jobs.sync_changes(tmp_path, DAY, get=get)["ok"] is True
    assert jobs.sync_changes(tmp_path, DAY, get=get)["rows"] == 5
    frame = store.read_partition(tmp_path, "changes", DAY)
    assert frame.height == 5
    assert set(frame["kind"].to_list()) == {"大笔买入", "大笔卖出", "封涨停板", "封跌停板", "火箭发射"}


def test_live_get_goes_through_em_get_with_timeout(monkeypatch, tmp_path) -> None:
    seen = {}

    def fake(url, *, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["timeout"] = timeout
        return _dc([], code=9201)

    monkeypatch.setattr("app.eastmoney.client.em_get", fake)
    jobs.sync_lhb(tmp_path, DAY)
    assert "eastmoney.com" in seen["url"]
    assert seen["timeout"] == (10.0, 30.0)


def test_order_change_lifts_an_unconfirmed_event() -> None:
    from app.news.market_confirm import score_confirmation

    event = {
        "headline": "回购",
        "direction": "利好",
        "_first": datetime(2026, 10, 9, 9, 20, tzinfo=CN_TZ),
        "mentioned_stocks": [{"key": "000001.SZ", "name": "平安银行"}],
        "concepts": [],
    }
    tape = {
        "session": DAY,
        "live": False,
        "members": {},
        "index_bars": [],
        "stocks": {},
        "concepts": {},
        "changes": {"000001": [{"kind": "大笔买入", "at": "09:36:00"}]},
    }
    now = datetime(2026, 10, 9, 15, 30, tzinfo=CN_TZ)
    scored = score_confirmation(event, tape, now)
    assert scored["abnormal"] is True
    assert scored["strength"] == "弱"
    assert scored["score"] >= 6


def test_intraday_due_is_changes_then_notice() -> None:
    now = datetime(2026, 10, 9, 10, 0, tzinfo=CN_TZ)
    flags = {"enabled": True, "changes": True, "notice": True, "lhb": True, "margin": True, "qa": True}
    due = scheduler.due_tasks(now, trading=True, schedule={}, flags=flags)
    assert due[0] == "changes"
    assert "lhb" not in due


def test_timed_out_task_does_not_start_another(monkeypatch, tmp_path) -> None:
    for name in (
        "EVENT_CONTEXT_ENABLED",
        "EVENT_CONTEXT_LHB",
        "EVENT_CONTEXT_MARGIN",
        "EVENT_CONTEXT_QA",
        "EVENT_CONTEXT_NOTICE",
        "EVENT_CONTEXT_CHANGES",
    ):
        monkeypatch.setenv(name, "true")
    monkeypatch.setattr("app.fund_flow.scheduler.tickflow_sync_busy", lambda: False)
    monkeypatch.setattr("app.event_context.timeouts.DEFAULT_TASK_TIMEOUT_S", 0.4)
    blocked = threading.Event()
    calls: list[str] = []

    def get(url, **_kwargs):
        calls.append(url)
        blocked.wait(5)
        return _dc([], code=9201)

    now = datetime(2026, 10, 10, 18, 0, tzinfo=CN_TZ)
    worker = EventWorker(tmp_path, get=get)
    assert worker.run_once(now) == "lhb"
    assert len(calls) == 1
    saved = store.load_state(tmp_path)["schedule"]
    assert saved.get("lhb_date") != "2026-10-09"
    assert "超时" in saved.get("last_error", "")
    assert worker.run_once(now) is None
    assert len(calls) == 1
    blocked.set()
