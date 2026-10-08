"""HTTP proxy from TSP to the vendored daily_stock_analysis API."""
from __future__ import annotations

import os
import time
from urllib.parse import unquote

import httpx

ALLOWED_PREFIXES = frozenset(
    {
        "health",
        "auth",
        "analysis",
        "history",
        "stocks",
        "agent",
        "portfolio",
        "alerts",
        "decision-signals",
        "screening",
        "data",
        "intelligence",
        "system",
        "usage",
        "backtest",
    }
)

_LONG_PREFIXES = frozenset({"analysis", "agent", "screening", "backtest", "intelligence"})
_MAX_BODY = 2_500_000
_MAX_RESPONSE = 12_000_000


class UpstreamError(Exception):
    """Sidecar is disabled or cannot be reached. The message is safe to show."""

    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


class InvalidUpstreamPathError(Exception):
    pass


def base_url() -> str:
    return os.getenv("DSA_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")


def enabled() -> bool:
    return bool(base_url())


def timeout_for(path: str) -> float:
    raw = os.getenv("DSA_TIMEOUT_SECONDS", "").strip()
    if path.endswith("/share-image"):
        default = 90.0
    elif path.split("/", 1)[0] in _LONG_PREFIXES:
        default = 120.0
    else:
        default = 20.0
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def normalize_upstream_path(path: str) -> str:
    raw = unquote(path or "").strip()
    if not raw or raw.startswith(("/", "\\")) or "\\" in raw or "://" in raw:
        raise InvalidUpstreamPathError("路径无效")
    parts = [unquote(part) for part in raw.split("/")]
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidUpstreamPathError("路径无效")
    if parts[0] not in ALLOWED_PREFIXES:
        raise InvalidUpstreamPathError("未开放的上游路径")
    if len(raw) > 512:
        raise InvalidUpstreamPathError("路径过长")
    return "/".join(parts)


# 上游会话缓存。为什么需要:
#   DSA 的登录 cookie (dsa_session) 只有 24 小时有效期, 而 DSA_UPSTREAM_COOKIE 是
#   手工填的静态值 —— 填进去当天能用, 第二天全部需要认证的接口就一起 401,
#   而这类失败在页面上只会显示"上游未连接", 很难查。
#   所以配了 DSA_PASSWORD 时改为自动登录换取会话并缓存, 过期前自动续。
#   没配 DSA_PASSWORD 就完全退回原来的静态 cookie 行为, 不影响既有部署。
_SESSION_COOKIE_NAME = "dsa_session"
_SESSION_TTL_SEC = 24 * 3600
_SESSION_REFRESH_MARGIN_SEC = 600
_session: dict[str, object] = {"cookie": "", "expire_at": 0.0}


def _login_upstream(password: str) -> str:
    """用密码换一个上游会话 cookie。失败返回空串（调用方会退回静态配置）。"""
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            response = client.post(
                f"{base_url()}/api/v1/auth/login", json={"password": password}
            )
        if response.status_code >= 400:
            return ""
        raw = response.headers.get("set-cookie", "")
        if _SESSION_COOKIE_NAME not in raw:
            return ""
        token = raw.split(_SESSION_COOKIE_NAME + "=", 1)[1].split(";")[0].strip()
        return f"{_SESSION_COOKIE_NAME}={token}" if token else ""
    except Exception:  # noqa: BLE001 - 登录失败不应让转发直接崩掉
        return ""


def _upstream_cookie() -> str:
    """优先返回自动登录获得的新鲜会话，否则退回手工配置的 DSA_UPSTREAM_COOKIE。"""
    static = os.getenv("DSA_UPSTREAM_COOKIE", "").strip()
    password = os.getenv("DSA_PASSWORD", "").strip()
    if not password:
        return static

    now = time.time()
    cached = str(_session.get("cookie") or "")
    if cached and now < float(_session.get("expire_at") or 0.0):
        return cached

    fresh = _login_upstream(password)
    if fresh:
        _session["cookie"] = fresh
        _session["expire_at"] = now + _SESSION_TTL_SEC - _SESSION_REFRESH_MARGIN_SEC
        return fresh

    # 自动登录拿不到会话时, 让下一次请求再试, 同时退回静态配置兜底。
    _session["cookie"] = ""
    _session["expire_at"] = 0.0
    return static


def _request_headers(content_type: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if content_type:
        headers["content-type"] = content_type
    cookie = _upstream_cookie()
    if cookie:
        headers["cookie"] = cookie
    return headers


def forward(
    method: str,
    path: str,
    *,
    params: list[tuple[str, str]] | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, bytes, str, dict[str, str]]:
    """Return status, body, media type and a small set of response headers."""
    if not enabled():
        raise UpstreamError("未配置 DSA_BASE_URL，决策服务未启用")
    normalized = normalize_upstream_path(path)
    if body and len(body) > _MAX_BODY:
        raise UpstreamError("请求体过大", status_code=413)
    url = f"{base_url()}/api/v1/{normalized}"
    try:
        with httpx.Client(timeout=timeout_for(normalized), follow_redirects=False) as client:
            response = client.request(
                method.upper(),
                url,
                params=params,
                content=body if body else None,
                headers=_request_headers(content_type),
            )
    except httpx.TimeoutException as exc:
        raise UpstreamError("决策服务响应超时") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError("决策服务未连接") from exc
    payload = response.content
    if len(payload) > _MAX_RESPONSE:
        raise UpstreamError("上游响应过大", status_code=502)
    media = response.headers.get("content-type", "application/json")
    extra: dict[str, str] = {}
    disposition = response.headers.get("content-disposition")
    if disposition:
        extra["content-disposition"] = disposition
    return response.status_code, payload, media, extra


def health() -> tuple[bool, str]:
    if not enabled():
        return False, "未配置 DSA_BASE_URL"
    try:
        status, payload, _media, _extra = forward("GET", "health")
    except (UpstreamError, InvalidUpstreamPathError) as exc:
        return False, str(exc)
    if status >= 400:
        text = payload.decode("utf-8", errors="replace")[:180]
        return False, text or f"HTTP {status}"
    return True, "已连接"
