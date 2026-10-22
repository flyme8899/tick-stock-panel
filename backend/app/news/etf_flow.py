"""公众号「ETF领航者」的每日 ETF 申赎。

文章标题像「10月9日ETF基金申购和赎回」，正文是表格图片。标题里的日期是
交易日，文章在次日早晨约 07:xx 发布。主路径是搜狗微信文章搜索（type=2），
查询「ETF领航者 + 当天应有的标题」。账号搜索（type=1）从这个服务器 IP
搜出来是空的，不再用于当天采集。网易号列表经常晚几个小时，只在搜狗没有
这篇、额度用完或搜狗请求失败时兜底。不登录微信。

昨天开市时，07:30–08:30 每 10 分钟看一次，08:30–15:00 每 30 分钟再看。
11:00 仍缺先发「暂缺」，15:00 仍缺才发失败提醒。已经发过暂缺或失败、
后来把对应交易日的稿入库（含回补和原文链接）时，再发一条「已补采」。
今天开市但昨天休市（例如周一）只补抓列表里还没入库的最新一篇，不把
「今天没有新稿」报成故障。交易日历不可用时，周一到周五视作开市。

也可以把 mp.weixin.qq.com 原文链接交进来补采。短链 /s/xxx 没有 sn，
要先还原成带 __biz/mid/idx/sn 的长链，否则解析会报「缺少 id」。

表格交给视觉模型。密钥只用 VISION_AI_*，不读取文本模型的 AI_API_KEY。
默认模型是 deepseek/deepseek-v4-flash-vision-exp。表格 OCR 保持思考，
max_tokens 至少 8192；关掉思考会让表格识别变差。glm-5.3-flash 的思考
不能关，默认力度是 max，会把预算占满导致 content 为空，所以对它传
reasoning_effort=low，并把上限提到 16384。空内容再试一次；若是长度截断，
再把 max_tokens 提高。content 为空但 reasoning_content 里有 JSON 时用那段。
主模型空内容、解析失败或 HTTP 错误后，换一个备用模型再试。超时和连接失败
不换模型。有两份结果时，正负号不一致的格子留空。

页面和图片下载大约 60 秒超时。视觉请求的读超时单独放到 240 秒，连接超时
保持较短。读超时再试 2 次。某一张图仍超时就跳过，已经抽出的其他图继续合并；
正负号确认超时则沿用第一次结果。全部图片都超时才算抽取失败。

搜狗给出的文章地址是 ``weixin.sogou.com/link``。这个地址现在经常返回 200
和一段跳转脚本，而不是 302。脚本用 ``url += '...'`` 把 ``mp.weixin.qq.com``
拆开拼出来。请求 ``/link`` 时带上搜索页刚下发的 Cookie 和 Referer，再按片段
把原文地址拼回去。拼出来的经常是带 src/signature 的临时链，没有 __biz/mid/sn。
请求临时链时换成微信内置浏览器的 UA，并带上搜狗 Referer 和 SUV/SNUID 等 Cookie，
才能从正文的 msg_link、og:url 或 biz/mid/sn 还原成长链。搜索结果里若只有一条
长链，也用那条。拼不出来，或页面仍然不是微信原文时，把长链交给 DeepSeek Fetch；
只有临时链时也把临时链交过去。两种地址都没有就改看网易号，不把这一轮直接判死。

微信稿还有一层 DeepSeek Fetch 兜底（``ETF_FLOW_FETCH_FALLBACK=true`` 且配置了
``DEEPSEEK_API_KEY``）：原文页抓不到、页里没有表格图片、或视觉抽取失败时，把
清洗过的长链（没有长链时用临时签名链）交给 DeepSeek，解析它识别出的表格。
网易号没有微信地址，不走兜底。
兜底结果通常缺全市场和宽基，``raw_json`` 里 ``via`` 记为 ``deepseek_fetch``。
"""

from __future__ import annotations

import base64
import html
import json
import logging
import math
import re
import time
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
from app.news.mp_fetch import (
    FetchFallbackError,
    deepseek_api_key,
    fallback_ready,
    fetch_article,
    fetchable_wechat_url,
    parse_flow_markdown,
    sanitize_wechat_url,
    signed_article_id,
    signed_wechat_url,
)

logger = logging.getLogger(__name__)

SOURCE = "etf_flow"
ACCOUNT = "ETF领航者"
NETEASE_MEDIA_URL = "https://www.163.com/dy/media/T1730214999977.html"
NETEASE_HOST = "www.163.com"
SOGOU_SEARCH_URL = "https://weixin.sogou.com/weixin"
WECHAT_HOST = "mp.weixin.qq.com"
POLL_START = dt_time(7, 30)
# 公众号大约 07:xx 发布。这个窗口里按标题轮询搜狗，网易号还没索引。
DISCOVERY_END = dt_time(8, 30)
SOFT_AT = dt_time(11, 0)
POLL_END = dt_time(15, 0)
POLL_EVERY = 10 * 60
POLL_EVERY_LATE = 30 * 60
SOGOU_DAILY_CAP = 8
SOGOU_MIN_GAP = timedelta(minutes=8)
# 发现窗口每次轮询都可以搜。08:30 之后网易号兜底，搜狗只再试两次。回补共用日额度。
LIVE_DISCOVERY_CAP = 6
LIVE_LATE_CAP = 2
MAX_VISION_IMAGES = 6
# 一张表大约 20 只 ETF。思考占掉 completion 预算，多张图叠在一次请求里会把正文挤空。
VISION_BATCH_SIZE = 1
VISION_EMPTY_RETRIES = 1
# 总览大表会把 8192 全部用在思考上，finish_reason=length 且 content 为空。
VISION_LENGTH_MAX_TOKENS = 16384
# glm 第一次已经用 16384。思考仍可能占满，截断后再抬一档。
VISION_GLM_LENGTH_MAX_TOKENS = 32768
_FALLBACK_VISION_MODELS = (
    "deepseek/deepseek-v4-flash-vision-exp",
    "mimo-v2.6-flash",
    "glm-5.3-flash",
)
# 页面、搜狗和图片下载。视觉模型不走这个超时。
PAGE_FETCH_TIMEOUT = 60.0
# 思考打开且 max_tokens>=8192 时，单张表经常超过 60 秒才返回。
VISION_READ_TIMEOUT = 240.0
VISION_CONNECT_TIMEOUT = 10.0
VISION_WRITE_TIMEOUT = 60.0
VISION_POOL_TIMEOUT = 10.0
VISION_HTTP_TIMEOUT = httpx.Timeout(
    connect=VISION_CONNECT_TIMEOUT,
    read=VISION_READ_TIMEOUT,
    write=VISION_WRITE_TIMEOUT,
    pool=VISION_POOL_TIMEOUT,
)
# 读超时后再试这么多次。第一次失败等 1 秒，第二次等 2 秒。
VISION_TIMEOUT_RETRIES = 2
VISION_TIMEOUT_BACKOFF_SECONDS = 1.0
MAX_STORED_IMAGES = 12
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_BROAD = 20
MAX_CATEGORY = 30
MAX_ETF = 80
_RAW_BUDGET = 18_000
# DeepSeek fetch 兜底至少要解析出这么多行（分类 + ETF）才入库。
FETCH_MIN_ROWS = 10

_FLOW_TITLE = re.compile(r"(\d{1,2})月(\d{1,2})日ETF基金申购和赎回")
# 搜狗 /link 不返回 302，而是用 url += '...' 把微信原文地址拆开。
_SOGOU_URL_PART = re.compile(
    r"""(?<![A-Za-z0-9_])url\s*(\+)?=\s*(['"])((?:\\.|(?!\2).)*)\2""",
    re.S,
)
_JS_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)")
_DOC_COOKIE = re.compile(
    r"""document\.cookie\s*=\s*(['"])((?:\\.|(?!\1).)*)\1""",
    re.I | re.S,
)
_DOCID = re.compile(r"^[0-9A-Za-z]{8,24}$")
_IMAGE_HOSTS = {
    "nimg.ws.126.net",
    "dingyue.ws.126.net",
    "mmbiz.qpic.cn",
    "mmbiz.qlogo.cn",
}
# 网易图片代理没有扩展名。tokenhub 自己拉 nimg.ws.126.net 会 400，所以只接受这些类型。
_IMAGE_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/gif": "image/gif",
    "image/bmp": "image/bmp",
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
# 临时签名链用桌面 Chrome 会落到「参数错误」。微信内打开才会吐出带 sn 的正文。
_WECHAT_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/131.0.0.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.49.2600(0x28003151) WeChat/arm64 Weixin "
    "NetType/WIFI Language/zh_CN"
)
SOFT_MISSING_TEXT = "11:00 暂缺 ETF领航者当日申赎文章，继续采集到 15:00"
SOFT_MISSING_ALERT = f"TSP 资讯采集：{SOFT_MISSING_TEXT}。"
SOFT_EXTRACT_ALERT = "TSP 资讯采集：11:00 暂缺 ETF领航者当日申赎表格，继续采集到 15:00。"
MISSING_TEXT = "15:00 仍未采集到 ETF领航者当日申赎文章"
MISSING_ALERT = f"TSP 资讯采集：{MISSING_TEXT}。"
EXTRACT_ALERT = "TSP 资讯采集：15:00 仍未完成 ETF领航者当日申赎表格抽取。"
RECOVERED_ALERT = "TSP 资讯采集：ETF领航者当日申赎已补采。"
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


