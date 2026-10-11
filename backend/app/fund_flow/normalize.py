"""把各源的原始行收成内部字段。金额单位在这里一次换完，上层只见元或份。"""
from __future__ import annotations

import math

from app.fund_flow.units import (
    BARE_SHARES,
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


def normalize_rank_rows(
    rows: list[dict],
    *,
    trade_date: str,
    source: str,
    window: str,
) -> list[dict]:
    """全市场排名收成当日主力，或 5 日主力。两边不会写进同一个金额字段。

    东财排名的净额是元。同花顺单元格带「亿」「万」时按后缀；没有后缀时也按元。
    """
    if window not in {"today", "5d"}:
        raise ValueError("window 只能是 today 或 5d")
    out = []
    for row in rows:
        code = code6(_text(row, "code", "股票代码", "代码"))
        if not code:
            continue
        name = _text(row, "name", "股票简称", "名称")
        if window == "today":
            main = _first_money(row, "main_net", "净额", "今日主力净流入-净额")
            large = _first_money(row, "large_net", "今日大单净流入-净额")
            super_net = _first_money(row, "super_net", "今日超大单净流入-净额")
            five = None
        else:
            main = large = super_net = None
            five = _first_money(row, "main_net_5d", "资金流入净额", "5日主力净流入-净额")
        if main is None and large is None and super_net is None and five is None:
            continue
        out.append({
            "symbol": code,
            "code": code,
            "name": name or None,
            "trade_date": trade_date,
            "main_net": main,
            "large_net": large,
            "super_net": super_net,
            "main_net_5d": five,
            "source": source,
        })
    return out


def _first_money(row: dict, *names: str) -> float | None:
    for name in names:
        if name not in row:
            continue
        amount = money_to_yuan(row.get(name), bare=BARE_YUAN)
        if amount is not None:
            return amount
    return None


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


def normalize_margin_summary(
    rows: list[dict],
    *,
    market: str,
    trade_date: str | None = None,
) -> list[dict]:
    """两融汇总。上交所自带信用交易日期。深交所汇总没有日期列，用调用方传入的交易日。

    akshare 1.19.1 的 ``stock_margin_sse`` / ``stock_margin_szse`` 都只做
    ``to_numeric``，不再乘单位。上交所融资余额是元（2026-10-09 实盘约 1.29e12）。
    深交所汇总目录是亿元（例如 12380.27），这里按市场换成元。明细行仍是元，不在这里乘。
    """
    bare = BARE_YI if market == "szse" else BARE_YUAN
    out = []
    for row in rows:
        day = _date(_text(row, "trade_date", "信用交易日期", "日期")) or (trade_date or "")
        if not day:
            continue
        out.append({
            "market": market,
            "symbol": "",
            "name": "",
            "trade_date": day,
            "row_kind": "summary",
            "margin_balance": money_to_yuan(
                row.get("margin_balance", row.get("融资余额")), bare=bare,
            ),
            "short_balance": money_to_yuan(
                row.get("short_balance", row.get("融券余量金额", row.get("融券余额"))),
                bare=bare,
            ),
            "margin_buy": money_to_yuan(
                row.get("margin_buy", row.get("融资买入额")), bare=bare,
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


def _fund_code(value: object) -> str:
    """基金代码收成 6 位。浮点 ``159915.0`` 不能先变成字符串再抽数字。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return f"{value:06d}" if 0 <= value <= 999999 else code6(str(value))
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or value != int(value):
            return ""
        return f"{int(value):06d}" if value <= 999999 else code6(str(int(value)))
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return code6(text)


def _etf_share_value(row: dict) -> object:
    if "shares" in row and row.get("shares") is not None:
        return row.get("shares")
    if row.get("基金份额") is not None:
        return row.get("基金份额")
    return row.get("基金规模(份)")


def normalize_etf_shares(rows: list[dict]) -> list[dict]:
    """沪深 ETF 份额。裸数字已是份，入库不再乘 10000。

    上交所 ``基金份额`` 经 akshare 把万份乘过 10000。深交所官方报表列名是
    「基金规模(份)」，akshare 改名为 ``基金份额`` 后同样是份。带「万」「亿」
    的文本仍按后缀换。
    """
    out = []
    for row in rows:
        code = _fund_code(row.get("code", row.get("基金代码", row.get("symbol"))))
        trade_date = _date(_text(row, "trade_date", "统计日期", "日期", "stat_date"))
        if not code or not trade_date:
            continue
        shares = shares_to_count(_etf_share_value(row), bare=BARE_SHARES)
        if shares is None:
            continue
        source = _text(row, "source") or "akshare"
        out.append({
            "symbol": code,
            "code": code,
            "name": _text(row, "name", "基金简称"),
            "trade_date": trade_date,
            "shares": shares,
            "source": source,
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
