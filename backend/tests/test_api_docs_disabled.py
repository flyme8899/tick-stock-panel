"""交互式 API 文档默认关闭。

默认开着的 ``/openapi.json`` + ``/docs`` + ``/redoc`` 在生产里是**不认证就能拿到
的资产清单**: 一份 openapi.json 列出全部路由、参数与请求体 schema (300+ 条
``/api/`` 路由), 攻击面不用猜。认证中间件只拦 ``/api/`` 前缀, 这三个路径原先靠
白名单放行, 于是等于公开。

改为默认不挂载, ``TSP_ENABLE_API_DOCS=1`` 显式开启。

注意 ``/api/openapi.json`` 是另一条路由 (Tier A 契约视图, 在 api_gateway 之后
注册, 走认证), 与 ``app.openapi()`` 的挂载开关无关, 本测试一并守住它没被误删。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

DOC_PATHS = ("/openapi.json", "/docs", "/redoc")


def _fresh_app(monkeypatch: pytest.MonkeyPatch, docs_env: str | None):
    """按给定环境变量重建 app (docs 开关在 import 时求值)。"""
    if docs_env is None:
        monkeypatch.delenv("TSP_ENABLE_API_DOCS", raising=False)
    else:
        monkeypatch.setenv("TSP_ENABLE_API_DOCS", docs_env)
    import app.main as main_module

    return importlib.reload(main_module)


def test_api_docs_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认: 三个文档端点都不挂载, 访问得到 404。"""
    main_module = _fresh_app(monkeypatch, None)
    assert main_module.app.openapi_url is None
    assert main_module.app.docs_url is None
    assert main_module.app.redoc_url is None

    with TestClient(main_module.app) as client:
        for path in DOC_PATHS:
            resp = client.get(path)
            assert resp.status_code == 404, f"{path} 不应该可达, 实际 {resp.status_code}"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_api_docs_on_when_env_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """显式开启: /openapi.json 与 /docs 恢复 200, 方便自托管调试。"""
    main_module = _fresh_app(monkeypatch, value)
    with TestClient(main_module.app) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", "  "])
def test_api_docs_stay_off_for_falsey_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """常见的「关」写法都当关闭, 不能被误判成开启。"""
    main_module = _fresh_app(monkeypatch, value)
    with TestClient(main_module.app) as client:
        for path in DOC_PATHS:
            assert client.get(path).status_code == 404


def test_contract_view_still_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉文档不能误伤 /api/openapi.json (Tier A 契约视图, 走认证)。"""
    main_module = _fresh_app(monkeypatch, None)
    paths = {getattr(route, "path", None) for route in main_module.app.routes}
    assert "/api/openapi.json" in paths
    assert "/api/events" in paths


def test_docs_paths_removed_from_auth_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    """白名单不再为文档开口子; /api/events 与健康检查仍在。"""
    main_module = _fresh_app(monkeypatch, None)
    whitelist = set(main_module._AUTH_WHITELIST_EXACT)
    for path in DOC_PATHS:
        assert path not in whitelist
    assert "/api/openapi.json" not in whitelist
    assert "/api/events" in whitelist
    assert "/health" in whitelist


def test_docs_off_does_not_break_authenticated_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """/api/ 业务路由的行为不受影响: 未认证仍是 4xx, 不是 404。"""
    main_module = _fresh_app(monkeypatch, None)
    with TestClient(main_module.app) as client:
        resp = client.get("/api/settings/preferences")
        assert resp.status_code in (401, 403), resp.status_code