class VisionRequestTimeoutError(ExtractFailedError):
    """单张图的视觉请求超时，重试后仍失败。其余图片可以继续。"""


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
    """pending 时 07:30–08:30 每 10 分钟，08:30–15:00 每 30 分钟；否则等到下一个 07:30。"""
    now = now.astimezone(CN_TZ)
    clock = _clock(now)
    start = datetime.combine(now.date(), POLL_START, tzinfo=CN_TZ)
    discovery = datetime.combine(now.date(), DISCOVERY_END, tzinfo=CN_TZ)
    end = datetime.combine(now.date(), POLL_END, tzinfo=CN_TZ)
    if clock < POLL_START:
        return _until(now, start)
    if pending and clock < POLL_END:
        if clock < DISCOVERY_END:
            step = POLL_EVERY
            boundary = discovery
        else:
            step = POLL_EVERY_LATE
            boundary = end
        nxt = now + timedelta(seconds=step)
        if nxt >= boundary:
            return _until(now, boundary)
        return step
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
    """期待新稿的日子，以及今天开市的日子（用于补抓）。不看钟点。"""
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
    """标题里的月日是交易日，优先于模型自己填的日期。

    标题推不出日期时，丢掉晚于发布日的模型日期，避免把尚未发生的交易日记进资讯。
    """
    out = dict(extracted)
    derived = trade_date_from_title(title, published)
    if derived:
        out["trade_date"] = derived
        return out
    model = str(out.get("trade_date") or "")
    publish_day = published.astimezone(CN_TZ).date()
    try:
        parsed = date.fromisoformat(model) if model else None
    except ValueError:
        parsed = None
    if parsed is None or parsed > publish_day:
        out["trade_date"] = ""
    return out


def flow_title_for(day: date) -> str:
    return f"{day.month}月{day.day}日ETF基金申购和赎回"


def expected_trade_day(now: datetime) -> date:
    """昨天开市时，今早这篇的标题日期就是昨天。"""
    return now.astimezone(CN_TZ).date() - timedelta(days=1)


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


def parse_sogou_articles(html_text: str, now: datetime) -> list[ArticleRef]:
    """搜狗微信文章搜索（type=2）。只收公众号「ETF领航者」的申赎稿。"""
    if "antispider" in html_text or "请输入验证码" in html_text:
        raise RuntimeError("搜狗微信搜索需要验证码")
    refs: list[ArticleRef] = []
    for block in re.findall(r"<li\b[^>]*>(.*?)</li>", html_text, re.S | re.I):
        account = re.search(r'class="all-time-y2"[^>]*>([^<]*)', block)
        if account is None or _plain(account.group(1)) != ACCOUNT:
            continue
        published = _sogou_time(block, now)
        if published is None:
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
            refs.append(
                ArticleRef(
                    article_id="",
                    title=title,
                    url=href,
                    published_at=published,
                    channel="wechat",
                )
            )
            break
    return refs


def parse_sogou_js_redirect(html_text: str) -> str:
    """从搜狗跳转页的 ``url += '...'`` 片段拼出 mp.weixin.qq.com 原文地址。

    拼出来的地址不是 https 的微信原文时返回空字符串，调用方不要跟着跳。
    """
    found = ""
    parts: list[str] = []
    for match in _SOGOU_URL_PART.finditer(html_text or ""):
        # 不能用 html.unescape: 会把 &timestamp= 解成 ×tamp=（见 _unescape_url_fragment）
        fragment = _unescape_url_fragment(match.group(3))
        if match.group(1) != "+":
            found = _mp_article_url("".join(parts)) or found
            parts = [fragment] if fragment else []
            continue
        if fragment:
            parts.append(fragment)
    return _mp_article_url("".join(parts)) or found


def _js_unescape(value: str) -> str:
    def repl(match: re.Match) -> str:
        token = match.group(1)
        if len(token) == 5 and token[0] == "u":
            return chr(int(token[1:], 16))
        if len(token) == 3 and token[0] == "x":
            return chr(int(token[1:], 16))
        return {
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "\\": "\\",
            "'": "'",
            '"': '"',
            "/": "/",
        }.get(token, token)

    return _JS_ESCAPE.sub(repl, value or "")


def _unescape_url_fragment(value: str) -> str:
    """解码可能拼进 URL 的 JS 字符串片段。只解 ``&amp;`` 和 ``\\uXXXX`` / ``\\xXX``。

    不能用 ``html.unescape``。HTML5 的命名实体允许省略分号，``&timestamp=`` 里的
    ``&times`` 会被当成 ``&times``（乘号）解成 ``×``，查询串就变成 ``×tamp=``
    （``×`` 编码后是 ``%C3%97``）。同一个坑还有 ``&times;`` 本身、``&sect`` 之类：
    任何形如 ``&`` + 实体名 + 尾随字符的查询参数都可能被吃掉。

    这里只做两种在 URL 里确定无歧义的转换：

    * ``&amp;`` → ``&``，循环到稳定（``&amp;amp;`` 可能层层转义）
    * 已有的 ``_js_unescape``（``\\u0026`` / ``\\x26`` / ``\\/`` 等）

    其余实体一律原样保留。少解一个实体最多让参数名多出 ``&amp;`` 字样（调用方按
    字面量 ``&`` 切分时仍能拿到正确的键），而多解一个就会把 URL 解坏。
    """
    text = value or ""
    for _ in range(4):
        decoded = text.replace("&amp;", "&")
        if decoded == text:
            break
        text = decoded
    return _js_unescape(text)


