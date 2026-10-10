"""把各源的原始行收成内部字段。金额单位在这里一次换完，上层只见元或份。"""
from __future__ import annotations

from app.fund_flow.units import (
    BARE_WAN_SHARES,
    BARE_YI,
    BARE_YUAN,
    money_to_yuan,
    percent_points_to_decimal,
    shares_to_count,
)


def _points(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:
            return None
        return float(value)
    text = str(value).strip().replace("%", "").replace("％", "")
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def code6(symbol: str) -> str:
    digits = "".join(ch for ch in str(symbol) if ch.isdigit())
    if len(digits) < 6:
        return ""
    return digits[-6:]


def _text(row: dict, *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"nan", "none", "null"}:
            return text
    return ""


def _date(value: str) -> str:
    text = value.strip().replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def normalize_bills(rows: list[dict], *, symbol: str, source: str) -> list[dict]:
    """个股主力/大单/超大单。efinance 与妙想的裸金额都是元。"""
    code = code6(symbol)
    stored_symbol = symbol if "." in symbol else code
    out = []
    for row in rows:
        trade_date = _date(_text(row, "trade_date", "日期", "_date"))
        if not trade_date:
            continue
        main = row.get("main_net")
        if main is None:
            main = money_to_yuan(row.get("主力净流入"), bare=BARE_YUAN)
            if main is None:
                main = money_to_yuan(row.get("主力净流入-净额"), bare=BARE_YUAN)
        else:
            main = money_to_yuan(main, bare=BARE_YUAN)
        large = row.get("large_net")
        if large is None:
            large = money_to_yuan(row.get("大单净流入"), bare=BARE_YUAN)
            if large is None:
                large = money_to_yuan(row.get("大单净流入-净额"), bare=BARE_YUAN)
        else:
            large = money_to_yuan(large, bare=BARE_YUAN)
        super_net = row.get("super_net")
        if super_net is None:
            super_net = money_to_yuan(row.get("超大单净流入"), bare=BARE_YUAN)
            if super_net is None:
                super_net = money_to_yuan(row.get("超大单净流入-净额"), bare=BARE_YUAN)
        else:
            super_net = money_to_yuan(super_net, bare=BARE_YUAN)
        if main is None and large is None and super_net is None:
            continue
        out.append({
            "symbol": stored_symbol,
            "code": code,
            "trade_date": trade_date,
            "main_net": main,
            "large_net": large,
            "super_net": super_net,
            "source": source,
        })
    return out


def normalize_sectors(
    rows: list[dict],
    *,
    trade_date: str,
    captured_at: str,
    snapshot: str,
) -> list[dict]:
    """同花顺行业/概念。``净额`` 裸数字按亿元；带亿/万后缀的按后缀。

    ``net_inflow`` 已是元，不再换算。排名 1 为净流入最大。
    """
    parsed = []
    for row in rows:
        name = _text(row, "name", "行业", "概念", "板块名称")
        if not name or name == "序号":
            continue
        if "net_inflow" in row and row.get("net_inflow") is not None:
            net = money_to_yuan(row.get("net_inflow"), bare=BARE_YUAN)
        else:
            net = money_to_yuan(row.get("净额"), bare=BARE_YI)
            if net is None:
                net = money_to_yuan(row.get("资金流入净额"), bare=BARE_YI)
        if net is None:
            continue
        parsed.append({"name": name, "net_inflow": net})
    parsed.sort(key=lambda item: item["net_inflow"], reverse=True)
    return [
        {
            "name": item["name"],
            "trade_date": trade_date,
            "captured_at": captured_at,
            "snapshot": snapshot,
            "net_inflow": item["net_inflow"],
            "rank": index + 1,
            "source": "akshare_ths",
        }
        for index, item in enumerate(parsed)
    ]


def normalize_margin_summary(rows: list[dict], *, market: str) -> list[dict]:
    out = []
    for row in rows:
        trade_date = _date(_text(row, "trade_date", "信用交易日期", "日期"))
        if not trade_date:
            continue
        out.append({
            "market": market,
            "symbol": "",
            "name": "",
            "trade_date": trade_date,
            "row_kind": "summary",
            "margin_balance": money_to_yuan(
                row.get("margin_balance", row.get("融资余额")), bare=BARE_YUAN,
            ),
            "short_balance": money_to_yuan(
                row.get("short_balance", row.get("融券余量金额", row.get("融券余额"))),
                bare=BARE_YUAN,
            ),
            "margin_buy": money_to_yuan(
                row.get("margin_buy", row.get("融资买入额")), bare=BARE_YUAN,
            ),
            "source": "akshare",
        })
    return out


def normalize_margin_detail(rows: list[dict], *, market: str, trade_date: str) -> list[dict]:
    out = []
    for row in rows:
        symbol = _text(row, "symbol", "标的证券代码", "证券代码", "股票代码")
        code = code6(symbol)
        day = _date(_text(row, "trade_date", "信用交易日期", "日期")) or trade_date
        if not code or not day:
            continue
        out.append({
            "market": market,
            "symbol": code,
            "name": _text(row, "name", "标的证券简称", "证券简称", "股票简称"),
            "trade_date": day,
            "row_kind": "detail",
            "margin_balance": money_to_yuan(
                row.get("margin_balance", row.get("融资余额")), bare=BARE_YUAN,
            ),
            "short_balance": money_to_yuan(
                row.get("short_balance", row.get("融券余额", row.get("融券余量金额"))),
                bare=BARE_YUAN,
            ),
            "margin_buy": money_to_yuan(
                row.get("margin_buy", row.get("融资买入额")), bare=BARE_YUAN,
            ),
            "source": "akshare",
        })
    return out


def normalize_etf_shares(rows: list[dict]) -> list[dict]:
    """上交所基金份额。裸数字是万份，入库为份。"""
    out = []
    for row in rows:
        code = code6(_text(row, "code", "基金代码", "symbol"))
        trade_date = _date(_text(row, "trade_date", "统计日期", "日期"))
        if not code or not trade_date:
            continue
        if "shares" in row and row.get("shares") is not None:
            shares = shares_to_count(row.get("shares"), bare=BARE_YUAN)
        else:
            shares = shares_to_count(row.get("基金份额"), bare=BARE_WAN_SHARES)
        if shares is None:
            continue
        out.append({
            "symbol": code,
            "code": code,
            "name": _text(row, "name", "基金简称"),
            "trade_date": trade_date,
            "shares": shares,
            "source": "akshare",
        })
    return out


def normalize_southbound(rows: list[dict]) -> list[dict]:
    """南向净流入。``stock_hsgt_hist_em`` 的裸 ``当日成交净买额`` 是亿元。"""
    out = []
    for row in rows:
        trade_date = _date(_text(row, "trade_date", "日期"))
        if not trade_date:
            continue
        if "net_flow" in row and row.get("net_flow") is not None:
            net = money_to_yuan(row.get("net_flow"), bare=BARE_YUAN)
        else:
            net = money_to_yuan(row.get("当日成交净买额"), bare=BARE_YI)
        if net is None:
            continue
        out.append({"trade_date": trade_date, "net_flow": net, "source": "akshare"})
    return out


def normalize_northbound_turnover(rows: list[dict]) -> list[dict]:
    """只留成交额。净流入字段即使出现在原始行里也不写入。"""
    out = []
    for row in rows:
        trade_date = _date(_text(row, "trade_date", "日期"))
        if not trade_date:
            continue
        if "turnover" in row and row.get("turnover") is not None:
            turnover = money_to_yuan(row.get("turnover"), bare=BARE_YUAN)
        else:
            buy = money_to_yuan(row.get("买入成交额"), bare=BARE_YI)
            sell = money_to_yuan(row.get("卖出成交额"), bare=BARE_YI)
            if buy is None and sell is None:
                turnover = money_to_yuan(row.get("成交额"), bare=BARE_YI)
            else:
                turnover = (buy or 0.0) + (sell or 0.0)
        if turnover is None:
            continue
        out.append({
            "trade_date": trade_date,
            "turnover": turnover,
            "source": "akshare",
        })
    return out


def normalize_lhb(rows: list[dict]) -> list[dict]:
    """东财龙虎榜备份。净买额是元，涨跌幅是百分点。"""
    out = []
    for row in rows:
        code = code6(_text(row, "code", "代码", "symbol"))
        trade_date = _date(_text(row, "trade_date", "上榜日", "日期"))
        if not code or not trade_date:
            continue
        if "net_buy" in row and row.get("net_buy") is not None:
            net = money_to_yuan(row.get("net_buy"), bare=BARE_YUAN)
        else:
            net = money_to_yuan(row.get("龙虎榜净买额"), bare=BARE_YUAN)
        points = row.get("change_pct_points", row.get("涨跌幅"))
        out.append({
            "symbol": code,
            "code": code,
            "name": _text(row, "name", "名称"),
            "trade_date": trade_date,
            "net_buy": net,
            "change_pct_points": _points(points),
            "source": "akshare",
        })
    return out


def lhb_change_decimal(points: object) -> float | None:
    return percent_points_to_decimal(points)
