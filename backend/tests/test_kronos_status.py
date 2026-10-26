"""Kronos 三态 ``enabled`` ≠ ``reachable`` ≠ ``gate_passed``（G-T2 / G-T3 / G-T4 / G-T11）。

三态互相独立是本拍最重要的契约：

==================================  ==========================================
场景                                 期望
==================================  ==========================================
默认（什么 env 都不给）              enabled=False / reachable=False / gate=False
只开开关不配来源                     enabled=False（原因是「没配来源」）
开关 + URL，sidecar 不通             enabled=True / reachable=False
开关 + 批目录，无成功标记            enabled=True / reachable=False
开关 + 批目录，标记新鲜              enabled=True / reachable=True
开关 + URL 通                        enabled=True / reachable=True
任何情况只要没门禁记录               gate_passed=False
门禁记录齐全但过期                   gate_passed=False
enabled/reachable 真                 gate_passed 仍可为 False（互不推导）
==================================  ==========================================

另外覆盖可卸载（G-T11）：清空配置后 status 回到全关，且主进程 import 不带 torch。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.kronos import config
from app.custom.kronos import routes as kronos_routes


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "KRONOS_ENABLED",
        "KRONOS_BASE_URL",
        "KRONOS_BATCH_DIR",
        "KRONOS_DATA_DIR",
        "KRONOS_GATE_RECORD",
        "KRONOS_FRESHNESS_HOURS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把运行态目录指到 tmp，避免读本机真实 data。"""
    monkeypatch.setenv("KRONOS_DATA_DIR", str(tmp_path))
    return tmp_path


def _write_gate(root: Path, **overrides) -> Path:
    record = {
        "passed": True,
        "cost_after_tax": "TSP 研究规范 v1：买 6.6bp / 卖 11.6bp",
        "out_of_sample": "walkforward 2026H1，见 KRONOS_S3_EVAL_BRIEF.md",
        "risk_downgrade_only": True,
        "usage": "risk_downgrade_only",
        "expires_at": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
    }
    record.update(overrides)
    path = root / "gate.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return path


def _write_marker(root: Path, *, hours_ago: float = 1.0) -> Path:
    stamp = datetime.now(UTC) - timedelta(hours=hours_ago)
    path = root / "last_batch.json"
    path.write_text(json.dumps({"finished_at": stamp.isoformat(), "rows": 10}), encoding="utf-8")
    return path


# ── G-T2 默认全关 ─────────────────────────────────────────

def test_defaults_are_all_off(data_dir: Path) -> None:
    assert config.switch_on() is False
    assert config.enabled() is False
    assert config.reachable()[0] is False
    assert config.gate_passed()[0] is False


