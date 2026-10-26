"""``GET /api/dsa/status`` 的 ``enabled ≠ reachable`` 契约。

升级验收里最容易写错的一句话是「配了 URL 就算连上了」。这里把三态钉死：

====================  =========  ===========  ==========================
场景                    enabled    reachable    说明
====================  =========  ===========  ==========================
URL 留空               False      False        ``未配置 DSA_BASE_URL``
URL 配了但 sidecar 不在  True       False        转发失败也要带回 detail
URL 配了且 health 200    True       True         ``已连接``
health 返回 4xx/5xx     True       False        截断上游文本，不抛崩
====================  =========  ===========  ==========================

任何一条塌成「enabled 冒充 reachable」都应该在这里变红。
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.dsa import EXTENSION_API_VERSION, EXTENSION_ID, setup
from app.custom.dsa import proxy
from app.custom.dsa import routes as dsa_routes
from app.extensions.registry import BackendExtensionRegistrar


def _client() -> TestClient:
    app = FastAPI()
    registrar = BackendExtensionRegistrar(EXTENSION_ID, api_version=EXTENSION_API_VERSION)
    setup(registrar)
    for router in registrar.routers:
        app.include_router(router)
    return TestClient(app)


@pytest.fixture
def status_client() -> Iterator[TestClient]:
    yield _client()


def _patch_health(monkeypatch: pytest.MonkeyPatch, result: tuple[bool, str]) -> None:
    monkeypatch.setattr(dsa_routes, "health", lambda: result)


def test_status_shape(monkeypatch: pytest.MonkeyPatch, status_client: TestClient) -> None:
    monkeypatch.setenv("DSA_BASE_URL", "http://dsa.test")
    _patch_health(monkeypatch, (True, "已连接"))

    payload = status_client.get("/api/dsa/status").json()

    assert set(payload) == {"enabled", "base_url", "reachable", "detail"}
    assert payload["base_url"] == "http://dsa.test"


def test_unconfigured_is_not_enabled_nor_reachable(monkeypatch: pytest.MonkeyPatch, status_client: TestClient) -> None:
    """URL 显式留空：enabled=False，reachable=False，detail 说明未配置。"""
    monkeypatch.setenv("DSA_BASE_URL", "")
    monkeypatch.setattr(proxy, "health", proxy.health)

    payload = status_client.get("/api/dsa/status").json()

    assert payload["enabled"] is False
    assert payload["reachable"] is False
    assert payload["base_url"] == ""
    assert payload["detail"] == "未配置 DSA_BASE_URL"


def test_configured_but_down_is_enabled_and_unreachable(
    monkeypatch: pytest.MonkeyPatch, status_client: TestClient
) -> None:
    """URL 配了但连不上：enabled=True + reachable=False，前端才能显示「未连接」。"""
    monkeypatch.setenv("DSA_BASE_URL", "http://dsa.down")
    _patch_health(monkeypatch, (False, "DSA 未连接：Connection refused"))

    payload = status_client.get("/api/dsa/status").json()

    assert payload["enabled"] is True
    assert payload["reachable"] is False
    assert "Connection refused" in payload["detail"]


def test_configured_and_healthy_is_both_true(monkeypatch: pytest.MonkeyPatch, status_client: TestClient) -> None:
    monkeypatch.setenv("DSA_BASE_URL", "http://dsa.up")
    _patch_health(monkeypatch, (True, "已连接"))

    payload = status_client.get("/api/dsa/status").json()

    assert payload["enabled"] is True
    assert payload["reachable"] is True
    assert payload["detail"] == "已连接"


def test_health_exception_becomes_unreachable_detail(
    monkeypatch: pytest.MonkeyPatch, status_client: TestClient
) -> None:
    """health 抛 UpstreamError：status 不崩，转成 reachable=False + 可读 detail。"""
    monkeypatch.setenv("DSA_BASE_URL", "http://dsa.down")

    def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise proxy.UpstreamError("DSA 未连接：timed out")

    monkeypatch.setattr(proxy, "forward", boom)

    payload = status_client.get("/api/dsa/status").json()

    assert payload["enabled"] is True
    assert payload["reachable"] is False
    assert "timed out" in payload["detail"]


def test_health_http_error_is_unreachable(monkeypatch: pytest.MonkeyPatch, status_client: TestClient) -> None:
    """health 返回 503：reachable=False，detail 带截断后的上游文本。"""
    monkeypatch.setenv("DSA_BASE_URL", "http://dsa.up")

    def failing(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return 503, b"database is locked", "application/json", {}

    monkeypatch.setattr(proxy, "forward", failing)

    payload = status_client.get("/api/dsa/status").json()

    assert payload["enabled"] is True
    assert payload["reachable"] is False
    assert payload["detail"] == "database is locked"


def test_default_base_url_keeps_enabled_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """没设 env 时 base_url 默认 127.0.0.1:8000，因此 enabled 恒为真。"""
    monkeypatch.delenv("DSA_BASE_URL", raising=False)

    assert proxy.base_url() == "http://127.0.0.1:8000"
    assert proxy.enabled() is True


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_base_url_disables(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("DSA_BASE_URL", value)

    assert proxy.enabled() is False
    assert proxy.health() == (False, "未配置 DSA_BASE_URL")


def test_enabled_never_substitutes_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """enabled 只读 URL；health 必须真的发请求。把 forward 打断后 enabled 仍真，health 变假。"""
    monkeypatch.setenv("DSA_BASE_URL", "http://dsa.up")

    def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise proxy.UpstreamError("DSA 未连接：refused")

    monkeypatch.setattr(proxy, "forward", boom)

    assert proxy.enabled() is True
    assert proxy.health() == (False, "DSA 未连接：refused")
