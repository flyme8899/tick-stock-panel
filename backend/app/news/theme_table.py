"""知识星球复盘笔记里的主题解析（只读、纯函数）。

两条来源，互为补充：

1. **Markdown 表格**。正文里如果有表头含「主题 / 题材 / 板块 / …」的表，按列抽取。
   这是原有路径，列识别和容错行为保持不变。
2. **散文复盘笔记**。真实帖子通常不写这种表，常见写法是：

   - 标题 ``复盘笔记：人形机器人/低空经济``，用 ``/``、``、`` 等把主题列出来；
   - 正文分成「一、重磅财经信息 / 二、大盘表现 / 三、强势板块和个股分析」，
     第三段里主题名单独成一行，下面跟 ``①②`` 要点。

「涨停板复盘」「雁阵图」多为图片帖，正文几乎为空，本模块不处理。

设计边界（有意为之）
--------------------

- **只解析，不算强弱**：表格里的 ``强弱`` 原样透传。散文没有这一列，留空。
  不做打分、排序、映射或再解释。
- **不是信号源**：输出仅用于展示。本模块不产生买卖建议、不写任何存储、不触发推送。
  调用方也不得把它接成买点/做T 入口。
- **容错优先**：解析不了就跳过那一行/那一块，不抛异常。
- 纯函数、无 I/O、无第三方依赖，可直接单测。

表格列的识别
------------

按表头文字**宽松匹配**（去空白、去全角空格后做包含判断）：

    主题   <- 主题 / 题材 / 板块 / 方向 / 概念 / 主线 / 赛道
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
    """一条主题记录。``strength`` 原样保留作者写法，不做解释。

    ``source_kind`` 标记抽取来源：``table`` 表格、``prose`` 散文、
    ``mixed`` 同一主题两边都抽到了。散文没有强弱列时 ``strength`` 为空。
    ``summary`` 是强势板块段落里紧随该主题的 ``①`` 要点摘录，没有则为空。
    """

    theme: str
    strength: str = ""
    day: str = ""
    source_day: str = ""
    summary: str = ""
    source_kind: str = "table"

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


#: 散文主题名：短到能单独成行，长到装得下「低空经济（eVTOL）」这类写法。
_THEME_MIN_LEN = 2
_THEME_MAX_LEN = 16
#: ① 要点只带前若干字，复盘板不是全文阅读器。
_SUMMARY_LIMIT = 80

_SECTION_LINE = re.compile(r"^([一二三四五六七八九十]{1,3})[、.．]\s*(.+?)\s*$")
_POINT_MARK = re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]\s*")
_LIST_MARK = re.compile(
    r"^(?:\d{1,2}|[（(][0-9一二三四五六七八九十]{1,3}[)）])[、.．]\s*",
)
_TITLE_LEAD = re.compile(r"(?:【|\[)?复盘笔记(?:】|\])?\s*[:：]\s*(.+)$")
_TITLE_SPLIT = re.compile(r"[/／、|｜]+")
_DATE_TOKEN = re.compile(
    r"^(?:\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"
    r"|\d{1,2}月\d{1,2}日"
    r"|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})$",
)
_BAD_CHARS = re.compile(r"[。！？；;：:!?,，、]")
_HAS_WORD = re.compile(r"[\u4e00-\u9fffA-Za-z]")
_NOT_THEMES = frozenset({
    "强势板块",
    "强势板块和个股分析",
    "强势板块及个股分析",
    "大盘表现",
    "重磅财经信息",
    "风险提示",
    "免责声明",
    "个股分析",
    "复盘笔记",
    "涨停板复盘",
    "雁阵图",
    "以上",
    "小结",
    "总结",
    "备注",
    "说明",
})


def theme_rows_from_text(
    text: str,
    *,
    published_at: str = "",
    title: str = "",
) -> list[ThemeRow]:
    """从一篇帖子抽主题。表格优先，散文补表格里没有的主题和要点摘要。

    同一篇里同名主题合成一行：表格保留强弱，散文补上 ``①`` 摘要。
    ``published_at`` 只在行内没有日期时兜底。
    """
    source_day = _day_of(published_at)
    table_rows = parse_theme_tables(text, source_day=source_day)
    prose_rows = _parse_prose_themes(text, title=title, source_day=source_day)
    return _merge_post_rows(table_rows, prose_rows)


def _day_of(stamp: str) -> str:
    """``2026-10-15T09:30:00+08:00`` -> ``2026-10-15``；认不出返回空串。"""
    if not stamp:
        return ""
    try:
        return datetime.fromisoformat(str(stamp).strip()).date().isoformat()
    except ValueError:
        return _clean_day(stamp)


def _clean_candidate(raw: str) -> str:
    text = (raw or "").replace("\u3000", "").strip().strip("*#").strip()
    return re.sub(r"[：:]+$", "", text).strip()


def _theme_name_ok(name: str) -> bool:
    """单独成行的短主题名。节标题、要点行、日期和句子都排除。"""
    text = name.strip()
    if not text or re.search(r"\s", text):
        return False
    if not (_THEME_MIN_LEN <= len(text) <= _THEME_MAX_LEN):
        return False
    if text in _NOT_THEMES:
        return False
    if _SECTION_LINE.match(text) or _POINT_MARK.match(text) or _LIST_MARK.match(text):
        return False
    if _BAD_CHARS.search(text) or _DATE_TOKEN.match(text):
        return False
    return _HAS_WORD.search(text) is not None


def _summarize_points(points: list[str]) -> str:
    text = "；".join(point for point in points if point)
    if len(text) <= _SUMMARY_LIMIT:
        return text
    return text[: _SUMMARY_LIMIT - 1].rstrip() + "…"


def _themes_from_title(title: str) -> list[str]:
    """``复盘笔记：A/B/C``。只按斜杠和顿号切开，不把逗号句子拆成主题。"""
    if not title or "复盘笔记" not in title:
        return []
    match = _TITLE_LEAD.search(title.strip())
    if not match:
        return []
    tail = match.group(1).strip()
    tail = re.sub(r"\s*[（(]\s*\d{4}[-/.年].*?[)）]\s*$", "", tail).strip()
    return [_clean_candidate(part) for part in _TITLE_SPLIT.split(tail) if part.strip()]


def _strong_section_ranges(lines: list[str]) -> list[tuple[int, int]]:
    headers: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _SECTION_LINE.match(line.strip())
        if match:
            headers.append((index, match.group(2).replace(" ", "").replace("\u3000", "")))
    ranges: list[tuple[int, int]] = []
    for pos, (start, heading) in enumerate(headers):
        if "强势板块" not in heading:
            continue
        end = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
        ranges.append((start + 1, end))
    return ranges


def _point_text(raw: str) -> str:
    text = _POINT_MARK.sub("", raw, count=1)
    return _LIST_MARK.sub("", text, count=1).strip()


def _themes_from_sections(text: str) -> list[tuple[str, str]]:
    """只扫「强势板块」这一段。段外的短行、节标题和 ① 行都不是主题。"""
    if not text or "强势板块" not in text:
        return []
    lines = text.splitlines()
    found: list[tuple[str, str]] = []
    for start, end in _strong_section_ranges(lines):
        current = ""
        points: list[str] = []

        def flush() -> None:
            nonlocal current, points
            if current:
                found.append((current, _summarize_points(points)))
            current = ""
            points = []

        for line in lines[start:end]:
            raw = line.strip()
            if not raw:
                continue
            if _POINT_MARK.match(raw) or _LIST_MARK.match(raw):
                if current:
                    point = _point_text(raw)
                    if point:
                        points.append(point)
                continue
            candidate = _clean_candidate(raw)
            if _theme_name_ok(candidate):
                flush()
                current = candidate
        flush()
    return found


def _parse_prose_themes(text: str, *, title: str, source_day: str) -> list[ThemeRow]:
    by_name: dict[str, ThemeRow] = {}
    order: list[str] = []

    def put(theme: str, summary: str) -> None:
        name = theme.strip()
        if not _theme_name_ok(name):
            return
        prev = by_name.get(name)
        if prev is None:
            by_name[name] = ThemeRow(
                theme=name,
                strength="",
                day="",
                source_day=source_day,
                summary=summary,
                source_kind="prose",
            )
            order.append(name)
            return
        if summary and not prev.summary:
            by_name[name] = ThemeRow(
                theme=name,
                strength="",
                day="",
                source_day=source_day,
                summary=summary,
                source_kind="prose",
            )

    for name in _themes_from_title(title):
        put(name, "")
    for name, summary in _themes_from_sections(text):
        put(name, summary)
    return [by_name[name] for name in order]


def _merge_post_rows(table_rows: list[ThemeRow], prose_rows: list[ThemeRow]) -> list[ThemeRow]:
    """同一篇内同名主题合成一行。表格的强弱保留，散文只补摘要。"""
    merged: list[ThemeRow] = []
    index: dict[str, int] = {}
    for row in table_rows:
        index[row.theme] = len(merged)
        merged.append(row)
    for row in prose_rows:
        pos = index.get(row.theme)
        if pos is None:
            index[row.theme] = len(merged)
            merged.append(row)
            continue
        current = merged[pos]
        summary = current.summary or row.summary
        kind = current.source_kind
        if row.source_kind and row.source_kind != kind:
            kind = "mixed"
        if summary == current.summary and kind == current.source_kind:
            continue
        merged[pos] = ThemeRow(
            theme=current.theme,
            strength=current.strength,
            day=current.day,
            source_day=current.source_day or row.source_day,
            summary=summary,
            source_kind=kind,
        )
    return merged
