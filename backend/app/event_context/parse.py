"""东财响应归一。金额按元保存，带「万」「亿」的文本才换算。"""
from __future__ import annotations

import json
import re

_DATE = re.compile(r"(\d{4})\D?(\d{2})\D?(\d{2})")
_CODE = re.compile(r"(\d{6})")


def code6(raw: object) -> str:
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    head = text.split(".", 1)[0]
    match = _CODE.search(head)
    if match is None:
        match = _CODE.search(text)
    return match.group(1) if match else ""


def day_text(raw: object) -> str:
    text = str(raw or "").strip()
    match = _DATE.search(text)
    if match is None:
        return ""
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def stamp_text(raw: object) -> str:
    text = str(raw or "").strip().replace("/", "-")
    if len(text) >= 19 and text[4] == "-" and text[13] == ":":
        return text[:19]
    day = day_text(text)
    return day


def money_yuan(raw: object) -> float | None:
    """裸数字是元。后缀「万」「亿」按字面换算。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", "").replace("，", "").replace(" ", "")
    if not text or text in {"-", "--", "None", "null"}:
        return None
    factor = 1.0
    if text.endswith("亿元"):
        factor = 1e8
        text = text[:-2]
    elif text.endswith("亿"):
        factor = 1e8
        text = text[:-1]
    elif text.endswith("万元"):
        factor = 1e4
        text = text[:-2]
    elif text.endswith("万"):
        factor = 1e4
        text = text[:-1]
    elif text.endswith("元"):
        text = text[:-1]
    try:
        return float(text) * factor
    except ValueError:
        return None


def market_of(code: str, trade_market: object = "") -> str:
    text = str(trade_market or "")
    if "深" in text:
        return "szse"
    if "上" in text or "沪" in text:
        return "sse"
    if "北" in text:
        return "bj"
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "szse"
    if code.startswith(("6", "688", "689")):
        return "sse"
    if code.startswith(("8", "4", "92")):
        return "bj"
    return ""


def parse_lhb_summary(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = code6(_pick(row, "SECURITY_CODE", "SECURITYCODE", "代码"))
        day = day_text(_pick(row, "TRADE_DATE", "交易日"))
        if not code or not day:
            continue
        out.append({
            "code": code,
            "name": str(_pick(row, "SECURITY_NAME_ABBR", "SECURITYNAME", "名称") or ""),
            "trade_date": day,
            "net_buy": money_yuan(_pick(row, "BILLBOARD_NET_AMT", "净买额")),
            "change_pct_points": money_yuan(_pick(row, "CHANGE_RATE", "涨跌幅")),
            "reason": str(_pick(row, "EXPLANATION", "上榜原因") or ""),
            "broker": "",
            "broker_net": None,
            "side": "",
            "source": "eastmoney_lhb",
        })
    return out


def parse_lhb_seats(rows: list[dict], *, side: str) -> list[dict]:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = code6(_pick(row, "SECURITY_CODE", "SECURITYCODE", "代码"))
        day = day_text(_pick(row, "TRADE_DATE", "交易日"))
        broker = str(_pick(row, "OPERATEDEPT_NAME", "营业部名称") or "").strip()
        if not code or not day or not broker:
            continue
        net = money_yuan(_pick(row, "NET", "净额"))
        if net is None:
            buy = money_yuan(_pick(row, "BUY", "买入金额"))
            sell = money_yuan(_pick(row, "SELL", "卖出金额"))
            if buy is not None or sell is not None:
                net = (buy or 0.0) - (sell or 0.0)
        out.append({
            "code": code,
            "name": str(_pick(row, "SECURITY_NAME_ABBR", "SECURITYNAME", "名称") or ""),
            "trade_date": day,
            "net_buy": None,
            "change_pct_points": None,
            "reason": str(_pick(row, "EXPLANATION", "上榜原因") or ""),
            "broker": broker,
            "broker_net": net,
            "side": side,
            "source": "eastmoney_lhb",
        })
    return out


def parse_margin_detail(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = code6(_pick(row, "SCODE", "SECURITY_CODE", "SECUCODE", "代码"))
        day = day_text(_pick(row, "TRADE_DATE", "DATE", "交易日"))
        if not code or not day:
            continue
        out.append({
            "code": code,
            "name": str(_pick(row, "SECNAME", "SECURITY_NAME_ABBR", "名称") or ""),
            "trade_date": day,
            "market": market_of(code, _pick(row, "TRADE_MARKET", "市场")),
            "margin_balance": money_yuan(_pick(row, "RZYE", "融资余额")),
            "short_balance": money_yuan(_pick(row, "RQYE", "融券余额")),
            "margin_buy": money_yuan(_pick(row, "RZMRE", "融资买入额")),
            "source": "eastmoney_margin",
        })
    return out


def parse_qa_payload(payload: object, *, code: str = "") -> list[dict]:
    data = as_data(payload)
    if not isinstance(data, dict):
        return []
    wanted = code6(code)
    out = []
    for row in _qa_rows(data):
        if not isinstance(row, dict):
            continue
        qid = str(_pick(row, "questionId", "question_id", "post_id", "id") or "").strip()
        question = str(_pick(row, "questionContent", "question", "title", "askContent") or "").strip()
        if not qid or not question:
            continue
        item_code = code6(_pick(row, "securityCode", "code", "stock_code", "SCODE")) or wanted
        if wanted and item_code and item_code != wanted:
            continue
        out.append({
            "question_id": qid,
            "code": item_code,
            "name": str(_pick(row, "securityShortName", "shortName", "name") or ""),
            "published_at": stamp_text(_pick(
                row, "questionTime", "showTime", "publishTime", "noticeDate", "time",
            )),
            "question": question,
            "answer": str(_pick(row, "answerContent", "answer", "content", "reply") or ""),
            "source": "eastmoney_qa",
        })
    return out


def parse_notices(payload: object) -> list[dict]:
    data = as_data(payload)
    rows = _notice_rows(data)
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        art = str(row.get("art_code") or row.get("artCode") or "").strip()
        if not art:
            continue
        code, name = _notice_stock(row)
        title = str(row.get("title") or row.get("notice_title") or "").strip()
        if not title:
            continue
        day = day_text(row.get("notice_date") or row.get("noticeDate"))
        column = _notice_column(row)
        url = ""
        if code:
            url = f"https://data.eastmoney.com/notices/detail/{code}/{art}.html"
        out.append({
            "art_code": art,
            "code": code,
            "name": name,
            "title": title,
            "column_name": column,
            "notice_date": day,
            "url": url,
            "source": "eastmoney_notice",
        })
    return out


def parse_changes(payload: object, *, kind: str, trade_date: str) -> list[dict]:
    data = as_data(payload)
    stocks = []
    if isinstance(data, dict):
        block = data.get("data") if isinstance(data.get("data"), dict) else data
        raw = block.get("allstock") if isinstance(block, dict) else None
        if isinstance(raw, list):
            stocks = raw
    out = []
    for row in stocks:
        if not isinstance(row, dict):
            continue
        code = code6(row.get("c") or row.get("code"))
        if not code:
            continue
        out.append({
            "code": code,
            "name": str(row.get("n") or row.get("name") or ""),
            "trade_date": trade_date,
            "kind": kind,
            "at": _clock(row.get("tm") if row.get("tm") is not None else row.get("at")),
            "info": str(row.get("i") or row.get("info") or ""),
            "source": "eastmoney_changes",
        })
    return out


def as_data(payload: object) -> object:
    if isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, str):
        return _unwrap(payload)
    reader = getattr(payload, "json", None)
    if callable(reader):
        try:
            value = reader()
        except Exception:  # noqa: BLE001
            value = None
        if isinstance(value, (dict, list)):
            return value
    text = getattr(payload, "text", "")
    if isinstance(text, str) and text.strip():
        return _unwrap(text)
    raise RuntimeError("响应无法解析")


def _unwrap(text: str) -> object:
    raw = text.strip().lstrip("\ufeff")
    if raw[:1] in "{[":
        return json.loads(raw)
    start = raw.find("(")
    end = raw.rfind(")")
    if start >= 0 and end > start:
        return json.loads(raw[start + 1:end])
    raise RuntimeError("响应无法解析")


def _pick(row: dict, *names: str) -> object:
    for name in names:
        if name in row and row.get(name) not in (None, ""):
            return row.get(name)
    return None


def _qa_rows(data: dict) -> list:
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    block = result.get("wenDongMiWeb") if isinstance(result, dict) else None
    if isinstance(block, list):
        return block
    if isinstance(block, dict):
        for key in ("data", "list", "result"):
            if isinstance(block.get(key), list):
                return block[key]
    found: list = []
    _walk_qa(data, found)
    return found


def _walk_qa(node: object, found: list) -> None:
    if isinstance(node, list):
        if node and all(isinstance(item, dict) and _pick(item, "questionId", "questionContent", "question") for item in node):
            found.extend(node)
            return
        for item in node:
            _walk_qa(item, found)
        return
    if isinstance(node, dict):
        for value in node.values():
            _walk_qa(value, found)


def _notice_rows(data: object) -> list:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    block = data.get("data") if isinstance(data.get("data"), dict) else data
    if isinstance(block, dict):
        for key in ("list", "data"):
            if isinstance(block.get(key), list):
                return block[key]
    return []


def _notice_stock(row: dict) -> tuple[str, str]:
    codes = row.get("codes")
    if isinstance(codes, list) and codes and isinstance(codes[0], dict):
        item = codes[0]
        return code6(item.get("stock_code") or item.get("code")), str(item.get("short_name") or "")
    return code6(row.get("stock_code") or row.get("code")), str(row.get("short_name") or row.get("name") or "")


def _notice_column(row: dict) -> str:
    columns = row.get("columns")
    if isinstance(columns, list) and columns and isinstance(columns[0], dict):
        return str(columns[0].get("column_name") or "")
    return str(row.get("column_name") or "")


def _clock(raw: object) -> str:
    text = str(raw or "").strip()
    if ":" in text:
        return text[:8]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return ""
    digits = digits.zfill(6)[-6:]
    return f"{digits[:2]}:{digits[2:4]}:{digits[4:6]}"
