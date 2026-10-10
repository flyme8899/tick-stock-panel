"""HTTP proxy from TSP to the vendored daily_stock_analysis API."""
from __future__ import annotations

import os
import threading
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

import httpx

from app.custom.dsa.etf_job import (
    ETF_UPSTREAM_PATH,
    INTERNAL_TOKEN_HEADER,
    TOKEN_MISSING_DETAIL,
    configured_token,
    token_usable,
)

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

# 只放行这一条内部任务，不把整个 tsp/ 前缀交给转发层。
ALLOWED_EXACT_PATHS = frozenset({ETF_UPSTREAM_PATH})

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
    if path == ETF_UPSTREAM_PATH:
        default = 200.0
    elif path.endswith("/share-image"):
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
    normalized = "/".join(parts)
    if normalized not in ALLOWED_EXACT_PATHS and parts[0] not in ALLOWED_PREFIXES:
        raise InvalidUpstreamPathError("未开放的上游路径")
    if len(normalized) > 512:
        raise InvalidUpstreamPathError("路径过长")
    return normalized


# 上游会话。DSA 的 dsa_session 会过期；只靠手工填的 DSA_UPSTREAM_COOKIE
# 时，过期后需要登录的接口会 401。配了 DSA_PASSWORD 才自动登录。
# 没配密码时不读、不改静态 cookie，行为与原来一致。
# 过期时间来自 Set-Cookie 的 Max-Age / Expires，并用 cookie 里的签发时间
# 做绝对截止（签发 + Max-Age），不写死 24 小时。提前一个安全边际刷新。
_SESSION_COOKIE_NAME = "dsa_session"
_SESSION_REFRESH_MARGIN_SEC = 600
# 登录失败后退避。状态接口约 30 秒轮询一次；60 秒起、封顶 300 秒，
# 保证不会在 DSA 的 5 次/300 秒 IP 限额里打满。
_LOGIN_BACKOFF_BASE_SEC = 60
_LOGIN_BACKOFF_MAX_SEC = 300
_session_lock = threading.Lock()
_session: dict[str, object] = {
    "cookie": "",
    "expire_at": 0.0,
    "next_login_at": 0.0,
    "login_failures": 0,
}


def _reset_session_cache() -> None:
    """测试用。清掉进程内会话，避免用例之间串缓存。"""
    with _session_lock:
        _session["cookie"] = ""
        _session["expire_at"] = 0.0
        _session["next_login_at"] = 0.0
        _session["login_failures"] = 0


def _static_cookie() -> str:
    return os.getenv("DSA_UPSTREAM_COOKIE", "").strip()


def _password_configured() -> bool:
    return bool(os.getenv("DSA_PASSWORD", "").strip())


def _cookie_creation_ts(token: str) -> float | None:
    """DSA 会话形如 nonce.签发秒.签名。中间段是签发时间，不是截止时间。"""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    return float(parts[1])


def _parse_http_date(value: str) -> float | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _apply_refresh_margin(absolute: float, now: float) -> float | None:
    remaining = absolute - now
    if remaining <= 1:
        return None
    margin = min(_SESSION_REFRESH_MARGIN_SEC, remaining / 2)
    return absolute - margin


def _session_deadline(
    token: str,
    max_age: int | None,
    expires_at: float | None,
    now: float,
) -> float | None:
    """绝对过期时刻减去安全边际。无法从响应里算出截止时间时返回 None。"""
    created = _cookie_creation_ts(token)
    deadlines: list[float] = []
    if max_age is not None and max_age > 0:
        deadlines.append((created if created is not None else now) + max_age)
    if expires_at is not None:
        deadlines.append(expires_at)
    if not deadlines:
        return None
    return _apply_refresh_margin(min(deadlines), now)


def _parse_session_set_cookie(raw: str, now: float) -> tuple[str, float] | None:
    """从一条 Set-Cookie 取出 dsa_session 和应刷新的时刻。解析失败返回 None。"""
    if _SESSION_COOKIE_NAME not in raw:
        return None
    token = ""
    max_age: int | None = None
    expires_at: float | None = None
    for part in raw.split(";"):
        item = part.strip()
        lower = item.lower()
        if lower.startswith(_SESSION_COOKIE_NAME + "="):
            token = item.split("=", 1)[1].strip().strip('"')
        elif lower.startswith("max-age="):
            try:
                max_age = int(item.split("=", 1)[1].strip())
            except ValueError:
                max_age = None
        elif lower.startswith("expires="):
            expires_at = _parse_http_date(item.split("=", 1)[1].strip())
    if not token or any(ord(char) < 32 or char in " ;," for char in token):
        return None
    if len(token) > 512:
        return None
    deadline = _session_deadline(token, max_age, expires_at, now)
    if deadline is None:
        return None
    return f"{_SESSION_COOKIE_NAME}={token}", deadline


def _login_upstream(password: str, now: float) -> tuple[str, float] | None:
    """用密码换会话。失败返回 None。不记录密码，也不把 Set-Cookie 交给浏览器。"""
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as client:
            response = client.post(
                f"{base_url()}/api/v1/auth/login",
                json={"password": password},
            )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    headers = response.headers
    raw_values = headers.get_list("set-cookie") if hasattr(headers, "get_list") else []
    if not raw_values:
        single = headers.get("set-cookie", "")
        raw_values = [single] if single else []
    for raw in raw_values:
        parsed = _parse_session_set_cookie(raw, now)
        if parsed is not None:
            return parsed
    return None


