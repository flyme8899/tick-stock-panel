"""DSA .env writes when os.replace cannot retarget a single-file bind mount.

Production save returns 500 with
``Operation not permitted: '/app/config/.env.tmp' -> '/app/config/.env'``.
That is PermissionError / OSError EPERM. The previous fallback only handled
EBUSY and EXDEV, so it never ran.

Watchlist add/remove/remove-batch, the schedule page, settings save, and
.env import all persist through ConfigManager.apply_updates.
"""
from __future__ import annotations

import ast
import errno
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_VENDOR = _ROOT / "vendor" / "daily_stock_analysis"
sys.path.insert(0, str(_VENDOR))

from src.core.config_manager import ConfigManager  # noqa: E402

_ORIGINAL = (
    "# shared with TSP\n"
    "STOCK_LIST=600519,000001,300750\n"
    "SCHEDULE_ENABLED=false\n"
    "SCHEDULE_TIME=18:00\n"
    "SCHEDULE_TIMES=18:00\n"
    "FEISHU_WEBHOOK_URL=https://example.invalid/hook\n"
)
_UPDATED = (
    "# shared with TSP\n"
    "STOCK_LIST=600519\n"
    "SCHEDULE_ENABLED=true\n"
    "SCHEDULE_TIME=19:05\n"
    "SCHEDULE_TIMES=19:05\n"
    "FEISHU_WEBHOOK_URL=https://example.invalid/hook\n"
)
_UPDATES = [
    ("STOCK_LIST", "600519"),
    ("SCHEDULE_ENABLED", "true"),
    ("SCHEDULE_TIME", "19:05"),
    ("SCHEDULE_TIMES", "19:05"),
]


def _eperm_permission(src: str, dst: str) -> OSError:
    return PermissionError(errno.EPERM, "Operation not permitted", src, dst)


def _eperm_oserror(src: str, dst: str) -> OSError:
    return OSError(errno.EPERM, "Operation not permitted", src, dst)


def _eacces(src: str, dst: str) -> OSError:
    return OSError(errno.EACCES, "Permission denied", src, dst)


def _ebusy(src: str, dst: str) -> OSError:
    return OSError(errno.EBUSY, "Device or resource busy", src, dst)


def _exdev(src: str, dst: str) -> OSError:
    return OSError(errno.EXDEV, "Invalid cross-device link", src, dst)


def _permission_without_errno(_src: str, _dst: str) -> OSError:
    return PermissionError(None, "Operation not permitted")


def _apply(env: Path) -> list[str]:
    updated, _skipped, _version = ConfigManager(env).apply_updates(
        updates=_UPDATES,
        sensitive_keys=set(),
        mask_token="******",
    )
    return updated


