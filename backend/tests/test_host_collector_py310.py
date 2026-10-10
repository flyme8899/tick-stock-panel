"""宿主机采集器的导入闭包必须能在 Python 3.10 上加载。"""
from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
_SCRIPT = _ROOT / "scripts" / "news_host_collector.py"
_NESTED = {
    _SCRIPT,
    _BACKEND / "app" / "news" / "host_collector.py",
}
# 导入期会执行的模块，加上 host_collector 里发送登录提醒时才导入的 dingtalk。
# 不跟 config 函数体内的 preferences：采集器读群号和环境变量，不进那条路径。
_ALLOWED = {
    "scripts/news_host_collector.py",
    "backend/app/__init__.py",
    "backend/app/config.py",
    "backend/app/market_time.py",
    "backend/app/news/__init__.py",
    "backend/app/news/cleaning.py",
    "backend/app/news/cls_sign.py",
    "backend/app/news/collectors.py",
    "backend/app/news/config.py",
    "backend/app/news/dingtalk.py",
    "backend/app/news/extract.py",
    "backend/app/news/host_collector.py",
}

_SPEC = importlib.util.spec_from_file_location("news_host_collector", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
news_host_collector = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = news_host_collector
_SPEC.loader.exec_module(news_host_collector)


def _module_path(module: str) -> Path | None:
    if module != "app" and not module.startswith("app."):
        return None
    parts = module.split(".")
    file_path = _BACKEND.joinpath(*parts).with_suffix(".py")
    if file_path.is_file():
        return file_path
    init = _BACKEND.joinpath(*parts, "__init__.py")
    if init.is_file():
        return init
    return None


def _parent_inits(module: str) -> list[Path]:
    parts = module.split(".")
    found: list[Path] = []
    for index in range(1, len(parts)):
        init = _BACKEND.joinpath(*parts[:index], "__init__.py")
        if init.is_file():
            found.append(init)
    return found


def _imported_modules(path: Path, *, nested: bool) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = ast.walk(tree) if nested else tree.body
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            for alias in node.names:
                candidate = f"{node.module}.{alias.name}"
                if _module_path(candidate) is not None:
                    found.add(candidate)
    return found


def _uses_datetime_utc(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "datetime"
            and any(alias.name == "UTC" for alias in node.names)
        ):
            return True
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "UTC"
            and isinstance(node.value, ast.Name)
            and node.value.id == "datetime"
        ):
            return True
    return False


def host_collector_files() -> set[Path]:
    pending = [_SCRIPT]
    seen: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for module in _imported_modules(path, nested=path in _NESTED):
            resolved = _module_path(module)
            if resolved is None:
                continue
            pending.append(resolved)
            pending.extend(_parent_inits(module))
    return seen


def test_host_collector_closure_has_no_datetime_utc():
    relative = {path.relative_to(_ROOT).as_posix() for path in host_collector_files()}
    assert relative == _ALLOWED
    offenders = sorted(name for name in relative if _uses_datetime_utc(_ROOT / name))
    assert offenders == []


def test_ensure_supported_python_rejects_39(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as caught:
        news_host_collector.ensure_supported_python((3, 9, 18))
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert "3.9.18" in err
    assert "3.10" in err
    assert "docs/news-sources.md" in err


def test_ensure_supported_python_accepts_310_and_311():
    news_host_collector.ensure_supported_python((3, 10, 12))
    news_host_collector.ensure_supported_python((3, 11, 17))


def test_main_checks_python_before_importing_app():
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    main = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    first = main.body[0]
    assert isinstance(first, ast.Expr)
    assert isinstance(first.value, ast.Call)
    assert isinstance(first.value.func, ast.Name)
    assert first.value.func.id == "ensure_supported_python"
    imports = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.ImportFrom) and node.module == "app.news.host_collector"
    ]
    assert imports
    assert imports[0].lineno > first.lineno


def test_host_collector_imports_under_python310():
    interpreter = os.environ.get("TSP_PYTHON310") or shutil.which("python3.10")
    if not interpreter:
        pytest.skip("python3.10 不在 PATH 上；静态检查已覆盖 datetime.UTC")
    probe = subprocess.run(
        [interpreter, "-c", "import pydantic, pydantic_settings"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("python3.10 未安装 pydantic，无法走完采集器导入")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_BACKEND)
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text("", encoding="utf-8")
        env["TICKFLOW_ENV_FILE"] = str(env_file)
        proc = subprocess.run(
            [
                interpreter,
                "-c",
                "import app.news.host_collector, app.news.dingtalk; print('ok')",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=_ROOT,
            env=env,
        )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
