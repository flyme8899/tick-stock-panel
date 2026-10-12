"""每个来源独立开关。未配置凭据或群号时强制关闭，环境变量优先于页面偏好。"""

from __future__ import annotations

import hmac
import os
import re

from app.config import settings

SOURCE_ORDER = (
    "dws", "zsxq", "ima", "cls", "wscn", "glh", "etf_flow",
    "cnbc", "marketwatch", "wsj", "bloomberg", "scmp", "reuters", "reddit", "sec",
    "research",
    "ima_research",
    "notice",
)

SOURCE_LABELS = {
    "dws": "钉钉作文实时",
    "zsxq": "知识星球",
    "ima": "ima爱分享",
    "cls": "财联社",
    "wscn": "华尔街见闻",
    "glh": "格隆汇",
    "etf_flow": "ETF领航者",
    "cnbc": "CNBC",
    "marketwatch": "MarketWatch",
    "wsj": "华尔街日报市场",
    "bloomberg": "彭博",
    "scmp": "南华早报",
    "reuters": "路透",
    "reddit": "Reddit",
    "sec": "SEC 8-K",
    "research": "研报",
    "ima_research": "ima券商研报",
    "notice": "公告",
    "hot": "TSP热门候选",
}

_DEFAULT_VISION_BASE = "https://tokenhub.tencentmaas.com/v1"
_DEFAULT_VISION_MODEL = "deepseek/deepseek-v4-flash-vision-exp"
# 表格 OCR 不能关思考，预算要留给 reasoning 之后的 JSON。
VISION_OCR_MAX_TOKENS = 8192
# 短任务关掉思考后，正文不跟 reasoning 抢同一段预算。
VISION_SHORT_MAX_TOKENS = 1024
_EMAIL_IN_UA = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# 不走资讯轮询。东财研报、公告和 ima 券商全文各自有后台线程。
BACKGROUND_SOURCES = frozenset({"research", "notice", "ima_research"})

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        raw = str(getattr(settings, name.lower(), "") or "")
    if raw is None or str(raw).strip() == "":
        return None
    value = str(raw).strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def ima_configured() -> bool:
    return bool(settings.ima_client_id.strip() and settings.ima_api_key.strip())


def ima_research_kb_id() -> str:
    """个人知识库 ID。只从环境变量读取，代码里不写默认库。"""
    raw = os.environ.get("IMA_RESEARCH_KB_ID")
    if raw is not None and raw.strip():
        return raw.strip()
    return str(getattr(settings, "ima_research_kb_id", "") or "").strip()


def ima_research_configured() -> bool:
    return ima_configured() and bool(ima_research_kb_id())


def ima_research_times() -> tuple[str, ...]:
    """北京时间的拉取时刻。非法或留空时用 06:30 和 08:30。"""
    from app.research_reports.ima_schedule import parse_clocks

    raw = os.environ.get("IMA_RESEARCH_TIMES")
    if raw is None or not str(raw).strip():
        raw = str(getattr(settings, "ima_research_times", "") or "")
    return parse_clocks(str(raw)) or ("06:30", "08:30")


def zsxq_source_label(group_name: str = "") -> str:
    """单条资讯的来源名。有星球名时写成「知识星球·每日投研」。"""
    name = (group_name or "").strip()
    if not name or name.startswith("知识星球"):
        return name or SOURCE_LABELS["zsxq"]
    return f"知识星球·{name}"


_ZSXQ_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").replace("，", ",").split(",") if part.strip()]


