"""把 akshare 表格收成选股用的字段。

百分比保持上游给的百分数：``2.35`` 表示 2.35%，不按数值大小再乘 100。
北向「占流通股比」是百分数；增减比例里带「‰」的列这里不用。
"""
from __future__ import annotations

from datetime import date, datetime

# 展示顺序。一只股票可以同时在多个指数里。
INDEXES: tuple[tuple[str, str], ...] = (
    ("000300", "沪深300"),
    ("000016", "上证50"),
    ("000905", "中证500"),
    ("000852", "中证1000"),
    ("399673", "创业板50"),
)
INDEX_LABELS = {code: label for code, label in INDEXES}

GAP_NORTHBOUND = "北向持股"
GAP_HOLDERS = "股东户数"
GAP_INDEX = "指数成分"
GAP_UNLOCK = "解禁时间"
GAP_PE = "PE(TTM)"
GAP_PERIOD = "周线、月线、连涨天数"

GAP_KEYS: dict[str, tuple[str, ...]] = {
    GAP_NORTHBOUND: ("northbound_hold_pct",),
    GAP_HOLDERS: ("holder_num", "holder_change_pct"),
    GAP_INDEX: ("index_membership",),
    GAP_UNLOCK: ("next_unlock_date", "unlock_float_ratio"),
    GAP_PE: ("pe_ttm",),
    GAP_PERIOD: (
        "weekly_change_pct",
        "monthly_change_pct",
        "up_streak",
        "down_streak",
        "macd_dif_w",
        "macd_dea_w",
        "macd_hist_w",
        "kdj_k_w",
        "kdj_d_w",
        "kdj_j_w",
        "macd_dif_m",
        "macd_dea_m",
        "macd_hist_m",
        "kdj_k_m",
        "kdj_d_m",
        "kdj_j_m",
    ),
}


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number:
            return None
        return number
    text = str(value).strip().replace(",", "").replace("%", "").replace("％", "")
    if not text or text.lower() in {"-", "--", "nan", "none", "null"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number:
        return None
    return number


def _text(row: dict, *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"nan", "none", "null"}:
            return text
    return ""


def _date_text(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip().replace("/", "-")
    if text.lower() in {"", "nan", "none", "null", "nat"}:
        return ""
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def ashare_symbol(raw: object) -> str:
    """``600519`` / ``600519.SH`` → ``600519.SH``。指数代码和空值返回空串。"""
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if text.endswith((".SH", ".SZ", ".BJ")):
        code = text.split(".", 1)[0]
        suffix = text.split(".", 1)[1]
    else:
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) < 6:
            return ""
        code = digits[-6:]
        if code.startswith(("8", "4")) or code.startswith("92"):
            suffix = "BJ"
        elif code.startswith(("5", "6", "9")):
            suffix = "SH"
        else:
            suffix = "SZ"
    if len(code) != 6 or not code.isdigit():
        return ""
    if suffix == "SH" and not code.startswith("6"):
        return ""
    if suffix == "SZ" and (not code.startswith(("0", "3")) or code.startswith("399")):
        return ""
    if suffix == "BJ" and not (code.startswith(("4", "8")) or code.startswith("92")):
        return ""
    if suffix not in {"SH", "SZ", "BJ"}:
        return ""
    return f"{code}.{suffix}"


def code6(symbol: str) -> str:
    text = ashare_symbol(symbol)
    if not text:
        digits = "".join(ch for ch in str(symbol) if ch.isdigit())
        return digits[-6:] if len(digits) >= 6 else ""
    return text.split(".", 1)[0]


def parse_northbound(rows: list[dict]) -> list[dict]:
    """北向持股占流通股比，单位是百分数。"""
    out = []
    for row in rows:
        symbol = ashare_symbol(_text(row, "代码", "股票代码"))
        hold = _num(row.get("今日持股-占流通股比"))
        if not symbol or hold is None:
            continue
        out.append({
            "symbol": symbol,
            "northbound_hold_pct": hold,
            "trade_date": _date_text(row.get("日期")),
        })
    return out


def parse_holders(rows: list[dict]) -> list[dict]:
    """股东户数。增减比例是百分数。"""
    out = []
    for row in rows:
        symbol = ashare_symbol(_text(row, "代码", "股票代码"))
        count = _num(row.get("股东户数-本次"))
        if not symbol or count is None:
            continue
        out.append({
            "symbol": symbol,
            "holder_num": int(count),
            "holder_change_pct": _num(row.get("股东户数-增减比例")),
            "report_date": _date_text(row.get("股东户数统计截止日-本次")),
        })
    return out


def parse_index_members(rows: list[dict], label: str) -> dict[str, str]:
    """一个指数的成分。键是股票代码。"""
    found: dict[str, str] = {}
    for row in rows:
        symbol = ashare_symbol(_text(row, "成分券代码", "品种代码", "代码", "股票代码"))
        if symbol:
            found[symbol] = label
    return found


def combine_index_members(parts: list[tuple[str, dict[str, str]]]) -> list[dict]:
    """按指数表的顺序拼标签。失败的指数不在 parts 里，不会把别的指数清掉。"""
    order = [label for _code, label in INDEXES]
    symbols: set[str] = set()
    for _label, mapping in parts:
        symbols.update(mapping)
    rows = []
    for symbol in sorted(symbols):
        labels = [
            label
            for label in order
            if any(mapping.get(symbol) == label for _name, mapping in parts)
        ]
        if not labels:
            continue
        rows.append({"symbol": symbol, "index_membership": "、".join(labels)})
    return rows


def parse_unlock_schedule(rows: list[dict], *, today: date) -> list[dict]:
    """每只股票保留今天及以后最近的一次解禁。比例是百分数，原样保存。"""
    best: dict[str, tuple[date, float | None]] = {}
    for row in rows:
        symbol = ashare_symbol(_text(row, "股票代码", "代码"))
        day_text = _date_text(row.get("解禁时间"))
        if not symbol or not day_text:
            continue
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            continue
        if day < today:
            continue
        ratio = _num(row.get("占解禁前流通市值比例"))
        current = best.get(symbol)
        if current is None or day < current[0]:
            best[symbol] = (day, ratio)
    return [
        {
            "symbol": symbol,
            "next_unlock_date": day.isoformat(),
            "unlock_float_ratio": ratio,
        }
        for symbol, (day, ratio) in best.items()
    ]


def parse_pe_ttm(rows: list[dict]) -> float | None:
    """取日期最晚的一条非空 PE(TTM)。倍数，不是百分数。"""
    latest: float | None = None
    latest_day = ""
    for row in rows:
        pe = _num(row.get("PE(TTM)"))
        day = _date_text(row.get("数据日期"))
        if pe is None or not day:
            continue
        if latest is None or day >= latest_day:
            latest = pe
            latest_day = day
    return latest
