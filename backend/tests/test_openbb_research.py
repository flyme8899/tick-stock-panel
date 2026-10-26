"""G-T1–G-T10：OpenBB V5 sidecar 只读研究边界。全部使用 mock，不依赖生产或密钥。"""
from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.openbb import EXTENSION_API_VERSION, EXTENSION_ID, setup
from app.data_providers import custom as custom_sources
from app.data_providers.capabilities import build_capability_matrix
from app.data_providers.schemas import DAILY_COLUMNS, MACRO_COLUMNS
from app.extensions.registry import BackendExtensionRegistrar
from app.plugins.openbb import client as openbb_client
from app.plugins.openbb.provider import availability

ROOT = Path(__file__).resolve().parents[2]
HEALTH = {
    "status": "ok",
    "openbb_version": "5.0.0",
    "license": "Apache-2.0",
    "providers": ["fred", "tsp_yahoo"],
}
DAILY_RESULT = {
    "results": [{
        "date": "2026-10-23", "open": 100, "high": 110, "low": 99,
        "close": 108, "volume": 12345,
    }]
}
MACRO_RESULT = {"results": [{"date": "2026-10-01", "value": 4.25}]}


def _app() -> FastAPI:
    app = FastAPI()
    registrar = BackendExtensionRegistrar(EXTENSION_ID, api_version=EXTENSION_API_VERSION)
    setup(registrar)
    for router in registrar.routers:
        app.include_router(router)
    return app


def _mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == openbb_client.HEALTH_PATH:
            return httpx.Response(200, json=HEALTH)
        if request.url.path == openbb_client.EQUITY_PATH:
            assert request.url.params["provider"] == "tsp_yahoo"
            assert request.url.params["interval"] == "1d"
            return httpx.Response(200, json=DAILY_RESULT)
        if request.url.path == openbb_client.MACRO_PATH:
            assert request.url.params["provider"] == "fred"
            assert request.url.params["symbol"] in {"DGS10", "CPIAUCSL"}
            return httpx.Response(200, json=MACRO_RESULT)
        return httpx.Response(404, json={"detail": "unexpected"})

    return httpx.MockTransport(handler)


def _sources(monkeypatch: pytest.MonkeyPatch, available: bool, reason: str = "ok") -> None:
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [{
        "name": "openbb", "display_name": "OpenBB 研究源",
        "datasets": ["hk_daily", "us_daily", "macro"],
        "available": available, "status": reason,
    }])
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])


