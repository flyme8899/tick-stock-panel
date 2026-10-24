"""未授权访问面: 一条测试守住"什么在不登录的情况下可达"。

分散在多个文件里的安全断言容易各自漂移 —— 改一处白名单, 另一处的测试可能还在过。
这里把**对外的可达性契约**收成一张表, 一个入口:

| 路径 | 未认证预期 | 理由 |
|---|---|---|
| ``/docs`` ``/redoc`` ``/openapi.json`` | 404 | 默认不挂载 (攻击面不用猜) |
| ``/health`` ``/api/health`` | 200 | 探活本身就要匿名可打 |
| ``/api/news/dsa-feed`` 带错令牌 | 404 (不是 401) | 401 会把内部地址暴露成可探测接口 |
| ``/api/fund-flow/dsa-context`` 带错令牌 | 404 | 同上 |
| ``/api/dsa/stock-context`` ``/api/dsa/sniper-lines`` 带错令牌 | 404 | 同上 |
| ``/api/watchlist`` 无会话 | 401/403 | 业务数据一律要认证 |

另外守住两个**默认值**: CORS 默认仍是 ``*``(自托管局域网不能一升级就白屏),
cookie ``secure`` 默认仍是 False(纯 http 部署不能被锁在登录页外), 两者都要能被
环境变量显式打开。
"""
from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient

DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
# /health 由 app/api/routes.py 注册; 各业务 router 另有自己的 /api/<域>/health。
# 认证中间件只拦 /api/ 前缀, 所以只有根路径的 /health 是真"匿名可达"的探活端点。
HEALTH_PATHS = ("/health",)
DSA_PATHS_WITH_TOKEN_HEADER = (
    "/api/news/dsa-feed",
    "/api/fund-flow/dsa-context",
    "/api/dsa/stock-context",
    "/api/dsa/sniper-lines",
)


def _fresh_main(monkeypatch: pytest.MonkeyPatch, **env: str | None):
    """按给定环境变量重建 ``app.main``。

    这些开关都在模块 import 时求值 (它们决定 app 的构建参数), 想改只能 reload。
    """
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import app.main as main_module

    return importlib.reload(main_module)


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch):
    """未设密码的现场 —— 让"无会话访问业务接口"退化成 403 而不是 401。"""
    from app.services import auth

    monkeypatch.setattr(auth, "is_configured", lambda: False)
    return auth


# --------------------------------------------------------------------------- #
# 文档端点: 默认不可达
# --------------------------------------------------------------------------- #

def test_docs_are_not_mounted_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(monkeypatch, TSP_ENABLE_API_DOCS=None)
    with TestClient(main_module.app) as client:
        for path in DOC_PATHS:
            assert client.get(path).status_code == 404, path


