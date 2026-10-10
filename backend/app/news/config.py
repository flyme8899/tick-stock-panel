"""每个来源独立开关。未配置凭据或群号时强制关闭，环境变量优先于页面偏好。"""

from __future__ import annotations

import hmac
import os
import re

from app.config import settings

SOURCE_ORDER = (
    "dws", "zsxq", "ima", "cls", "wscn", "etf_flow",
    "cnbc", "marketwatch", "wsj", "bloomberg", "scmp", "reuters", "reddit", "sec",
)

SOURCE_LABELS = {
    "dws": "钉钉作文实时",
    "zsxq": "知识星球纳指星球调研",
    "ima": "ima爱分享",
    "cls": "财联社",
    "wscn": "华尔街见闻",
    "etf_flow": "ETF领航者",
    "cnbc": "CNBC",
    "marketwatch": "MarketWatch",
    "wsj": "华尔街日报市场",
    "bloomberg": "彭博",
    "scmp": "南华早报",
    "reuters": "路透",
    "reddit": "Reddit",
    "sec": "SEC 8-K",
    "hot": "TSP热门候选",
}

_DEFAULT_VISION_BASE = "https://tokenhub.tencentmaas.com/v1"
_DEFAULT_VISION_MODEL = "deepseek/deepseek-v4-flash-vision-exp"
# 表格 OCR 不能关思考，预算要留给 reasoning 之后的 JSON。
VISION_OCR_MAX_TOKENS = 8192
# 短任务关掉思考后，正文不跟 reasoning 抢同一段预算。
VISION_SHORT_MAX_TOKENS = 1024
_EMAIL_IN_UA = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

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


def group_id(source: str) -> str:
    """钉钉群号或知识星球号。留空表示这个来源未配置。"""
    if source == "dws":
        env_name, attr = "NEWS_DWS_GROUP_ID", "news_dws_group_id"
    elif source == "zsxq":
        env_name, attr = "NEWS_ZSXQ_GROUP_ID", "news_zsxq_group_id"
    else:
        return ""
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        return raw.strip()
    return str(getattr(settings, attr, "") or "").strip()


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
    if source == "etf_flow":
        return bool(vision_api_key())
    if source == "ima":
        return ima_configured()
    if source in {"dws", "zsxq"}:
        return bool(group_id(source))
    if source == "sec":
        return sec_configured()
    return source in SOURCE_LABELS


def source_enabled(source: str) -> bool:
    if not source_configured(source):
        return False
    env_name = f"NEWS_{source.upper()}_ENABLED"
    flag = _flag(env_name)
    if flag is not None:
        return flag
    from app.services import preferences

    saved = preferences.load().get("news_sources") or {}
    row = saved.get(source) if isinstance(saved, dict) else None
    if isinstance(row, dict):
        return bool(row.get("enabled"))
    return False


def source_locked(source: str) -> bool:
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


PUSH_TYPES = ("hot", "abnormal", "t_trade")

_PUSH_ENV = {
    "hot": "NEWS_PUSH_HOT_ENABLED",
    "abnormal": "NEWS_PUSH_ABNORMAL_ENABLED",
    "t_trade": "NEWS_PUSH_T_ENABLED",
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
    }
    labels = {"hot": "热点候选", "abnormal": "异动监控", "t_trade": "做T提醒"}
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