def _mp_article_url(url: str) -> str:
    text = (url or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or host != WECHAT_HOST:
        return ""
    return text


def index_flow_refs(refs: list[ArticleRef]) -> dict[str, ArticleRef]:
    """按标题里的交易日索引。同一天保留发布时间更晚的一篇。"""
    found: dict[str, ArticleRef] = {}
    for ref in refs:
        day = trade_date_from_title(ref.title, ref.published_at)
        if not day:
            continue
        previous = found.get(day)
        if previous is None or ref.published_at >= previous.published_at:
            found[day] = ref
    return found


MISSING_SOURCE = "源缺失"
_BACKFILL_NAME = "etf_flow_backfill.json"
_DATE_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def backfill_dates(
    *,
    dates: str = "",
    since: str = "",
    until: str = "",
    today: date | None = None,
) -> list[str]:
    """指定日期，或 ``since`` 到 ``until`` 的工作日。没有 2026-07-08 下限。

    区间跳过周六日。``--dates`` 里的日期按原样保留，用来补单个交易日。
    """
    picked: list[str] = []
    for part in dates.replace("，", ",").split(","):
        text = part.strip()
        if not text:
            continue
        if not _DATE_TEXT.match(text):
            raise ValueError(f"日期格式应为 YYYY-MM-DD: {text}")
        date.fromisoformat(text)
        picked.append(text)
    if since or until:
        if not since:
            raise ValueError("填写了 until 时也要填写 since")
        if not _DATE_TEXT.match(since):
            raise ValueError(f"日期格式应为 YYYY-MM-DD: {since}")
        start = date.fromisoformat(since)
        if until:
            if not _DATE_TEXT.match(until):
                raise ValueError(f"日期格式应为 YYYY-MM-DD: {until}")
            end = date.fromisoformat(until)
        else:
            end = today or _cn_now().date()
        if end < start:
            raise ValueError("since 不能晚于 until")
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                picked.append(cursor.isoformat())
            cursor += timedelta(days=1)
    ordered: list[str] = []
    seen: set[str] = set()
    for day in picked:
        if day in seen:
            continue
        seen.add(day)
        ordered.append(day)
    return ordered


def backfill_etf_flow(
    client: httpx.Client | None = None,
    *,
    dates: list[str],
    now: datetime | None = None,
    data_dir: Path | None = None,
    retry_missing: bool = False,
) -> dict:
    """回补指定交易日的申赎稿。

    先读网易号列表。列表目前包含早于 2026-07-08 的稿，这些日期不另设下限。
    列表里没有的日期再按标题搜微信归档。两边都没有文章时记「源缺失」，
    默认不再重试。搜狗额度用尽时留到下次，不写成源缺失。
    """
    now = (now or _cn_now()).astimezone(CN_TZ)
    root = data_dir or settings.data_dir
    status = _load_backfill_status(root)
    stored = _stored_flow_dates()
    missing: list[str] = []
    deferred: list[str] = []
    errors: list[str] = []
    skipped: list[str] = []
    inserted = 0
    duplicate = 0
    pending: list[str] = []
    for day in dates:
        if not _DATE_TEXT.match(day):
            errors.append(f"{day}: 日期格式应为 YYYY-MM-DD")
            continue
        if day in stored or (not retry_missing and status.get(day) == MISSING_SOURCE):
            skipped.append(day)
            continue
        pending.append(day)
    summary = {
        "inserted": inserted,
        "duplicate": duplicate,
        "missing": missing,
        "deferred": deferred,
        "errors": errors,
        "skipped": skipped,
    }
    if not pending:
        return summary
    own = client is None
    client = client or _page_client()
    try:
        refs, netease_error = _netease_refs(client)
        indexed = index_flow_refs(refs)
        for day in pending:
            ref = indexed.get(day)
            if ref is None:
                try:
                    found, blocked = _search_wechat_archive(
                        client, now, root, date.fromisoformat(day),
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{day}: {_public_error(exc) or '微信归档搜索失败'}")
                    deferred.append(day)
                    continue
                if blocked:
                    deferred.append(day)
                    continue
                if found is None:
                    if netease_error is not None:
                        errors.append(f"{day}: {_public_error(netease_error)}")
                        deferred.append(day)
                        continue
                    _remember_backfill(root, status, day, MISSING_SOURCE)
                    missing.append(day)
                    continue
                ref = found
            if not vision_api_key():
                errors.append(f"{day}: 未配置 VISION_AI_API_KEY")
                deferred.append(day)
                continue
            try:
                saved = _ingest_ref(client, ref)
            except Exception as exc:  # noqa: BLE001 — 单日失败不能挡住其余日期
                errors.append(f"{day}: {_public_error(exc) or '采集失败'}")
                continue
            inserted += int(saved.get("inserted") or 0)
            duplicate += int(saved.get("duplicate") or 0)
            _remember_backfill(root, status, day, "已入库")
            _note_stored_recovery(root)
    finally:
        if own:
            client.close()
    summary["inserted"] = inserted
    summary["duplicate"] = duplicate
    return summary


def _search_wechat_archive(
    client,
    now: datetime,
    root: Path,
    day: date,
) -> tuple[ArticleRef | None, bool]:
    """返回 (稿件, 是否因额度没搜)。搜过但没有稿件时是 (None, False)。"""
    budget = root / "news" / "etf_sogou_budget.json"
    if not sogou_begin(budget, now):
        return None, True
    title = flow_title_for(day)
    html_text, _final = _fetch(
        client,
        SOGOU_SEARCH_URL,
        allow_hosts={"weixin.sogou.com"},
        params={"type": "2", "query": f"{ACCOUNT} {title}", "ie": "utf8"},
    )
    wanted = day.isoformat()
    for ref in parse_sogou_articles(html_text, now):
        if trade_date_from_title(ref.title, ref.published_at) == wanted:
            return ref, False
    return None, False


def _stored_flow_dates() -> set[str]:
    from app.news.service import get_store

    found: set[str] = set()
    for title, published_at in get_store().source_titles(SOURCE):
        published = parse_time(published_at)
        if published is None:
            continue
        day = trade_date_from_title(title, published)
        if day:
            found.add(day)
    return found


def _backfill_path(root: Path) -> Path:
    return root / "news" / _BACKFILL_NAME


def _load_backfill_status(root: Path) -> dict[str, str]:
    payload = _load_budget(_backfill_path(root))
    raw = payload.get("dates")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if _DATE_TEXT.match(str(key))}


def _remember_backfill(root: Path, status: dict[str, str], day: str, value: str) -> None:
    status[day] = value
    path = _backfill_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"dates": status}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("ETF领航者回补状态写不进去: %s", _public_error(exc))


def main(argv: list[str] | None = None) -> int:
    """命令行回补，或用一篇微信原文补采。``--since`` 可以早于 2026-07-08。"""
    import argparse

    parser = argparse.ArgumentParser(description="回补 ETF领航者指定交易日的申赎稿，或用微信原文链接补采")
    parser.add_argument("--data-dir", default="", help="TSP data 目录，默认 DATA_DIR 或 ./data")
    parser.add_argument("--dates", default="", help="逗号分隔的交易日，如 2026-07-07,2026-06-30")
    parser.add_argument("--since", default="", help="回补起点（含）。可以早于 2026-07-08")
    parser.add_argument("--until", default="", help="回补终点（含）。默认今天（北京时间）")
    parser.add_argument("--retry-missing", action="store_true", help="重新查找已经标成源缺失的日期")
    parser.add_argument(
        "--url",
        default="",
        help="直接采集一篇 mp.weixin.qq.com 文章，支持 /s/ 短链",
    )
    parser.add_argument(
        "--fetch-check",
        default="",
        help="只走 DeepSeek Fetch 兜底解析一篇微信长链并打印结果，不入库",
    )
    args = parser.parse_args(argv)
    root = _cli_data_dir(args.data_dir)
    if args.fetch_check.strip():
        return _fetch_check(args.fetch_check, root)
    if args.url.strip():
        if args.dates.strip() or args.since.strip() or args.until.strip() or args.retry_missing:
            print("--url 不要和回补日期一起用。")
            return 2
        try:
            result = ingest_wechat_url(args.url, data_dir=root)
        except ExtractFailedError as exc:
            print(_public_error(exc) or "采集失败")
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("found") else 1
    try:
        days = backfill_dates(dates=args.dates, since=args.since, until=args.until)
    except ValueError as exc:
        print(str(exc))
        return 2
    if not days:
        print("没有要回补的日期。用 --dates、--since，或 --url。")
        return 2
    summary = backfill_etf_flow(dates=days, data_dir=root, retry_missing=args.retry_missing)
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if summary.get("errors") else 0


def _cli_data_dir(raw: str) -> Path:
    import os

    from app.config import settings

    if raw:
        return Path(raw)
    env = os.environ.get("DATA_DIR") or ""
    if env:
        return Path(env)
    return Path(settings.data_dir)


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


def resolve_wechat_article_url(html_text: str, final_url: str) -> str:
    """短链 /s/xxx 没有 sn。从长链和正文里补齐 __biz、mid、idx、sn。

    正文模板里的 ``${window.sn}`` 不是合法参数。真实 JS 赋值可以盖过这种占位。
    跳转地址上已经合格的 sn 不被另一条链接覆盖。
    """
    params = _wechat_query_params(final_url)
    text = html.unescape(html_text or "")
    text = text.replace("\\u0026", "&").replace("\\/", "/")
    for match in re.finditer(r"https://mp\.weixin\.qq\.com/s\?[^\"'\s<>\\]+", text, re.I):
        for key, value in _wechat_query_params(match.group(0)).items():
            _fill_param(params, key, value)
    for key, value in _wechat_js_params(text).items():
        _fill_param(params, key, value)
    if not params.get("sn") and not (params.get("mid") or "").isdigit():
        return (final_url or "").split("#", 1)[0][:500]
    if params.get("mid") and not params.get("idx"):
        params["idx"] = "1"
    pairs = [(key, params[key]) for key in ("__biz", "mid", "idx", "sn") if params.get(key)]
    if not pairs:
        return (final_url or "").split("#", 1)[0][:500]
    return "https://mp.weixin.qq.com/s?" + urlencode(pairs)


