"""DeepSeek Fetch 兜底：微信原文页抓不到或表格图片抽不出时，用它取正文。

``POST https://api.deepseek.com/v1/tools/fetch``，body ``{"url": ...}``，Bearer
``DEEPSEEK_API_KEY``，返回 ``{title, content}``。content 是 markdown，表格图片
已被对方识别成文字，但 ``| . - #`` 等字符带反斜杠转义，要先还原再解析。

同一篇文章的 content 有两种排版：

- markdown 表格行 ``| 512480 | 半导体ETF | -0.23 | 0.96 | ... |``；
- 一格一行：代码、名称、6 个数字各占一行。

识别结果会漏格、混进水印（如「公众号：E-55领航者」）。所以只收严格的数字，
格数和表头对不上的行整行丢掉，不去猜错位。分类表常被拆成「名称+当日」和
「当日+5日+20日」两张，按当日数值逐行对齐，对不上的行只保留当日。

全市场汇总和宽基那几张图 DeepSeek 经常不识别，兜底结果多半只有分类和行业 ETF。

只用环境变量 ``DEEPSEEK_API_KEY``，不读文本模型的 ``AI_API_KEY``。开关是
``ETF_FLOW_FETCH_FALLBACK``，默认关闭。单次 60 秒超时，最多再试 1 次。按 5 元 /
1000 次计费，次数记在 ``data/news/deepseek_fetch_usage.json``。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from app.config import settings
from app.market_time import CN_TZ

logger = logging.getLogger(__name__)

FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
FETCH_TIMEOUT = 60.0
FETCH_RETRIES = 1
# 少于这么多字的 content 当成没有识别出表格（只剩标题和作者）。
MIN_CONTENT_CHARS = 400
USAGE_NAME = "deepseek_fetch_usage.json"
WECHAT_HOST = "mp.weixin.qq.com"
# 文章表格统一按亿元印刷，content 里没有单位。和视觉结果的 unit 保持一致。
DEFAULT_UNIT = "亿"

_TRUE = {"1", "true", "yes", "on"}
_ESCAPED = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~=:])")
_STRICT_NUM = re.compile(r"[+-]?\d+(?:\.\d+)?")
_CODE = re.compile(r"\d{6}")
_SN = re.compile(r"[0-9a-f]{32}")
_SN_LOOSE = re.compile(r"[0-9A-Za-z_-]{6,128}")
_BIZ = re.compile(r"[0-9A-Za-z+/]{8,64}={0,2}")
_ETF_COLUMNS = {"1日申赎": "net_1d", "5日申赎": "net_5d", "20日申赎": "net_20d"}
_CAT_COLUMNS = {"申赎额": "net_1d", "五日申赎额": "net_5d", "二十日申赎额": "net_20d"}
_VERTICAL_HEADER = (
    "基金代码",
    "基金名称",
    "1日涨幅",
    "1日申赎",
    "5日涨幅",
    "5日申赎",
    "20日涨幅",
    "20日申赎",
)


class FetchFallbackError(Exception):
    """兜底没有拿到可入库的表格。文案不含密钥和完整地址。"""


def fallback_enabled() -> bool:
    raw = os.environ.get("ETF_FLOW_FETCH_FALLBACK")
    if raw is None or not raw.strip():
        raw = str(getattr(settings, "etf_flow_fetch_fallback", "") or "")
    return raw.strip().lower() in _TRUE


def deepseek_api_key() -> str:
    raw = os.environ.get("DEEPSEEK_API_KEY")
    if raw is not None and raw.strip():
        return raw.strip()
    return str(getattr(settings, "deepseek_api_key", "") or "").strip()


def fallback_ready() -> bool:
    return fallback_enabled() and bool(deepseek_api_key())


def unescape_markdown(text: str) -> str:
    return _ESCAPED.sub(r"\1", text or "")


def sanitize_wechat_url(url: str) -> str:
    """只留 __biz/mid/idx/sn，并去掉粘在参数尾部的杂字（如 sn=...ETF）。

    合格的长链才返回，否则空字符串。sn 是 32 位十六进制时只取前 32 位。
    """
    text = (url or "").strip().split("#", 1)[0]
    parsed = urlparse(text)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != WECHAT_HOST:
        return ""
    if parsed.username or parsed.password or parsed.path != "/s":
        return ""
    query = parse_qs(parsed.query)
    biz = _prefix(_BIZ, (query.get("__biz") or [""])[0])
    mid = _digits((query.get("mid") or [""])[0])
    idx = _digits((query.get("idx") or [""])[0]) or ("1" if mid else "")
    raw_sn = (query.get("sn") or [""])[0].strip()
    sn = (
        _prefix(_SN, raw_sn.lower())
        if re.match(r"[0-9a-fA-F]{32}", raw_sn)
        else _prefix(_SN_LOOSE, raw_sn)
    )
    if not (biz and mid and sn):
        return ""
    return "https://mp.weixin.qq.com/s?" + urlencode(
        [("__biz", biz), ("mid", mid), ("idx", idx), ("sn", sn)]
    )


def _alternate_url(url: str) -> str:
    """同一篇文章的另一种写法（__biz 不做百分号编码）。

    DeepSeek 按地址缓存识别结果，识别失败的空结果也会缓存。重试换写法才会重新识别。
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    pairs = [(key, (query.get(key) or [""])[0]) for key in ("__biz", "mid", "idx", "sn")]
    return "https://mp.weixin.qq.com/s?" + "&".join(
        f"{key}={quote(value, safe='=')}" for key, value in pairs if value
    )


