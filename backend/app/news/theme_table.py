"""知识星球复盘笔记里的**主题表**解析（只读、纯函数）。

星球帖子的正文（``news_items.clean_text``）里常有 Markdown 表格形式的「主题表」，
形如::

    | 主题 | 强弱 | 日期 |
    | --- | --- | --- |
    | 算力租赁 | 强 | 2026-10-14 |
    | 固态电池 | 弱转强 | 2026-10-15 |

本模块把这些表格抽成结构化行，供**盘后复盘板**只读展示。

设计边界（有意为之）
--------------------

- **只解析，不算强弱**：``强弱`` 是笔记作者手写的判断，本模块**原样透传字符串**，
  不做任何打分、排序、映射或再解释。没有该列就留空。
- **不是信号源**：输出仅用于展示。本模块不产生买卖建议、不写任何存储、不触发推送。
  调用方也不得把它接成买点/做T 入口。
- **容错优先**：表格千奇百怪（对齐行写法不同、列名同义、单元格含 ``|`` 转义）。
  解析不了就跳过那一行/那一块，不抛异常 —— 宁可少抽几条，不能因为一条怪表格
  让整个复盘板打不开。
- 纯函数、无 I/O、无第三方依赖，可直接单测。

列的识别
--------

按表头文字**宽松匹配**（去空白、去全角空格后做包含判断）：

    主题   <- 主题 / 题材 / 板块 / 方向 / 概念 / 主线
    强弱   <- 强弱 / 强度 / 力度（也接受 表现 / 状态）
    日期   <- 日期 / 时间 / 交易日（缺省时回退到帖子发布时间）

必需列只有「主题」；缺了就不是主题表，整块跳过。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

__all__ = [
    "DATE_HEADERS",
    "STRENGTH_HEADERS",
    "THEME_HEADERS",
    "ThemeRow",
    "is_separator_row",
    "looks_like_table",
    "parse_theme_tables",
    "split_table_cells",
    "theme_rows_from_text",
]

#: 表头同义词。命中任意一个即认定为该列（按包含匹配，``主题`` 能命中 ``核心主题``）。
THEME_HEADERS = ("主题", "题材", "板块", "方向", "概念", "主线", "赛道")
STRENGTH_HEADERS = ("强弱", "强度", "力度", "表现", "状态")
DATE_HEADERS = ("日期", "时间", "交易日")

_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")
_DATE_IN_TEXT = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
#: 单元格里被转义的竖线（``\|``）先占位，避免被当成列分隔。
_ESCAPED_PIPE = "\x00PIPE\x00"


@dataclass(frozen=True)
class ThemeRow:
    """一条主题表记录。``strength`` 原样保留作者写法，不做解释。"""

    theme: str
    strength: str = ""
    day: str = ""
    source_day: str = ""

    @property
    def display_day(self) -> str:
        """展示用日期：优先行内日期，其次帖子日期。"""
        return self.day or self.source_day


def split_table_cells(line: str) -> list[str]:
    """把 ``| a | b |`` 拆成 ``["a", "b"]``。保留 ``\\|`` 转义。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith(_ESCAPED_PIPE):
        text = text[:-1]
    text = text.replace("\\|", _ESCAPED_PIPE)
    cells = [c.replace(_ESCAPED_PIPE, "|").strip() for c in text.split("|")]
    return cells


def is_separator_row(cells: list[str]) -> bool:
    """``| --- | :---: |`` 这种对齐行。"""
    return bool(cells) and all(_SEPARATOR_CELL.match(c.replace(" ", "")) for c in cells)


def looks_like_table(line: str) -> bool:
    """粗判是否像表格行：至少两个竖线。"""
    return line.count("|") >= 2


def _match_column(header: str, synonyms: tuple[str, ...]) -> bool:
    flat = header.replace(" ", "").replace("\u3000", "")
    return any(word in flat for word in synonyms)


def _columns_of(header_cells: list[str]) -> dict[str, int]:
    """表头 -> 各语义列的下标。同名取最先出现的那个。"""
    found: dict[str, int] = {}
    for index, cell in enumerate(header_cells):
        if "theme" not in found and _match_column(cell, THEME_HEADERS):
            found["theme"] = index
            continue
        if "strength" not in found and _match_column(cell, STRENGTH_HEADERS):
            found["strength"] = index
            continue
        if "day" not in found and _match_column(cell, DATE_HEADERS):
            found["day"] = index
    return found


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(cells):
        return ""
    return cells[index].strip()


def _clean_day(value: str) -> str:
    """把 ``2026/10/14``、``2026.10.14`` 归一成 ``2026-10-14``。认不出返回空串。"""
    match = _DATE_IN_TEXT.search(value or "")
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def parse_theme_tables(text: str, *, source_day: str = "") -> list[ThemeRow]:
    """从正文里抽所有主题表。解析不了的块整块跳过，不抛异常。"""
    if not text:
        return []
    rows: list[ThemeRow] = []
    lines = text.splitlines()
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not looks_like_table(line):
            index += 1
            continue
        header_cells = split_table_cells(line)
        columns = _columns_of(header_cells)
        if "theme" not in columns:
            index += 1
            continue

        # 表头下面通常紧跟一行对齐行；没有也能解析。
        body_start = index + 1
        if body_start < total and looks_like_table(lines[body_start]):
            maybe = split_table_cells(lines[body_start])
            if is_separator_row(maybe):
                body_start += 1

        cursor = body_start
        while cursor < total and looks_like_table(lines[cursor]):
            cells = split_table_cells(lines[cursor])
            if is_separator_row(cells):
                cursor += 1
                continue
            theme = _cell(cells, columns.get("theme"))
            if theme:
                rows.append(ThemeRow(
                    theme=theme,
                    strength=_cell(cells, columns.get("strength")),
                    day=_clean_day(_cell(cells, columns.get("day"))),
                    source_day=source_day,
                ))
            cursor += 1
        index = cursor
    return rows


def theme_rows_from_text(
    text: str,
    *,
    published_at: str = "",
    title: str = "",
) -> list[ThemeRow]:
    """便捷入口：从帖子正文抽主题表，用 ``published_at`` 兜底日期。

    ``title`` 只用于极少数「表头写在标题里」的情况，当前不参与解析，
    保留参数是为了调用方不必分两处取数。
    """
    return parse_theme_tables(text, source_day=_day_of(published_at))


def _day_of(stamp: str) -> str:
    """``2026-10-15T09:30:00+08:00`` -> ``2026-10-15``；认不出返回空串。"""
    if not stamp:
        return ""
    try:
        return datetime.fromisoformat(str(stamp).strip()).date().isoformat()
    except ValueError:
        return _clean_day(stamp)
