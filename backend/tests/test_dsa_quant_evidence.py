"""Quant-evidence rendering and the sidecar skill hook."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.dsa import EXTENSION_API_VERSION, EXTENSION_ID, setup
from app.custom.dsa.dsa_bootstrap import install
from app.custom.dsa.quant_evidence import evidence_summary, render_skill_appendix
from app.extensions.registry import BackendExtensionRegistrar

_VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "daily_stock_analysis"
_SHIPPED = Path(__file__).resolve().parents[1] / "app" / "custom" / "dsa" / "quant_evidence.yaml"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("TSP_QUANT_EVIDENCE_FILE", raising=False)
    app = FastAPI()
    registrar = BackendExtensionRegistrar(EXTENSION_ID, api_version=EXTENSION_API_VERSION)
    setup(registrar)
    for router in registrar.routers:
        app.include_router(router)
    return TestClient(app)


def test_shipped_evidence_renders_known_skill_and_skips_others() -> None:
    golden = render_skill_appendix("ma_golden_cross", _SHIPPED)
    wave = render_skill_appendix("wave_theory", _SHIPPED)

    assert "量化回测参考" in golden
    assert "-14.70%" in golden
    assert "要跑赢的基准线，不是禁用令" in golden
    assert wave == ""


def test_malformed_returns_do_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "evidence.yaml"
    path.write_text(
        """
_meta:
  period: "2026-07-08 ~ 2026-10-08"
  universe: 3
ma_golden_cross:
  matched: ["MA金叉"]
  returns: ["bad"]
  sample_sizes: [59]
  note: "仍应出现"
  guidance: |
    1. 保持谨慎
""",
        encoding="utf-8",
    )

    text = render_skill_appendix("ma_golden_cross", path)

    assert "量化回测参考" in text
    assert "「MA金叉」" in text
    assert "59 笔" in text
    assert "仍应出现" in text
    assert "bad" not in text
    summary = evidence_summary(path)
    assert summary["skills"][0]["returns"] == []
    assert summary["skills"][0]["sample_sizes"] == [59]


def test_missing_file_is_empty(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"

    assert render_skill_appendix("ma_golden_cross", missing) == ""
    assert evidence_summary(missing)["available"] is False


def test_off_switch_hides_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TSP_QUANT_EVIDENCE_FILE", "off")

    summary = evidence_summary()

    assert summary["available"] is False
    assert summary["skills"] == []


def test_quant_evidence_route_lists_shipped_skills(client: TestClient) -> None:
    response = client.get("/api/dsa/quant-evidence")

    assert response.status_code == 200
    body = response.json()
    names = {item["name"] for item in body["skills"]}
    assert "ma_golden_cross" in names
    assert "wave_theory" not in names
    golden = next(item for item in body["skills"] if item["name"] == "ma_golden_cross")
    assert golden["returns"][0] == -14.70
    catalog = client.get("/api/dsa/catalog")
    assert any(item["name"] == "TSP_QUANT_EVIDENCE_FILE" for item in catalog.json()["env_vars"])


def test_hook_appends_only_matching_skills(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(_VENDOR))
    import src.agent.skills as skills_pkg
    import src.agent.skills.base as base

    monkeypatch.setattr(base, "load_skill_from_yaml", base.load_skill_from_yaml)
    monkeypatch.setattr(skills_pkg, "load_skill_from_yaml", skills_pkg.load_skill_from_yaml)
    monkeypatch.setattr(base, "load_skill_from_markdown", base.load_skill_from_markdown)

    golden_path = _VENDOR / "strategies" / "ma_golden_cross.yaml"
    wave_path = _VENDOR / "strategies" / "wave_theory.yaml"
    before_golden = base.load_skill_from_yaml(golden_path).instructions
    before_wave = base.load_skill_from_yaml(wave_path).instructions

    monkeypatch.setenv("TSP_QUANT_EVIDENCE_FILE", str(_SHIPPED))
    install()
    after_golden = base.load_skill_from_yaml(golden_path).instructions
    after_wave = base.load_skill_from_yaml(wave_path).instructions

    assert after_golden.startswith(before_golden)
    assert "**量化回测参考**" in after_golden
    assert "-14.70%" in after_golden
    assert after_wave == before_wave
    assert base.load_skill_from_yaml(golden_path).instructions.count("**量化回测参考**") == 1

    monkeypatch.setenv("TSP_QUANT_EVIDENCE_FILE", "off")
    assert base.load_skill_from_yaml(golden_path).instructions == before_golden

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "ma_golden_cross:\n  matched: [MA金叉]\n  returns: [bad]\n  sample_sizes: [59]\n  note: 仍应出现\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TSP_QUANT_EVIDENCE_FILE", str(bad))
    patched = base.load_skill_from_yaml(golden_path).instructions
    appendix = patched.split("**量化回测参考**", 1)[1]
    assert "59 笔" in appendix
    assert "bad" not in appendix
