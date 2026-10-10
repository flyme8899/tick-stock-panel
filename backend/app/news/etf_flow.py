"""公众号「ETF领航者」的每日 ETF 申赎。

文章标题像「10月9日ETF基金申购和赎回」，正文是表格图片。优先读网易号
https://www.163.com/dy/media/T1730214999977.html ；列表里没有当天稿件，
或列表请求失败时，才用搜狗微信搜索备份，再打开 mp.weixin.qq.com。
不登录微信。

发布时刻是交易日的次日早晨（实测周五的稿在周六 07:30 左右），所以
「昨天开市」的早晨才期待新稿，并在 09:00 仍缺稿时告警。今天开市但昨天
休市（例如周一）只补抓列表里还没入库的最新一篇，不把「今天没有新稿」
报成故障。交易日历不可用时，周一到周五视作开市。

表格交给视觉模型。密钥只用 VISION_AI_*，不读取文本模型的 AI_API_KEY。
默认模型是 deepseek/deepseek-v4-flash-vision-exp。表格 OCR 保持思考，
max_tokens 至少 8192；关掉思考会让表格识别变差。备选 glm-5.3-flash、
mimo-v2.6-flash 不改默认。一张图一次请求；空内容再试一次。有两份结果时，
正负号不一致的格子留空。
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from app.config import settings
from app.market_time import CN_TZ
from app.news.collectors import Item, parse_time
from app.news.config import vision_api_key, vision_base_url, vision_generation, vision_model
from app.news.extract import StructuredStock
from app.news.host_collector import send_dingtalk

logger = logging.getLogger(__name__)

SOURCE = "etf_flow"
ACCOUNT = "ETF领航者"
NETEASE_MEDIA_URL = "https://www.163.com/dy/media/T1730214999977.html"
NETEASE_HOST = "www.163.com"
SOGOU_SEARCH_URL = "https://weixin.sogou.com/weixin"
WECHAT_HOST = "mp.weixin.qq.com"
POLL_START = dt_time(7, 35)
POLL_END = dt_time(9, 0)
POLL_EVERY = 15 * 60
SOGOU_DAILY_CAP = 4
SOGOU_MIN_GAP = timedelta(minutes=20)
MAX_VISION_IMAGES = 6
# 一张表大约 20 只 ETF。思考占掉 completion 预算，多张图叠在一次请求里会把正文挤空。
VISION_BATCH_SIZE = 1
VISION_EMPTY_RETRIES = 1
MAX_STORED_IMAGES = 12
MAX_BROAD = 20
MAX_CATEGORY = 30
MAX_ETF = 80
_RAW_BUDGET = 18_000

_FLOW_TITLE = re.compile(r"(\d{1,2})月(\d{1,2})日ETF基金申购和赎回")
_DOCID = re.compile(r"^[0-9A-Za-z]{8,24}$")
_IMAGE_HOSTS = {
    "nimg.ws.126.net",
    "dingyue.ws.126.net",
    "mmbiz.qpic.cn",
    "mmbiz.qlogo.cn",
}
_WECHAT_FETCH_HOSTS = {"weixin.sogou.com", WECHAT_HOST}
_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
MISSING_TEXT = "09:00 仍未采集到 ETF领航者当日申赎文章"
MISSING_ALERT = f"TSP 资讯采集：{MISSING_TEXT}。"
EXTRACT_ALERT = "TSP 资讯采集：09:00 仍未完成 ETF领航者当日申赎表格抽取。"
VISION_PROMPT = (
    "你是表格抽取器。下面这张图是公众号「ETF领航者」申赎表中的一张，正文几乎没有文字。\n"
    "只输出一个 JSON 对象，不要解释，不要编造看不清的数字。\n"
    "单位按图中印刷写入 unit（例如亿元、万份），不要把亿元换算成元。\n"
    "净申购用正数，净赎回用负数。看不清就填 null。\n"
    "当日、5日、20日分别写入 net_1d、net_5d、net_20d。\n"
    "overview 是全市场汇总。broad_index 只放指数名称。categories 是行业或主题。\n"
    "带 6 位基金代码的 ETF 写入 etfs，包括宽基 ETF。code 不是 6 位数字就留空字符串。\n"
    '{"trade_date":"YYYY-MM-DD","unit":"","overview":{"net_1d":null,"net_5d":null,"net_20d":null},'
    '"broad_index":[{"name":"","code":"","net_1d":null,"net_5d":null,"net_20d":null}],'
    '"categories":[{"name":"","net_1d":null,"net_5d":null,"net_20d":null}],'
    '"etfs":[{"name":"","code":"","net_1d":null,"net_5d":null,"net_20d":null}]}'
)


class ArticleMissingError(Exception):
    """列表里还没有应采集的申赎稿。"""


class ExtractFailedError(Exception):
    """稿件已出现，但图片或视觉结果不能入库。"""


class EmptyVisionError(ValueError):
    """content 为空。思考占满了 max_tokens，或模型没有写出 JSON。"""


@dataclass(frozen=True)
class ArticleRef:
    article_id: str
    title: str
    url: str
    published_at: datetime
    channel: str


@dataclass(frozen=True)
class ArticlePage:
    article_id: str
    title: str
    url: str
    published_at: datetime
    images: list[str]
    channel: str


def _clock(now: datetime) -> dt_time:
    return now.astimezone(CN_TZ).timetz().replace(tzinfo=None)


def _until(now: datetime, target: datetime) -> int:
    return max(30, int((target - now).total_seconds()))


def etf_wait_seconds(now: datetime, *, pending: bool) -> int:
    """pending 时停在 07:35–09:00 里每 15 分钟再看；否则等到下一个 07:35。"""
    now = now.astimezone(CN_TZ)
    clock = _clock(now)
    start = datetime.combine(now.date(), POLL_START, tzinfo=CN_TZ)
    end = datetime.combine(now.date(), POLL_END, tzinfo=CN_TZ)
    if clock < POLL_START:
        return _until(now, start)
    if pending and clock < POLL_END:
        nxt = now + timedelta(seconds=POLL_EVERY)
        if nxt >= end:
            return _until(now, end)
        return POLL_EVERY
    return _until(now, start + timedelta(days=1))


def day_was_trading(day: date) -> bool | None:
    """周末直接否。工作日只问富尧日历，不问行情探针，避免改写「今天是否开市」的缓存。"""
    if day.weekday() >= 5:
        return False
    return _fuyao_contains(day)


def expects_publication(now: datetime, yesterday_trading: bool | None) -> bool:
    """昨天开市时，今天早晨应有新稿。未知时把周一到周五当成开市。"""
    if yesterday_trading is False:
        return False
    if yesterday_trading is True:
        return True
    yesterday = now.astimezone(CN_TZ).date() - timedelta(days=1)
    return yesterday.weekday() < 5


def is_check_morning(
    now: datetime,
    today_trading: bool | None,
    yesterday_trading: bool | None,
) -> bool:
    """期待新稿的早晨，以及今天开市的早晨（用于补抓）。"""
    if expects_publication(now, yesterday_trading):
        return True
    if today_trading is False:
        return False
    if today_trading is True:
        return True
    return now.astimezone(CN_TZ).weekday() < 5


def _fuyao_contains(day: date) -> bool | None:
    try:
        from app.data_providers import custom as custom_sources

        if not custom_sources.is_custom_provider("fuyao"):
            return None
        provider = custom_sources.get_provider("fuyao")
        days = provider.trading_days()
        if not days:
            return None
        return day in days
    except Exception:  # noqa: BLE001 — 日历失败按未知，不挡住别的资讯源
        return None


def trade_date_from_title(title: str, published: datetime) -> str:
    match = _FLOW_TITLE.search(_plain(title))
    if not match:
        return ""
    month, day = int(match.group(1)), int(match.group(2))
    year = published.astimezone(CN_TZ).year
    try:
        guessed = date(year, month, day)
    except ValueError:
        return ""
    if guessed > published.astimezone(CN_TZ).date():
        try:
            guessed = date(year - 1, month, day)
        except ValueError:
            return ""
    return guessed.isoformat()


def apply_trade_date(title: str, published: datetime, extracted: dict) -> dict:
    """标题里的月日是交易日，优先于模型自己填的日期。"""
    out = dict(extracted)
    derived = trade_date_from_title(title, published)
    if derived:
        out["trade_date"] = derived
    return out


def choose_flow_article(
    refs: list[ArticleRef],
    *,
    day: date,
    expect: bool,
) -> tuple[ArticleRef | None, str]:
    """期待新稿时不拿旧稿充数。不期待时用最新一篇做补抓。"""
    flows = [ref for ref in refs if _FLOW_TITLE.search(_plain(ref.title))]
    today = [ref for ref in flows if _on_day(ref.published_at, day)]
    if today:
        today.sort(key=lambda ref: ref.published_at, reverse=True)
        return today[0], "today"
    if expect or not flows:
        return None, "missing"
    flows.sort(key=lambda ref: ref.published_at, reverse=True)
    return flows[0], "catchup"


def parse_netease_media(html_text: str) -> list[ArticleRef]:
    refs: list[ArticleRef] = []
    for block in re.findall(r'<li class="js-item item">(.*?)</li>', html_text, re.S):
        link = re.search(
            r'href="(https://www\.163\.com/dy/article/([0-9A-Za-z]+)\.html[^"]*)"',
            block,
        )
        title_match = re.search(r'class="title"[^>]*>([^<]+)</a>', block)
        time_match = re.search(r'class="time"[^>]*>([^<]+)</span>', block)
        if not link or not title_match or not time_match:
            continue
        docid = link.group(2)
        published = parse_time(html.unescape(time_match.group(1)).strip())
        if published is None or not _DOCID.match(docid):
            continue
        refs.append(
            ArticleRef(
                article_id=docid,
                title=html.unescape(title_match.group(1)).strip(),
                url=f"https://www.163.com/dy/article/{docid}.html",
                published_at=published,
                channel="netease",
            )
        )
    return refs


def parse_netease_article(html_text: str, ref: ArticleRef) -> ArticlePage:
    doc = re.search(r'data-docid="([0-9A-Za-z]+)"', html_text)
    if doc and doc.group(1) != ref.article_id:
        raise ExtractFailedError("文章 id 不一致")
    title = _h1(html_text) or ref.title
    if not _FLOW_TITLE.search(_plain(title)):
        raise ExtractFailedError("文章标题不是申购赎回")
    body = re.search(r'<div class="post_body">(.*?)</div>', html_text, re.S)
    images = _image_urls(body.group(1) if body else "")
    return ArticlePage(
        article_id=ref.article_id,
        title=title,
        url=ref.url,
        published_at=ref.published_at,
        images=images[:MAX_STORED_IMAGES],
        channel="netease",
    )


def parse_sogou_account(html_text: str, now: datetime) -> list[ArticleRef]:
    if "antispider" in html_text or "请输入验证码" in html_text:
        raise RuntimeError("搜狗微信搜索需要验证码")
    refs: list[ArticleRef] = []
    for block in re.findall(r"<li\b[^>]*>(.*?)</li>", html_text, re.S | re.I):
        anchors = re.findall(r"<a\b[^>]*>(.*?)</a>", block, re.S | re.I)
        if ACCOUNT not in {_plain(item) for item in anchors}:
            continue
        for match in re.finditer(
            r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            re.S | re.I,
        ):
            title = _plain(match.group(2))
            if not _FLOW_TITLE.search(title):
                continue
            href = _normalize_sogou_href(match.group(1))
            if not href:
                continue
            published = _sogou_time(block[match.end() : match.end() + 240], now)
            if published is None:
                continue
            refs.append(
                ArticleRef(
                    article_id="",
                    title=title,
                    url=href,
                    published_at=published,
                    channel="wechat",
                )
            )
    return refs


def parse_wechat_article(html_text: str, final_url: str, ref: ArticleRef) -> ArticlePage:
    article_id = wechat_article_id(final_url)
    if not article_id:
        raise ExtractFailedError("微信文章缺少 id")
    title = _activity_title(html_text) or ref.title
    if not _FLOW_TITLE.search(_plain(title)):
        raise ExtractFailedError("文章标题不是申购赎回")
    fragment = _region(
        html_text,
        'id="js_content"',
        ('id="js_tags"', 'id="js_pc_qr_code"', 'class="rich_media_tool"'),
    )
    return ArticlePage(
        article_id=article_id,
        title=title,
        url=_clean_wechat_url(final_url),
        published_at=ref.published_at,
        images=_image_urls(fragment)[:MAX_STORED_IMAGES],
        channel="wechat",
    )


def _clean_wechat_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    pairs = []
    for key in ("__biz", "mid", "idx", "sn"):
        values = query.get(key)
        if values and values[0]:
            pairs.append((key, values[0]))
    if not pairs:
        return url.split("#", 1)[0][:500]
    return "https://mp.weixin.qq.com/s?" + urlencode(pairs)


def wechat_article_id(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    sn = (query.get("sn") or [""])[0].strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{6,128}", sn):
        return sn
    mid = (query.get("mid") or [""])[0].strip()
    if mid.isdigit():
        return mid
    return ""


def vision_payload(images: list[str], *, model: str) -> dict:
    """一次请求只放一张表。表格 OCR 不关思考。"""
    content: list[dict] = [{"type": "text", "text": VISION_PROMPT}]
    kept = 0
    for url in images:
        normalized = _normalize_image(url)
        if not normalized:
            continue
        content.append({"type": "image_url", "image_url": {"url": normalized}})
        kept += 1
        if kept >= VISION_BATCH_SIZE:
            break
    body = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "user", "content": content}],
    }
    body.update(vision_generation(model, ocr=True))
    return body


def _message_text(payload: dict) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("视觉模型没有返回内容")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        content = "".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise EmptyVisionError("视觉模型没有返回内容")
    return content


def parse_vision_response(payload: dict) -> dict:
    return normalize_flow(parse_json_object(_message_text(payload)))


def parse_json_object(text: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", text.strip())
    raw = re.sub(r"\s*```$", "", raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("视觉模型没有返回 JSON") from None
        payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("视觉模型返回的不是对象")
    return payload


def normalize_flow(payload: dict) -> dict:
    payload = _canonicalize_payload(payload)
    overview = _overview(payload.get("overview"))
    broad = _rows(payload.get("broad_index"), MAX_BROAD + MAX_ETF, with_code=True)
    categories = _rows(payload.get("categories"), MAX_CATEGORY, with_code=False)
    etfs = _rows(payload.get("etfs"), MAX_ETF, with_code=True)
    kept_broad: list[dict] = []
    for row in broad:
        if _is_fund_code(str(row.get("code") or "")):
            etfs.append(row)
        else:
            kept_broad.append(row)
    extracted = {
        "trade_date": _trade_date(payload.get("trade_date")),
        "unit": _unit(payload.get("unit")),
        "overview": overview,
        "broad_index": kept_broad[:MAX_BROAD],
        "categories": categories,
        "etfs": _dedupe_rows(etfs)[:MAX_ETF],
    }
    if not _has_number(extracted):
        raise ValueError("视觉模型没有抽出申赎数字")
    return _shrink(extracted)


def agree_flow_signs(left: dict, right: dict) -> dict:
    """两次抽取对同一格的正负号相反时，该格留空。只比较符号，不要求数值相等。"""
    overview = _agree_fields(left.get("overview") or {}, right.get("overview") or {})
    extracted = {
        "trade_date": left.get("trade_date") or right.get("trade_date") or "",
        "unit": left.get("unit") or right.get("unit") or "",
        "overview": overview,
        "broad_index": _agree_rows(left.get("broad_index") or [], right.get("broad_index") or []),
        "categories": _agree_rows(left.get("categories") or [], right.get("categories") or []),
        "etfs": _agree_rows(left.get("etfs") or [], right.get("etfs") or []),
    }
    if not _has_number(extracted):
        raise ValueError("两次结果正负号不一致")
    return _shrink(extracted)


def merge_flows(parts: list[dict]) -> dict:
    """把每张表的抽取结果按代码拼起来。同一只基金只留先看到的一行。"""
    if not parts:
        raise ValueError("视觉模型没有抽出申赎数字")
    if len(parts) == 1:
        return parts[0]
    overview = {"net_1d": None, "net_5d": None, "net_20d": None}
    trade_date = ""
    unit = ""
    buckets: dict[str, list] = {key: [] for key in ("broad_index", "categories", "etfs")}
    for part in parts:
        if not trade_date and part.get("trade_date"):
            trade_date = str(part["trade_date"])
        if not unit and part.get("unit"):
            unit = str(part["unit"])
        part_overview = part.get("overview") or {}
        for key in overview:
            if overview[key] is None and part_overview.get(key) is not None:
                overview[key] = part_overview[key]
        for key in buckets:
            buckets[key].extend(part.get(key) or [])
    extracted = {
        "trade_date": trade_date,
        "unit": unit,
        "overview": overview,
        "broad_index": _dedupe_rows(buckets["broad_index"])[:MAX_BROAD],
        "categories": _dedupe_rows(buckets["categories"])[:MAX_CATEGORY],
        "etfs": _dedupe_rows(buckets["etfs"])[:MAX_ETF],
    }
    if not _has_number(extracted):
        raise ValueError("视觉模型没有抽出申赎数字")
    return _shrink(extracted)


def flow_mentions(extracted: dict) -> tuple[list[StructuredStock], list[str]]:
    sectors: list[str] = []
    for row in list(extracted.get("broad_index") or []) + list(extracted.get("categories") or []):
        name = str(row.get("name") or "").strip()
        if name and name not in sectors:
            sectors.append(name)
        if len(sectors) >= 6:
            break
    stocks: list[StructuredStock] = []
    for row in _ranked_etfs(extracted):
        code = str(row.get("code") or "")
        if not code:
            continue
        stocks.append(StructuredStock(name=str(row.get("name") or code), code=code))
        if len(stocks) >= 8:
            break
    return stocks, sectors


def flow_summary(title: str, extracted: dict) -> str:
    unit = str(extracted.get("unit") or "")
    overview = extracted.get("overview") or {}
    lines = [title]
    if extracted.get("trade_date"):
        lines.append(f"交易日 {extracted['trade_date']}")
    lines.append(
        "全市场净申购 当日 {day}，5日 {five}，20日 {twenty}".format(
            day=_fmt(overview.get("net_1d"), unit),
            five=_fmt(overview.get("net_5d"), unit),
            twenty=_fmt(overview.get("net_20d"), unit),
        )
    )
    broad = _bits(list(extracted.get("broad_index") or [])[:6], unit, with_code=False)
    categories = _bits(list(extracted.get("categories") or [])[:6], unit, with_code=False)
    etfs = _bits(_ranked_etfs(extracted)[:8], unit, with_code=True)
    if broad:
        lines.append("宽基 " + "；".join(broad))
    if categories:
        lines.append("分类 " + "；".join(categories))
    if etfs:
        lines.append("ETF " + "；".join(etfs))
    return "\n".join(lines)[:4000]


def sogou_begin(path: Path, now: datetime) -> bool:
    """一次备份算一次。一天最多 4 次，两次至少隔 20 分钟。"""
    now = now.astimezone(CN_TZ)
    state = _load_budget(path)
    day = now.date().isoformat()
    if state.get("date") != day:
        state = {"date": day, "count": 0, "last_at": ""}
    try:
        count = int(state.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count >= SOGOU_DAILY_CAP:
        return False
    if state.get("last_at"):
        last = parse_time(state.get("last_at"))
        if last is None or now - last < SOGOU_MIN_GAP:
            return False
    state["count"] = count + 1
    state["last_at"] = now.isoformat(timespec="seconds")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return False
    return True


def collect_etf_flow(
    client: httpx.Client | None = None,
    *,
    now: datetime | None = None,
    today_trading: bool | None = None,
    yesterday_trading: bool | None = None,
    data_dir: Path | None = None,
) -> dict:
    now = (now or _cn_now()).astimezone(CN_TZ)
    root = data_dir or settings.data_dir
    if _clock(now) < POLL_START:
        return _result(skipped=True)
    today_flag, yesterday_flag = _trading_flags(now, today_trading, yesterday_trading)
    expect = expects_publication(now, yesterday_flag)
    if not is_check_morning(now, today_flag, yesterday_flag):
        return _result(skipped=True)
    if not vision_api_key():
        _mark_err("未配置 VISION_AI_API_KEY")
        return _result(skipped=True, error="未配置 VISION_AI_API_KEY")
    if _published_on(now.date()):
        _mark_ok()
        return _result(found=True)
    own = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=False)
    try:
        return _pull(client, now, root, expect)
    except ArticleMissingError:
        return _missing(now, root, expect)
    except ExtractFailedError as exc:
        message = _public_error(exc) or "表格抽取失败"
        logger.warning("ETF领航者表格抽取失败: %s", message)
        _mark_err(message)
        if _deadline(now):
            _alert(root, now.date(), EXTRACT_ALERT)
            return _result(error=message)
        return _result(error=message, pending=True)
    except Exception as exc:  # noqa: BLE001 — 单源失败不能拖垮其他资讯轮询
        message = _public_error(exc) or "采集失败"
        logger.warning("ETF领航者采集失败: %s", message)
        _mark_err(message)
        if _deadline(now) and expect:
            _alert(root, now.date(), MISSING_ALERT)
            return _result(error=message)
        return _result(error=message, pending=not _deadline(now))
    finally:
        if own:
            client.close()


def _pull(client, now: datetime, root: Path, expect: bool) -> dict:
    ref, _reason = _resolve_ref(client, now, root, expect)
    if ref.channel == "netease" and ref.article_id and _known(f"netease:{ref.article_id}"):
        _mark_ok()
        return _result(found=True)
    page = _load_page(client, ref)
    source_id = f"{page.channel}:{page.article_id}"
    if _known(source_id):
        _mark_ok()
        return _result(found=True)
    if not page.images:
        raise ExtractFailedError("文章没有可抽取的表格图片")
    extracted = apply_trade_date(page.title, page.published_at, _call_vision(client, page.images))
    stocks, sectors = flow_mentions(extracted)
    images = page.images[:MAX_STORED_IMAGES]
    raw = _fit_raw(
        {
            "article_id": page.article_id,
            "channel": page.channel,
            "trade_date": extracted.get("trade_date") or "",
            "images": images,
            "extracted": extracted,
        }
    )
    from app.news.service import ingest_items

    saved = ingest_items(
        [
            Item(
                source=SOURCE,
                source_id=source_id,
                published_at=page.published_at,
                author=ACCOUNT,
                title=page.title,
                text=flow_summary(page.title, extracted),
                url=page.url,
                level="flow",
                media_ids=images,
                stocks=stocks,
                sectors=sectors,
                raw=raw,
            )
        ]
    )
    _mark_ok()
    return _result(
        found=True,
        inserted=int(saved.get("inserted") or 0),
        duplicate=int(saved.get("duplicate") or 0),
    )


def _resolve_ref(client, now: datetime, root: Path, expect: bool) -> tuple[ArticleRef, str]:
    refs, netease_error = _netease_refs(client)
    chosen, reason = choose_flow_article(refs, day=now.date(), expect=expect)
    if chosen is not None and (reason == "today" or not expect):
        return chosen, reason
    budget = root / "news" / "etf_sogou_budget.json"
    if not sogou_begin(budget, now):
        if netease_error is not None and chosen is None:
            raise netease_error
        raise ArticleMissingError()
    try:
        backup = parse_sogou_account(_sogou_html(client), now)
    except Exception as exc:
        if netease_error is not None:
            raise RuntimeError(f"{_public_error(netease_error)}；{_public_error(exc)}") from exc
        raise
    backup_chosen, backup_reason = choose_flow_article(backup, day=now.date(), expect=expect)
    if backup_chosen is not None and (backup_reason == "today" or not expect):
        return backup_chosen, backup_reason
    if netease_error is not None:
        raise netease_error
    raise ArticleMissingError()


def _netease_refs(client) -> tuple[list[ArticleRef], Exception | None]:
    try:
        html_text, _final = _fetch(client, NETEASE_MEDIA_URL, allow_hosts={NETEASE_HOST})
    except Exception as exc:  # noqa: BLE001 — 主源失败改走搜狗，不在这里结束
        return [], exc
    return parse_netease_media(html_text), None


def _sogou_html(client) -> str:
    html_text, _final = _fetch(
        client,
        SOGOU_SEARCH_URL,
        allow_hosts={"weixin.sogou.com"},
        params={"type": "1", "query": ACCOUNT, "ie": "utf8"},
    )
    return html_text


def _load_page(client, ref: ArticleRef) -> ArticlePage:
    if ref.channel == "netease":
        html_text, final = _fetch(client, ref.url, allow_hosts={NETEASE_HOST})
        if (urlparse(final).hostname or "").lower() != NETEASE_HOST:
            raise ExtractFailedError("网易文章地址无效")
        return parse_netease_article(html_text, ref)
    html_text, final = _fetch(client, ref.url, allow_hosts=_WECHAT_FETCH_HOSTS)
    if (urlparse(final).hostname or "").lower() != WECHAT_HOST:
        raise ExtractFailedError("微信文章地址无效")
    return parse_wechat_article(html_text, final, ref)


def _call_vision(client, images: list[str]) -> dict:
    usable = [url for url in images if _normalize_image(url)][:MAX_VISION_IMAGES]
    if not usable:
        raise ExtractFailedError("文章没有可抽取的表格图片")
    key = vision_api_key()
    if not key:
        raise ExtractFailedError("未配置 VISION_AI_API_KEY")
    parts: list[dict] = []
    saw_empty = False
    for url in usable:
        parsed, empty = _vision_batch(client, [url], key)
        saw_empty = saw_empty or empty
        if parsed is not None:
            parts.append(parsed)
    if not parts:
        if saw_empty:
            raise ExtractFailedError("视觉模型没有返回内容")
        raise ExtractFailedError("视觉模型没有抽出申赎数字")
    try:
        return merge_flows(parts)
    except (ValueError, KeyError, TypeError) as exc:
        raise ExtractFailedError("视觉模型没有抽出申赎数字") from exc


def _vision_batch(client, images: list[str], key: str) -> tuple[dict | None, bool]:
    """空内容再试一次。第一次已有数字时，再用一次结果核对正负号。"""
    first, empty = _read_vision(client, images, key)
    if first is None:
        if not empty:
            return None, False
        second = None
        for _extra in range(VISION_EMPTY_RETRIES):
            logger.warning("ETF申赎视觉模型返回空内容，重试一次")
            second, _again = _read_vision(client, images, key)
            if second is not None:
                break
        if second is None:
            logger.warning("ETF申赎视觉模型重试后仍无内容")
            return None, True
        return second, True
    try:
        second, confirm_empty = _read_vision(client, images, key)
    except ExtractFailedError as exc:
        logger.warning("ETF申赎正负号确认失败，沿用第一次结果: %s", _public_error(exc))
        return first, empty
    if second is None:
        logger.warning("ETF申赎正负号确认没有内容，沿用第一次结果")
        return first, True
    try:
        return agree_flow_signs(first, second), empty or confirm_empty
    except ValueError:
        logger.warning("ETF申赎两次结果正负号不一致")
        return None, empty or confirm_empty


def _read_vision(client, images: list[str], key: str) -> tuple[dict | None, bool]:
    payload = _post_vision(client, vision_payload(images, model=vision_model()), key)
    try:
        return parse_vision_response(payload), False
    except EmptyVisionError:
        return None, True
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ExtractFailedError("视觉模型没有抽出申赎数字") from exc


def _post_vision(client, body: dict, key: str) -> dict:
    try:
        response = client.post(
            f"{vision_base_url()}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
        )
        raiser = getattr(response, "raise_for_status", None)
        if raiser:
            raiser()
        payload = response.json()
    except ExtractFailedError:
        raise
    except Exception as exc:
        logger.warning("ETF申赎视觉模型失败: %s", _public_error(exc))
        raise ExtractFailedError("视觉模型请求失败") from exc
    if not isinstance(payload, dict):
        raise ExtractFailedError("视觉模型没有抽出申赎数字")
    return payload


def _missing(now: datetime, root: Path, expect: bool) -> dict:
    if not _deadline(now):
        return _result(pending=True)
    if not expect:
        return _result()
    logger.warning("ETF领航者当日文章缺失")
    _mark_err(MISSING_TEXT)
    _alert(root, now.date(), MISSING_ALERT)
    return _result()


def _fetch(
    client,
    url: str,
    *,
    allow_hosts: set[str],
    params: dict | None = None,
) -> tuple[str, str]:
    current = url
    query = params
    for _hop in range(4):
        _check_url(current, allow_hosts)
        response = client.get(current, params=query, headers=_headers(current))
        query = None
        status = int(getattr(response, "status_code", 200) or 200)
        if status in {301, 302, 303, 307, 308}:
            location = _location(response)
            if not location:
                raise RuntimeError("重定向缺少地址")
            current = urljoin(current, location)
            continue
        if status >= 400:
            raiser = getattr(response, "raise_for_status", None)
            if raiser:
                raiser()
            raise RuntimeError(f"http {status}")
        text = getattr(response, "text", "") or ""
        return text[:2_000_000], current
    raise RuntimeError("重定向次数过多")


def _check_url(url: str, allow_hosts: set[str]) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or host not in allow_hosts:
        raise RuntimeError("地址不在允许的站点里")


def _headers(url: str) -> dict[str, str]:
    headers = dict(_BROWSER)
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("sogou.com"):
        headers["Referer"] = "https://weixin.sogou.com/"
    elif host.endswith("163.com"):
        headers["Referer"] = NETEASE_MEDIA_URL
    return headers


def _location(response) -> str:
    headers = getattr(response, "headers", None) or {}
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    return str(getter("location") or getter("Location") or "")


def _alert(root: Path, day: date, message: str) -> None:
    path = root / "news" / "health" / f"alert-etf_flow-{day.isoformat()}.txt"
    if path.is_file():
        return
    try:
        send_dingtalk(settings.dingtalk_webhook_url, settings.dingtalk_secret, message)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ETF申赎提醒发送失败: %s", _public_error(exc))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(day.isoformat(), encoding="utf-8")


def _trading_flags(
    now: datetime,
    today_trading: bool | None,
    yesterday_trading: bool | None,
) -> tuple[bool | None, bool | None]:
    if today_trading is None:
        today_trading = day_was_trading(now.date())
    if yesterday_trading is None:
        yesterday_trading = day_was_trading(now.date() - timedelta(days=1))
    return today_trading, yesterday_trading


def _deadline(now: datetime) -> bool:
    return _clock(now) >= POLL_END


def _result(
    *,
    found: bool = False,
    pending: bool = False,
    skipped: bool = False,
    inserted: int = 0,
    duplicate: int = 0,
    error: str = "",
) -> dict:
    payload = {
        "found": found,
        "pending": pending,
        "inserted": inserted,
        "duplicate": duplicate,
    }
    if skipped:
        payload["skipped"] = True
    if error:
        payload["error"] = error
    return payload


def _published_on(day: date) -> bool:
    from app.news.service import get_store

    for row in get_store().recent_for_feed(SOURCE, 8):
        published = parse_time(row["published_at"])
        if published is not None and _on_day(published, day):
            return True
    return False


def _known(source_id: str) -> bool:
    from app.news.service import get_store

    return bool(source_id) and source_id in get_store().existing_ids(SOURCE, [source_id])


def _mark_ok() -> None:
    from app.news.service import get_store

    get_store().mark_health(SOURCE, ok=True, auth_state="ok")


def _mark_err(message: str) -> None:
    from app.news.service import get_store

    get_store().mark_health(SOURCE, ok=False, error=message, auth_state="n/a")


def _cn_now() -> datetime:
    from app.market_time import cn_now

    return cn_now()


def _load_budget(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _public_error(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ")
    key = vision_api_key()
    if key and len(key) >= 6:
        text = text.replace(key, "***")
    text = re.sub(r"Bearer\s+\S+", "Bearer ***", text)
    return text[:200]


def _on_day(when: datetime, day: date) -> bool:
    if when.tzinfo is None:
        when = when.replace(tzinfo=CN_TZ)
    return when.astimezone(CN_TZ).date() == day


def _plain(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment or "")
    return html.unescape(re.sub(r"\s+", "", text))


def _h1(html_text: str) -> str:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.S)
    if not match:
        return ""
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1)))).strip()


def _activity_title(html_text: str) -> str:
    match = re.search(r'id="activity-name"[^>]*>(.*?)</h1>', html_text, re.S)
    if match:
        title = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1)))).strip()
        if title:
            return title
    og = re.search(r'property="og:title"\s+content="([^"]+)"', html_text)
    return html.unescape(og.group(1)).strip() if og else ""


def _region(html_text: str, start: str, ends: tuple[str, ...]) -> str:
    pos = html_text.find(start)
    if pos < 0:
        return ""
    pos += len(start)
    end = len(html_text)
    for marker in ends:
        found = html_text.find(marker, pos)
        if found != -1:
            end = min(end, found)
    return html_text[pos:end]


def _image_urls(fragment: str) -> list[str]:
    found: list[str] = []
    for tag in re.findall(r"<img\b[^>]*>", fragment, re.I):
        data = re.search(r"""data-src\s*=\s*["']([^"']+)["']""", tag, re.I)
        src = re.search(r"""src\s*=\s*["']([^"']+)["']""", tag, re.I)
        raw = data.group(1) if data else (src.group(1) if src else "")
        url = _normalize_image(raw)
        if url and url not in found:
            found.append(url)
    return found


def _normalize_image(raw: str) -> str:
    text = html.unescape((raw or "").strip())
    if text.startswith("//"):
        text = "https:" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or host not in _IMAGE_HOSTS:
        return ""
    return text


def _normalize_sogou_href(href: str) -> str:
    text = html.unescape((href or "").strip())
    if text.startswith("//"):
        text = "https:" + text
    elif text.startswith("/"):
        text = "https://weixin.sogou.com" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in _WECHAT_FETCH_HOSTS:
        return ""
    return text


def _sogou_time(text: str, now: datetime) -> datetime | None:
    epoch = re.search(r"timeConvert\('(\d{10})'\)", text)
    if epoch:
        return datetime.fromtimestamp(int(epoch.group(1)), CN_TZ)
    full = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if full:
        return _safe_dt(int(full.group(1)), int(full.group(2)), int(full.group(3)))
    month_day = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if month_day:
        return _safe_dt(
            now.astimezone(CN_TZ).year, int(month_day.group(1)), int(month_day.group(2))
        )
    parsed = parse_time(text.strip())
    return parsed


def _safe_dt(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day, 7, 30, tzinfo=CN_TZ)
    except ValueError:
        return None


def _canonicalize_payload(payload: dict) -> dict:
    data = dict(payload)
    if not isinstance(data.get("broad_index"), list) and isinstance(data.get("broad"), list):
        data["broad_index"] = data["broad"]
    if not isinstance(data.get("etfs"), list):
        for key in ("broad_etfs", "rows", "items"):
            if isinstance(data.get(key), list):
                data["etfs"] = data[key]
                break
    if not isinstance(data.get("overview"), dict) and any(
        key in data for key in ("day", "d5", "d20", "net_1d", "net_5d", "net_20d")
    ):
        data["overview"] = {
            "net_1d": data.get("net_1d", data.get("day")),
            "net_5d": data.get("net_5d", data.get("d5")),
            "net_20d": data.get("net_20d", data.get("d20")),
        }
    return data


def _overview(value) -> dict:
    row = value if isinstance(value, dict) else {}
    return {
        "net_1d": _pick_num(row, "net_1d", "day", "d1"),
        "net_5d": _pick_num(row, "net_5d", "d5"),
        "net_20d": _pick_num(row, "net_20d", "d20"),
    }


def _rows(value, limit: int, *, with_code: bool) -> list[dict]:
    rows: list[dict] = []
    if not isinstance(value, list):
        return rows
    for item in value:
        if len(rows) >= limit or not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:40]
        if not name:
            continue
        row = {
            "name": name,
            "net_1d": _pick_num(item, "net_1d", "day", "d1"),
            "net_5d": _pick_num(item, "net_5d", "d5"),
            "net_20d": _pick_num(item, "net_20d", "d20"),
        }
        if with_code:
            row["code"] = _code(item.get("code"))
        if row["net_1d"] is None and row["net_5d"] is None and row["net_20d"] is None:
            continue
        rows.append(row)
    return rows


def _direction(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return 0
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _signs_disagree(left, right) -> bool:
    left_sign = _direction(left)
    right_sign = _direction(right)
    if left_sign == 0 or right_sign == 0:
        return False
    return left_sign != right_sign


def _pick_num(row: dict, *keys: str):
    found = []
    for key in keys:
        if key not in row:
            continue
        number = _num(row.get(key))
        if number is not None:
            found.append(number)
    if not found:
        return None
    if any(_signs_disagree(found[0], item) for item in found[1:]):
        return None
    return found[0]


def _agree_fields(left: dict, right: dict) -> dict:
    agreed = {}
    for key in ("net_1d", "net_5d", "net_20d"):
        lv, rv = left.get(key), right.get(key)
        if _signs_disagree(lv, rv):
            agreed[key] = None
        elif lv is not None:
            agreed[key] = lv
        else:
            agreed[key] = rv
    return agreed


def _row_identity(row: dict) -> str:
    code = str(row.get("code") or "")
    if re.fullmatch(r"\d{6}", code):
        return f"c:{code}"
    return f"n:{row.get('name') or ''}"


def _agree_one(left: dict, right: dict) -> dict:
    merged: dict = {"name": left.get("name") or right.get("name") or ""}
    if "code" in left or "code" in right:
        code = str(left.get("code") or right.get("code") or "")
        merged["code"] = code if re.fullmatch(r"\d{6}", code) else ""
    merged.update(_agree_fields(left, right))
    return merged


def _row_has_flow(row: dict) -> bool:
    return any(row.get(key) is not None for key in ("net_1d", "net_5d", "net_20d"))


def _agree_rows(left_rows: list, right_rows: list) -> list[dict]:
    right_map: dict[str, dict] = {}
    for row in right_rows:
        if isinstance(row, dict):
            right_map.setdefault(_row_identity(row), row)
    seen: set[str] = set()
    agreed: list[dict] = []
    for row in left_rows:
        if not isinstance(row, dict):
            continue
        key = _row_identity(row)
        seen.add(key)
        other = right_map.get(key)
        merged = _agree_one(row, other) if other is not None else row
        if _row_has_flow(merged):
            agreed.append(merged)
    for row in right_rows:
        if not isinstance(row, dict):
            continue
        key = _row_identity(row)
        if key in seen or not _row_has_flow(row):
            continue
        agreed.append(row)
    return agreed


def _is_fund_code(code: str) -> bool:
    """沪深 ETF 基金代码。指数代码（如 000300）留在宽基名称里，不进个股提及。"""
    return bool(re.fullmatch(r"(?:15|16|51|56|58)\d{4}", code))


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    kept: list[dict] = []
    for row in rows:
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")
        key = f"c:{code}" if code else f"n:{name}"
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return kept


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    text = str(value).strip().replace(",", "").replace("，", "").replace("−", "-")
    if text.lower() in {"", "null", "none", "n/a", "-", "—", "nan"}:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group(0))
    if number < 0:
        return number
    if "赎回" in text and "申购" not in text:
        return -number
    return number


def _code(value) -> str:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value or ""))
    return match.group(1) if match else ""


def _trade_date(value) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return ""
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


def _unit(value) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 8 or any(ch in text for ch in "\n\r{}[]"):
        return ""
    return text


def _has_number(extracted: dict) -> bool:
    overview = extracted.get("overview") or {}
    if any(overview.get(key) is not None for key in ("net_1d", "net_5d", "net_20d")):
        return True
    for key in ("broad_index", "categories", "etfs"):
        for row in extracted.get(key) or []:
            if any(row.get(field) is not None for field in ("net_1d", "net_5d", "net_20d")):
                return True
    return False


def _shrink(extracted: dict) -> dict:
    etfs = extracted.get("etfs")
    if not isinstance(etfs, list):
        return extracted
    while etfs and len(json.dumps(extracted, ensure_ascii=False)) > _RAW_BUDGET:
        etfs.sort(key=_abs_flow)
        etfs.pop(0)
    extracted["etfs"] = etfs
    return extracted


def _ranked_etfs(extracted: dict) -> list[dict]:
    rows = list(extracted.get("etfs") or [])
    rows.sort(key=_abs_flow, reverse=True)
    return rows


def _abs_flow(row: dict) -> float:
    value = row.get("net_1d")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return abs(float(value))
    return -1.0


def _bits(rows: list[dict], unit: str, *, with_code: bool) -> list[str]:
    bits = []
    for row in rows:
        name = str(row.get("name") or "")
        if with_code and row.get("code"):
            name = f"{name}({row['code']})"
        bits.append(f"{name} 当日 {_fmt(row.get('net_1d'), unit)}")
    return bits


def _fmt(value, unit: str) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        text = "0"
    return f"{text}{unit}"


def _fit_raw(raw: dict) -> dict:
    clone = json.loads(json.dumps(raw, ensure_ascii=False))
    extracted = clone.get("extracted")
    if isinstance(extracted, dict):
        clone["extracted"] = _shrink(extracted)
    return clone
