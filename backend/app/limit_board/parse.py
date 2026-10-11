"""东财四池、情绪汇总、同花顺涨停揭秘的字段解析。

字段和除以 1000 的价格口径移植自 simonlin1212/a-stock-data SKILL.md
§8.1–§8.3（Apache-2.0，Copyright 2026 Simon Lin）。见 ``app/eastmoney/NOTICE``。

改动：封板时间按北京时间格式化，不使用机器本地时区；炸板率存成 0–1 的比例，
不存百分数，避免和页面上的小数涨跌幅混用。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from app.market_time import CN_TZ
from app.news.extract import canonical_symbol, code6_of

_THEME_SPLIT = re.compile(r"[+＋、,，;；|/]+")


def compact_date(trade_date: str) -> str:
    text = str(trade_date).strip()
    if len(text) == 8 and text.isdigit():
        return text
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:4] + text[5:7] + text[8:10]
    raise ValueError(f"交易日格式应为 YYYY-MM-DD: {trade_date}")


def iso_date(compact: str) -> str:
    text = compact_date(compact)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def fmt_zt_time(value) -> str:
    """涨停板时间整数 → HH:MM:SS（92500 → 09:25:00）。"""
    if value is None or value == "":
        return ""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return ""
    text = str(number).zfill(6)[-6:]
    return f"{text[0:2]}:{text[2:4]}:{text[4:6]}"


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _int(value) -> int | None:
    number = _num(value)
    if number is None:
        return None
    return int(number)


def _price(value) -> float | None:
    """东财池子里的价格是 ×1000 的整数。"""
    number = _num(value)
    if number is None:
        return None
    return round(number / 1000.0, 4)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _zt_stat(row: dict) -> str:
    stat = row.get("zttj") or {}
    if not isinstance(stat, dict):
        return ""
    days = stat.get("days", "?")
    count = stat.get("ct", "?")
    return f"{days}天{count}板"


def pool_rows(payload) -> list | None:
    """``None`` 表示这次不能当确认结果写盘（data 缺失）。空列表是确认过的空池。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    pool = data.get("pool")
    if not isinstance(pool, list):
        return None
    return pool


def parse_zt_pool(payload, trade_date: str) -> list[dict] | None:
    rows = pool_rows(payload)
    if rows is None:
        return None
    day = iso_date(trade_date)
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        code = code6_of(str(raw.get("c") or ""))
        if not code:
            continue
        out.append({
            "code": code,
            "symbol": canonical_symbol(code),
            "name": _text(raw.get("n")),
            "trade_date": day,
            "price": _price(raw.get("p")),
            "pct": _round(_num(raw.get("zdp")), 2),
            "amount": _num(raw.get("amount")),
            "float_cap": _num(raw.get("ltsz")),
            "turnover": _round(_num(raw.get("hs")), 2),
            "limit_days": _int(raw.get("lbc")),
            "first_seal": fmt_zt_time(raw.get("fbt")),
            "last_seal": fmt_zt_time(raw.get("lbt")),
            "seal_fund": _num(raw.get("fund")),
            "break_times": _int(raw.get("zbc")),
            "industry": _text(raw.get("hybk")),
            "zt_stat": _zt_stat(raw),
            "source": "eastmoney",
        })
    return out


def parse_zb_pool(payload, trade_date: str) -> list[dict] | None:
    rows = pool_rows(payload)
    if rows is None:
        return None
    day = iso_date(trade_date)
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        code = code6_of(str(raw.get("c") or ""))
        if not code:
            continue
        out.append({
            "code": code,
            "symbol": canonical_symbol(code),
            "name": _text(raw.get("n")),
            "trade_date": day,
            "price": _price(raw.get("p")),
            "limit_price": _price(raw.get("ztp")),
            "pct": _round(_num(raw.get("zdp")), 2),
            "turnover": _round(_num(raw.get("hs")), 2),
            "first_seal": fmt_zt_time(raw.get("fbt")),
            "break_times": _int(raw.get("zbc")),
            "amplitude": _round(_num(raw.get("zf")), 2),
            "speed": _round(_num(raw.get("zs")), 2),
            "industry": _text(raw.get("hybk")),
            "zt_stat": _zt_stat(raw),
            "source": "eastmoney",
        })
    return out