def test_production_eperm_message_uses_the_inplace_fallback() -> None:
    # os.replace puts the destination in filename2, which is the fifth OSError arg.
    exc = PermissionError(
        errno.EPERM,
        "Operation not permitted",
        "/app/config/.env.tmp",
        None,
        "/app/config/.env",
    )

    assert isinstance(exc, OSError)
    assert ConfigManager._should_rewrite_in_place(exc)
    assert "Operation not permitted" in str(exc)
    assert "'/app/config/.env.tmp' -> '/app/config/.env'" in str(exc)
    assert not ConfigManager._should_rewrite_in_place(OSError(errno.EIO, "Input/output error"))


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(_eperm_permission, id="PermissionError-EPERM"),
        pytest.param(_eperm_oserror, id="OSError-EPERM"),
        pytest.param(_eacces, id="EACCES"),
        pytest.param(_ebusy, id="EBUSY"),
        pytest.param(_exdev, id="EXDEV"),
        pytest.param(_permission_without_errno, id="PermissionError-no-errno"),
    ],
)
def test_replace_refusal_rewrites_shared_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory,
) -> None:
    env = tmp_path / ".env"
    env.write_text(_ORIGINAL, encoding="utf-8")
    inode = env.stat().st_ino
    modes: list[str] = []
    real_open = Path.open

    def refuse(src, dst):
        raise factory(src, dst)

    def recording_open(self: Path, mode: str = "r", *args, **kwargs):
        if self == env:
            modes.append(mode)
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.setattr(Path, "open", recording_open)

    updated = _apply(env)

    assert set(updated) == {
        "STOCK_LIST",
        "SCHEDULE_ENABLED",
        "SCHEDULE_TIME",
        "SCHEDULE_TIMES",
    }
    assert env.read_text(encoding="utf-8") == _UPDATED
    assert env.stat().st_ino == inode
    assert "r+b" in modes
    assert "w" not in modes
    assert "wb" not in modes
    backup = tmp_path / ".env.bak"
    assert backup.read_bytes() == _ORIGINAL.encode("utf-8")
    assert backup.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / ".env.tmp").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_successful_replace_keeps_atomic_path(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(_ORIGINAL, encoding="utf-8")

    _apply(env)

    assert env.read_text(encoding="utf-8") == _UPDATED
    assert not (tmp_path / ".env.bak").exists()
    assert not (tmp_path / ".env.tmp").exists()


def test_unrelated_oserror_does_not_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text(_ORIGINAL, encoding="utf-8")

    def explode(src, dst):
        raise OSError(errno.EIO, "Input/output error", src, dst)

    monkeypatch.setattr(os, "replace", explode)

    with pytest.raises(OSError) as raised:
        _apply(env)

    assert raised.value.errno == errno.EIO
    assert env.read_bytes() == _ORIGINAL.encode("utf-8")
    assert not (tmp_path / ".env.bak").exists()
    assert not (tmp_path / ".env.tmp").exists()


def test_failed_inplace_write_restores_original_and_keeps_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = tmp_path / ".env"
    env.write_bytes(_ORIGINAL.encode("utf-8"))
    inode = env.stat().st_ino

    def refuse(src, dst):
        raise _eperm_permission(src, dst)

    monkeypatch.setattr(os, "replace", refuse)
    real_open = Path.open
    armed = True

    def failing_open(self: Path, mode: str = "r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        nonlocal armed
        if self == env and mode == "r+b" and armed:
            armed = False
            real_write = handle.write

            def boom(data):
                real_write(b"CORRUPT")
                raise OSError(errno.EIO, "simulated short write")

            handle.write = boom
        return handle

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(OSError) as raised:
        _apply(env)

    assert raised.value.errno == errno.EIO
    assert env.read_bytes() == _ORIGINAL.encode("utf-8")
    assert env.stat().st_ino == inode
    assert (tmp_path / ".env.bak").read_bytes() == _ORIGINAL.encode("utf-8")


def test_watchlist_routes_reach_the_env_writer() -> None:
    tree = ast.parse((_VENDOR / "api/v1/endpoints/stocks.py").read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    routes = {
        dec.args[0].value: node.name
        for node in functions.values()
        for dec in node.decorator_list
        if isinstance(dec, ast.Call)
        and isinstance(dec.func, ast.Attribute)
        and dec.func.attr == "post"
        and dec.args
        and isinstance(dec.args[0], ast.Constant)
        and isinstance(dec.args[0].value, str)
    }

    assert routes["/watchlist/add"] == "add_to_watchlist"
    assert routes["/watchlist/remove"] == "remove_from_watchlist"
    assert routes["/watchlist/remove-batch"] == "remove_from_watchlist_batch"
    for name in ("add_to_watchlist", "remove_from_watchlist", "remove_from_watchlist_batch"):
        assert _calls(functions[name], "_write_watchlist_codes")
    assert _calls(functions["_write_watchlist_codes"], "update")


def test_schedule_settings_and_import_share_config_manager() -> None:
    service = (_VENDOR / "src/services/system_config_service.py").read_text(encoding="utf-8")
    auth = (_VENDOR / "api/v1/endpoints/auth.py").read_text(encoding="utf-8")
    schedule = (_ROOT / "backend/app/custom/dsa/schedule.py").read_text(encoding="utf-8")
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert service.count("self._manager.apply_updates(") >= 2
    assert "return self.update(" in service
    assert "manager.apply_updates(" in auth
    assert '_json_call("PUT", "system/config"' in schedule
    assert "./.env:/app/.env:ro" in compose
    assert "./.env:/app/config/.env" in compose
    assert "./:/app/config" not in compose


def _calls(fn: ast.FunctionDef, name: str) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == name:
            return True
    return False