def fetch_article(url: str, client=None, *, data_dir: Path | None = None) -> dict:
    """抓一次正文，返回 ``{"title", "content"}``，content 已还原转义。

    超时、连接失败、5xx 或正文过短时再试 1 次（换一种地址写法）。4xx 不重试。
    """
    key = deepseek_api_key()
    if not key:
        raise FetchFallbackError("未配置 DEEPSEEK_API_KEY")
    target = sanitize_wechat_url(url)
    if not target:
        raise FetchFallbackError("微信文章地址无效")
    own = client is None
    client = client or httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False)
    last = "DeepSeek fetch 失败"
    try:
        for attempt in range(FETCH_RETRIES + 1):
            address = target if attempt == 0 else _alternate_url(target)
            _count(data_dir, "requests")
            try:
                response = client.post(
                    FETCH_URL,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"url": address},
                    timeout=FETCH_TIMEOUT,
                )
                status = int(getattr(response, "status_code", 200) or 200)
                if status >= 400:
                    last = f"DeepSeek fetch http {status}"
                    if status < 500:
                        break
                    continue
                payload = response.json()
            except httpx.TimeoutException:
                last = "DeepSeek fetch 超时"
                continue
            except httpx.HTTPError:
                last = "DeepSeek fetch 连接失败"
                continue
            except (ValueError, TypeError):
                last = "DeepSeek fetch 返回不是 JSON"
                continue
            if not isinstance(payload, dict):
                last = "DeepSeek fetch 返回格式不对"
                continue
            content = unescape_markdown(str(payload.get("content") or ""))
            if len(content) < MIN_CONTENT_CHARS:
                last = "DeepSeek fetch 正文过短"
                continue
            _count(data_dir, "ok")
            return {
                "title": unescape_markdown(str(payload.get("title") or "")).strip(),
                "content": content,
            }
    finally:
        if own:
            client.close()
    _count(data_dir, "failed")
    logger.warning("ETF领航者 DeepSeek fetch 兜底失败: %s", last)
    raise FetchFallbackError(last)


def parse_flow_markdown(content: str) -> dict:
    """把 fetch 正文里的分类表和 ETF 表转成视觉抽取同样的结构（未归一化）。"""
    lines = [line.strip() for line in (content or "").splitlines()]
    categories = _category_rows(lines)
    etfs = _dedupe(_table_etf_rows(lines) + _vertical_etf_rows(lines))
    return {
        "trade_date": "",
        "unit": DEFAULT_UNIT,
        "overview": {"net_1d": None, "net_5d": None, "net_20d": None},
        "broad_index": [],
        "categories": categories,
        "etfs": etfs,
    }


def usage(data_dir: Path | None = None) -> dict:
    return _load_usage(_usage_path(data_dir))


def _cells(line: str) -> list[str] | None:
    if not line.startswith("|"):
        return None
    body = line.strip().strip("|")
    return [cell.strip() for cell in body.split("|")]


def _strict(text: str):
    value = (text or "").strip().replace(",", "").replace("−", "-")
    if not _STRICT_NUM.fullmatch(value):
        return None
    return float(value)


def _category_rows(lines: list[str]) -> list[dict]:
    names: list[dict] = []
    values: list[list] = []
    mode = ""
    for line in lines:
        cells = _cells(line)
        if cells is None:
            if line and not line.startswith("|"):
                mode = "" if mode and _is_caption(line) else mode
            continue
        joined = "".join(cells)
        if "分类" in cells and "申赎额" in cells:
            mode = "names"
            name_col, value_col = cells.index("分类"), cells.index("申赎额")
            continue
        if cells[:3] == ["申赎额", "五日申赎额", "二十日申赎额"]:
            mode = "values"
            continue
        if "基金代码" in joined:
            mode = ""
            continue
        if mode == "names" and len(cells) > max(name_col, value_col):
            name = cells[name_col]
            if name and _strict(name) is None:
                names.append(
                    {
                        "name": name[:40],
                        "net_1d": _strict(cells[value_col]),
                        "net_5d": None,
                        "net_20d": None,
                    }
                )
        elif mode == "values" and len(cells) >= 3:
            values.append([_strict(cell) for cell in cells[:3]])
    # 第二张表可能少行。按当日数值顺序对齐，对不上的名称只留当日。
    cursor = 0
    for row in values:
        day = row[0]
        if day is None:
            continue
        for index in range(cursor, len(names)):
            if names[index]["net_1d"] is not None and abs(names[index]["net_1d"] - day) < 1e-9:
                names[index]["net_5d"] = row[1]
                names[index]["net_20d"] = row[2]
                cursor = index + 1
                break
    return [row for row in names if row["net_1d"] is not None]


