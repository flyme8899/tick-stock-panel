"""ETF 名称和跟踪指数参考表。

份额分区里的 ``name`` 保持交易所原文，不改历史文件。看板和份额接口读这份
参考表，把沪市短简称换成扩位全称，并用跟踪指数代码归类宽基。

来源：

* 上交所 ``commonSoaQuery.do?sqlId=FUND_LIST&fundType=00``
* 深交所 ``ShowReport`` ``CATALOGID=1105`` 的 ETF 行
* 新浪 ``etf_hq_fund`` 只补两边都没有的代码

某一源失败时沿用该源上一版，不把整张表写成空。
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

import polars as pl

from app.fund_flow.normalize import code6
from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger(__name__)

SSE_URL = "https://query.sse.com.cn/commonSoaQuery.do"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport"
SINA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/jsonp.php/"
    "IO.XSRV2.CallbackList['da_yPT46_Ll7K6WD']/Market_Center.getHQNodeDataSimple"
)
_SSE_PAGE_SIZE = 500
_SSE_PAGE_CAP = 20
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_COLUMNS = (
    "code",
    "full_name",
    "short_name",
    "index_code",
    "index_name",
    "company",
    "source",
)
_SCHEMA = {name: pl.Utf8 for name in _COLUMNS}
# 成功刷新时整源替换。新浪只补沪深两边都没有的代码。
_PRIMARY = ("sse", "szse")

Getter = Callable[..., bytes]


def meta_path(data_dir: Path) -> Path:
    return Path(data_dir) / "fund_flow" / "etf_meta" / "meta.parquet"


def load_rows(data_dir: Path) -> list[dict]:
    """读上一版。文件不存在或坏了就当没有，不把查询打成 500。"""
    path = meta_path(data_dir)
    if not path.exists():
        return []
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("etf_meta 读不出来，当作没有: %s", exc)
        return []
    rows = []
    for record in frame.to_dicts():
        code = _code(record.get("code"))
        if not code:
            continue
        rows.append({
            "code": code,
            "full_name": _text(record.get("full_name")),
            "short_name": _text(record.get("short_name")),
            "index_code": _index_code(record.get("index_code")),
            "index_name": _text(record.get("index_name")),
            "company": _text(record.get("company")),
            "source": _text(record.get("source")),
        })
    return rows


def load_index(data_dir: Path) -> dict[str, dict]:
    return {row["code"]: row for row in load_rows(data_dir)}


def refresh(data_dir: Path, *, get: Getter | None = None) -> list[str]:
    """拉三个源并落盘。返回错误列表。全部失败时不写文件。"""
    fetch = get or _http_get
    previous = load_rows(data_dir)
    fresh: dict[str, list[dict]] = {}
    errors: list[str] = []
    for source, loader in (
        ("sse", _load_sse),
        ("szse", _load_szse),
        ("sina", _load_sina),
    ):
        try:
            rows = loader(fetch)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"etf_meta {source}: {exc}")
            logger.warning("etf_meta %s 失败，沿用上一版: %s", source, exc)
            continue
        if not rows:
            errors.append(f"etf_meta {source}: 无数据")
            continue
        fresh[source] = rows
    if not fresh:
        return errors
    merged = _merge(previous, fresh)
    if not merged:
        errors.append("etf_meta: 合并后为空，保留上一版")
        return errors
    _write(data_dir, merged)
    return errors


def _merge(previous: list[dict], fresh: dict[str, list[dict]]) -> list[dict]:
    refreshed = set(fresh)
    out = {
        row["code"]: row
        for row in previous
        if row.get("source") not in refreshed
    }
    for source in _PRIMARY:
        for row in fresh.get(source, []):
            out[row["code"]] = row
    for row in fresh.get("sina", []):
        existing = out.get(row["code"])
        if existing is None or existing.get("source") in {"", "sina"}:
            out[row["code"]] = row
    return [out[code] for code in sorted(out)]


def _write(data_dir: Path, rows: list[dict]) -> None:
    path = meta_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        [{name: row.get(name) or None for name in _COLUMNS} for row in rows],
        schema=_SCHEMA,
    )
    atomic_write_parquet(frame, path)


def _load_sse(get: Getter) -> list[dict]:
    first = _sse_page(get, 1)
    payload = first["payload"]
    rows = list(first["rows"])
    page_count = _page_count(payload)
    for page in range(2, min(page_count, _SSE_PAGE_CAP) + 1):
        nxt = _sse_page(get, page)
        if not nxt["rows"]:
            break
        rows.extend(nxt["rows"])
    return _dedupe(rows)


def _sse_page(get: Getter, page: int) -> dict:
    body = get(
        SSE_URL,
        params={
            "sqlId": "FUND_LIST",
            "fundType": "00",
            "isPagination": "true",
            "pageHelp.pageSize": str(_SSE_PAGE_SIZE),
            "pageHelp.pageNo": str(page),
            "pageHelp.beginPage": str(page),
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": str(page),
        },
        headers={
            "Referer": "https://www.sse.com.cn/assortment/fund/list/",
            "User-Agent": _UA,
        },
    )
    payload = _json_bytes(body)
    if not isinstance(payload, dict):
        raise RuntimeError("上交所响应不是对象")
    result = payload.get("result")
    if not isinstance(result, list):
        raise RuntimeError("上交所响应没有 result")
    rows = []
    for item in result:
        if not isinstance(item, dict):
            continue
        code = _code(item.get("fundCode"))
        full_name = _text(item.get("secNameFull"))
        if not code or not full_name:
            continue
        rows.append({
            "code": code,
            "full_name": full_name,
            "short_name": _text(item.get("fundAbbr")),
            "index_code": _index_code(item.get("INDEX_CODE")),
            "index_name": _text(item.get("INDEX_NAME")),
            "company": _text(item.get("companyName")),
            "source": "sse",
        })
    return {"payload": payload, "rows": rows}


def _page_count(payload: dict) -> int:
    page_help = payload.get("pageHelp")
    if not isinstance(page_help, dict):
        return 1
    try:
        count = int(page_help.get("pageCount") or 1)
    except (TypeError, ValueError):
        return 1
    return max(1, count)


def _load_szse(get: Getter) -> list[dict]:
    body = get(
        SZSE_URL,
        params={"SHOWTYPE": "xlsx", "CATALOGID": "1105", "TABKEY": "tab1"},
        headers={
            "Referer": "https://www.szse.cn/market/product/list/etfList/index.html",
            "User-Agent": _UA,
        },
    )
    table = _xlsx_rows(body)
    if not table:
        raise RuntimeError("深交所报表是空的")
    header = table[0]
    index = {name: pos for pos, name in enumerate(header)}
    for column in ("基金代码", "基金简称", "基金类别"):
        if column not in index:
            raise RuntimeError(f"深交所报表缺少{column}")
    company_at = index.get("基金管理人")
    rows = []
    for cells in table[1:]:
        kind = _at(cells, index["基金类别"])
        if kind != "ETF":
            continue
        code = _code(_at(cells, index["基金代码"]))
        full_name = _at(cells, index["基金简称"])
        if not code or not full_name:
            continue
        rows.append({
            "code": code,
            "full_name": full_name,
            "short_name": "",
            "index_code": "",
            "index_name": "",
            "company": _at(cells, company_at),
            "source": "szse",
        })
    return _dedupe(rows)


def _at(cells: list[str], pos: int | None) -> str:
    if pos is None or pos >= len(cells):
        return ""
    return cells[pos].strip()


def _load_sina(get: Getter) -> list[dict]:
    body = get(
        SINA_URL,
        params={
            "page": "1",
            "num": "5000",
            "sort": "symbol",
            "asc": "0",
            "node": "etf_hq_fund",
        },
        headers={
            "Referer": "https://vip.stock.finance.sina.com.cn/fund_center/index.html",
            "User-Agent": _UA,
        },
    )
    text = body.decode("utf-8", errors="replace")
    # 回调名本身带方括号：CallbackList['...']([...])。数组从 ([ 开始。
    wrapped = text.find("([")
    start = wrapped + 1 if wrapped >= 0 else text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise RuntimeError("新浪响应没有列表")
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("新浪响应不是 JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("新浪响应不是列表")
    rows = []
    for item in payload:
        code, name = _sina_item(item)
        if not code or not name:
            continue
        rows.append({
            "code": code,
            "full_name": name,
            "short_name": "",
            "index_code": "",
            "index_name": "",
            "company": "",
            "source": "sina",
        })
    return _dedupe(rows)


def _sina_item(item: object) -> tuple[str, str]:
    if isinstance(item, dict):
        code = _code(item.get("code") or item.get("symbol"))
        return code, _text(item.get("name"))
    if isinstance(item, list) and len(item) >= 2:
        return _code(item[0]), _text(item[1])
    return "", ""


def _xlsx_rows(body: bytes) -> list[list[str]]:
    import io

    try:
        package = ZipFile(io.BytesIO(body))
    except BadZipFile as exc:
        raise RuntimeError("深交所报表不是 xlsx") from exc
    with package:
        try:
            sheet = package.read("xl/worksheets/sheet1.xml")
        except KeyError as exc:
            raise RuntimeError("深交所报表没有工作表") from exc
        shared = _shared_strings(package)
    root = ET.fromstring(sheet)
    rows = []
    for row in root.findall(f"{_NS}sheetData/{_NS}row"):
        cells: dict[int, str] = {}
        for cell in row.findall(f"{_NS}c"):
            ref = cell.attrib.get("r") or ""
            cells[_col_index(ref)] = _cell_text(cell, shared)
        if not cells:
            continue
        width = max(cells) + 1
        rows.append([cells.get(pos, "") for pos in range(width)])
    return rows


def _shared_strings(package: ZipFile) -> list[str]:
    try:
        raw = package.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    out = []
    for item in root.findall(f"{_NS}si"):
        out.append("".join(node.text or "" for node in item.iter(f"{_NS}t")))
    return out


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_NS}t")).strip()
    value = cell.find(f"{_NS}v")
    if value is None or value.text is None:
        return ""
    if kind == "s":
        try:
            return shared[int(value.text)].strip()
        except (ValueError, IndexError):
            return ""
    return value.text.strip()


def _dedupe(rows: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for row in rows:
        out[row["code"]] = row
    return list(out.values())


def _json_bytes(body: bytes) -> object:
    text = body.decode("utf-8", errors="replace").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start > 0 and end > start:
        # 上交所有时在 JSON 前加空白或回调名。
        text = text[start:end + 1]
    return json.loads(text)


def _code(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or value != int(value) or value > 999999:
            return ""
        return f"{int(value):06d}"
    if isinstance(value, int):
        return f"{value:06d}" if 0 <= value <= 999999 else ""
    text = str(value).strip()
    lower = text.lower()
    if lower.startswith(("sh", "sz")) and len(text) > 2:
        text = text[2:]
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return code6(text)


def _index_code(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or value != int(value):
            return ""
        value = int(value)
    if isinstance(value, int):
        return f"{value:06d}" if 0 <= value <= 999999 else ""
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "-"}:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return text


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _http_get(url: str, *, params: dict | None = None, headers: dict | None = None) -> bytes:
    import httpx

    response = httpx.get(
        url,
        params=params,
        headers=headers,
        timeout=httpx.Timeout(20.0, connect=5.0),
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.content


# 列字母只接受 ASCII，避免把中文表头当成单元格引用。
_COL_RE = re.compile(r"^[A-Z]+$")


def _col_index(ref: str) -> int:
    col = ""
    for ch in ref:
        if ch.isalpha():
            col += ch
        else:
            break
    if not col or _COL_RE.match(col.upper()) is None:
        return 0
    number = 0
    for ch in col.upper():
        number = number * 26 + (ord(ch) - 64)
    return max(0, number - 1)