# G-T1
def test_main_process_has_no_openbb_dependency_or_import() -> None:
    pyproject = (ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")
    lock = (ROOT / "backend/uv.lock").read_text(encoding="utf-8")
    assert 'name = "openbb' not in lock
    assert '"openbb' not in pyproject
    for path in (ROOT / "backend/app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.split(".")[0].startswith("openbb") for alias in node.names), path
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.split(".")[0].startswith("openbb"), path


# G-T2
def test_unconfigured_is_pending_and_matrix_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENBB_BASE_URL", raising=False)
    assert openbb_client.enabled() is False
    assert availability() == (False, "未配置 OPENBB_BASE_URL")
    status = openbb_client.status()
    assert status["enabled"] is False and status["reachable"] is False
    _sources(monkeypatch, False, "未配置 OPENBB_BASE_URL")
    caps = {c["id"]: c for c in build_capability_matrix({}, tickflow_tier="expert")["capabilities"]}
    for cap_id in ("hk_daily", "us_daily", "macro"):
        assert caps[cap_id]["usable"] is False
        assert caps[cap_id]["default"] == "none"
        assert caps[cap_id]["pending"][0]["note"] == "未配置 OPENBB_BASE_URL"
        assert caps[cap_id]["candidates"] == []


# G-T3
def test_unreachable_fails_closed_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBB_BASE_URL", "http://openbb.down")
    transport = httpx.MockTransport(lambda _request: (_ for _ in ()).throw(httpx.ConnectError("down")))
    reachable, detail = openbb_client.OpenBBClient(transport=transport).health()
    assert reachable is False and "不可达" in detail
    with pytest.raises(openbb_client.OpenBBError) as error:
        openbb_client.OpenBBClient(transport=transport).query("us_daily", "AAPL")
    assert error.value.status_code == 503
    _sources(monkeypatch, False, "OpenBB sidecar 不可达")
    caps = {c["id"]: c for c in build_capability_matrix({})["capabilities"]}
    assert all(not caps[c]["usable"] for c in ("hk_daily", "us_daily", "macro"))
    real_client = openbb_client.OpenBBClient
    monkeypatch.setattr(openbb_client, "OpenBBClient", lambda: real_client(transport=transport))
    status = openbb_client.status()
    assert status["enabled"] is True and status["reachable"] is False
    assert "不可达" in status["detail"]
    monkeypatch.setattr("app.custom.openbb.routes.OpenBBClient", lambda: real_client(transport=transport))
    response = TestClient(_app()).get("/api/openbb/us_daily?symbol=AAPL")
    assert response.status_code == 503
    assert "不可达" in response.json()["detail"]


# G-T4
@pytest.mark.parametrize(("dataset", "symbol"), [
    ("hk_daily", "0700.HK"), ("hk_daily", "9988.HK"),
    ("us_daily", "AAPL"), ("us_daily", "MSFT"),
])
def test_healthy_equity_allowlist_maps_daily_schema(
    monkeypatch: pytest.MonkeyPatch, dataset: str, symbol: str
) -> None:
    monkeypatch.setenv("OPENBB_BASE_URL", "http://openbb.test")
    result = openbb_client.OpenBBClient(transport=_mock_transport()).query(
        dataset, symbol, date(2026, 10, 1), date(2026, 10, 25)
    )
    assert result["research_only"] is True and result["source"] == "openbb"
    assert result["columns"] == [*DAILY_COLUMNS, "market"]
    row = result["rows"][0]
    assert set(DAILY_COLUMNS) <= set(row)
    assert row["market"] == dataset[:2]
    assert row["volume"] == 123
    # 上游无成交额、前收盘、涨跌幅时保留空值，绝不伪造 0。
    assert row["amount"] is None and row["pre_close"] is None and row["change_pct"] is None


def test_healthy_macro_uses_independent_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBB_BASE_URL", "http://openbb.test")
    result = openbb_client.OpenBBClient(transport=_mock_transport()).query("macro", "DGS10")
    assert result["columns"] == MACRO_COLUMNS
    assert result["rows"] == [{
        "date": "2026-10-01", "series_id": "DGS10", "value": 4.25,
        "provider": "fred", "source": "openbb",
    }]
    assert "open" not in result["rows"][0] and "close" not in result["rows"][0]
    missing = openbb_client.map_macro_rows(
        "DGS10", {"results": [{"date": "2026-10-01", "value": "."}]}
    )
    assert missing[0]["value"] is None


def test_status_enabled_is_not_reachability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBB_BASE_URL", "http://openbb.test")
    monkeypatch.setattr(openbb_client.OpenBBClient, "health", lambda self: (True, "已连接"))
    body = openbb_client.status()
    assert body["enabled"] is True and body["reachable"] is True
    assert body["pin"] == "5.0.0" and body["research_only"] is True
    assert set(body["allowlist"]) == {"hk_daily", "us_daily", "macro"}


@pytest.mark.parametrize("providers", [None, 7, [{}], ["fred", "tsp_yahoo", "news"]])
def test_invalid_health_provider_shape_fails_closed(
    monkeypatch: pytest.MonkeyPatch, providers: object
) -> None:
    monkeypatch.setenv("OPENBB_BASE_URL", "http://openbb.test")
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={**HEALTH, "providers": providers}))
    assert openbb_client.OpenBBClient(transport=transport).health() == (
        False, "OpenBB provider 集合不符合最小白名单"
    )


def test_query_does_not_fetch_data_when_health_gate_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENBB_BASE_URL", "http://openbb.test")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == openbb_client.HEALTH_PATH:
            return httpx.Response(200, json={**HEALTH, "openbb_version": "4.7.1"})
        return httpx.Response(200, json=DAILY_RESULT)

    with pytest.raises(openbb_client.OpenBBError, match="版本不符合"):
        openbb_client.OpenBBClient(transport=httpx.MockTransport(handler)).query("us_daily", "AAPL")
    assert paths == [openbb_client.HEALTH_PATH]


