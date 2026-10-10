"""部署预检：data/news 可写、采集 venv 版本、属主漂移。"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "deploy_preflight.py"
_SPEC = importlib.util.spec_from_file_location("deploy_preflight", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
deploy_preflight = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = deploy_preflight
_SPEC.loader.exec_module(deploy_preflight)


def _layout(tmp_path: Path, mode: int = 0o755) -> Path:
    data = tmp_path / "data"
    news = data / "news"
    news.mkdir(parents=True)
    data.chmod(mode)
    news.chmod(mode)
    return data


def test_version_error_bounds():
    assert deploy_preflight.version_error((3, 9, 18))
    assert "3.9.18" in deploy_preflight.version_error((3, 9, 18))
    assert deploy_preflight.version_error((3, 10, 12)) is None
    assert deploy_preflight.version_error((3, 11, 17)) is None
    assert deploy_preflight.version_warning((3, 10, 12))
    assert deploy_preflight.version_warning((3, 11, 17)) is None


def test_parse_version_and_ids():
    assert deploy_preflight.parse_version("3.10.12\n") == (3, 10, 12)
    assert deploy_preflight.resolve_id(None, None, None, 1000, "APP_UID") == 1000
    assert deploy_preflight.resolve_id(None, "1001", "1000", 1000, "APP_UID") == 1001
    assert deploy_preflight.resolve_id(7, "1001", "1000", 1000, "APP_UID") == 7
    with pytest.raises(ValueError):
        deploy_preflight.parse_id("nope", "APP_UID")


def test_default_python_path():
    path = deploy_preflight.default_python(None, None)
    assert path == Path.home() / ".venvs" / "tsp-collector" / "bin" / "python"
    assert deploy_preflight.default_python("/tmp/py", None) == Path("/tmp/py")


def test_preflight_passes_for_matching_owner(tmp_path: Path):
    data = _layout(tmp_path)
    (data / "news" / "inbox").mkdir()
    (data / "news" / "news.sqlite").write_text("ok", encoding="utf-8")
    errors, warnings = deploy_preflight.collect_problems(
        data,
        uid=os.getuid(),
        gid=os.getgid(),
        python=Path(sys.executable),
        version=(3, 11, 0),
    )
    assert errors == []
    assert warnings == []


def test_preflight_flags_ownership_drift_even_if_world_writable(tmp_path: Path):
    data = _layout(tmp_path, mode=0o777)
    (data / "news" / "news.sqlite").write_text("ok", encoding="utf-8")
    (data / "news" / "push_state.json").write_text("{}", encoding="utf-8")
    errors, _warnings = deploy_preflight.collect_problems(
        data,
        uid=4242,
        gid=4242,
        python=Path(sys.executable),
        version=(3, 11, 0),
    )
    text = "\n".join(errors)
    assert "news.sqlite" in text
    assert "push_state.json" in text
    assert "4242" in text
    assert "不可写" not in text


def test_preflight_rejects_unwritable_news(tmp_path: Path):
    data = _layout(tmp_path)
    news = data / "news"
    news.chmod(0o555)
    errors, _warnings = deploy_preflight.collect_problems(
        data,
        uid=os.getuid(),
        gid=os.getgid(),
        python=Path(sys.executable),
        version=(3, 11, 0),
    )
    assert any("不可写" in item and str(news) in item for item in errors)


def test_preflight_rejects_old_or_missing_python(tmp_path: Path):
    data = _layout(tmp_path)
    errors, warnings = deploy_preflight.collect_problems(
        data,
        uid=os.getuid(),
        gid=os.getgid(),
        python=Path(sys.executable),
        version=(3, 9, 0),
    )
    assert any("3.9.0" in item for item in errors)
    assert warnings == []

    missing, _notes = deploy_preflight.collect_problems(
        data,
        uid=os.getuid(),
        gid=os.getgid(),
        python=tmp_path / "no-such-python",
    )
    assert any("找不到采集器 Python" in item for item in missing)


def test_preflight_warns_on_python_310(tmp_path: Path):
    data = _layout(tmp_path)
    errors, warnings = deploy_preflight.collect_problems(
        data,
        uid=os.getuid(),
        gid=os.getgid(),
        python=Path(sys.executable),
        version=(3, 10, 12),
    )
    assert errors == []
    assert warnings and "3.10.12" in warnings[0]
    assert "3.11" in warnings[0]


def test_cli_exits_nonzero_on_drift(tmp_path: Path):
    data = _layout(tmp_path, mode=0o777)
    (data / "news" / "news.sqlite-wal").write_text("", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--data-dir",
            str(data),
            "--uid",
            "4242",
            "--gid",
            "4242",
            "--python",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "news.sqlite-wal" in proc.stderr
    assert "部署预检通过" not in proc.stdout


def test_cli_exits_zero_when_layout_matches(tmp_path: Path):
    data = _layout(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--data-dir",
            str(data),
            "--uid",
            str(os.getuid()),
            "--gid",
            str(os.getgid()),
            "--python",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "部署预检通过" in proc.stdout


def test_compose_runs_as_configurable_user_outside_root():
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert 'user: "${APP_UID:-1000}:${APP_GID:-1000}"' in compose
    assert "/codex-home:ro" in compose
    assert ":/root/.codex" not in compose
    assert "HOME=/home/app" in compose
    assert "UV_CACHE_DIR=/home/app/.cache/uv" in compose
    assert "CODEX_HOME=/codex-home" in compose
    assert "HOME=/home/app" in dockerfile
    assert "UV_CACHE_DIR=/home/app/.cache/uv" in dockerfile
    assert "CODEX_HOME=/codex-home" in dockerfile
    assert "UV_NO_SYNC=1" in dockerfile
    assert "chmod 1777" in dockerfile
    assert 'CMD ["uv", "run", "--no-sync"' in dockerfile
    assert "APP_UID=1000" in example
    assert "APP_GID=1000" in example


def test_docs_cover_venv_rebuild_chown_and_preflight():
    news = (_ROOT / "docs" / "news-sources.md").read_text(encoding="utf-8")
    deploy = (_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for text in (news, deploy):
        assert "scripts/deploy_preflight.py" in text
        assert "sudo chown -R 1000:1000 data" in text
    assert "deadsnakes" in news
    assert "uv python install 3.11" in news
    assert "APP_UID=0" in deploy
    assert "data/news" in deploy
