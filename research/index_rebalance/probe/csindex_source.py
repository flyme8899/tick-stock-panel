"""中证指数「定期调样」公告的只读采集（研究用）。

**只读**：只发 HTTP GET/POST 到中证指数官网，不写任何 TSP 数据目录、不碰 backend。
产物由调用方决定落盘位置（本探测落 `research/index_rebalance/probe/_cache/`）。

数据路径（本次探测确认）
------------------------

1. 公告列表  ``POST /csindex-home/announcement/queryAnnouncementByVo``
   参数 ``{"page": {"page": N, "rows": 200}, "lang": "cn"}``
   返回 ``data[].theme``，其中 ``指数调样`` 即调样公告。
2. 公告详情  ``GET  /csindex-home/announcement/queryAnnouncementById?id=<id>``
   返回 ``publishDate`` 与 ``content``（HTML），**生效日在正文里**：
   「于2026年6月12日收市后生效」。
3. 名单附件  ``data.enclosureList[].fileUrl`` → PDF
   内含「调出名单 / 调入名单」两列，带证券代码与名称。

为什么不用 akshare
------------------

akshare 的 ``index_stock_cons_csindex`` 只给**当日快照**（实测三指数日期唯一值
均为当天），``index_detail_hist_adjust_cni`` 只覆盖国证系列（399001/399005/399006/
399102 有数据，沪深300/中证500/中证1000 全为 0 行）。
"""
from __future__ import annotations

import json
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

BASE = "https://www.csindex.com.cn/csindex-home"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Referer": "https://www.csindex.com.cn/",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
}

#: 目标指数：名称 -> 代码
TARGET_INDEXES = {"沪深300": "000300", "中证500": "000905", "中证1000": "000852"}

ADJUST_THEME = "指数调样"

#: 「于2026年6月12日收市后生效」
_EFFECTIVE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*收市后生效")
#: 「沪深300指数更换19只样本」
_REPLACED = re.compile(r"(沪深\s*300|中证\s*500|中证\s*1000)\s*指数更换\s*(\d+)\s*只")
_TAG = re.compile(r"<[^>]+>")

#: 报告表头里的指数名（PDF 里常写成「沪深 300 指数样本调整名单：」，中间有空格）
_PDF_SECTION = re.compile(
    r"(沪深\s*300|中证\s*500|中证\s*1000)\s*指数样本调整名单"
)
_PAIR = re.compile(r"^(\d{6})\s+(\S+)\s+(\d{6})\s+(\S+)$")


def strip_html(html: str) -> str:
    """去标签、归一空白。"""
    text = _TAG.sub("", html or "")
    text = text.replace("&nbsp;", " ").replace("　", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def _post(path: str, payload: dict, *, timeout: int = 40, retries: int = 3) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers=UA, method="POST")
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - 网络抖动重试，最终抛给调用方
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"POST {path} 失败: {type(last).__name__}: {last}")


def _get(path: str, params: dict, *, timeout: int = 40, retries: int = 4) -> dict:
    """GET。中证官网对详情接口有 WAF：**突发请求会返回 403 + HTML 拦截页**。

    所以这里对 403/HTML 响应做**指数退避**（而不是当致命错误），
    退避上限约 30 秒。持续 403 说明已被限流，需要隔更久再来。
    """
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=UA, method="GET")
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
            if body.lstrip().startswith("<"):
                # WAF 拦截页（状态码可能仍是 200）
                raise RuntimeError("WAF 拦截（返回 HTML 而非 JSON）")
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            last = exc
            wait = min(30.0, 3.0 * (2 ** attempt))
            if exc.code == 403:
                time.sleep(wait)
                continue
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(30.0, 3.0 * (2 ** attempt)))
    raise RuntimeError(f"GET {path} 失败: {type(last).__name__}: {last}")


def fetch_announcements(*, max_pages: int = 25, rows: int = 200,
                        sleep_s: float = 0.2) -> list[dict]:
    """翻页抓全部公告（本地按 theme 过滤，因为接口不吃 theme 参数 —— 实测忽略）。"""
    out: list[dict] = []
    total = None
    for page in range(1, max_pages + 1):
        res = _post("/announcement/queryAnnouncementByVo",
                    {"page": {"page": page, "rows": rows}, "lang": "cn"})
        if total is None:
            total = res.get("total")
        batch = res.get("data") or []
        if not batch:
            break
        out.extend(batch)
        if total and len(out) >= total:
            break
        if sleep_s:
            time.sleep(sleep_s)
    return out