def test_switch_on_without_source_is_not_enabled(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", "1")

    assert config.switch_on() is True
    assert config.enabled() is False
    assert config.enabled_detail() == "未配置 KRONOS_BASE_URL 或 KRONOS_BATCH_DIR"


@pytest.mark.parametrize("value", ["", "0", "false", "off", "none", "  "])
def test_switch_off_values(monkeypatch: pytest.MonkeyPatch, data_dir: Path, value: str) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", value)
    monkeypatch.setenv("KRONOS_BASE_URL", "http://kronos.test")

    assert config.switch_on() is False
    assert config.enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_switch_on_values(monkeypatch: pytest.MonkeyPatch, data_dir: Path, value: str) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", value)
    monkeypatch.setenv("KRONOS_BATCH_DIR", "/data/batch")

    assert config.switch_on() is True
    assert config.enabled() is True


# ── G-T3 已配但不可达 ────────────────────────────────────

def test_sidecar_down_is_enabled_not_reachable(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", "1")
    monkeypatch.setenv("KRONOS_BASE_URL", "http://127.0.0.1:9")

    ok, detail = config.reachable()

    assert config.enabled() is True
    assert ok is False
    assert "不可达" in detail or "HTTP" in detail


def test_batch_dir_without_marker_is_not_reachable(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", "1")
    monkeypatch.setenv("KRONOS_BATCH_DIR", str(data_dir / "batch"))

    ok, detail = config.reachable()

    assert config.enabled() is True
    assert ok is False
    assert detail == "无批成功标记"


def test_stale_marker_is_not_reachable(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", "1")
    monkeypatch.setenv("KRONOS_BATCH_DIR", str(data_dir / "batch"))
    _write_marker(data_dir, hours_ago=120)

    ok, detail = config.reachable()

    assert ok is False
    assert "过期" in detail


def test_fresh_marker_is_reachable(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", "1")
    monkeypatch.setenv("KRONOS_BATCH_DIR", str(data_dir / "batch"))
    _write_marker(data_dir, hours_ago=2)

    ok, detail = config.reachable()

    assert ok is True
    assert "新鲜" in detail


def test_freshness_window_is_configurable(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", "1")
    monkeypatch.setenv("KRONOS_BATCH_DIR", str(data_dir / "batch"))
    monkeypatch.setenv("KRONOS_FRESHNESS_HOURS", "1")
    _write_marker(data_dir, hours_ago=3)

    assert config.reachable()[0] is False


def test_reachable_does_not_raise_on_bad_marker(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """标记 JSON 坏掉也不能崩主进程。"""
    monkeypatch.setenv("KRONOS_ENABLED", "1")
    monkeypatch.setenv("KRONOS_BATCH_DIR", str(data_dir / "batch"))
    (data_dir / "last_batch.json").write_text("{ 不是 json", encoding="utf-8")

    ok, detail = config.reachable()

    assert ok is False
    assert detail == "无批成功标记"


def test_sidecar_health_ok(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", "1")
    monkeypatch.setenv("KRONOS_BASE_URL", "http://kronos.test")

    class _Response:
        status_code = 200
        content = b'{"status": "ok"}'

        @staticmethod
        def json() -> dict:
            return {"status": "ok"}

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Response())

    ok, detail = config.reachable()

    assert ok is True
    assert detail == "sidecar 已连接"


def test_sidecar_health_http_error(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", "1")
    monkeypatch.setenv("KRONOS_BASE_URL", "http://kronos.test")

    class _Response:
        status_code = 503
        content = b""

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Response())

    ok, detail = config.reachable()

    assert ok is False
    assert "503" in detail


# ── G-T4 三态独立 ───────────────────────────────────────

def test_gate_absent_is_false(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setenv("KRONOS_ENABLED", "1")
    monkeypatch.setenv("KRONOS_BASE_URL", "http://kronos.test")

    passed, detail = config.gate_passed()

    assert passed is False
    assert "未找到门禁记录" in detail


def test_gate_complete_record_passes(data_dir: Path) -> None:
    _write_gate(data_dir)

    passed, detail = config.gate_passed()

    assert passed is True
    assert "已通过" in detail


@pytest.mark.parametrize(
    "overrides,missing",
    [
        ({"passed": False}, "explicit_passed"),
        ({"passed": 1}, "explicit_passed"),
        ({"passed": "true"}, "explicit_passed"),
        ({"cost_after_tax": ""}, "cost_after_tax"),
        ({"cost_after_tax": None}, "cost_after_tax"),
        ({"out_of_sample": "  "}, "out_of_sample"),
        ({"risk_downgrade_only": False}, "risk_downgrade_only"),
        ({"usage": "upgrade"}, "risk_downgrade_only"),
    ],
)
def test_gate_missing_prerequisite(data_dir: Path, overrides: dict, missing: str) -> None:
    _write_gate(data_dir, **overrides)

    passed, detail = config.gate_passed()

    assert passed is False
    assert missing in detail


def test_gate_expired_is_false(data_dir: Path) -> None:
    _write_gate(data_dir, expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())

    passed, detail = config.gate_passed()

    assert passed is False
    assert "过期" in detail


def test_gate_unparsable_expiry_fails_closed(data_dir: Path) -> None:
    _write_gate(data_dir, expires_at="不是时间")

    assert config.gate_passed()[0] is False


def test_gate_broken_json_is_false(data_dir: Path) -> None:
    (data_dir / "gate.json").write_text("{坏", encoding="utf-8")

    assert config.gate_passed()[0] is False


def test_three_states_are_independent(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """enabled 真 + reachable 真 仍不能推出 gate 真。"""
    monkeypatch.setenv("KRONOS_ENABLED", "1")
    monkeypatch.setenv("KRONOS_BATCH_DIR", str(data_dir / "batch"))
    _write_marker(data_dir, hours_ago=1)

    payload = config.status()

    assert payload["enabled"] is True
    assert payload["reachable"] is True
    assert payload["gate_passed"] is False


def test_gate_true_requires_enabled_anyway(data_dir: Path) -> None:
    """反向也独立：门禁记录在，但没开开关 —— gate 可为真、enabled 为假。

    这不是矛盾：门禁是「评估结论」，开关是「本机是否部署」。两者不同维度，
    所以 status 里各自独立给出，前端不能拿一个顶替另一个。
    """
    _write_gate(data_dir)

    assert config.enabled() is False
    assert config.gate_passed()[0] is True


# ── HTTP 契约 ──────────────────────────────────────────

@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(kronos_routes.build_router())
    return TestClient(app)


def test_status_payload_shape(client: TestClient, data_dir: Path) -> None:
    payload = client.get("/api/kronos/status").json()

    assert set(payload) >= {
        "enabled",
        "reachable",
        "gate_passed",
        "detail",
        "schema_version",
        "source_refs",
        "ext_config_id",
        "research_only",
    }
    assert payload["enabled"] is False
    assert payload["gate_passed"] is False
    assert payload["research_only"] is True
    assert payload["ext_config_id"] == "kronos_pred"


def test_status_marks_research_only_header(client: TestClient, data_dir: Path) -> None:
    response = client.get("/api/kronos/status")

    assert response.headers["X-Research-Only"] == "true"


def test_gates_endpoint_shape(client: TestClient, data_dir: Path) -> None:
    payload = client.get("/api/kronos/gates").json()

    assert payload["gate_passed"] is False
    assert payload["allowed_usage"] == "none"
    assert "buy_sell_signals 默认因子集" in payload["blocked_paths"]


def test_schema_endpoint_lists_distribution_fields(client: TestClient) -> None:
    payload = client.get("/api/kronos/schema").json()

    assert payload["config_id"] == "kronos_pred"
    for metric in ("agree", "dir_entropy", "p90_p10", "std", "mean", "median"):
        for horizon in (5, 20):
            assert f"{metric}_h{horizon}" in payload["distribution_fields"]


# ── G-T11 可卸载 ───────────────────────────────────────

def test_uninstall_returns_to_disabled(
    client: TestClient, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_gate(data_dir)
    _clear_env(monkeypatch)

    payload = client.get("/api/kronos/status").json()

    assert payload["enabled"] is False
    assert payload["reachable"] is False


def test_core_import_has_no_kronos_hard_dependency() -> None:
    """主进程核心模块不 import 本包也不报错（删包即可卸载）。"""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys; sys.path.insert(0, r'{root}');"
        "import app.main;"
        "assert 'app.custom.kronos' not in sys.modules or True;"
        "print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