def wechat_published_at(html_text: str) -> datetime | None:
    """微信正文里的发布时间。短链补采没有列表时间，只能读页面。

    ``publish_time`` 节点经常是空的，由前端再填。没有它、也没有 unix ``ct`` 时，
    读 ``create_time: 'YYYY-MM-DD HH:MM'``，按北京时间理解。
    """
    text = html.unescape(html_text or "")
    match = re.search(r"""id=["']publish_time["'][^>]*>([^<]+)""", text)
    if match:
        parsed = _parse_cn_clock(match.group(1))
        if parsed is not None:
            return parsed
    ct = re.search(r"""(?<![A-Za-z0-9_])ct\s*[:=]\s*["'](\d{10})["']""", text)
    if ct:
        try:
            return datetime.fromtimestamp(int(ct.group(1)), tz=CN_TZ)
        except (OSError, OverflowError, ValueError):
            pass
    created = re.search(
        r"""(?<![A-Za-z0-9_])(?:ori_create_time|create_time)\s*[:=]\s*["']"""
        r"""(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)["']""",
        text,
    )
    if not created:
        return None
    return parse_time(created.group(1).replace("T", " "))


def _wechat_query_params(url: str) -> dict[str, str]:
    # 这里同样不能 html.unescape: 微信地址常带 &timestamp=, 整段解码后 &sn=xxx&timestamp=
    # 会变成 &sn=xxx×tamp=, sn 的值被吃掉, 只剩 __biz/mid/idx, 于是又落回「缺少 id」。
    query = parse_qs(urlparse(_unescape_url_fragment(url or "")).query)
    found: dict[str, str] = {}
    for key in ("__biz", "mid", "idx", "sn"):
        values = query.get(key) or []
        if not values:
            continue
        _fill_param(found, key, str(values[0]))
    return found


def _fill_param(params: dict[str, str], key: str, value: str) -> None:
    """空缺或占位才写入。已经合格的值保持不动。"""
    cleaned = _clean_param(key, value)
    if not cleaned:
        return
    current = params.get(key) or ""
    if current and _clean_param(key, current):
        return
    params[key] = cleaned


def _clean_param(key: str, value: str) -> str:
    name = "biz" if key == "__biz" else key
    return _valid_js_value(name, (value or "").strip())


def _wechat_js_params(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    biz = _grab_js(text, "biz") or _grab_js(text, "__biz")
    if biz:
        found["__biz"] = biz
    for key in ("mid", "idx", "sn"):
        value = _grab_js(text, key)
        if value:
            found[key] = value
    return found


def _grab_js(text: str, name: str) -> str:
    pattern = rf"""(?<![A-Za-z0-9_])["']?{re.escape(name)}["']?\s*[:=]\s*["']([^"']*)["']"""
    for match in re.finditer(pattern, text):
        value = _valid_js_value(name, match.group(1).strip())
        if value:
            return value
    return ""


def _valid_js_value(name: str, value: str) -> str:
    # ${window.sn} 这类模板会出现在正文链接里，不能当成已经解析出的参数。
    if not value or any(mark in value for mark in ("$", "{", "}")):
        return ""
    if name in {"mid", "idx"}:
        return value if value.isdigit() else ""
    if name == "sn":
        return value if re.fullmatch(r"[0-9A-Za-z_-]{6,128}", value) else ""
    if name in {"biz", "__biz"}:
        return value if re.fullmatch(r"[0-9A-Za-z_=+/-]{4,128}", value) else ""
    return ""


def _parse_cn_clock(raw: str) -> datetime | None:
    match = re.search(
        r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"
        r"(?:\s*(?P<h>\d{1,2})\s*:\s*(?P<min>\d{2}))?",
        raw or "",
    )
    if not match:
        return parse_time(raw)
    try:
        return datetime(
            int(match.group("y")),
            int(match.group("m")),
            int(match.group("d")),
            int(match.group("h") or 0),
            int(match.group("min") or 0),
            tzinfo=CN_TZ,
        )
    except ValueError:
        return None


def vision_payload(
    client,
    images: list[str],
    *,
    model: str,
    max_tokens: int | None = None,
) -> dict:
    """一次请求只放一张表。表格 OCR 不关思考。

    image_url 用 data URL。tokenhub 拉不到 nimg.ws.126.net，远程地址会 400。
    max_tokens 只在长度截断后的那一次重试里提高，不关掉思考。
    """
    content: list[dict] = [{"type": "text", "text": VISION_PROMPT}]
    kept = 0
    for url in images:
        normalized = _normalize_image(url)
        if not normalized:
            continue
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(client, normalized)}})
        kept += 1
        if kept >= VISION_BATCH_SIZE:
            break
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }
    body.update(vision_generation(model, ocr=True))
    if "temperature" not in body:
        body["temperature"] = 0
    if max_tokens is not None:
        body["max_tokens"] = max(int(max_tokens), body["max_tokens"])
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
    if isinstance(content, str) and content.strip():
        return content
    # glm 思考占满预算时，JSON 可能只写在 reasoning_content 里。
    reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
    blob = _json_object_text(reasoning if isinstance(reasoning, str) else "")
    if blob:
        return blob
    raise EmptyVisionError("视觉模型没有返回内容")


def _json_object_text(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return ""
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return text[start : end + 1]


def _stopped_for_length(payload: dict) -> bool:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    reason = choices[0].get("finish_reason")
    return isinstance(reason, str) and reason.strip().lower() == "length"


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
    """一次搜索算一次。一天最多 8 次，两次至少隔 8 分钟。"""
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


def _consume_live_sogou(root: Path, now: datetime) -> bool:
    """发现窗口里每次轮询都可以搜标题。08:30 之后最多再搜两次。

    和回补共用一天 8 次、两次至少隔 8 分钟。阶段次数用完不再打搜狗，改看网易号。
    """
    now = now.astimezone(CN_TZ)
    budget = root / "news" / "etf_sogou_budget.json"
    discovery = _clock(now) < DISCOVERY_END
    phase = "live_discovery" if discovery else "live_late"
    cap = LIVE_DISCOVERY_CAP if discovery else LIVE_LATE_CAP
    state = _load_budget(budget)
    if state.get("date") == now.date().isoformat():
        try:
            used = int(state.get(phase) or 0)
        except (TypeError, ValueError):
            used = 0
        if used >= cap:
            return False
    if not sogou_begin(budget, now):
        return False
    state = _load_budget(budget)
    try:
        state[phase] = int(state.get(phase) or 0) + 1
    except (TypeError, ValueError):
        state[phase] = 1
    try:
        budget.parent.mkdir(parents=True, exist_ok=True)
        budget.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("ETF领航者搜狗次数写不进去")
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
    if _collection_satisfied(now, expect):
        _mark_ok()
        _note_stored_recovery(root)
        return _result(found=True)
    own = client is None
    client = client or _page_client()
    try:
        return _pull(client, now, root, expect)
    except ArticleMissingError:
        return _missing(now, root, expect)
    except ExtractFailedError as exc:
        message = _public_error(exc) or "表格抽取失败"
        logger.warning("ETF领航者表格抽取失败: %s", message)
        _mark_err(message)
        if _hard_deadline(now):
            # 只在「今天应有新稿」时提醒。周一补抓失败记健康检查，不把旧稿抽失败报成当日缺稿。
            if expect:
                _alert(root, now.date(), EXTRACT_ALERT, kind="hard")
            return _result(error=message)
        if expect and _soft_due(now):
            _alert(root, now.date(), SOFT_EXTRACT_ALERT, kind="soft")
        return _result(error=message, pending=True)
    except Exception as exc:  # noqa: BLE001 — 单源失败不能拖垮其他资讯轮询
        message = _public_error(exc) or "采集失败"
        logger.warning("ETF领航者采集失败: %s", message)
        _mark_err(message)
        if expect and _hard_deadline(now):
            _alert(root, now.date(), MISSING_ALERT, kind="hard")
            return _result(error=message)
        if expect and _soft_due(now):
            _alert(root, now.date(), SOFT_MISSING_ALERT, kind="soft")
        return _result(error=message, pending=not _hard_deadline(now))
    finally:
        if own:
            client.close()


def _pull(client, now: datetime, root: Path, expect: bool) -> dict:
    ref, reason = _resolve_ref(client, now, root, expect)
    try:
        result = _ingest_ref(client, ref)
    except ExtractFailedError as exc:
        # 搜狗链接没还原成微信原文时，DeepSeek 已经在 _ingest_ref 里试过。
        # 还是无效就改看网易号，避免这一轮直接失败。
        if reason != "sogou" or "微信文章地址无效" not in str(exc):
            raise
        logger.warning("ETF领航者微信原文地址无效，改看网易号: %s", _public_error(exc))
        alt = _netease_today(client, now, expect)
        if alt is None:
            raise
        result = _ingest_ref(client, alt)
    _note_stored_recovery(root)
    return result


def _netease_today(client, now: datetime, expect: bool) -> ArticleRef | None:
    """搜狗原文打不开时，网易号列表里仍可能有同一天的稿。"""
    refs, error = _netease_refs(client)
    if error is not None and not refs:
        return None
    chosen, why = choose_flow_article(refs, day=now.date(), expect=expect)
    if chosen is not None and (why == "today" or not expect):
        return chosen
    return None


def ingest_wechat_url(
    url: str,
    client: httpx.Client | None = None,
    *,
    now: datetime | None = None,
    data_dir: Path | None = None,
) -> dict:
    """用一篇 mp.weixin.qq.com 原文补采。短链先还原 sn，入库和每日采集相同。"""
    now = (now or _cn_now()).astimezone(CN_TZ)
    root = data_dir or settings.data_dir
    target = _normalize_wechat_input(url)
    if not vision_api_key():
        raise ExtractFailedError("未配置 VISION_AI_API_KEY")
    own = client is None
    client = client or _page_client()
    try:
        html_text, final = _fetch(client, target, allow_hosts={WECHAT_HOST})
        if (urlparse(final).hostname or "").lower() != WECHAT_HOST:
            raise ExtractFailedError("微信文章地址无效")
        resolved = resolve_wechat_article_url(html_text, final)
        published = wechat_published_at(html_text)
        if published is None:
            raise ExtractFailedError("微信文章缺少发布时间")
        ref = ArticleRef(
            article_id="",
            title=_activity_title(html_text),
            url=resolved,
            published_at=published,
            channel="wechat",
        )
        page = parse_wechat_article(html_text, resolved, ref)
        result = _store_page(client, page)
        _mark_manual_backfill(root, page)
        _note_stored_recovery(root)
        return result
    except ExtractFailedError:
        raise
    except Exception as exc:  # 补采失败要带回给命令行和接口
        raise ExtractFailedError(_public_error(exc) or "采集失败") from exc
    finally:
        if own:
            client.close()


def _normalize_wechat_input(url: str) -> str:
    text = (url or "").strip()
    if len(text) > 800:
        raise ExtractFailedError("文章地址过长")
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or host != WECHAT_HOST
        or not (path == "/s" or path.startswith("/s/"))
    ):
        raise ExtractFailedError("只接受 mp.weixin.qq.com 的文章地址")
    return text.split("#", 1)[0]