@dataclass
class Adjustment:
    """一次指数调样事件。"""

    notice_id: int
    title: str
    announce_date: str            # 公告日
    effective_date: str = ""      # 生效日（正文解析）
    replaces: dict = field(default_factory=dict)   # 指数名 -> 更换只数
    attachment: str = ""          # 名单 PDF URL
    targets: list[str] = field(default_factory=list)  # 命中的目标指数名
    regular: bool = False         # 是否「定期调整」（相对临时调整）

    @property
    def is_target(self) -> bool:
        return bool(self.targets)


def parse_notice(notice_id: int, *, title: str = "", announce_date: str = "",
                 cache_dir: pathlib.Path | None = None) -> Adjustment:
    """取公告详情，解析生效日 / 更换只数 / 附件。

    ``cache_dir`` 给定时把详情 JSON 缓存到磁盘 —— 详情接口有 WAF，
    重跑时命中缓存可以少打扰官网（也避免重复触发拦截）。
    """
    cached = None
    if cache_dir is not None:
        path = pathlib.Path(cache_dir) / f"notice_{notice_id}.json"
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached = None
    if cached is None:
        res = _get("/announcement/queryAnnouncementById", {"id": notice_id})
        cached = res.get("data") or {}
        if cache_dir is not None and cached:
            path = pathlib.Path(cache_dir) / f"notice_{notice_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")

    data = cached
    text = strip_html(data.get("content") or "")
    item = Adjustment(
        notice_id=notice_id,
        title=data.get("title") or title,
        announce_date=data.get("publishDate") or announce_date,
    )
    m = _EFFECTIVE.search(text)
    if m:
        item.effective_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    for name, count in _REPLACED.findall(text):
        item.replaces[re.sub(r"\s+", "", name)] = int(count)
    item.targets = [n for n in TARGET_INDEXES if n in item.replaces]
    item.regular = "定期调整" in item.title
    encs = data.get("enclosureList") or []
    if encs:
        item.attachment = encs[0].get("fileUrl") or ""
    return item


def download(url: str, *, timeout: int = 120, retries: int = 3) -> bytes:
    """下载附件。

    ⚠️ 附件 URL 有两种写法，都实测过：

    - 中文原样（``.../20260529155822-附件：....pdf``）
    - 已百分号编码且**把斜杠也编码了**（``.../notice%2F2022...xlsx``）

    所以这里**只补安全字符、不重复编码已编码的部分**：先尝试原样请求，
    失败再试「按路径分段编码」的写法。
    """
    headers = {"User-Agent": UA["User-Agent"]}
    candidates = [url]
    if "%" not in url:
        candidates.append(urllib.parse.quote(url, safe=":/?=&"))
    else:
        # 已经是编码形式；再补一次「把剩余非 ASCII 转义」的写法
        candidates.append(urllib.parse.quote(url, safe=":/?=&%"))

    last: Exception | None = None
    for candidate in candidates:
        req = urllib.request.Request(candidate, headers=headers)
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 404:
                    break          # 这个写法不存在，换下一个
                time.sleep(2.0 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"附件下载失败: {type(last).__name__}: {last}")


