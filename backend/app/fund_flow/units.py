"""金额和份额换算。只按字段来源声明的单位换，不看数值大小猜单位。"""
from __future__ import annotations

import math
import re

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

# 裸数字（没有亿/万/元后缀）的单位，按数据源字段写死。
BARE_YUAN = "yuan"
BARE_YI = "yi"  # 亿元
BARE_WAN = "wan"  # 万
BARE_WAN_SHARES = "wan_shares"  # 万份


def money_to_yuan(value: object, *, bare: str) -> float | None:
    """换成元。字符串里的亿/万/元优先于 ``bare``。空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return _scale(float(value), bare)
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text or text in {"-", "--", "nan", "None", "null"}:
        return None
    matched = _NUMBER.search(text)
    if matched is None:
        return None
    number = float(matched.group(0))
    if "亿" in text:
        return number * 1e8
    if "万" in text:
        return number * 1e4
    if "元" in text:
        return number
    return _scale(number, bare)


def shares_to_count(value: object, *, bare: str = BARE_WAN_SHARES) -> float | None:
    """基金份额换成份。上交所接口的裸数字是万份。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if not text or text in {"-", "--", "nan", "None"}:
            return None
        if "亿" in text:
            matched = _NUMBER.search(text)
            return None if matched is None else float(matched.group(0)) * 1e8
        if "万" in text:
            matched = _NUMBER.search(text)
            return None if matched is None else float(matched.group(0)) * 1e4
        matched = _NUMBER.search(text)
        if matched is None:
            return None
        number = float(matched.group(0))
    if bare == BARE_WAN_SHARES:
        return number * 1e4
    return number


def percent_points_to_decimal(value: object) -> float | None:
    """百分点换成小数。5.2（表示 5.2%）→ 0.052。已经是小数的字段不要走这里。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return float(value) / 100.0
    text = str(value).strip().replace("%", "").replace("％", "")
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text) / 100.0
    except ValueError:
        return None


def _scale(number: float, bare: str) -> float:
    if bare == BARE_YI:
        return number * 1e8
    if bare in {BARE_WAN, BARE_WAN_SHARES}:
        return number * 1e4
    return number