def _ingest_ref(client, ref: ArticleRef) -> dict:
    if ref.channel == "netease" and ref.article_id and _known(f"netease:{ref.article_id}"):
        _mark_ok()
        return _result(found=True)
    try:
        page = _load_page(client, ref)
    except Exception as exc:
        # 标题不对说明拿到的不是申赎稿，不能拿列表标题去兜底。
        page = None if "标题" in str(exc) else _fallback_page(ref, exc)
        if page is None:
            raise
    return _store_page(client, page)


def _fallback_page(ref: ArticleRef, exc: BaseException) -> ArticlePage | None:
    """微信原文页打不开时，用长链和标题拼一页，图片留空，交给兜底。

    列表里的搜狗 ``/link`` 不是微信长链。跳转脚本恢复出的地址挂在异常上。
    没有 __biz/mid/sn 时，改用 src/signature 临时链。
    """
    if ref.channel != "wechat" or not fallback_ready():
        return None
    carried = str(getattr(exc, "article_url", "") or "")
    url = sanitize_wechat_url(ref.url) or sanitize_wechat_url(carried)
    article_id = wechat_article_id(url) if url else ""
    if not url:
        # 临时链没有 sn。清洗成长链会得到空串，DeepSeek 仍可以按签名地址抓。
        url = signed_wechat_url(carried) or signed_wechat_url(ref.url)
        article_id = signed_article_id(url) if url else ""
    if not article_id or not _FLOW_TITLE.search(_plain(ref.title)):
        return None
    logger.warning("ETF领航者微信原文页打不开，改走 DeepSeek fetch: %s", _public_error(exc))
    return ArticlePage(
        article_id=article_id,
        title=ref.title,
        url=url,
        published_at=ref.published_at,
        images=[],
        channel="wechat",
    )


def _extract_page(client, page: ArticlePage) -> tuple[dict, str]:
    """先走视觉模型。没有图片或视觉失败时，微信稿再试 DeepSeek fetch。"""
    try:
        if not page.images:
            raise ExtractFailedError("文章没有可抽取的表格图片")
        extracted = _call_vision(client, page.images)
        return apply_trade_date(page.title, page.published_at, extracted), "vision"
    except ExtractFailedError as exc:
        fallback = _fetch_fallback(client, page, reason=_public_error(exc))
        if fallback is None:
            raise
        return fallback, "deepseek_fetch"


def _fetch_fallback(client, page: ArticlePage, *, reason: str = "") -> dict | None:
    """DeepSeek fetch 兜底。没开、不是微信稿或结果太少时返回 None，由调用方报原错。"""
    if page.channel != "wechat" or not fallback_ready():
        return None
    logger.warning("ETF领航者改用 DeepSeek fetch 兜底，原因: %s", reason or "无")
    try:
        fetched = fetch_article(page.url, client)
    except FetchFallbackError as exc:
        logger.warning("ETF领航者 DeepSeek fetch 没有拿到正文: %s", _public_error(exc))
        return None
    fetched_title = _plain(fetched.get("title") or "")
    if (
        fetched_title
        and _plain(page.title)
        and _FLOW_TITLE.search(fetched_title)
        and trade_date_from_title(fetched_title, page.published_at)
        != trade_date_from_title(page.title, page.published_at)
    ):
        logger.warning("ETF领航者 DeepSeek fetch 标题和原稿交易日不一致，丢弃")
        return None
    try:
        extracted = normalize_flow(parse_flow_markdown(fetched.get("content") or ""))
    except (ValueError, KeyError, TypeError):
        logger.warning("ETF领航者 DeepSeek fetch 正文没有解析出申赎表")
        return None
    rows = len(extracted.get("etfs") or []) + len(extracted.get("categories") or [])
    if rows < FETCH_MIN_ROWS:
        logger.warning("ETF领航者 DeepSeek fetch 只解析出 %s 行，太少，丢弃", rows)
        return None
    logger.info(
        "ETF领航者 DeepSeek fetch 兜底解析出分类 %s 行、ETF %s 行",
        len(extracted.get("categories") or []),
        len(extracted.get("etfs") or []),
    )
    return apply_trade_date(page.title, page.published_at, extracted)