def extract_sheets(blob: bytes) -> dict[str, list[list[str]]]:
    """把名单附件解析成 ``{sheet 名: [[单元格...], ...]}``。

    ⚠️ 实测坑：中证官网的名单附件**文件名写着 ``.pdf``，内容其实是 XLSX**
    （magic ``PK\\x03\\x04``）。而且三个 sheet 的名字就是语义本身：

        调入 / 调出 / 备选名单

    但 XML 文件名与 sheet 名的对应**不是顺序对应**（rId 映射会乱序），
    所以必须读 ``workbook.xml`` 的 ``name`` + ``r:id``，再经 rels 找 sheetN.xml。
    """
    import io
    import zipfile
    from xml.etree import ElementTree

    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    pkg_rel = "{http://schemas.openxmlformats.org/package/2006/relationships}"

    if blob[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            return {}
    else:
        return {}

    shared: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{ns}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))

    # sheet 名 -> 目标 xml
    name_to_target: dict[str, str] = {}
    wb = ElementTree.fromstring(zf.read("xl/workbook.xml"))
    for sheet in wb.iter(f"{ns}sheet"):
        name = sheet.get("name") or ""
        rid = sheet.get(f"{rel_ns}id") or ""
        name_to_target[name] = rid
    rid_to_target: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in zf.namelist():
        rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rels.iter(f"{pkg_rel}Relationship"):
            rid_to_target[rel.get("Id") or ""] = rel.get("Target") or ""

    out: dict[str, list[list[str]]] = {}
    for name, rid in name_to_target.items():
        target = rid_to_target.get(rid, "")
        if not target:
            continue
        path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        if path not in zf.namelist():
            continue
        root = ElementTree.fromstring(zf.read(path))
        rows: list[list[str]] = []
        for row in root.iter(f"{ns}row"):
            cells: list[str] = []
            for c in row.findall(f"{ns}c"):
                v = c.find(f"{ns}v")
                if v is None or v.text is None:
                    is_ = c.find(f"{ns}is")
                    text = ("".join(t.text or "" for t in is_.iter(f"{ns}t"))
                            if is_ is not None else "")
                elif c.get("t") == "s":
                    idx = int(v.text)
                    text = shared[idx] if 0 <= idx < len(shared) else ""
                else:
                    text = v.text
                cells.append((text or "").strip())
            while cells and not cells[-1]:
                cells.pop()
            if cells:
                rows.append(cells)
        out[name] = rows
    return out


def pick_sheet(sheets: dict[str, list[list[str]]], *keywords: str) -> list[list[str]]:
    """按关键词找 sheet（表头偶尔叫「调入名单」等，做包含匹配）。"""
    for name, rows in sheets.items():
        if any(k in name for k in keywords):
            return rows
    return []