def test_docs_become_available_with_the_explicit_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式开启后三个端点都回来 —— 开关是双向可用的, 不是单向焊死。"""
    main_module = _fresh_main(monkeypatch, TSP_ENABLE_API_DOCS="1")
    with TestClient(main_module.app) as client:
        for path in DOC_PATHS:
            assert client.get(path).status_code == 200, path


# --------------------------------------------------------------------------- #
# 探活: 必须匿名可打
# --------------------------------------------------------------------------- #

def test_health_stays_public(monkeypatch: pytest.MonkeyPatch, unconfigured) -> None:
    """/health 匿名 200。它是部署探针的唯一抓手, 被拦就等于整套监控失灵。"""
    main_module = _fresh_main(monkeypatch, TSP_ENABLE_API_DOCS=None)
    with TestClient(main_module.app) as client:
        for path in HEALTH_PATHS:
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.status_code)


def test_health_is_actually_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """白名单里登记的 /health 必须真的有路由 —— 否则探活全挂, 而白名单看起来没问题。

    `/api/health` 曾出现在白名单思路里, 但 main.py 并没有注册它 (各业务 router 的
    ``/api/<域>/health`` 是另一回事, 且它们走认证)。这里把"白名单项都要有实路由"
    钉住, 免得再写进去一个不存在的路径自我安慰。
    """
    main_module = _fresh_main(monkeypatch, TSP_ENABLE_API_DOCS=None)
    registered = {getattr(route, "path", None) for route in main_module.app.routes}
    for path in main_module._AUTH_WHITELIST_EXACT:
        if path == "/api/events":
            # SSE 端点: 路径在, 只是实现在 api/events.py 里单独注册。
            assert path in registered, path
            continue
        assert path in registered, f"白名单里的 {path} 没有对应路由"


# --------------------------------------------------------------------------- #
# DSA 旁路: 令牌不对必须 404, 不能 401
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", DSA_PATHS_WITH_TOKEN_HEADER)
def test_dsa_paths_return_404_on_wrong_token(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """错令牌 → 404 而不是 401/403。

    401 等于告诉扫描器"这个路径存在, 只是你没权限", 内部接口就此变成可探测的。
    404 让它和任何不存在的路径没区别。
    """
    main_module = _fresh_main(monkeypatch, TSP_ENABLE_API_DOCS=None)
    with TestClient(main_module.app) as client:
        resp = client.get(path, headers={"x-news-feed-token": "definitely-not-the-token"})
        assert resp.status_code == 404, (path, resp.status_code, resp.text[:200])


@pytest.mark.parametrize("path", DSA_PATHS_WITH_TOKEN_HEADER)
def test_dsa_paths_return_404_without_any_token(
    monkeypatch: pytest.MonkeyPatch, unconfigured, path: str,
) -> None:
    """连头都不带也是 404 —— 未配置令牌时更不能给出"存在但没权限"的信号。"""
    from app.news import config as news_config

    monkeypatch.setattr(news_config, "feed_matches", lambda presented: False)
    main_module = _fresh_main(monkeypatch, TSP_ENABLE_API_DOCS=None)
    with TestClient(main_module.app) as client:
        assert client.get(path).status_code == 404, path


# --------------------------------------------------------------------------- #
# 业务接口: 无会话一律拒绝
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "path",
    ["/api/watchlist", "/api/settings/preferences", "/api/regime/latest"],
)
def test_business_routes_require_a_session(
    monkeypatch: pytest.MonkeyPatch, unconfigured, path: str,
) -> None:
    """没设密码 + 公网来源 → 403; 已设密码 + 无会话 → 401。两种都算拒绝。

    这里不区分具体码, 因为区分它本来就是信息泄露(未初始化 vs 未登录); 但必须
    **不能是 200**, 那才是真的坏了。
    """
    main_module = _fresh_main(monkeypatch, TSP_ENABLE_API_DOCS=None)
    with TestClient(main_module.app) as client:
        resp = client.get(path, headers={"x-forwarded-for": "203.0.113.9"})
        assert resp.status_code in (401, 403), (path, resp.status_code, resp.text[:200])


def test_invalid_session_cookie_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """带一个假会话 cookie 不能换来 200。"""
    from app.services import auth

    monkeypatch.setattr(auth, "is_configured", lambda: True)
    monkeypatch.setattr(auth, "is_valid_session", lambda token: False)
    main_module = _fresh_main(monkeypatch, TSP_ENABLE_API_DOCS=None)
    with TestClient(main_module.app) as client:
        client.cookies.set("tf_session", "forged-token")
        resp = client.get("/api/watchlist")
        assert resp.status_code == 401, resp.status_code


# --------------------------------------------------------------------------- #
# CORS 默认值
# --------------------------------------------------------------------------- #

def test_cors_defaults_to_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设 TSP_CORS_ORIGINS 时仍放开所有来源。

    收紧默认值会让所有"局域网另一台机器打开面板"的现有部署前端直接白屏 ——
    安全边界的收紧必须是显式动作, 不能是升级副作用。
    """
    main_module = _fresh_main(monkeypatch, TSP_CORS_ORIGINS=None)
    assert main_module._CORS_ORIGINS == ["*"]


def test_cors_env_narrows_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """设了就只放行列出的来源, 顺带打开 credentials。"""
    main_module = _fresh_main(
        monkeypatch,
        TSP_CORS_ORIGINS="https://panel.example.com, http://192.168.1.20:5173/",
    )
    assert main_module._CORS_ORIGINS == [
        "https://panel.example.com",
        "http://192.168.1.20:5173",
    ]