def _is_caption(line: str) -> bool:
    return "统计" in line or "资金流" in line


def _table_etf_rows(lines: list[str]) -> list[dict]:
    rows: list[dict] = []
    header: list[str] | None = None
    for line in lines:
        cells = _cells(line)
        if cells is None:
            if line and _is_caption(line):
                header = None
            continue
        if "基金代码" in cells and "基金名称" in cells:
            header = cells
            continue
        if header is None or len(cells) != len(header):
            continue
        row = _etf_row(dict(zip(header, cells, strict=True)))
        if row is not None:
            rows.append(row)
    return rows


def _vertical_etf_rows(lines: list[str]) -> list[dict]:
    """一格一行的排版：表头 8 行，之后每只 ETF 是代码、名称和 6 个数字。"""
    rows: list[dict] = []
    plain = [line for line in lines if line and not line.startswith("|")]
    index = 0
    active = False
    while index < len(plain):
        line = plain[index]
        if tuple(plain[index : index + len(_VERTICAL_HEADER)]) == _VERTICAL_HEADER:
            active = True
            index += len(_VERTICAL_HEADER)
            continue
        if not active:
            index += 1
            continue
        if _CODE.fullmatch(line) and index + 1 < len(plain):
            numbers: list[str] = []
            probe = index + 2
            while (
                probe < len(plain)
                and _strict(plain[probe]) is not None
                and not _CODE.fullmatch(plain[probe])
            ):
                numbers.append(plain[probe])
                probe += 1
            if len(numbers) == len(_VERTICAL_HEADER) - 2:
                record = dict(
                    zip(_VERTICAL_HEADER, [line, plain[index + 1], *numbers], strict=True)
                )
                row = _etf_row(record)
                if row is not None:
                    rows.append(row)
            index = probe
            continue
        if _is_caption(line) or re.fullmatch(r"\d{1,2}月\d{1,2}日", line):
            active = False
        index += 1
    return rows


def _etf_row(record: dict) -> dict | None:
    code = (record.get("基金代码") or "").strip()
    name = (record.get("基金名称") or "").strip()
    if not _CODE.fullmatch(code) or not name or _strict(name) is not None:
        return None
    row = {"name": name[:40], "code": code}
    for label, key in _ETF_COLUMNS.items():
        row[key] = _strict(record.get(label) or "")
    if all(row[key] is None for key in _ETF_COLUMNS.values()):
        return None
    return row


def _dedupe(rows: list[dict]) -> list[dict]:
    """同一张表常被识别两遍。按代码只留第一次出现的行，免得重复行占满 80 行上限。"""
    seen: set[str] = set()
    kept: list[dict] = []
    for row in rows:
        if row["code"] in seen:
            continue
        seen.add(row["code"])
        kept.append(row)
    return kept


def _prefix(pattern: re.Pattern, value: str) -> str:
    match = pattern.match((value or "").strip())
    return match.group(0) if match else ""


def _digits(value: str) -> str:
    match = re.match(r"\d+", (value or "").strip())
    return match.group(0) if match else ""


def _usage_path(data_dir: Path | None) -> Path:
    root = Path(data_dir) if data_dir is not None else Path(settings.data_dir)
    return root / "news" / USAGE_NAME


def _load_usage(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _count(data_dir: Path | None, field: str) -> None:
    """按北京时间日期累计次数，同时记总数。写失败只记日志，不影响采集。"""
    path = _usage_path(data_dir)
    state = _load_usage(path)
    today = datetime.now(CN_TZ).date().isoformat()
    days = state.get("days") if isinstance(state.get("days"), dict) else {}
    day = days.get(today) if isinstance(days.get(today), dict) else {}
    day[field] = int(day.get(field) or 0) + 1
    days[today] = day
    # 只留最近 60 天。
    for stale in sorted(days)[:-60]:
        days.pop(stale, None)
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    total[field] = int(total.get(field) or 0) + 1
    state = {"days": days, "total": total}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("DeepSeek fetch 用量写不进去")
    if field == "requests":
        logger.info(
            "ETF领航者 DeepSeek fetch 请求：今日第 %s 次，累计 %s 次",
            day[field],
            total[field],
        )