def _parse_named_groups(raw: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in _split_csv(raw):
        gid, name = part, ""
        for sep in (":", "："):
            if sep in part:
                gid, name = part.split(sep, 1)
                break
        gid = gid.strip()
        name = name.strip()
        if not _ZSXQ_ID.fullmatch(gid) or gid in seen:
            continue
        seen.add(gid)
        found.append((gid, name))
    return found


def _parse_id_list(raw: str) -> list[tuple[str, str]]:
    """旧写法只写星球号。逗号分隔时每一段仍是星球号，不从冒号里拆名称。"""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in _split_csv(raw):
        if not _ZSXQ_ID.fullmatch(part) or part in seen:
            continue
        seen.add(part)
        found.append((part, ""))
    return found


def zsxq_groups() -> list[tuple[str, str]]:
    """(星球号, 名称)。NEWS_ZSXQ_GROUPS 非空时优先，否则读 NEWS_ZSXQ_GROUP_ID。"""
    named = _text_setting("NEWS_ZSXQ_GROUPS", "news_zsxq_groups")
    if named:
        return _parse_named_groups(named)
    return _parse_id_list(_text_setting("NEWS_ZSXQ_GROUP_ID", "news_zsxq_group_id"))


def zsxq_files_enabled() -> bool:
    """帖子 PDF 研报。留空默认下载，只有显式 false 才关。"""
    flag = _flag("NEWS_ZSXQ_FILES_ENABLED")
    return flag is None or flag


def zsxq_files_delete_after() -> bool:
    """正文入库后是否删除 PDF。留空则保留，靠天数和总量上限清盘。"""
    return _flag("NEWS_ZSXQ_FILES_DELETE_AFTER") is True


def _bounded_int(env_name: str, attr: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(env_name)
    if raw is None or not str(raw).strip():
        raw = getattr(settings, attr, "")
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    return min(hi, max(lo, value))


def zsxq_files_max_bytes() -> int:
    """单个 PDF 上限，默认 30MB。元数据已经更大的不请求下载。"""
    mb = _bounded_int("NEWS_ZSXQ_FILES_MAX_MB", "news_zsxq_files_max_mb", 30, 1, 200)
    return mb * 1024 * 1024


def zsxq_files_per_round() -> int:
    """每一轮最多下载几个 PDF。0 表示本轮不下载，收件箱仍保留元数据。"""
    return _bounded_int("NEWS_ZSXQ_FILES_PER_ROUND", "news_zsxq_files_per_round", 8, 0, 32)


def zsxq_files_gap_s() -> float:
    ms = _bounded_int("NEWS_ZSXQ_FILES_GAP_MS", "news_zsxq_files_gap_ms", 1000, 0, 60_000)
    return ms / 1000.0


def zsxq_files_keep_days() -> int:
    """已抽取的 PDF 保留天数。0 表示不按天数删除。"""
    return _bounded_int("NEWS_ZSXQ_FILES_KEEP_DAYS", "news_zsxq_files_keep_days", 60, 0, 3650)


def zsxq_files_max_total_bytes() -> int:
    """已抽取 PDF 的总量上限。0 表示不按总量删除。默认 2GB。"""
    mb = _bounded_int(
        "NEWS_ZSXQ_FILES_MAX_TOTAL_MB", "news_zsxq_files_max_total_mb", 2048, 0, 102_400
    )
    return mb * 1024 * 1024


def zsxq_ocr_max_per_round() -> int:
    """扫描版 PDF 每轮最多 OCR 几份。0 表示不 OCR。默认 5。"""
    return _bounded_int(
        "NEWS_ZSXQ_OCR_MAX_PER_ROUND", "news_zsxq_ocr_max_per_round", 5, 0, 32
    )


def group_id(source: str) -> str | list[tuple[str, str]]:
    """钉钉返回群号。知识星球返回 [(星球号, 名称), ...]。留空表示未配置。"""
    if source == "dws":
        return _text_setting("NEWS_DWS_GROUP_ID", "news_dws_group_id")
    if source == "zsxq":
        return zsxq_groups()
    return ""


def _text_setting(env_name: str, attr: str) -> str:
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        return raw.strip()
    return str(getattr(settings, attr, "") or "").strip()


def vision_api_key() -> str:
    """视觉模型密钥。不回落到文本模型的 AI_API_KEY。"""
    return _text_setting("VISION_AI_API_KEY", "vision_ai_api_key")


def vision_base_url() -> str:
    return (
        _text_setting("VISION_AI_BASE_URL", "vision_ai_base_url") or _DEFAULT_VISION_BASE
    ).rstrip("/")


def vision_model() -> str:
    """空白时用 deepseek 视觉模型。glm-5.3-flash 和 mimo-v2.6-flash 只是备选。"""
    return _text_setting("VISION_AI_MODEL", "vision_ai_model") or _DEFAULT_VISION_MODEL


def vision_generation(model: str, *, ocr: bool) -> dict:
    """表格 OCR 保持思考。只有非表格的短任务才按模型尝试关掉思考。

    deepseek 视觉模型可以传 thinking.type=disabled，但关掉后表格识别变差。
    glm-5.3-flash 传这个字段会 400，短任务只用 reasoning_effort=low。
    mimo-v2.6-flash 可以干净关掉思考，识别并不更好，所以不改默认模型。
    """
    if ocr:
        return {"max_tokens": VISION_OCR_MAX_TOKENS}
    name = (model or "").lower()
    if "glm-5.3-flash" in name:
        return {"max_tokens": VISION_SHORT_MAX_TOKENS, "reasoning_effort": "low"}
    if "deepseek-v4-flash-vision" in name or "mimo-v2.6-flash" in name:
        return {"max_tokens": VISION_SHORT_MAX_TOKENS, "thinking": {"type": "disabled"}}
    return {"max_tokens": VISION_OCR_MAX_TOKENS}


def sec_user_agent() -> str:
    """SEC 要求 User-Agent 里带联系邮箱。空字符串表示未配置。"""
    raw = os.environ.get("SEC_USER_AGENT")
    if raw is not None and raw.strip():
        return raw.strip()
    return str(getattr(settings, "sec_user_agent", "") or "").strip()


def sec_configured() -> bool:
    return _EMAIL_IN_UA.search(sec_user_agent()) is not None


_REDDIT_NAME = re.compile(r"^[A-Za-z0-9_]{2,21}$")
_REDDIT_DEFAULT_SUBS = ("wallstreetbets", "stocks", "investing")


def reddit_subreddits() -> tuple[str, ...]:
    """逗号分隔。留空用默认三个子版。写了但没有合法名字时返回空，调用方不再请求。"""
    env = os.environ.get("NEWS_REDDIT_SUBREDDITS")
    if env is not None and env.strip():
        return _parse_reddit_subs(env)
    saved = str(getattr(settings, "news_reddit_subreddits", "") or "")
    if saved.strip():
        return _parse_reddit_subs(saved)
    return _REDDIT_DEFAULT_SUBS


def _parse_reddit_subs(raw: str) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        name = part.strip()
        if name.lower().startswith("r/"):
            name = name[2:].strip()
        name = name.lower()
        if _REDDIT_NAME.fullmatch(name) is None or name in seen:
            continue
        seen.add(name)
        found.append(name)
    return tuple(found)


def reddit_oauth_ready() -> bool:
    """凭据是否都已填写。采集不调用这里，也不换 token。"""
    client_id = _text_setting("REDDIT_CLIENT_ID", "reddit_client_id")
    secret = _text_setting("REDDIT_CLIENT_SECRET", "reddit_client_secret")
    return bool(client_id and secret)


def source_configured(source: str) -> bool:
    if source == "notice":
        return True
    if source == "etf_flow":
        return bool(vision_api_key())
    if source == "ima":
        return ima_configured()
    if source == "ima_research":
        return ima_research_configured()
    if source in {"dws", "zsxq"}:
        return bool(group_id(source))
    if source == "sec":
        return sec_configured()
    return source in SOURCE_LABELS


def source_enabled(source: str) -> bool:
    if source == "notice":
        from app.event_context.config import notice_enabled

        return notice_enabled()
    if not source_configured(source):
        return False
    env_name = f"NEWS_{source.upper()}_ENABLED"
    flag = _flag(env_name)
    if flag is not None:
        return flag
    from app.services import preferences

    saved = preferences.load().get("news_sources") or {}
    row = saved.get(source) if isinstance(saved, dict) else None
    if isinstance(row, dict) and "enabled" in row:
        return bool(row.get("enabled"))
    # 研报和格隆汇没有凭据。没写偏好、环境变量也留空时默认打开。
    return source in {"research", "glh"}


def source_locked(source: str) -> bool:
    if source == "notice":
        return True
    return _flag(f"NEWS_{source.upper()}_ENABLED") is not None


def llm_extract_enabled() -> bool:
    return _flag("NEWS_LLM_EXTRACT") is True


def feed_token() -> str:
    return (settings.news_dsa_feed_token or os.environ.get("NEWS_DSA_FEED_TOKEN") or "").strip()


def feed_matches(presented: str) -> bool:
    """只比较请求头里的令牌。长度不同时按不匹配处理，避免抛错。"""
    expected = feed_token()
    if not expected or not presented:
        return False
    try:
        return hmac.compare_digest(str(presented), str(expected))
    except (TypeError, ValueError):
        return False


PUSH_TYPES = ("hot", "abnormal", "t_trade", "hot_verified", "abnormal_verified")

_PUSH_ENV = {
    "hot": "NEWS_PUSH_HOT_ENABLED",
    "abnormal": "NEWS_PUSH_ABNORMAL_ENABLED",
    "t_trade": "NEWS_PUSH_T_ENABLED",
    "hot_verified": "NEWS_PUSH_HOT_VERIFIED",
    "abnormal_verified": "NEWS_PUSH_ABNORMAL_VERIFIED",
}


def _push_prefs() -> dict:
    from app.services import preferences
    saved = preferences.load().get("news_push") or {}
    return saved if isinstance(saved, dict) else {}


def webhook_configured() -> bool:
    return bool((settings.dingtalk_webhook_url or "").strip())


def push_master_enabled() -> bool:
    """总开关。环境变量优先，缺省关闭。没配 webhook 时也关闭。"""
    if not webhook_configured():
        return False
    flag = _flag("NEWS_PUSH_ENABLED")
    if flag is not None:
        return flag
    return bool(_push_prefs().get("enabled"))


def push_master_locked() -> bool:
    return _flag("NEWS_PUSH_ENABLED") is not None


def push_type_saved(type_id: str) -> bool:
    if type_id not in PUSH_TYPES:
        return False
    flag = _flag(_PUSH_ENV[type_id])
    if flag is not None:
        return flag
    types = _push_prefs().get("types") or {}
    if isinstance(types, dict):
        return bool(types.get(type_id))
    return False


def push_type_enabled(type_id: str) -> bool:
    return push_master_enabled() and push_type_saved(type_id)


def push_type_locked(type_id: str) -> bool:
    return type_id in _PUSH_ENV and _flag(_PUSH_ENV[type_id]) is not None


def set_push_prefs(*, enabled: bool | None = None, types: dict[str, bool] | None = None) -> dict:
    """页面开关。环境变量锁定的项不改。未配机器人时不能打开。"""
    if (enabled or any((types or {}).values())) and not webhook_configured():
        raise ValueError("未配置钉钉机器人")
    current = _push_prefs()
    saved_enabled = bool(current.get("enabled"))
    saved_types = dict(current.get("types") or {})
    if enabled is not None and not push_master_locked():
        saved_enabled = bool(enabled)
    for type_id, value in (types or {}).items():
        if type_id not in PUSH_TYPES or push_type_locked(type_id):
            continue
        saved_types[type_id] = bool(value)
    from app.services import preferences
    preferences.save({"news_push": {"enabled": saved_enabled, "types": saved_types}})
    return push_status()


def push_master_saved() -> bool:
    flag = _flag("NEWS_PUSH_ENABLED")
    if flag is not None:
        return flag
    return bool(_push_prefs().get("enabled"))


def push_status() -> dict:
    summaries = {
        "hot": "交易日盘前和收盘后各一次，候选明显变化时再补一条",
        "abnormal": "自选股的涨停、炸板、跌停、新高新低，从无到有才推",
        "t_trade": "自选相对分时均价、日内高低和昨收的边沿提醒",
        "hot_verified": "开启后，热点候选只保留已经竞价或盘面验证的事件",
        "abnormal_verified": "开启后，异动监控附上同一标的已经验证的事件名",
    }
    labels = {
        "hot": "热点候选",
        "abnormal": "异动监控",
        "t_trade": "做T提醒",
        "hot_verified": "热点只推已验证",
        "abnormal_verified": "异动附带已验证事件",
    }
    return {
        "configured": webhook_configured(),
        "master_enabled": push_master_enabled(),
        "master_saved": push_master_saved(),
        "master_locked": push_master_locked(),
        "types": [
            {
                "id": type_id,
                "label": labels[type_id],
                "enabled": push_type_enabled(type_id),
                "saved": push_type_saved(type_id),
                "locked": push_type_locked(type_id),
                "summary": summaries[type_id],
            }
            for type_id in PUSH_TYPES
        ],
    }


def set_source_enabled(source: str, enabled: bool) -> bool:
    """页面开关。环境变量锁定时不改偏好，返回实际是否开启。"""
    if source not in SOURCE_ORDER:
        raise ValueError(f"未知来源 {source}")
    if source_locked(source) or not source_configured(source):
        return source_enabled(source)
    from app.services import preferences

    current = preferences.load()
    saved = dict(current.get("news_sources") or {})
    row = dict(saved.get(source) or {})
    row["enabled"] = bool(enabled)
    saved[source] = row
    preferences.save({"news_sources": saved})
    return source_enabled(source)
