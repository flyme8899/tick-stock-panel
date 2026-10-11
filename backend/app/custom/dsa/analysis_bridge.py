"""把 TSP 个股上下文和价位登记接进 DSA。

DSA 进程启动时由 dsa_bootstrap 调用 install()。不改 vendor 文件。
令牌或网络失败只记日志，分析照常进行。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("tsp.dsa.analysis_bridge")

CN = timezone(timedelta(hours=8))
_CONTEXT_PATH = "/api/dsa/stock-context"
_SNIPER_PATH = "/api/dsa/sniper-lines"
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal", "app", "tsp"}
_LEVEL_KEYS = ("ideal_buy", "stop_loss", "take_profit")
_MAX_BLOCK = 2000


def feed_base_url() -> str:
    raw = (os.environ.get("TSP_NEWS_BASE_URL") or "http://127.0.0.1:3018").strip().rstrip("/")
    return raw


def feed_token() -> str:
    return (os.environ.get("NEWS_DSA_FEED_TOKEN") or "").strip()


def _allowed(url: str, path: str) -> bool:
    parsed = urlparse(url)
    if (parsed.path or "").rstrip("/") != path:
        return False
    host = (parsed.hostname or "").lower()
    allowed = set(_ALLOWED_HOSTS)
    extra = urlparse(feed_base_url()).hostname
    if extra:
        allowed.add(extra.lower())
    return host in allowed


def context_url() -> str:
    return f"{feed_base_url()}{_CONTEXT_PATH}"


def sniper_url() -> str:
    return f"{feed_base_url()}{_SNIPER_PATH}"


def merge_news(existing: str | None, block: str | None) -> str | None:
    text = (block or "").strip()
    if not text:
        return existing
    if len(text) > _MAX_BLOCK:
        text = text[: _MAX_BLOCK - 1].rstrip() + "…"
    if existing and text in existing:
        return existing
    if existing:
        return f"{text}\n\n{existing}"
    return text


def sniper_payload(result: Any, *, query_id: str = "", history_id: int = 0) -> dict | None:
    if result is None or getattr(result, "success", True) is False:
        return None
    points = _sniper_points(result)
    levels = {key: _parse_price(points.get(key)) for key in _LEVEL_KEYS}
    if not any(value is not None for value in levels.values()):
        return None
    analysis_id = str(history_id) if isinstance(history_id, int) and history_id > 0 else str(query_id or "")
    return {
        "symbol": str(getattr(result, "code", "") or "")[:32],
        "name": str(getattr(result, "name", "") or "")[:40],
        "analysis_id": analysis_id[:64],
        "analyzed_at": datetime.now(CN).isoformat(timespec="seconds"),
        "ideal_buy": levels["ideal_buy"],
        "stop_loss": levels["stop_loss"],
        "take_profit": levels["take_profit"],
    }


def fetch_context_text(symbol: str, *, getter=None) -> str:
    payload = getter(symbol) if getter is not None else _get_json(context_url(), {"symbol": symbol})
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("text") or "")


def post_sniper(body: dict, *, poster=None) -> None:
    if poster is not None:
        poster(body)
        return
    _post_json(sniper_url(), body)


def wrap_context_loader(original, fetch):
    def wrapped(self, *, code, stock_name, market, limit=6):
        base = original(self, code=code, stock_name=stock_name, market=market, limit=limit)
        try:
            block = fetch(code)
        except Exception:  # noqa: BLE001 — 上下文失败不能挡住分析
            logger.warning("读取 TSP 个股上下文失败 %s", code, exc_info=True)
            return base
        return merge_news(base, block)

    wrapped._tsp_stock_context = True  # type: ignore[attr-defined]
    return wrapped


def wrap_history_save(original, post):
    def wrapped(self, result, query_id, report_type, news_content, context_snapshot=None, save_snapshot=True):
        saved = original(
            self,
            result,
            query_id,
            report_type,
            news_content,
            context_snapshot=context_snapshot,
            save_snapshot=save_snapshot,
        )
        try:
            history_id = saved if isinstance(saved, int) and not isinstance(saved, bool) else 0
            body = sniper_payload(result, query_id=str(query_id or ""), history_id=history_id)
            if body:
                post(body)
        except Exception:  # noqa: BLE001 — 登记失败不能挡住分析保存
            logger.warning("登记 DSA 价位失败", exc_info=True)
        return saved

    wrapped._tsp_sniper = True  # type: ignore[attr-defined]
    return wrapped


def install() -> None:
    if not feed_token():
        logger.info("未配置 NEWS_DSA_FEED_TOKEN，DSA 不注入个股上下文")
        return
    try:
        import src.core.pipeline as pipeline
        import src.storage as storage
    except ImportError:
        logger.info("当前不是 DSA 进程，跳过个股上下文")
        return
    pipeline_cls = getattr(pipeline, "StockAnalysisPipeline", None)
    history_cls = getattr(storage, "DatabaseManager", None)
    if pipeline_cls is None or history_cls is None:
        logger.info("DSA 分析入口不完整，跳过个股上下文")
        return
    current = pipeline_cls._load_persisted_intelligence_context
    if not getattr(current, "_tsp_stock_context", False):
        pipeline_cls._load_persisted_intelligence_context = wrap_context_loader(current, fetch_context_text)
    saver = history_cls.save_analysis_history
    if not getattr(saver, "_tsp_sniper", False):
        history_cls.save_analysis_history = wrap_history_save(saver, post_sniper)
    logger.info("已安装个股上下文与价位登记：%s", feed_base_url())


def _sniper_points(result: Any) -> dict:
    getter = getattr(result, "get_sniper_points", None)
    if callable(getter):
        raw = getter() or {}
        if isinstance(raw, dict) and _has_price(raw):
            return raw
    dashboard = getattr(result, "dashboard", None)
    if isinstance(dashboard, dict):
        battle = dashboard.get("battle_plan")
        if isinstance(battle, dict) and isinstance(battle.get("sniper_points"), dict):
            return battle["sniper_points"]
    return {}


def _has_price(points: dict) -> bool:
    return any(_parse_price(points.get(key)) is not None for key in _LEVEL_KEYS)


def _parse_price(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("元", "")
        if not text or text in {"-", "—", "无", "N/A", "null", "None"}:
            return None
        token = []
        started = False
        for char in text:
            if char.isdigit() or (char == "." and started):
                token.append(char)
                started = True
                continue
            if started:
                break
        if not token:
            return None
        try:
            number = float("".join(token))
        except ValueError:
            return None
    else:
        return None
    if number > 0 and number == number and number != float("inf"):
        return round(number, 4)
    return None


def _headers() -> dict:
    return {"X-News-Feed-Token": feed_token(), "User-Agent": "tsp-dsa-analysis/1.0"}


def _get_json(url: str, params: dict) -> dict:
    import requests

    if not feed_token() or not _allowed(url, _CONTEXT_PATH):
        return {}
    response = requests.get(url, params=params, headers=_headers(), timeout=8)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _post_json(url: str, body: dict) -> None:
    import requests

    if not feed_token() or not _allowed(url, _SNIPER_PATH):
        return
    response = requests.post(url, json=body, headers=_headers(), timeout=8)
    response.raise_for_status()