def lists_from_notice_html(html: str) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """从公告**正文 HTML** 的表格里抽名单。

    2022 年起的公告会把「沪深300指数样本调整名单：」等**部分名单直接写进正文**
    （完整名单仍在附件里）。正文表格是「调出名单 | 调入名单」两列结构，
    每行形如 ``<td>000783</td><td>长江证券</td><td>000408</td><td>藏格矿业</td>``。
    """
    from html.parser import HTMLParser

    result: dict[str, dict[str, list[tuple[str, str]]]] = {}

    class _Table(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.rows: list[list[str]] = []
            self._row: list[str] | None = None
            self._cell: list[str] | None = None

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self._row = []
            elif tag in ("td", "th"):
                self._cell = []

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self._row is not None and self._cell is not None:
                self._row.append("".join(self._cell).strip())
                self._cell = None
            elif tag == "tr" and self._row is not None:
                if self._row:
                    self.rows.append(self._row)
                self._row = None

        def handle_data(self, data):
            if self._cell is not None:
                self._cell.append(data)
            # 分节标题在表格外，单独记录
            elif data.strip():
                self._flat.append(data.strip())

    # 先按分节标题切块。⚠️ 分节不止目标三指数（还有中证A50/A100/A500 等），
    # 必须用「任意分节标题」切，否则后面的行会串进前一个指数。
    blocks = re.split(r"([\u4e00-\u9fa5A-Za-z0-9]{2,20}?指数(?:样本调整名单|备选名单))",
                      html or "")
    # blocks = [前置, 标题1, 表1, 标题2, 表2, ...]
    for i in range(1, len(blocks) - 1, 2):
        heading = blocks[i]
        target = _PDF_TARGET.search(heading)
        if not target:
            continue            # 非目标指数：跳过（但仍已用于断界）
        name = re.sub(r"\s+", "", target.group(1))
        chunk = blocks[i + 1]
        parser = _Table()
        parser._flat = []          # type: ignore[attr-defined]
        try:
            parser.feed(chunk)
        except Exception:  # noqa: BLE001 - 坏 HTML 跳过该块
            continue
        rows = parser.rows
        if not rows:
            continue
        slot = result.setdefault(name, {"in": [], "out": [], "alt": []})
        for row in rows:
            cells = [c for c in row if c]
            if len(cells) < 4:
                continue
            if not (cells[0].isdigit() and len(cells[0]) == 6):
                continue
            if not (cells[2].isdigit() and len(cells[2]) == 6):
                continue
            slot["out"].append((cells[0], cells[1]))
            slot["in"].append((cells[2], cells[3]))
    return result


def merge_lists(*maps: dict) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """合并多来源名单（正文 + 附件），按 (代码, 名称) 去重，保序。"""
    out: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for m in maps:
        for name, buckets in (m or {}).items():
            slot = out.setdefault(name, {"in": [], "out": [], "alt": []})
            for key in ("in", "out", "alt"):
                seen = set(slot[key])
                for pair in buckets.get(key, []):
                    if pair not in seen:
                        seen.add(pair)
                        slot[key].append(pair)
    return out
def lists_from_attachment(blob: bytes) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """统一入口：按 magic 判断附件真实格式并解析。

    实测两代格式：

    - **2021 年及更早**：附件文件名写 ``.pdf``，内容其实是 **XLSX**
      （sheet 名 = 调入 / 调出 / 备选名单）。
    - **2022 年起**：附件是**真 PDF**，调出/调入并排两列。
    """
    import io

    if blob[:4] == b"PK\x03\x04":
        return lists_from_sheets(extract_sheets(blob))
    if blob[:4] == b"%PDF":
        try:
            from pypdf import PdfReader
        except ImportError:
            return {}
        reader = PdfReader(io.BytesIO(blob))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return lists_from_pdf_text(text)
    if blob[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return parse_name_lists(_extract_xls_text(blob))
    return {}
    """抽取附件为「每行制表符分隔」的文本（兼容 xlsx 与真 PDF）。

    保留此函数是为了让调用方不必关心格式；新代码建议直接用 ``extract_sheets``。
    """
    import io

    if blob[:4] == b"PK\x03\x04":
        sheets = extract_sheets(blob)
        lines: list[str] = []
        for name, rows in sheets.items():
            lines.append(f"### {name}")
            lines.extend("\t".join(r) for r in rows)
        return "\n".join(lines)
    if blob[:4] == b"%PDF":
        try:
            from pypdf import PdfReader
        except ImportError:
            return ""
        reader = PdfReader(io.BytesIO(blob))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if blob[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return _extract_xls_text(blob)
    return ""


def _extract_xls_text(blob: bytes) -> str:
    """老式 .xls：交给 xlrd（装了才可用）。"""
    try:
        import xlrd
    except ImportError:
        return ""
    book = xlrd.open_workbook(file_contents=blob)
    lines: list[str] = []
    for sheet in book.sheets():
        for r in range(sheet.nrows):
            cells = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
            while cells and not cells[-1]:
                cells.pop()
            if cells:
                lines.append("\t".join(cells))
    return "\n".join(lines)


def parse_name_lists(text: str) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """从 ``extract_pdf_text`` 的文本里抽每个指数的 调入/调出 名单。

    文本形如::

        ### 调入
        指数代码  指数简称  证券代码  证券简称
        000300    沪深300   000301    东方盛虹
        ### 调出
        ...
    """
    result: dict[str, dict[str, list[tuple[str, str]]]] = {}
    section = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("### "):
            section = line[4:].strip()
            continue
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 4:
            continue
        if parts[0].startswith("指数代码") or not (parts[0].isdigit() and len(parts[0]) == 6):
            continue
        # 第 1 列是「指数代码」，但下游按指数**简称**归组，这里不需要它
        _, index_name, sec_code, sec_name = parts[0], parts[1], parts[2], parts[3]
        if not (sec_code.isdigit() or len(sec_code) == 6):
            continue
        bucket = ("in" if "调入" in section else
                  "out" if "调出" in section else
                  "alt" if "备选" in section else "")
        if not bucket:
            continue
        slot = result.setdefault(index_name, {"in": [], "out": [], "alt": []})
        slot[bucket].append((sec_code, sec_name))
    return result


def lists_from_sheets(sheets: dict[str, list[list[str]]]) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """从 XLSX 的 sheet 名 + 行解析（**2021 年及更早**的附件是这种格式）。"""
    result: dict[str, dict[str, list[tuple[str, str]]]] = {}
    mapping = (("调入", "in"), ("调出", "out"), ("备选", "alt"))
    for keyword, bucket in mapping:
        rows = pick_sheet(sheets, keyword)
        for row in rows:
            if len(row) < 4:
                continue
            code, name = row[0].strip(), row[1].strip()
            if not (code.isdigit() and len(code) == 6):
                continue
            # 备选名单多一列「排序」，证券代码在第 3 列
            if bucket == "alt" and len(row) >= 5:
                sec_code, sec_name = row[3].strip(), row[4].strip()
            else:
                sec_code, sec_name = row[2].strip(), row[3].strip()
            slot = result.setdefault(name, {"in": [], "out": [], "alt": []})
            slot[bucket].append((sec_code, sec_name))
    return result


#: PDF 版式：「沪深 300 指数样本调整名单：」分节，行是「调出码 调出名 调入码 调入名」。
#: ⚠️ 分节**不止目标三指数** —— 名单里还有中证A50/A100/A500、以及「备选名单」等。
#: 必须以「下一个任意分节标题」为界，否则后面的行会被算进前一个指数（实测会多算 47 行）。
_PDF_ANY_SECTION = re.compile(r"^\s*(.{2,20}?指数(?:样本调整名单|备选名单))\s*[:：]?\s*$")
#: ⚠️ 股票名**可能含空格**（如「陕国投 A」「九号公司-WD」「万 科Ａ」），
#: 所以名称段不能用 ``\S+``，否则该行整行失配、名单少一只。
_PDF_PAIR = re.compile(r"^(\d{6})\s+(.+?)\s+(\d{6})\s+(.+?)$")
#: 只认这三个作为「目标指数」，其余分节仍要用来断界
_PDF_TARGET = re.compile(r"(沪深\s*300|中证\s*500|中证\s*1000)\s*指数样本调整名单")


def lists_from_pdf_text(text: str) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """从**真 PDF** 的文本里解析（2022 年起的附件是这种格式）。

    PDF 版式：调出与调入**并排两列**，一行同时含调出与调入各一只。
    分节标题可能不属于目标指数，但**仍要用来断开上一节**。
    """
    result: dict[str, dict[str, list[tuple[str, str]]]] = {}
    current: str | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        sec = _PDF_ANY_SECTION.match(line)
        if sec:
            target = _PDF_TARGET.search(line)
            if target:
                current = re.sub(r"\s+", "", target.group(1))
                result.setdefault(current, {"in": [], "out": [], "alt": []})
            else:
                # 非目标指数的分节（含备选名单）：断界，别把行算进上一个指数
                current = None
            continue
        if current is None:
            continue
        if line.startswith(("调出名单", "调入名单", "证券代码", "附件", "备注")):
            continue
        m = _PDF_PAIR.match(line)
        if m:
            out_code, out_name, in_code, in_name = m.groups()
            result[current]["out"].append((out_code, out_name))
            result[current]["in"].append((in_code, in_name))
    return result


def harvest_target_events(*, start_year: int = 2018, max_pages: int = 25,
                          sleep_s: float = 0.25, progress=None,
                          detail_gap_s: float = 6.0,
                          on_block=None, cache_dir=None) -> list[Adjustment]:
    """抓取 2018 年至今、命中目标指数的调样公告，并解析详情。

    ``detail_gap_s``：详情接口的请求间隔。中证官网对它有 WAF —— 实测**突发约 20+
    次连续请求就会返回 403 拦截页**，且会持续约 10–15 分钟。所以这里默认放慢到
    6 秒一次；被拦时通过 ``on_block`` 回调交给调用方决定等多久。
    ``cache_dir``：详情 JSON 的磁盘缓存，重跑时命中缓存不再请求。
    """
    rows = fetch_announcements(max_pages=max_pages, rows=200, sleep_s=sleep_s)
    cands = [r for r in rows if str(r.get("theme")) == ADJUST_THEME]
    cands = [r for r in cands if str(r.get("publishDate", ""))[:4].isdigit()
             and int(str(r["publishDate"])[:4]) >= start_year]
    # 只保留标题里点名目标指数的（省掉大量三板/北证噪声）
    named = [r for r in cands
             if any(k in str(r.get("title", "")) for k in TARGET_INDEXES)]
    out: list[Adjustment] = []
    for i, r in enumerate(named, 1):
        item = None
        for attempt in range(3):
            try:
                item = parse_notice(int(r["id"]), title=str(r.get("title", "")),
                                    announce_date=str(r.get("publishDate", "")),
                                    cache_dir=cache_dir)
                break
            except Exception as exc:  # noqa: BLE001
                blocked = "403" in str(exc)
                if progress:
                    progress(f"  {r.get('id')} 第 {attempt + 1} 次失败"
                             f"{'（疑似 WAF 拦截）' if blocked else ''}")
                if on_block is not None:
                    on_block(exc)
                time.sleep(10.0 if blocked else 3.0)
        if item is None:
            continue
        if item.is_target:
            out.append(item)
        if progress and i % 5 == 0:
            progress(f"  已解析 {i}/{len(named)}，命中 {len(out)}")
        time.sleep(detail_gap_s)
    return out