def _note_login_failure_locked(now: float) -> None:
    failures = int(_session.get("login_failures") or 0) + 1
    delay = min(_LOGIN_BACKOFF_MAX_SEC, _LOGIN_BACKOFF_BASE_SEC * (2 ** (failures - 1)))
    _session["login_failures"] = failures
    _session["next_login_at"] = now + delay
    _session["cookie"] = ""
    _session["expire_at"] = 0.0


def _store_session_locked(cookie: str, expire_at: float) -> None:
    _session["cookie"] = cookie
    _session["expire_at"] = expire_at
    _session["login_failures"] = 0
    _session["next_login_at"] = 0.0


def _login_locked(password: str, now: float) -> str:
    """调用方已持有 _session_lock。登录进行中其它线程会等这一次结果。"""
    if now < float(_session.get("next_login_at") or 0.0):
        return _static_cookie()
    fresh = _login_upstream(password, now)
    if fresh is None:
        _note_login_failure_locked(now)
        return _static_cookie()
    _store_session_locked(fresh[0], fresh[1])
    return fresh[0]


def _upstream_cookie() -> str:
    """有密码时返回缓存会话；否则原样返回 DSA_UPSTREAM_COOKIE。"""
    static = _static_cookie()
    password = os.getenv("DSA_PASSWORD", "").strip()
    if not password:
        return static
    with _session_lock:
        now = time.time()
        cached = str(_session.get("cookie") or "")
        if cached and now < float(_session.get("expire_at") or 0.0):
            return cached
        return _login_locked(password, now)


def _invalidate_and_relogin(rejected_cookie: str) -> bool:
    """401 后丢掉被拒绝的会话并只登录一次。处于失败退避时不再打登录接口。"""
    password = os.getenv("DSA_PASSWORD", "").strip()
    if not password:
        return False
    with _session_lock:
        now = time.time()
        current = str(_session.get("cookie") or "")
        if (
            current
            and current != rejected_cookie
            and now < float(_session.get("expire_at") or 0.0)
        ):
            return True
        if now < float(_session.get("next_login_at") or 0.0):
            _session["cookie"] = ""
            _session["expire_at"] = 0.0
            return False
        _session["cookie"] = ""
        _session["expire_at"] = 0.0
        fresh = _login_locked(password, now)
    return bool(fresh) and fresh != _static_cookie()


def _suppress_further_refresh() -> None:
    """重登后仍然 401：保留新会话，但短时间内不再登录，避免把上游打进限流。"""
    with _session_lock:
        now = time.time()
        earliest = now + _LOGIN_BACKOFF_BASE_SEC
        if float(_session.get("next_login_at") or 0.0) < earliest:
            _session["next_login_at"] = earliest


def _request_headers(content_type: str | None, path: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if content_type:
        headers["content-type"] = content_type
    cookie = _upstream_cookie()
    if cookie:
        headers["cookie"] = cookie
    if path == ETF_UPSTREAM_PATH:
        token = configured_token()
        if not token_usable(token):
            raise UpstreamError(TOKEN_MISSING_DETAIL)
        headers[INTERNAL_TOKEN_HEADER] = token
    return headers


def _is_internal_token_rejection(response: httpx.Response) -> bool:
    """轮动入口自己的 401 不是会话过期，不能拿去触发自动登录。"""
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("ok") is False and body.get("detail") == "未授权"


def _should_refresh_on_unauthorized(path: str) -> bool:
    # auth/* 是登录本身或登录状态，401 不能再触发一次自动登录。
    return _password_configured() and not path.startswith("auth/")


def forward(
    method: str,
    path: str,
    *,
    params: list[tuple[str, str]] | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: float | None = None,
) -> tuple[int, bytes, str, dict[str, str]]:
    """Return status, body, media type and a small set of response headers."""
    if not enabled():
        raise UpstreamError("未配置 DSA_BASE_URL，决策服务未启用")
    normalized = normalize_upstream_path(path)
    if body and len(body) > _MAX_BODY:
        raise UpstreamError("请求体过大", status_code=413)
    url = f"{base_url()}/api/v1/{normalized}"
    client_timeout = timeout_for(normalized) if timeout is None or timeout <= 0 else timeout
    try:
        with httpx.Client(timeout=client_timeout, follow_redirects=False) as client:
            headers = _request_headers(content_type, normalized)
            response = client.request(
                method.upper(),
                url,
                params=params,
                content=body if body else None,
                headers=headers,
            )
            if (
                response.status_code == 401
                and not _is_internal_token_rejection(response)
                and _should_refresh_on_unauthorized(normalized)
                and _invalidate_and_relogin(headers.get("cookie", ""))
            ):
                response = client.request(
                    method.upper(),
                    url,
                    params=params,
                    content=body if body else None,
                    headers=_request_headers(content_type, normalized),
                )
                if response.status_code == 401:
                    _suppress_further_refresh()
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
