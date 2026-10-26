"""N1：compose 的 dsa 服务必须把五个钩子都只读挂进容器。

``dsa_bootstrap.py`` 用 ``Path(__file__).with_name("<hook>.py")`` 找钩子，
容器里 ``/opt/tsp/`` 只有显式挂进去的文件。少挂一行，容器内就走「未找到」
分支——那是显式跳过，不是装载成功，也就等于该钩子在生产里从未生效。

本拍选的是**选项 A**（补挂载），所以这里直接断言五行都在。
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "docker-compose.yml"
_HOOK_DIR = "backend/app/custom/dsa"

_HOOKS = {
    "quant_evidence.py": "/opt/tsp/quant_evidence.py",
    "news_bridge.py": "/opt/tsp/news_bridge.py",
    "etf_job.py": "/opt/tsp/etf_job.py",
    "fund_flow_bridge.py": "/opt/tsp/fund_flow_bridge.py",
    "analysis_bridge.py": "/opt/tsp/analysis_bridge.py",
}


@pytest.fixture(scope="module")
def compose_text() -> str:
    return _COMPOSE.read_text(encoding="utf-8")


@pytest.mark.parametrize("name,target", sorted(_HOOKS.items()))
def test_dsa_service_mounts_hook(compose_text: str, name: str, target: str) -> None:
    expected = f"./{_HOOK_DIR}/{name}:{target}:ro"

    assert expected in compose_text, f"compose 缺挂载：{expected}"


def test_fund_flow_bridge_mount_is_present(compose_text: str) -> None:
    """N1 专项：资金进出桥是这拍新补的挂载，单独按住。"""
    assert f"./{_HOOK_DIR}/fund_flow_bridge.py:/opt/tsp/fund_flow_bridge.py:ro" in compose_text


def test_hooks_are_mounted_read_only(compose_text: str) -> None:
    """五个挂载都必须带 ``:ro``，别让容器改宿主代码。"""
    for name, target in _HOOKS.items():
        line = f"./{_HOOK_DIR}/{name}:{target}:ro"
        assert line.endswith(":ro"), line
        assert line in compose_text


def test_dsa_service_keeps_bootstrap_entrypoint(compose_text: str) -> None:
    """挂了钩子却换掉 entrypoint 等于白挂。"""
    assert 'entrypoint: ["python", "/opt/tsp/dsa_bootstrap.py"]' in compose_text


def test_self_check_script_agrees(compose_text: str) -> None:
    """自检脚本与测试读同一份期望，避免两边各写一套挂载表。"""
    script = (_ROOT / "scripts" / "dsa_upgrade_check.py").read_text(encoding="utf-8")

    for name, target in _HOOKS.items():
        assert name in script
        assert target in script
