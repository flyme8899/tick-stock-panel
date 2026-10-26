"""主进程零 torch / 零 Kronos 运行时；compose profile 不默认拉起（G-T1 / G-T9）。

主进程嵌 torch 的代价不是镜像大几百 MB，而是把一条**不可控的运行时**塞进了面板：
GPU 抢占、numpy/ABI 冲突、启动时间翻倍、以及「机器上没显卡就起不来」。所以这里
用扫描的方式把边界钉死——不是靠自觉。
"""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
_APP = _BACKEND / "app"

# 允许出现 torch 字样的地方：文档字符串、注释、测试断言。代码里出现即 red。
_FORBIDDEN_IMPORTS = ("torch", "kronos", "tensorflow", "jax", "qlib")

_KRONOS_PKG = _APP / "custom" / "kronos"


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imported_modules(path: Path) -> set[str]:
    """静态解析 import 语句的顶层模块名（不求值、不执行）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif (
            isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
        ):
            names.add(node.module.split(".")[0])
    return names


# ── G-T1 主进程零重依赖 ───────────────────────────────

def test_app_never_imports_heavy_ml_runtime() -> None:
    offenders: list[str] = []
    for path in _python_files(_APP):
        hits = _imported_modules(path) & set(_FORBIDDEN_IMPORTS)
        if hits:
            offenders.append(f"{path.relative_to(_ROOT)}: {sorted(hits)}")

    assert offenders == [], "主进程 import 了重运行时：" + "；".join(offenders)


def test_kronos_package_has_no_runtime_imports() -> None:
    """Kronos 包里一个推理运行时都不许有——它只做配置、探活、门禁、导入。"""
    offenders: list[str] = []
    for path in _python_files(_KRONOS_PKG):
        hits = _imported_modules(path) & {"torch", "kronos", "tensorflow", "jax"}
        if hits:
            offenders.append(f"{path.name}: {sorted(hits)}")

    assert offenders == []


def test_runtime_dependencies_have_no_ml_framework() -> None:
    """主依赖表不能有 torch / 推理框架。"""
    data = tomllib.loads((_BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"].get("dependencies", [])
    names = {re.split(r"[<>=!~\[]", dep.strip(), maxsplit=1)[0].lower() for dep in deps}

    assert not (names & {"torch", "torchvision", "kronos", "tensorflow", "jax", "qlib"}), names


def test_kronos_import_does_not_pull_torch() -> None:
    """导入本包后 sys.modules 里没有 torch。"""
    import subprocess
    import sys

    code = (
        "import sys;"
        f"sys.path.insert(0, r'{_BACKEND}');"
        "import app.custom.kronos as k;"
        "assert 'torch' not in sys.modules, 'torch 被带进主进程';"
        "print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_no_weight_files_in_repo() -> None:
    """权重不入仓：仓库里不能有 .pt / .pth / .safetensors / .ckpt。"""
    heavy = [
        p.relative_to(_ROOT)
        for pattern in ("*.pt", "*.pth", "*.safetensors", "*.ckpt", "*.bin")
        for p in _ROOT.rglob(pattern)
        if "node_modules" not in p.parts and ".git" not in p.parts
    ]

    assert heavy == [], f"仓库里有权重文件：{heavy}"


# ── G-T9 compose profile ──────────────────────────────

@pytest.fixture(scope="module")
def compose_text() -> str:
    return (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_kronos_service_is_profiled(compose_text: str) -> None:
    assert "kronos:" in compose_text
    pattern = r"kronos:\s*\n\s+profiles:\s*\[\"kronos\"\]"
    assert re.search(pattern, compose_text), (
        "kronos 服务必须挂在 profiles: [\"kronos\"] 下，否则默认 up 会拉起"
    )


def test_kronos_is_not_in_default_boot(compose_text: str) -> None:
    """默认 `docker compose up`（不带 profile）不能拉起 kronos。"""
    import yaml

    data = yaml.safe_load(compose_text)
    service = data["services"]["kronos"]

    assert service.get("profiles") == ["kronos"]
    # 也不能被别的默认服务 depends_on 拉着一起起。
    for name, other in data["services"].items():
        if other.get("profiles"):
            continue
        depends = other.get("depends_on") or {}
        keys = depends if isinstance(depends, dict) else {d: {} for d in depends}
        assert "kronos" not in keys, f"{name} 通过 depends_on 拉起了 kronos"


def test_kronos_sidecar_is_not_the_app_image(compose_text: str) -> None:
    """sidecar 不能复用 app 镜像（会把 torch 混进来）。"""
    import yaml

    data = yaml.safe_load(compose_text)
    service = data["services"]["kronos"]
    build = service.get("build") or {}

    assert build.get("context") != ".", "kronos sidecar 用了主仓根目录构建"
    assert "kronos" in str(build.get("context", "")), build


def test_kronos_sidecar_env_has_no_secret(compose_text: str) -> None:
    import yaml

    data = yaml.safe_load(compose_text)
    env = data["services"]["kronos"].get("environment") or []
    joined = (
        " ".join(env)
        if isinstance(env, list)
        else " ".join(f"{k}={v}" for k, v in env.items())
    )

    assert "PASSWORD" not in joined.upper()
    assert "SECRET" not in joined.upper()
    assert "TOKEN" not in joined.upper()


# ── 静态：Kronos 不碰 BSS / 不转发上游 ────────────────

def test_kronos_routes_have_no_write_method() -> None:
    source = (_KRONOS_PKG / "routes.py").read_text(encoding="utf-8")

    for verb in ("@router.post", "@router.put", "@router.delete", "@router.patch"):
        assert verb not in source, f"Kronos 路由不该有 {verb}"


def test_kronos_routes_do_not_proxy_arbitrary_paths() -> None:
    source = (_KRONOS_PKG / "routes.py").read_text(encoding="utf-8")

    assert "{path" not in source
    assert "forward(" not in source


def test_kronos_package_does_not_import_bss() -> None:
    for path in _python_files(_KRONOS_PKG):
        text = path.read_text(encoding="utf-8")
        assert "from app.buy_sell_signals" not in text, path.name
        assert "import app.buy_sell_signals" not in text, path.name