def parse_dt_pool(payload, trade_date: str) -> list[dict] | None:
    rows = pool_rows(payload)
    if rows is None:
        return None
    day = iso_date(trade_date)
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        code = code6_of(str(raw.get("c") or ""))
        if not code:
            continue
        out.append({
            "code": code,
            "symbol": canonical_symbol(code),
            "name": _text(raw.get("n")),
            "trade_date": day,
            "price": _price(raw.get("p")),
            "pct": _round(_num(raw.get("zdp")), 2),
            "turnover": _round(_num(raw.get("hs")), 2),
            "pe": _num(raw.get("pe")),
            "seal_fund": _num(raw.get("fund")),
            "last_seal": fmt_zt_time(raw.get("lbt")),
            "board_amount": _num(raw.get("fba")),
            "dt_days": _int(raw.get("days")),
            "open_times": _int(raw.get("oc")),
            "industry": _text(raw.get("hybk")),
            "source": "eastmoney",
        })
    return out


def parse_yzt_pool(payload, trade_date: str) -> list[dict] | None:
    rows = pool_rows(payload)
    if rows is None:
        return None
    day = iso_date(trade_date)
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        code = code6_of(str(raw.get("c") or ""))
        if not code:
            continue
        out.append({
            "code": code,
            "symbol": canonical_symbol(code),
            "name": _text(raw.get("n")),
            "trade_date": day,
            "price": _price(raw.get("p")),
            "pct": _round(_num(raw.get("zdp")), 2),
            "turnover": _round(_num(raw.get("hs")), 2),
            "amplitude": _round(_num(raw.get("zf")), 2),
            "speed": _round(_num(raw.get("zs")), 2),
            "y_first_seal": fmt_zt_time(raw.get("yfbt")),
            "y_limit_days": _int(raw.get("ylbc")),
            "industry": _text(raw.get("hybk")),
            "zt_stat": _zt_stat(raw),
            "source": "eastmoney",
        })
    return out


def sentiment_row(
    trade_date: str,
    zt: list[dict],
    zb: list[dict],
    dt: list[dict],
    yzt: list[dict] | None,
) -> dict:
    """炸板率 = 炸板数 / (涨停数 + 炸板数)，结果是 0–1 的比例。"""
    day = iso_date(trade_date)
    ladder: dict[int, int] = {}
    for row in zt:
        boards = int(row.get("limit_days") or 0)
        if boards > 0:
            ladder[boards] = ladder.get(boards, 0) + 1
    zt_n, zb_n = len(zt), len(zb)
    denom = zt_n + zb_n
    break_rate = (zb_n / denom) if denom else 0.0
    heights = [int(row.get("limit_days") or 0) for row in zt]
    advanced = 0
    if yzt is not None:
        advanced = sum(1 for row in yzt if (row.get("pct") or 0) >= 9.8)
    return {
        "trade_date": day,
        "zt_count": zt_n,
        "zb_count": zb_n,
        "dt_count": len(dt),
        "yzt_count": None if yzt is None else len(yzt),
        "break_rate": round(break_rate, 4),
        "max_height": max(heights, default=0),
        "ladder_json": json.dumps(
            {str(key): ladder[key] for key in sorted(ladder)},
            ensure_ascii=False,
        ),
        "advance_count": None if yzt is None else advanced,
        "source": "eastmoney",
    }


def ths_info_rows(payload) -> list | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    info = data.get("info")
    if not isinstance(info, list):
        return None
    return info


def split_themes(reason: str) -> list[str]:
    found: list[str] = []
    for part in _THEME_SPLIT.split(reason or ""):
        text = part.strip()
        if 2 <= len(text) <= 16 and text not in found:
            found.append(text)
    return found


def parse_ths_pool(payload, trade_date: str) -> list[dict] | None:
    """同花顺涨停揭秘。``first_limit_up_time`` 是 Unix 秒，按北京时间格式化。"""
    rows = ths_info_rows(payload)
    if rows is None:
        return None
    day = iso_date(trade_date)
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        code = code6_of(str(raw.get("code") or ""))
        if not code:
            continue
        reason = _text(raw.get("reason_type"))
        out.append({
            "code": code,
            "symbol": canonical_symbol(code),
            "name": _text(raw.get("name")),
            "trade_date": day,
            "price": _num(raw.get("latest")),
            "pct": _num(raw.get("change_rate")),
            "reason": reason,
            "themes": "、".join(split_themes(reason)),
            "board_type": _text(raw.get("limit_up_type")),
            "seal_rate": _num(raw.get("limit_up_suc_rate")),
            "break_times": _int(raw.get("open_num")) or 0,
            "seal_amount": _num(raw.get("order_amount")),
            "high_days": _text(raw.get("high_days")),
            "first_time": _unix_hms(raw.get("first_limit_up_time")),
            "is_again": _int(raw.get("is_again_limit")),
            "source": "ths",
        })
    return out


def _unix_hms(value) -> str:
    number = _num(value)
    if number is None or number <= 0:
        return ""
    try:
        stamp = datetime.fromtimestamp(int(number), CN_TZ)
    except (OverflowError, OSError, ValueError):
        return ""
    return stamp.strftime("%H:%M:%S")


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits)