def test_cors_env_with_only_separators_falls_back_to_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写成一串逗号空格不能把 allowlist 变成空列表 —— 那会让所有跨域请求全挂。"""
    main_module = _fresh_main(monkeypatch, TSP_CORS_ORIGINS=" , ,  ")
    assert main_module._CORS_ORIGINS == ["*"]


def test_cors_policy_uses_the_env_value_on_a_real_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """不只看变量, 看真实响应头: 允许的来源会回 Access-Control-Allow-Origin。"""
    main_module = _fresh_main(
        monkeypatch,
        TSP_CORS_ORIGINS="https://panel.example.com",
        TSP_ENABLE_API_DOCS=None,
    )
    with TestClient(main_module.app) as client:
        allowed = client.get("/health", headers={"origin": "https://panel.example.com"})
        assert allowed.headers.get("access-control-allow-origin") == "https://panel.example.com"
        blocked = client.get("/health", headers={"origin": "https://evil.example.com"})
        assert blocked.headers.get("access-control-allow-origin") is None


# --------------------------------------------------------------------------- #
# cookie secure 默认值
# --------------------------------------------------------------------------- #

def test_cookie_secure_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 False —— 纯 http 自托管部署一旦默认 Secure, 表现是"登录成功又跳回登录页"。

    那种故障没有任何报错, 排查成本极高; 而现在所有存量部署都是这个形态。
    """
    monkeypatch.delenv("TSP_COOKIE_SECURE", raising=False)
    from app.api import auth as auth_api

    importlib.reload(auth_api)
    assert auth_api._cookie_secure() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_cookie_secure_can_be_enabled(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TSP_COOKIE_SECURE", value)
    from app.api import auth as auth_api

    importlib.reload(auth_api)
    assert auth_api._cookie_secure() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "  "])
def test_cookie_secure_falsey_values_stay_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TSP_COOKIE_SECURE", value)
    from app.api import auth as auth_api

    importlib.reload(auth_api)
    assert auth_api._cookie_secure() is False


def test_login_sets_the_secure_flag_from_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """真正下发 cookie 的地方要吃到这个开关, 不只是 _cookie_secure 自己返回对。"""
    from fastapi import FastAPI

    from app.services import auth

    monkeypatch.setattr(auth, "is_configured", lambda: True)
    monkeypatch.setattr(auth, "verify_and_create_session", lambda pw, user: "token-abc")
    monkeypatch.setattr(auth, "session_username", lambda token: "alice")

    monkeypatch.setenv("TSP_COOKIE_SECURE", "1")
    from app.api import auth as auth_api

    importlib.reload(auth_api)
    app = FastAPI()
    app.include_router(auth_api.router)
    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"username": "alice", "password": "x" * 8})
        assert resp.status_code == 200, resp.text
        header = resp.headers.get("set-cookie", "")
        assert "tf_session=token-abc" in header
        assert "Secure" in header
        assert "HttpOnly" in header
        assert "samesite=lax" in header.lower()


def test_login_cookie_has_no_secure_flag_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """对照组: 默认配置下 cookie 不带 Secure。"""
    from fastapi import FastAPI

    from app.services import auth

    monkeypatch.setattr(auth, "is_configured", lambda: True)
    monkeypatch.setattr(auth, "verify_and_create_session", lambda pw, user: "token-abc")
    monkeypatch.setattr(auth, "session_username", lambda token: "alice")

    monkeypatch.delenv("TSP_COOKIE_SECURE", raising=False)
    from app.api import auth as auth_api

    importlib.reload(auth_api)
    app = FastAPI()
    app.include_router(auth_api.router)
    with TestClient(app) as client:
        resp = client.post("/api/auth/login", json={"username": "alice", "password": "x" * 8})
        header = resp.headers.get("set-cookie", "")
        assert "Secure" not in header
        assert "HttpOnly" in header


# --------------------------------------------------------------------------- #
# 路由表: 文档端点不在白名单里
# --------------------------------------------------------------------------- #

def test_auth_whitelist_does_not_cover_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    main_module = _fresh_main(monkeypatch, TSP_ENABLE_API_DOCS=None)
    whitelist = set(main_module._AUTH_WHITELIST_EXACT)
    assert whitelist.isdisjoint(DOC_PATHS)
    assert "/health" in whitelist
    # /api/events 是 SSE, 凭证走 query 票据, 必须留在白名单里。
    assert "/api/events" in whitelist


def test_environment_is_not_leaked_through_error_bodies(
    monkeypatch: pytest.MonkeyPatch, unconfigured,
) -> None:
    """拒绝响应不能把配置细节(数据目录、密钥名)带出去。"""
    main_module = _fresh_main(monkeypatch, TSP_ENABLE_API_DOCS=None)
    with TestClient(main_module.app) as client:
        body = client.get("/api/watchlist", headers={"x-forwarded-for": "203.0.113.9"}).text
    lowered = body.lower()
    for phrase in ("traceback", "data_dir", "tsP_".lower(), os.sep + "root"):
        assert phrase not in lowered, (phrase, body[:300])
