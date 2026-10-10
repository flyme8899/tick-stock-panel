"""ETF rotation runs inside the DSA process, with a fixed argument list."""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.dsa.dsa_bootstrap import _install_etf_job
from app.custom.dsa.etf_job import COMMAND, dsa_root, install, install_import_hook, run_job


def _source_tree(path: Path) -> None:
    (path / "src").mkdir(parents=True, exist_ok=True)
    (path / "main.py").write_text("print('etf')\n", encoding="utf-8")


def test_run_job_uses_fixed_argv_and_ignores_nothing_from_the_caller(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _source_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="报告", stderr="")

    monkeypatch.setattr("app.custom.dsa.etf_job.subprocess.run", fake_run)

    result = run_job()

    assert result["ok"] is True
    assert result["code"] == 0
    assert result["detail"] == "报告"
    assert result["command"] == COMMAND
    assert captured["argv"] == [sys.executable, "main.py", "--etf-rotation", "--no-notify"]
    assert captured["kwargs"]["cwd"] == tmp_path.resolve()
    assert captured["kwargs"]["timeout"] == 180
    assert "shell" not in captured["kwargs"]
    assert "ETF_ROTATION_POOL" not in captured["argv"]


def test_run_job_keeps_stderr_when_the_command_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _source_tree(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="池子为空")

    monkeypatch.setattr("app.custom.dsa.etf_job.subprocess.run", fake_run)

    result = run_job()

    assert result["ok"] is False
    assert result["code"] == 2
    assert "池子为空" in result["detail"]


def test_run_job_timeout_does_not_look_successful(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _source_tree(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="main.py", timeout=180)

    monkeypatch.setattr("app.custom.dsa.etf_job.subprocess.run", fake_run)

    result = run_job()

    assert result == {"ok": False, "detail": "ETF 轮动超时", "command": COMMAND}


def test_run_job_refuses_to_start_without_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("app.custom.dsa.etf_job._root_candidates", lambda: (tmp_path,))
    monkeypatch.setattr(
        "app.custom.dsa.etf_job.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("没有源码不应启动")),
    )

    result = run_job()

    assert result["ok"] is False
    assert "main.py" in result["detail"]


def test_dsa_root_prefers_cwd_then_image_workdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    image = tmp_path / "image"
    empty.mkdir()
    _source_tree(image)
    monkeypatch.chdir(empty)
    monkeypatch.setattr("app.custom.dsa.etf_job._root_candidates", lambda: (Path.cwd(), image))

    assert dsa_root() == image.resolve()

    _source_tree(empty)
    assert dsa_root() == empty.resolve()


def test_overlapping_run_does_not_start_a_second_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _source_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def fake_run(argv, **_kwargs):
        calls["n"] += 1
        entered.set()
        assert release.wait(2)
        return subprocess.CompletedProcess(argv, 0, stdout="done", stderr="")

    monkeypatch.setattr("app.custom.dsa.etf_job.subprocess.run", fake_run)
    first = threading.Thread(target=run_job)
    first.start()
    assert entered.wait(2)
    second = run_job()
    release.set()
    first.join(2)

    assert second == {"ok": False, "detail": "ETF 轮动正在运行", "command": COMMAND}
    assert calls["n"] == 1
    assert not first.is_alive()


def test_route_ignores_request_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _source_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr("app.custom.dsa.etf_job.subprocess.run", fake_run)
    app = FastAPI()
    install(app)
    install(app)

    response = TestClient(app).post("/api/v1/tsp/etf-rotation", json={"cmd": "echo pwned"})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert seen["argv"] == [sys.executable, "main.py", "--etf-rotation", "--no-notify"]
    assert seen["cwd"] == tmp_path.resolve()
    paths = [getattr(route, "path", "") for route in app.routes]
    assert paths.count("/api/v1/tsp/etf-rotation") == 1
    assert TestClient(app).get("/api/v1/tsp/etf-rotation").status_code == 405


def test_import_hook_mounts_route_after_the_app_module_loads(tmp_path: Path) -> None:
    package = tmp_path / "tsp_etf_probe_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    fullname = "tsp_etf_probe_pkg.app"
    sys.path.insert(0, str(tmp_path))
    before = {id(item) for item in sys.meta_path}
    try:
        install_import_hook(fullname)
        import tsp_etf_probe_pkg.app as probed

        paths = [getattr(route, "path", "") for route in probed.app.routes]
        assert "/api/v1/tsp/etf-rotation" in paths
        install_import_hook(fullname)
        paths = [getattr(route, "path", "") for route in probed.app.routes]
        assert paths.count("/api/v1/tsp/etf-rotation") == 1
    finally:
        sys.meta_path[:] = [item for item in sys.meta_path if id(item) in before]
        sys.path[:] = [item for item in sys.path if item != str(tmp_path)]
        for name in list(sys.modules):
            if name == "tsp_etf_probe_pkg" or name.startswith("tsp_etf_probe_pkg."):
                del sys.modules[name]


def test_bootstrap_registers_one_import_hook() -> None:
    before = {id(item) for item in sys.meta_path}
    try:
        _install_etf_job()
        added = [item for item in sys.meta_path if id(item) not in before]
        assert len(added) == 1
        assert added[0].fullname == "api.app"
        _install_etf_job()
        assert [item for item in sys.meta_path if id(item) not in before] == added
    finally:
        sys.meta_path[:] = [item for item in sys.meta_path if id(item) in before]
