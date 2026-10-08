"""Integration boundary for the vendored daily_stock_analysis sidecar."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.dsa import EXTENSION_API_VERSION, EXTENSION_ID, setup
from app.custom.dsa.commands import CommandError, execute, parse_command
from app.custom.dsa.proxy import InvalidUpstreamPathError, UpstreamError, normalize_upstream_path
from app.extensions.registry import BackendExtensionRegistrar


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DSA_BASE_URL", "http://dsa.test")
    app = FastAPI()
    registrar = BackendExtensionRegistrar(EXTENSION_ID, api_version=EXTENSION_API_VERSION)
    setup(registrar)
    for router in registrar.routers:
        app.include_router(router)
    return TestClient(app)


def test_catalog_lists_user_facing_features(client: TestClient) -> None:
    response = client.get("/api/dsa/catalog")

    assert response.status_code == 200
    payload = response.json()
    ids = {item["id"] for item in payload["features"]}
    assert {
        "decision_dashboard",
        "stock_reports",
        "intelligence",
        "markets",
        "screening",
        "etf_rotation",
        "chat",
        "bot",
        "schedule",
        "alerts",
        "portfolio",
        "share_image",
    } <= ids
    assert payload["license"] == "MIT"
    assert any(item["name"] == "DSA_BASE_URL" for item in payload["env_vars"])


def test_upstream_path_rejects_escape_and_unknown_prefixes() -> None:
    assert normalize_upstream_path("history/12/markdown") == "history/12/markdown"
    with pytest.raises(InvalidUpstreamPathError):
        normalize_upstream_path("../secrets")
    with pytest.raises(InvalidUpstreamPathError):
        normalize_upstream_path("history/../../etc/passwd")
    with pytest.raises(InvalidUpstreamPathError):
        normalize_upstream_path("http://evil.example/health")
    with pytest.raises(InvalidUpstreamPathError):
        normalize_upstream_path("admin/exec")


def test_proxy_maps_connection_failure_to_unavailable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise UpstreamError("决策服务未连接")

    monkeypatch.setattr("app.custom.dsa.routes.forward", boom)

    response = client.get("/api/dsa/upstream/history")

    assert response.status_code == 503
    assert response.json()["detail"] == "决策服务未连接"


def test_proxy_forwards_allowlisted_get(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_forward(method, path, *, params=None, body=None, content_type=None):
        captured.update(method=method, path=path, params=params, body=body, content_type=content_type)
        return 200, b'{"items":[]}', "application/json", {}

    monkeypatch.setattr("app.custom.dsa.routes.forward", fake_forward)

    response = client.get("/api/dsa/upstream/history", params={"limit": 5})

    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert captured["method"] == "GET"
    assert captured["path"] == "history"
    assert ("limit", "5") in captured["params"]


def test_disabled_base_url_reports_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSA_BASE_URL", "")
    app = FastAPI()
    registrar = BackendExtensionRegistrar(EXTENSION_ID, api_version=EXTENSION_API_VERSION)
    setup(registrar)
    for router in registrar.routers:
        app.include_router(router)

    response = TestClient(app).get("/api/dsa/status")

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["reachable"] is False


def test_help_command_does_not_call_upstream() -> None:
    result = execute("/帮助")

    assert result["ok"] is True
    assert "/analyze" in result["text"]


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(CommandError, match="未知命令"):
        parse_command("/drop-database")


def test_analyze_command_submits_without_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_forward(method, path, *, params=None, body=None, content_type=None):
        captured.update(method=method, path=path, body=body)
        return 202, b'{"task_id":"t1"}', "application/json", {}

    monkeypatch.setattr("app.custom.dsa.commands.forward", fake_forward)

    result = execute("/分析 600519")

    assert result["ok"] is True
    assert captured["path"] == "analysis/analyze"
    assert b'"notify": false' in captured["body"]
    assert b"600519" in captured["body"]


def test_etf_job_does_not_spawn_without_runtime(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.custom.dsa.jobs.sidecar_python", lambda: None)

    response = client.post("/api/dsa/jobs/etf-rotation")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "DSA_PYTHON" in body["detail"]
    assert body["command"] == "python main.py --etf-rotation --no-notify"