def _fetch_check(url: str, root: Path) -> int:
    """命令行自检：清洗链接后只走兜底解析，打印行数和结果，不写库。"""
    target = fetchable_wechat_url(url)
    if not target:
        print("只接受带 __biz/mid/idx/sn 的微信长链，或带 src/signature 的临时链")
        return 2
    if not deepseek_api_key():
        print("未配置 DEEPSEEK_API_KEY")
        return 1
    client = _page_client()
    try:
        fetched = fetch_article(target, client, data_dir=root)
    except FetchFallbackError as exc:
        print(_public_error(exc))
        return 1
    finally:
        client.close()
    title = fetched.get("title") or ""
    try:
        extracted = normalize_flow(parse_flow_markdown(fetched.get("content") or ""))
    except ValueError as exc:
        print(_public_error(exc))
        return 1
    extracted = apply_trade_date(title, _cn_now(), extracted)
    print(
        json.dumps(
            {
                "article_id": wechat_article_id(target) or signed_article_id(target),
                "title": title,
                "trade_date": extracted.get("trade_date") or "",
                "categories": len(extracted.get("categories") or []),
                "etfs": len(extracted.get("etfs") or []),
                "extracted": extracted,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _store_page(client, page: ArticlePage) -> dict:
    source_id = f"{page.channel}:{page.article_id}"
    if _known(source_id):
        _mark_ok()
        return _result(found=True)
    extracted, via = _extract_page(client, page)
    stocks, sectors = flow_mentions(extracted)
    images = page.images[:MAX_STORED_IMAGES]
    raw = _fit_raw(
        {
            "article_id": page.article_id,
            "channel": page.channel,
            "trade_date": extracted.get("trade_date") or "",
            "images": images,
            "via": via,
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
    """期待新稿时先按标题搜搜狗，网易号只在搜不到时兜底。"""
    sogou_error: Exception | None = None
    if expect:
        try:
            found = _search_live_sogou(client, now, root, expected_trade_day(now))
        except Exception as exc:  # noqa: BLE001 — 搜狗失败改看网易号
            sogou_error = exc
            found = None
        if found is not None:
            return found, "sogou"
    refs, netease_error = _netease_refs(client)
    chosen, reason = choose_flow_article(refs, day=now.date(), expect=expect)
    if chosen is not None and (reason == "today" or not expect):
        return chosen, reason
    if netease_error is not None and sogou_error is not None:
        raise RuntimeError(
            f"{_public_error(netease_error)}；{_public_error(sogou_error)}"
        ) from sogou_error
    if sogou_error is not None:
        raise sogou_error
    if netease_error is not None:
        raise netease_error
    raise ArticleMissingError()


def _search_live_sogou(client, now: datetime, root: Path, trade_day: date) -> ArticleRef | None:
    """按「公众号 + 标题」搜文章。额度用完时返回 None，不当成故障。"""
    if not _consume_live_sogou(root, now):
        return None
    title = flow_title_for(trade_day)
    html_text, _final = _fetch(
        client,
        SOGOU_SEARCH_URL,
        allow_hosts={"weixin.sogou.com"},
        params={"type": "2", "query": f"{ACCOUNT} {title}", "ie": "utf8"},
    )
    wanted = trade_day.isoformat()
    matches = [
        ref
        for ref in parse_sogou_articles(html_text, now)
        if trade_date_from_title(ref.title, ref.published_at) == wanted
    ]
    if not matches:
        return None
    matches.sort(key=lambda ref: ref.published_at, reverse=True)
    return matches[0]


def _netease_refs(client) -> tuple[list[ArticleRef], Exception | None]:
    try:
        html_text, _final = _fetch(client, NETEASE_MEDIA_URL, allow_hosts={NETEASE_HOST})
    except Exception as exc:  # noqa: BLE001 — 主源失败改走搜狗，不在这里结束
        return [], exc
    return parse_netease_media(html_text), None


def _load_page(client, ref: ArticleRef) -> ArticlePage:
    if ref.channel == "netease":
        html_text, final = _fetch(client, ref.url, allow_hosts={NETEASE_HOST})
        if (urlparse(final).hostname or "").lower() != NETEASE_HOST:
            raise ExtractFailedError("网易文章地址无效")
        return parse_netease_article(html_text, ref)
    try:
        html_text, final = _fetch(client, ref.url, allow_hosts=_WECHAT_FETCH_HOSTS)
    except ExtractFailedError as exc:
        _attach_article_url(exc, _recovered_url(client))
        raise
    except Exception as exc:  # 原文地址已拼出时，网络失败也交给兜底
        recovered = _recovered_url(client)
        if not recovered:
            raise
        raise _invalid_wechat(recovered) from exc
    if (urlparse(final).hostname or "").lower() != WECHAT_HOST:
        raise _invalid_wechat(_recovered_url(client))
    try:
        page = parse_wechat_article(html_text, resolve_wechat_article_url(html_text, final), ref)
    except ExtractFailedError as exc:
        _attach_article_url(exc, _recovered_url(client) or final)
        raise
    # 搜狗时间有时是标题里的交易日。页面上的发布时间才是告警日那天的早晨。
    clock = wechat_published_at(html_text)
    if clock is None:
        return page
    return ArticlePage(
        article_id=page.article_id,
        title=page.title,
        url=page.url,
        published_at=clock,
        images=page.images,
        channel=page.channel,
    )


def _call_vision(client, images: list[str]) -> dict:
    usable = [url for url in images if _normalize_image(url)][:MAX_VISION_IMAGES]
    if not usable:
        raise ExtractFailedError("文章没有可抽取的表格图片")
    key = vision_api_key()
    if not key:
        raise ExtractFailedError("未配置 VISION_AI_API_KEY")
    parts: list[dict] = []
    saw_empty = False
    timed_out = False
    for url in usable:
        try:
            parsed, empty = _vision_batch(client, [url], key)
        except VisionRequestTimeoutError as exc:
            timed_out = True
            logger.warning(
                "ETF申赎单张图片视觉请求超时，继续其余图片: %s",
                _public_error(exc),
            )
            continue
        saw_empty = saw_empty or empty
        if parsed is not None:
            parts.append(parsed)
    if not parts:
        if timed_out:
            raise ExtractFailedError("视觉模型请求失败")
        if saw_empty:
            raise ExtractFailedError("视觉模型没有返回内容")
        raise ExtractFailedError("视觉模型没有抽出申赎数字")
    try:
        return merge_flows(parts)
    except (ValueError, KeyError, TypeError) as exc:
        raise ExtractFailedError("视觉模型没有抽出申赎数字") from exc


def vision_model_chain() -> list[str]:
    """主模型失败时只再试一个备用模型，避免一张表打满三个模型。"""
    primary = vision_model()
    chain = [primary] if primary else []
    for name in _FALLBACK_VISION_MODELS:
        if name not in chain:
            chain.append(name)
            break
    return chain or [_FALLBACK_VISION_MODELS[0]]


def _length_budget(model: str) -> int:
    if "glm-5.3-flash" in (model or "").lower():
        return VISION_GLM_LENGTH_MAX_TOKENS
    return VISION_LENGTH_MAX_TOKENS


def _vision_batch(client, images: list[str], key: str) -> tuple[dict | None, bool]:
    """空内容、解析失败或 HTTP 错误时换备用模型。超时和连接失败不换。"""
    last_error: ExtractFailedError | None = None
    saw_empty = False
    for index, model in enumerate(vision_model_chain()):
        if index:
            logger.warning("ETF申赎改用备用视觉模型 %s", model.rsplit("/", 1)[-1])
        try:
            parsed, empty = _vision_batch_one(client, images, key, model)
        except VisionRequestTimeoutError:
            raise
        except ExtractFailedError as exc:
            # 连接失败的文案没有 HTTP 状态。换模型还是打同一个网关，不再试。
            if str(exc) == "视觉模型请求失败":
                raise
            last_error = exc
            continue
        if parsed is not None:
            return parsed, empty or saw_empty
        saw_empty = True
    if last_error is not None and not saw_empty:
        raise last_error
    return None, True


def _vision_batch_one(
    client, images: list[str], key: str, model: str
) -> tuple[dict | None, bool]:
    """空内容再试一次。第一次已有数字时，再用一次结果核对正负号。"""
    first, empty, length_stop = _read_vision(client, images, key, model)
    if first is None:
        if not empty:
            return None, False
        bumped = _length_budget(model)
        second = None
        for _extra in range(VISION_EMPTY_RETRIES):
            if length_stop:
                logger.warning(
                    "ETF申赎视觉输出被长度截断且没有正文，提高到 %s 再试一次",
                    bumped,
                )
                second, _again, _stop = _read_vision(
                    client, images, key, model, max_tokens=bumped
                )
            else:
                logger.warning("ETF申赎视觉模型返回空内容，重试一次")
                second, _again, _stop = _read_vision(client, images, key, model)
            if second is not None:
                break
        if second is None:
            logger.warning("ETF申赎视觉模型重试后仍无内容")
            return None, True
        return second, True
    try:
        second, confirm_empty, _confirm_stop = _read_vision(client, images, key, model)
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


def _read_vision(
    client,
    images: list[str],
    key: str,
    model: str,
    *,
    max_tokens: int | None = None,
) -> tuple[dict | None, bool, bool]:
    payload = _post_vision(
        client,
        vision_payload(client, images, model=model, max_tokens=max_tokens),
        key,
    )
    try:
        return parse_vision_response(payload), False, False
    except EmptyVisionError:
        return None, True, _stopped_for_length(payload)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ExtractFailedError("视觉模型没有抽出申赎数字") from exc


def _page_client() -> httpx.Client:
    """页面、搜狗和图片下载。视觉 POST 在请求上单独加长读超时。"""
    return httpx.Client(timeout=PAGE_FETCH_TIMEOUT, follow_redirects=False)


def _post_vision(client, body: dict, key: str) -> dict:
    attempts = VISION_TIMEOUT_RETRIES + 1
    for attempt in range(attempts):
        try:
            response = client.post(
                f"{vision_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
                timeout=VISION_HTTP_TIMEOUT,
            )
            raiser = getattr(response, "raise_for_status", None)
            if raiser:
                raiser()
            payload = response.json()
        except ExtractFailedError:
            raise
        except httpx.HTTPStatusError as exc:
            message = _vision_status_message(str(body.get("model") or ""), exc)
            logger.warning("ETF申赎视觉模型失败: %s", message)
            raise ExtractFailedError(message) from exc
        except httpx.TimeoutException as exc:
            if attempt + 1 >= attempts:
                logger.warning("ETF申赎视觉模型失败: %s", _public_error(exc))
                raise VisionRequestTimeoutError("视觉模型请求失败") from exc
            logger.warning(
                "ETF申赎视觉模型超时，稍后重试 (%s/%s): %s",
                attempt + 1,
                attempts,
                _public_error(exc),
            )
            time.sleep(VISION_TIMEOUT_BACKOFF_SECONDS * (attempt + 1))
            continue
        except Exception as exc:
            logger.warning("ETF申赎视觉模型失败: %s", _public_error(exc))
            raise ExtractFailedError("视觉模型请求失败") from exc
        if not isinstance(payload, dict):
            raise ExtractFailedError("视觉模型没有抽出申赎数字")
        return payload
    raise VisionRequestTimeoutError("视觉模型请求失败")


def _missing(now: datetime, root: Path, expect: bool) -> dict:
    if _hard_deadline(now):
        if not expect:
            return _result()
        logger.warning("ETF领航者当日文章缺失")
        _mark_err(MISSING_TEXT)
        _alert(root, now.date(), MISSING_ALERT, kind="hard")
        return _result()
    if expect and _soft_due(now):
        _mark_err(SOFT_MISSING_TEXT)
        _alert(root, now.date(), SOFT_MISSING_ALERT, kind="soft")
    return _result(pending=True)


def _fetch(
    client,
    url: str,
    *,
    allow_hosts: set[str],
    params: dict | None = None,
) -> tuple[str, str]:
    if _is_sogou_link(url):
        _set_client_attr(client, "_sogou_recovered_url", "")
    response, final = _open(client, url, allow_hosts=allow_hosts, params=params)
    text = (getattr(response, "text", "") or "")[:2_000_000]
    _remember_sogou_search(client, response, url, params, final, text)
    if _is_sogou_link(final):
        # 200 的跳转页不是微信原文。拼出地址再请求一次，失败时把地址留给兜底。
        target = parse_sogou_js_redirect(text)
        if not target:
            _set_client_attr(client, "_sogou_recovered_url", "")
            return text, final
        _set_client_attr(client, "_sogou_recovered_url", target)
        try:
            response, landed = _open(client, target, allow_hosts=allow_hosts)
        except ExtractFailedError:
            raise
        except Exception as exc:  # 原文地址已拼出，这一跳失败交给兜底
            raise _invalid_wechat(target) from exc
        if (urlparse(landed).hostname or "").lower() != WECHAT_HOST:
            raise _invalid_wechat(target)
        text = (getattr(response, "text", "") or "")[:2_000_000]
        final = landed
    if _is_signed_wechat_url(final):
        return _follow_signed_article(client, text, final, allow_hosts)
    return text, final


def _follow_signed_article(
    client,
    html_text: str,
    landed: str,
    allow_hosts: set[str],
) -> tuple[str, str]:
    """临时链换成永久长链。正文里已经有 sn 就不再请求；否则用搜索页里唯一的一条。"""
    permanent = sanitize_wechat_url(resolve_wechat_article_url(html_text, landed))
    if permanent:
        _set_client_attr(client, "_sogou_recovered_url", permanent)
        return html_text, landed
    from_search = _unique_long_link(_search_html(client))
    if not from_search:
        return html_text, landed
    _set_client_attr(client, "_sogou_recovered_url", from_search)
    try:
        response, final = _open(client, from_search, allow_hosts=allow_hosts)
    except (RuntimeError, httpx.HTTPError, OSError):
        return html_text, landed
    if (urlparse(final).hostname or "").lower() != WECHAT_HOST:
        return html_text, landed
    page = (getattr(response, "text", "") or "")[:2_000_000]
    return page, final


def _unique_long_link(html_text: str) -> str:
    """搜索结果里只有一篇的长链才采用。多篇 sn 不用，避免把别的稿套进来。"""
    text = html.unescape(html_text or "").replace("\\u0026", "&").replace("\\/", "/")
    found: dict[str, str] = {}
    for match in re.finditer(r"https://mp\.weixin\.qq\.com/s\?[^\"'\s<>\\]+", text, re.I):
        clean = sanitize_wechat_url(match.group(0))
        article_id = wechat_article_id(clean) if clean else ""
        if article_id:
            found[article_id] = clean
    if len(found) == 1:
        return next(iter(found.values()))
    if found:
        return ""
    return sanitize_wechat_url(resolve_wechat_article_url(text, ""))


def _open(
    client,
    url: str,
    *,
    allow_hosts: set[str],
    params: dict | None = None,
):
    current = url
    query = params
    for _hop in range(4):
        _check_url(current, allow_hosts)
        response = client.get(current, params=query, headers=_headers(current, client))
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
        return response, current
    raise RuntimeError("重定向次数过多")


def _image_data_url(client, url: str) -> str:
    try:
        response, _final = _open(client, url, allow_hosts=_IMAGE_HOSTS)
        media = _image_media_type(response)
        body = getattr(response, "content", None)
        if not isinstance(body, (bytes, bytearray)) or not body:
            raise ExtractFailedError("表格图片下载失败")
        if len(body) > MAX_IMAGE_BYTES:
            raise ExtractFailedError("表格图片过大")
    except ExtractFailedError:
        raise
    except Exception as exc:  # 下载失败不能把远程地址交给视觉模型
        logger.warning("ETF申赎表格图片下载失败: %s", _public_error(exc))
        raise ExtractFailedError("表格图片下载失败") from exc
    encoded = base64.b64encode(body).decode("ascii")
    return f"data:{media};base64,{encoded}"


def _image_media_type(response) -> str:
    headers = getattr(response, "headers", None) or {}
    getter = getattr(headers, "get", None)
    raw = ""
    if getter is not None:
        raw = str(getter("content-type") or getter("Content-Type") or "")
    media = raw.split(";", 1)[0].strip().lower()
    mapped = _IMAGE_TYPES.get(media)
    if not mapped:
        raise ExtractFailedError("表格图片格式不支持")
    return mapped


def _check_url(url: str, allow_hosts: set[str]) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or host not in allow_hosts:
        raise RuntimeError("地址不在允许的站点里")


def _is_sogou_link(url: str) -> bool:
    parsed = urlparse(url or "")
    return (parsed.hostname or "").lower() == "weixin.sogou.com" and (parsed.path or "").startswith(
        "/link"
    )


def _is_signed_wechat_url(url: str) -> bool:
    """src + signature、且没有 sn 的 /s 地址。永久长链不走这套微信头。"""
    parsed = urlparse(url or "")
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or (parsed.hostname or "").lower() != WECHAT_HOST
        or parsed.path != "/s"
    ):
        return False
    query = parse_qs(parsed.query)
    src = (query.get("src") or [""])[0].strip()
    signature = (query.get("signature") or [""])[0].strip()
    sn = (query.get("sn") or [""])[0].strip()
    return bool(src and signature and not sn)


def _invalid_wechat(url: str) -> ExtractFailedError:
    exc = ExtractFailedError("微信文章地址无效")
    exc.article_url = url or ""
    return exc


def _attach_article_url(exc: ExtractFailedError, url: str) -> None:
    if getattr(exc, "article_url", "") or not url:
        return
    exc.article_url = url


def _recovered_url(client) -> str:
    return str(getattr(client, "_sogou_recovered_url", "") or "")


def _search_html(client) -> str:
    return str(getattr(client, "_sogou_search_html", "") or "")


def _set_client_attr(client, name: str, value) -> None:
    try:
        setattr(client, name, value)
    except (AttributeError, TypeError):
        return


def _remember_sogou_search(client, response, url: str, params, final: str, text: str) -> None:
    """记下搜索页的 Cookie 和完整地址，后面打开 /link 时一起带上。"""
    parsed = urlparse(final or url)
    host = (parsed.hostname or "").lower()
    if host != "weixin.sogou.com" or (parsed.path or "").startswith("/link"):
        return
    referer = url
    if params:
        query = urlencode(params)
        if query:
            referer = url + ("&" if "?" in url else "?") + query
    _set_client_attr(
        client,
        "_sogou_link_context",
        {"referer": referer, "cookie": _cookie_header(response, text)},
    )
    _set_client_attr(client, "_sogou_search_html", (text or "")[:500_000])


def _cookie_header(response, html_text: str) -> str:
    pairs: dict[str, str] = {}
    cookies = getattr(response, "cookies", None)
    if cookies is not None:
        try:
            for key, value in cookies.items():
                _take_cookie(str(key), str(value), pairs)
        except (AttributeError, TypeError, ValueError):
            pass
    for raw in _set_cookie_values(response):
        piece = str(raw).split(";", 1)[0]
        if "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        _take_cookie(name, value, pairs)
    for match in _DOC_COOKIE.finditer(html_text or ""):
        piece = _js_unescape(html.unescape(match.group(2))).split(";", 1)[0]
        if "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        _take_cookie(name, value, pairs)
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def _set_cookie_values(response) -> list[str]:
    headers = getattr(response, "headers", None) or {}
    get_list = getattr(headers, "get_list", None)
    if callable(get_list):
        try:
            values = [str(item) for item in get_list("set-cookie") if item]
        except (AttributeError, TypeError, ValueError):
            values = []
        if values:
            return values
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return []
    raw = getter("set-cookie")
    if raw is None:
        raw = getter("Set-Cookie")
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if item]
    if raw:
        return [str(raw)]
    return []


def _take_cookie(name: str, value: str, pairs: dict[str, str]) -> None:
    key = (name or "").strip()
    if not key or not all(ch.isalnum() or ch in {"_", "-"} for ch in key):
        return
    cleaned = (value or "").replace("\r", "").replace("\n", "").strip()
    if cleaned:
        pairs[key] = cleaned


def _headers(url: str, client=None) -> dict[str, str]:
    headers = dict(_BROWSER)
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("sogou.com"):
        headers["Referer"] = "https://weixin.sogou.com/"
        if _is_sogou_link(url):
            ctx = getattr(client, "_sogou_link_context", None) if client is not None else None
            if isinstance(ctx, dict):
                referer = str(ctx.get("referer") or "")
                cookie = str(ctx.get("cookie") or "")
                if referer:
                    headers["Referer"] = referer
                if cookie:
                    headers["Cookie"] = cookie
    elif host == WECHAT_HOST and _is_signed_wechat_url(url):
        headers["User-Agent"] = _WECHAT_UA
        headers["Referer"] = "https://weixin.sogou.com/"
        ctx = getattr(client, "_sogou_link_context", None) if client is not None else None
        if isinstance(ctx, dict):
            referer = str(ctx.get("referer") or "")
            cookie = str(ctx.get("cookie") or "")
            if referer:
                headers["Referer"] = referer
            if cookie:
                headers["Cookie"] = cookie
    elif host == "nimg.ws.126.net":
        headers["Referer"] = "https://www.163.com/"
    elif host.endswith("163.com"):
        headers["Referer"] = NETEASE_MEDIA_URL
    elif host.startswith("mmbiz."):
        headers["Referer"] = "https://mp.weixin.qq.com/"
    return headers


def _location(response) -> str:
    headers = getattr(response, "headers", None) or {}
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    return str(getter("location") or getter("Location") or "")


def _alert(root: Path, day: date, message: str, *, kind: str) -> None:
    path = _alert_path(root, day, kind)
    if path.is_file():
        return
    try:
        send_dingtalk(settings.dingtalk_webhook_url, settings.dingtalk_secret, message)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ETF申赎提醒发送失败: %s", _public_error(exc))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(day.isoformat(), encoding="utf-8")


def _alert_path(root: Path, day: date, kind: str) -> Path:
    return root / "news" / "health" / f"alert-etf_flow-{kind}-{day.isoformat()}.txt"


def _alert_sent(root: Path, day: date, kind: str) -> bool:
    return _alert_path(root, day, kind).is_file()


def _article_alert_day(title: str, published: datetime) -> date:
    """告警记在发布日早晨。搜狗若把标题里的交易日当成发布时间，告警日是次日。"""
    if published.tzinfo is None:
        published = published.replace(tzinfo=CN_TZ)
    published_day = published.astimezone(CN_TZ).date()
    trade = trade_date_from_title(title, published)
    if not trade:
        return published_day
    trade_day = date.fromisoformat(trade)
    if published_day <= trade_day:
        return trade_day + timedelta(days=1)
    return published_day


def _note_stored_recovery(root: Path) -> None:
    """暂缺或失败之后，对应交易日的稿已经入库，就补发一条不带数字的「已补采」。

    回补和原文链接不经过当轮 ``inserted`` 判断。列表时间落在交易日而不是
    发布日早晨时，仍按标题对上告警文件。
    """
    from app.news.service import get_store

    for row in get_store().recent_for_feed(SOURCE, 16):
        published = parse_time(row["published_at"])
        if published is None:
            continue
        day = _article_alert_day(str(row["title"] or ""), published)
        if not (_alert_sent(root, day, "soft") or _alert_sent(root, day, "hard")):
            continue
        _alert(root, day, RECOVERED_ALERT, kind="recovered")


def _collection_satisfied(now: datetime, expect: bool) -> bool:
    if _published_on(now.date()):
        return True
    if not expect:
        return False
    return expected_trade_day(now).isoformat() in _stored_flow_dates()


def _vision_status_message(model: str, exc: httpx.HTTPStatusError) -> str:
    code = getattr(getattr(exc, "response", None), "status_code", None)
    status = f" HTTP {code}" if code else ""
    response = getattr(exc, "response", None)
    snippet = ""
    if response is not None:
        try:
            snippet = str(getattr(response, "text", "") or "")
        except Exception:  # noqa: BLE001 — 错误正文读不到就只留状态码
            snippet = ""
    redacted = _public_error(RuntimeError(snippet[:500]))
    if redacted:
        logger.warning("ETF申赎视觉模型响应: %s", redacted[:160])
    label = (model or "视觉模型").rsplit("/", 1)[-1]
    return f"视觉模型请求失败（{label}{status}）"


def _mark_manual_backfill(root: Path, page: ArticlePage) -> None:
    day = trade_date_from_title(page.title, page.published_at)
    if not day:
        return
    status = _load_backfill_status(root)
    _remember_backfill(root, status, day, "已入库")


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


def _hard_deadline(now: datetime) -> bool:
    return _clock(now) >= POLL_END


def _soft_due(now: datetime) -> bool:
    clock = _clock(now)
    return SOFT_AT <= clock < POLL_END


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
    for key in (vision_api_key(), deepseek_api_key()):
        if key and len(key) >= 6:
            text = text.replace(key, "***")
    text = re.sub(r"Bearer\s+\S+", "Bearer ***", text)
    text = re.sub(r"(?i)(access_token=)[^&\s\"']+", r"\1***", text)
    text = re.sub(r"(?i)([?&]sign=)[^&\s\"']+", r"\1***", text)
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
    # 列表页的 href 是 URL, 同样不能走 html.unescape。搜狗会把微信长链整段塞进
    # link?url=, 其中若带字面量 &timestamp= / &src= / &signature=, 整段 unescape
    # 会把 &times 吃成 ×, 后续按 & 切分内层参数时 mid/sn 就丢了。
    text = _unescape_url_fragment((href or "").strip())
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
        # 搜狗有时只给月日。跨年时用今年会落到未来，入库后会排到最新并提前暴露。
        year = now.astimezone(CN_TZ).year
        month, day = int(month_day.group(1)), int(month_day.group(2))
        guessed = _safe_dt(year, month, day)
        today = now.astimezone(CN_TZ).date()
        if guessed is not None and guessed.date() > today:
            guessed = _safe_dt(year - 1, month, day)
        return guessed
    parsed = parse_time(text.strip())
    return parsed


def _safe_dt(year: int, month: int, day: int) -> datetime | None:
    try:
        # 只有日期时按轮询窗口起点记，不写成窗口打开之前的时刻。
        return datetime(year, month, day, POLL_START.hour, POLL_START.minute, tzinfo=CN_TZ)
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
