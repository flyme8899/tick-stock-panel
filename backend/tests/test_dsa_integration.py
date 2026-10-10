"""Integration boundary for the vendored daily_stock_analysis sidecar."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.dsa import EXTENSION_API_VERSION, EXTENSION_ID, setup
from app.custom.dsa.commands import CommandError, execute, parse_command
from app.custom.dsa.proxy import (
    InvalidUpstreamPathError,
    UpstreamError,
    forward,
    normalize_upstream_path,
    timeout_for,
)
from app.custom.dsa.schedule import ScheduleSettingsError, schedule_config_items
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
    def fail(*_args, **_kwargs):
        raise AssertionError("有 vendor 源码时不应改去请求 sidecar")

    monkeypatch.setattr("app.custom.dsa.jobs.sidecar_python", lambda: None)
    monkeypatch.setattr("app.custom.dsa.jobs.forward", fail)
    monkeypatch.setattr(
        "app.custom.dsa.jobs.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应启动进程")),
    )

    response = client.post("/api/dsa/jobs/etf-rotation")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "DSA_PYTHON" in body["detail"]
    assert body["command"] == "python main.py --etf-rotation --no-notify"


_ETF_TOKEN = "tsp-etf-shared-secret"


def test_etf_job_runs_inside_sidecar_when_vendor_tree_is_missing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_forward(method, path, *, params=None, body=None, content_type=None, timeout=None):
        captured.update(method=method, path=path, body=body, timeout=timeout)
        payload = json.dumps({"ok": True, "code": 0, "detail": "目标 510300"}).encode()
        return 200, payload, "application/json", {}

    monkeypatch.setenv("DSA_INTERNAL_TOKEN", _ETF_TOKEN)
    monkeypatch.setattr("app.custom.dsa.jobs.vendor_root", lambda: Path("/no/such/dsa-vendor"))
    monkeypatch.setattr("app.custom.dsa.jobs.forward", fake_forward)
    monkeypatch.setattr(
        "app.custom.dsa.jobs.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("app 进程不应执行轮动")),
    )

    response = client.post("/api/dsa/jobs/etf-rotation", json={"pool": "rm -rf /"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["detail"] == "目标 510300"
    assert body["code"] == 0
    assert body["command"] == "python main.py --etf-rotation --no-notify"
    assert captured == {
        "method": "POST",
        "path": "tsp/etf-rotation",
        "body": None,
        "timeout": 200.0,
    }


def test_etf_job_reports_sidecar_down_without_spawning(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def down(*_args, **_kwargs):
        raise UpstreamError("决策服务未连接")

    monkeypatch.setenv("DSA_INTERNAL_TOKEN", _ETF_TOKEN)
    monkeypatch.setattr("app.custom.dsa.jobs.vendor_root", lambda: Path("/no/such/dsa-vendor"))
    monkeypatch.setattr("app.custom.dsa.jobs.forward", down)

    response = client.post("/api/dsa/jobs/etf-rotation")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "决策服务未连接" in body["detail"]
    assert "docker compose --profile dsa" in body["detail"]
    assert body["command"] == "python main.py --etf-rotation --no-notify"


def test_etf_job_explains_missing_sidecar_route(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_args, **_kwargs):
        return 404, b'{"detail":"Not Found"}', "application/json", {}

    monkeypatch.setenv("DSA_INTERNAL_TOKEN", _ETF_TOKEN)
    monkeypatch.setattr("app.custom.dsa.jobs.vendor_root", lambda: Path("/no/such/dsa-vendor"))
    monkeypatch.setattr("app.custom.dsa.jobs.forward", missing)

    response = client.post("/api/dsa/jobs/etf-rotation")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "dsa_bootstrap" in body["detail"]


def test_etf_job_does_not_call_sidecar_without_internal_token(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DSA_INTERNAL_TOKEN", raising=False)
    monkeypatch.setattr("app.custom.dsa.jobs.vendor_root", lambda: Path("/no/such/dsa-vendor"))
    monkeypatch.setattr(
        "app.custom.dsa.jobs.forward",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("未配置密钥时不应请求 sidecar")),
    )

    response = client.post("/api/dsa/jobs/etf-rotation")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "DSA_INTERNAL_TOKEN" in body["detail"]
    assert "X-TSP-Internal-Token" not in body["detail"]


def test_forward_attaches_internal_token_only_on_etf_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSA_BASE_URL", "http://dsa.test")
    monkeypatch.setenv("DSA_INTERNAL_TOKEN", _ETF_TOKEN)
    seen: list[dict] = []

    class FakeResponse:
        def __init__(self, status: int, content: bytes) -> None:
            self.status_code = status
            self.content = content
            self.headers: dict[str, str] = {}

        def json(self) -> dict:
            return json.loads(self.content)

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args) -> bool:
            return False

        def request(self, _method, url, *, params=None, content=None, headers=None):
            seen.append({"url": url, "headers": dict(headers or {})})
            if url.endswith("/tsp/etf-rotation") and len(seen) == 1:
                return FakeResponse(401, b'{"error":"unauthorized","message":"Login required"}')
            return FakeResponse(200, b'{"ok": true, "detail": "done"}')

    monkeypatch.setattr("app.custom.dsa.proxy.httpx.Client", FakeClient)
    monkeypatch.setenv("DSA_PASSWORD", "panel-secret")
    monkeypatch.setattr("app.custom.dsa.proxy._upstream_cookie", lambda: "dsa_session=cached")
    monkeypatch.setattr("app.custom.dsa.proxy._invalidate_and_relogin", lambda _cookie: True)

    status, payload, _media, _extra = forward("POST", "tsp/etf-rotation", timeout=5)

    assert status == 200
    assert json.loads(payload)["ok"] is True
    assert [call["headers"].get("X-TSP-Internal-Token") for call in seen] == [_ETF_TOKEN, _ETF_TOKEN]
    assert all("tsp/other" not in call["url"] for call in seen)

    seen.clear()
    health_status, _health_body, _media, _extra = forward("GET", "health")
    assert health_status == 200
    assert "X-TSP-Internal-Token" not in seen[0]["headers"]


def test_forward_does_not_refresh_session_for_token_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSA_BASE_URL", "http://dsa.test")
    monkeypatch.setenv("DSA_INTERNAL_TOKEN", _ETF_TOKEN)
    monkeypatch.setenv("DSA_PASSWORD", "panel-secret")
    monkeypatch.setattr("app.custom.dsa.proxy._upstream_cookie", lambda: "dsa_session=cached")
    calls = {"n": 0}

    class FakeResponse:
        def __init__(self) -> None:
            self.status_code = 401
            self.content = '{"ok": false, "detail": "未授权"}'.encode()
            self.headers: dict[str, str] = {}

        def json(self) -> dict:
            return json.loads(self.content)

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args) -> bool:
            return False

        def request(self, *_args, **_kwargs):
            calls["n"] += 1
            return FakeResponse()

    monkeypatch.setattr("app.custom.dsa.proxy.httpx.Client", FakeClient)
    monkeypatch.setattr(
        "app.custom.dsa.proxy._invalidate_and_relogin",
        lambda _cookie: (_ for _ in ()).throw(AssertionError("密钥错误不应重新登录")),
    )

    status, payload, _media, _extra = forward("POST", "tsp/etf-rotation")

    assert status == 401
    assert calls["n"] == 1
    assert json.loads(payload)["detail"] == "未授权"


def test_schedule_items_cover_shanghai_trading_day_watchlist() -> None:
    items = {
        item["key"]: item["value"]
        for item in schedule_config_items(
            enabled=True,
            time="18:05",
            trading_days_only=True,
            region="cn",
            watchlist="600519，000858 600519",
        )
    }

    assert items == {
        "SCHEDULE_ENABLED": "true",
        "SCHEDULE_TIME": "18:05",
        "SCHEDULE_TIMES": "18:05",
        "SCHEDULE_RUN_IMMEDIATELY": "false",
        "TRADING_DAY_CHECK_ENABLED": "true",
        "MARKET_REVIEW_ENABLED": "true",
        "MARKET_REVIEW_REGION": "cn",
        "STOCK_LIST": "600519,000858",
    }


def test_schedule_items_reject_empty_watchlist_and_bad_clock() -> None:
    with pytest.raises(ScheduleSettingsError, match="自选"):
        schedule_config_items(
            enabled=True,
            time="18:00",
            trading_days_only=True,
            region="cn",
            watchlist="  ",
        )
    with pytest.raises(ScheduleSettingsError, match="HH:MM"):
        schedule_config_items(
            enabled=False,
            time="25:99",
            trading_days_only=True,
            region="cn",
            watchlist="600519",
        )


def test_schedule_put_rejects_bad_clock_without_upstream(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("invalid schedule must not reach the sidecar")

    monkeypatch.setattr("app.custom.dsa.schedule.forward", fail)

    response = client.put(
        "/api/dsa/schedule",
        json={
            "enabled": True,
            "time": "25:00",
            "trading_days_only": True,
            "region": "cn",
            "watchlist": "600519",
        },
    )

    assert response.status_code == 400
    assert "HH:MM" in response.json()["detail"]


def test_schedule_put_persists_through_system_config(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, bytes | None]] = []

    def fake_forward(method, path, *, params=None, body=None, content_type=None):
        calls.append((method, path, body))
        if method == "GET" and path == "system/config":
            payload = {
                "config_version": "v1",
                "items": [
                    {"key": "SCHEDULE_ENABLED", "value": "true"},
                    {"key": "SCHEDULE_TIME", "value": "18:05"},
                    {"key": "SCHEDULE_TIMES", "value": "18:05"},
                    {"key": "TRADING_DAY_CHECK_ENABLED", "value": "true"},
                    {"key": "MARKET_REVIEW_REGION", "value": "cn"},
                    {"key": "STOCK_LIST", "value": "600519,000858"},
                ],
            }
            return 200, json.dumps(payload).encode(), "application/json", {}
        if method == "PUT" and path == "system/config":
            return 200, b'{"success": true, "warnings": []}', "application/json", {}
        if method == "GET" and path == "system/scheduler/status":
            payload = {
                "enabled": True,
                "running": False,
                "schedule_times": ["18:05"],
                "next_run_at": "2026-10-09T18:05:00",
                "last_success_at": None,
                "last_error": None,
                "last_skip_reason": "non_trading_day",
            }
            return 200, json.dumps(payload).encode(), "application/json", {}
        raise AssertionError(path)

    monkeypatch.setattr("app.custom.dsa.schedule.forward", fake_forward)

    response = client.put(
        "/api/dsa/schedule",
        json={
            "enabled": True,
            "time": "18:05",
            "trading_days_only": True,
            "region": "cn",
            "watchlist": "600519,000858",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["saved"] is True
    assert body["timezone"] == "Asia/Shanghai"
    assert body["scheduler"]["last_skip_reason"] == "non_trading_day"
    put = next(call for call in calls if call[0] == "PUT")
    payload = json.loads(put[2] or b"{}")
    items = {item["key"]: item["value"] for item in payload["items"]}
    assert payload["reload_now"] is True
    assert items["SCHEDULE_ENABLED"] == "true"
    assert items["MARKET_REVIEW_REGION"] == "cn"
    assert items["TRADING_DAY_CHECK_ENABLED"] == "true"
    assert items["STOCK_LIST"] == "600519,000858"
    assert all(call[1] != "analysis/analyze" for call in calls)


def test_share_image_timeout_exceeds_plain_history_reads() -> None:
    assert timeout_for("history/12/markdown") == 20
    assert timeout_for("history/12/share-image") == 90
    assert timeout_for("tsp/etf-rotation") == 200
    assert normalize_upstream_path("tsp/etf-rotation") == "tsp/etf-rotation"
    with pytest.raises(InvalidUpstreamPathError):
        normalize_upstream_path("tsp")
    with pytest.raises(InvalidUpstreamPathError):
        normalize_upstream_path("tsp/other")
    with pytest.raises(InvalidUpstreamPathError):
        normalize_upstream_path("tsp/etf-rotation/extra")


def test_catalog_documents_screening_and_efinance_priority(client: TestClient) -> None:
    payload = client.get("/api/dsa/catalog").json()
    purposes = {item["name"]: item["purpose"] for item in payload["env_vars"]}
    assert "screening_disabled" in purposes["SCREENING_ENABLED"]
    assert "3" in purposes["EFINANCE_PRIORITY"]
    assert "TickFlow" in purposes["EFINANCE_PRIORITY"]


def test_compose_mounts_etf_job_next_to_bootstrap() -> None:
    text = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./backend/app/custom/dsa/etf_job.py:/opt/tsp/etf_job.py:ro" in text
    assert 'profiles: ["dsa"]' in text


def test_env_example_and_guide_document_screening_and_priority() -> None:
    root = Path(__file__).resolve().parents[2]
    example = (root / ".env.example").read_text(encoding="utf-8")
    guide = (root / "docs" / "dsa-integration.md").read_text(encoding="utf-8")
    for text in (example, guide):
        assert "SCREENING_ENABLED" in text
        assert "EFINANCE_PRIORITY=3" in text
        assert "TickFlow" in text
        assert "DSA_INTERNAL_TOKEN" in text
        assert "X-TSP-Internal-Token" in text