# G-T5
@pytest.mark.parametrize(("dataset", "symbol"), [
    ("us_daily", "TSLA"), ("hk_daily", "AAPL"), ("macro", "GDP"),
    ("financial", "AAPL"), ("news", "AAPL"), ("../health", "AAPL"),
])
def test_unlisted_dataset_symbol_or_provider_is_rejected(
    monkeypatch: pytest.MonkeyPatch, dataset: str, symbol: str
) -> None:
    monkeypatch.setenv("OPENBB_BASE_URL", "http://openbb.test")
    with pytest.raises(openbb_client.OpenBBError) as error:
        openbb_client.OpenBBClient(transport=_mock_transport()).query(dataset, symbol)
    assert error.value.status_code == 403


def test_no_arbitrary_proxy_route_exists() -> None:
    app = _app()
    assert TestClient(app).get("/api/openbb/upstream/health").status_code == 404
    assert TestClient(app).post("/api/openbb/us_daily").status_code == 405


def test_healthy_research_plugin_never_enters_general_provider_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.data_providers.custom import loader

    manifest = yaml.safe_load(
        (ROOT / "backend/app/plugins/openbb/plugin.yaml").read_text(encoding="utf-8")
    )
    monkeypatch.setattr(loader, "_call_check", lambda _ref: (True, "已连接"))
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(
        loader, "_load_entry", lambda _ref: pytest.fail("研究源不得加载进通用 provider 注册表")
    )
    loader._register_one_plugin(manifest)
    assert loader.list_plugins()[0]["available"] is True
    assert loader.names() == set()


# G-T6
def test_research_candidates_never_capture_a_share_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _sources(monkeypatch, True)
    caps = {c["id"]: c for c in build_capability_matrix({}, tickflow_tier="expert")["capabilities"]}
    for cap_id in ("hk_daily", "us_daily", "macro"):
        assert [c["name"] for c in caps[cap_id]["candidates"]] == ["openbb"]
        assert caps[cap_id]["research_only"] is True
        assert caps[cap_id]["field"] is None and caps[cap_id]["usable"] is True
    for cap_id in ("daily", "financial"):
        assert caps[cap_id]["default"] == caps[cap_id]["effective"] == "tickflow"
        assert "openbb" not in {c["name"] for c in caps[cap_id]["candidates"]}


# G-T7/G-T8/G-T10
def test_sidecar_pin_license_profile_and_documentation() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["openbb"]
    assert service["profiles"] == ["openbb"]
    assert service["build"]["context"] == "./docker/openbb"
    requirements = (ROOT / "docker/openbb/requirements.txt").read_text(encoding="utf-8")
    dependencies = [
        line.strip() for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "openbb-core[pandas]==2.0.1" in dependencies
    assert "openbb-platform-api==2.0.1" in dependencies
    assert "openbb-fred==2.0.0" in dependencies
    assert all(not item.startswith("openbb-yfinance") for item in dependencies)
    assert all(not item.startswith("openbb==") for item in dependencies)  # 不装全集元包
    sidecar = (ROOT / "docker/openbb/sidecar.py").read_text(encoding="utf-8")
    assert '"openbb_version": "5.0.0"' in sidecar
    assert '"license": "Apache-2.0"' in sidecar
    doc = (ROOT / "docs/openbb-integration.md").read_text(encoding="utf-8")
    for text in ("openbq-org", "5.0", "禁 4.x", "PIT", "OPENBB_BASE_URL", "--profile openbb", "使用者自担"):
        assert text in doc
    assert "仅供研究，不构成投资建议；执行须人工确认" in doc
    assert "OPENBB_BASE_URL=" in (ROOT / ".env.example").read_text(encoding="utf-8")


# G-T9/G-T10：本拍边界与旧测试由 CI 命令另行复跑，这里防止表面收口、实际接线。
def test_plugin_has_no_main_sync_protocol_and_gateway_is_read_only() -> None:
    source = (ROOT / "backend/app/plugins/openbb/provider.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    provider = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OpenBBProvider")
    methods = {node.name for node in provider.body if isinstance(node, ast.FunctionDef)}
    assert "get_daily" not in methods and "get_financials" not in methods
    from app.services.api_gateway import required_scope
    for path in ("/api/openbb/status", "/api/openbb/hk_daily", "/api/openbb/us_daily", "/api/openbb/macro"):
        assert required_scope("GET", path) == "read:market"
        assert required_scope("POST", path) is None


def test_sidecar_python_compiles_without_importing_openbb_into_test_process() -> None:
    for path in (ROOT / "docker/openbb").rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    manifest = json.loads(json.dumps(openbb_client.ALLOWED_SYMBOLS, default=list))
    assert set(manifest) == {"hk_daily", "us_daily", "macro"}
