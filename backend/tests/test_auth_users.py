"""多用户登录: 哈希存储、独立会话、旧版密码兼容、失败限流。"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import bcrypt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import config as app_config
from app.api import auth as auth_api
from app.services import auth, user_accounts

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(app_config.settings, "auth_users", "")
    monkeypatch.setattr(app_config.settings, "auth_password", "")
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(app_config, "_ENV_FILE", env_path)
    monkeypatch.delenv("AUTH_USERS", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    auth._sessions.clear()
    auth._configured_cache = None
    user_accounts.reset_cache()
    auth_api._fail_counter.clear()
    yield tmp_path
    auth._sessions.clear()
    auth._configured_cache = None
    user_accounts.reset_cache()
    auth_api._fail_counter.clear()


def _api() -> TestClient:
    app = FastAPI()
    app.include_router(auth_api.router)
    return TestClient(app)


def test_add_user_stores_argon2_hash_not_plaintext(tmp_path: Path) -> None:
    password = "plain-secret-should-not-leak"
    record = user_accounts.add_user("Alice", password)

    assert record.username == "alice"
    raw = (tmp_path / "users.json").read_text(encoding="utf-8")
    assert password not in raw
    assert record.password_hash.startswith("$argon2id$")
    assert user_accounts.is_password_hash(record.password_hash)
    mode = stat.S_IMODE((tmp_path / "users.json").stat().st_mode)
    assert mode == 0o600


def test_users_login_are_independent_and_logout_is_per_session() -> None:
    user_accounts.add_user("alice", "alice-password-1")
    user_accounts.add_user("bob", "bob-password-22")

    alice = auth.verify_and_create_session("alice-password-1", "Alice")
    bob = auth.verify_and_create_session("bob-password-22", "bob")
    assert alice and bob and alice != bob
    assert auth.session_username(alice) == "alice"
    assert auth.session_username(bob) == "bob"
    assert auth.verify_and_create_session("alice-password-1", "bob") is None
    assert auth.verify_and_create_session("bob-password-22", "alice") is None

    alice_info = auth.session_info(alice)
    bob_info = auth.session_info(bob)
    assert alice_info is not None and bob_info is not None
    assert "role" not in alice_info and "role" not in bob_info
    assert alice_info["kind"] == bob_info["kind"] == "user"

    auth.revoke_session(alice)
    assert auth.is_valid_session(alice) is False
    assert auth.is_valid_session(bob) is True
    assert auth.session_username(bob) == "bob"


def test_reset_and_remove_invalidate_only_that_user() -> None:
    user_accounts.add_user("alice", "alice-password-1")
    user_accounts.add_user("bob", "bob-password-22")
    alice = auth.verify_and_create_session("alice-password-1", "alice")
    bob = auth.verify_and_create_session("bob-password-22", "bob")
    assert alice and bob

    user_accounts.set_user_password("alice", "alice-password-2")
    assert auth.is_valid_session(alice) is False
    assert auth.verify_and_create_session("alice-password-1", "alice") is None
    assert auth.verify_and_create_session("alice-password-2", "alice") is not None
    assert auth.is_valid_session(bob) is True

    user_accounts.remove_user("bob")
    assert auth.is_valid_session(bob) is False
    assert auth.verify_and_create_session("bob-password-22", "bob") is None


def test_legacy_password_still_logs_in_and_does_not_cover_users() -> None:
    auth.set_password("legacy-secret")
    user_accounts.add_user("alice", "alice-password-1")
    legacy_before = auth.verify_and_create_session("legacy-secret")
    assert legacy_before is not None
    assert auth.session_username(legacy_before) == "admin"

    assert auth.verify_and_create_session("legacy-secret", "admin") is not None
    assert auth.verify_and_create_session("legacy-secret", "") is not None
    assert auth.verify_and_create_session("alice-password-1") is None
    assert auth.verify_and_create_session("legacy-secret", "alice") is None
    assert auth.is_configured() is True


def test_named_admin_shadows_legacy_username_but_blank_stays_legacy() -> None:
    auth.set_password("legacy-secret")
    user_accounts.add_user("admin", "admin-user-pass")

    named = auth.verify_and_create_session("admin-user-pass", "admin")
    assert named is not None
    assert auth.session_info(named)["kind"] == "user"
    assert auth.verify_and_create_session("legacy-secret", "admin") is None

    blank = auth.verify_and_create_session("legacy-secret", "")
    assert blank is not None
    assert auth.session_info(blank)["kind"] == "legacy"


def test_changing_legacy_password_keeps_user_sessions() -> None:
    auth.set_password("legacy-secret")
    user_accounts.add_user("alice", "alice-password-1")
    legacy = auth.verify_and_create_session("legacy-secret")
    alice = auth.verify_and_create_session("alice-password-1", "alice")
    assert legacy and alice

    auth.set_password("legacy-secret-2")
    assert auth.is_valid_session(legacy) is False
    assert auth.is_valid_session(alice) is True
    assert auth.verify_and_create_session("legacy-secret") is None
    assert auth.verify_and_create_session("legacy-secret-2", "admin") is not None


def test_old_session_format_still_restores(tmp_path: Path) -> None:
    auth.set_password("legacy-secret")
    auth._sessions.clear()
    path = tmp_path / "user_data" / "auth.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sessions"] = {"old-token": time.time() + 3600}
    path.write_text(json.dumps(data), encoding="utf-8")

    auth._restore_sessions()
    assert auth.is_valid_session("old-token") is True
    assert auth.session_username("old-token") == "admin"
    saved = json.loads(path.read_text(encoding="utf-8"))["sessions"]["old-token"]
    assert isinstance(saved, dict)
    assert saved["kind"] == "legacy"


def test_plaintext_and_unknown_hash_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "users.json"
    path.write_text(
        json.dumps({
            "users": {
                "eve": {"password_hash": "plaintext-secret", "rev": 1},
                "mallory": {"password_hash": "sha256:abcd", "rev": 1},
            },
        }),
        encoding="utf-8",
    )
    user_accounts.reset_cache()
    assert user_accounts.list_usernames() == []
    assert user_accounts.verify_user("eve", "plaintext-secret") is None
    assert auth.verify_and_create_session("plaintext-secret", "eve") is None


def test_bcrypt_hash_can_log_in(tmp_path: Path) -> None:
    encoded = bcrypt.hashpw(b"bcrypt-secret", bcrypt.gensalt(rounds=4)).decode("ascii")
    (tmp_path / "users.json").write_text(
        json.dumps({"users": {"bob": {"password_hash": encoded, "rev": 3}}}),
        encoding="utf-8",
    )
    user_accounts.reset_cache()
    token = auth.verify_and_create_session("bcrypt-secret", "bob")
    assert token is not None
    assert auth.session_info(token)["rev"] == 3
    assert auth.verify_and_create_session("other-secret", "bob") is None


def test_auth_users_inline_json_and_path_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    password_hash = user_accounts.hash_password("inline-password")
    monkeypatch.setattr(
        app_config.settings,
        "auth_users",
        json.dumps([{"username": "cara", "password_hash": password_hash}]),
    )
    user_accounts.reset_cache()
    assert user_accounts.list_usernames() == ["cara"]
    assert auth.verify_and_create_session("inline-password", "cara") is not None
    assert not (tmp_path / "users.json").exists()

    custom = tmp_path / "elsewhere" / "accounts.json"
    monkeypatch.setattr(app_config.settings, "auth_users", "")
    monkeypatch.setenv("AUTH_USERS", str(custom))
    user_accounts.reset_cache()
    user_accounts.add_user("dave", "dave-password-99")
    assert custom.is_file()
    assert "dave-password-99" not in custom.read_text(encoding="utf-8")
    assert user_accounts.list_usernames() == ["dave"]


def test_bootstrap_still_initializes_legacy_password_when_users_exist(tmp_path: Path) -> None:
    user_accounts.add_user("alice", "alice-password-1")
    (tmp_path / "empty.env").write_text("AUTH_PASSWORD=legacy-secret\n", encoding="utf-8")
    user_accounts.reset_cache()

    assert auth.bootstrap_from_env() is True
    assert auth.verify_and_create_session("legacy-secret") is not None
    assert auth.verify_and_create_session("alice-password-1", "alice") is not None
    assert auth.bootstrap_from_env() is False


def test_users_alone_mark_auth_configured() -> None:
    assert auth.is_configured() is False
    user_accounts.add_user("alice", "alice-password-1")
    auth._configured_cache = None
    assert auth.is_configured() is True


def test_login_api_sessions_logout_and_rate_limit() -> None:
    user_accounts.add_user("alice", "alice-password-1")
    user_accounts.add_user("bob", "bob-password-22")
    alice = _api()
    bob = _api()

    denied = alice.post("/api/auth/login", json={"username": "alice", "password": "nope"})
    assert denied.status_code == 401
    assert denied.json()["detail"] == "用户名或密码错误"

    ok = alice.post(
        "/api/auth/login",
        json={"username": "alice", "password": "alice-password-1"},
    )
    assert ok.status_code == 200
    assert ok.json()["username"] == "alice"
    bob_ok = bob.post(
        "/api/auth/login",
        json={"username": "bob", "password": "bob-password-22"},
    )
    assert bob_ok.status_code == 200

    assert alice.get("/api/auth/status").json()["authenticated"] is True
    assert bob.get("/api/auth/status").json()["username"] == "bob"

    assert alice.post("/api/auth/logout").status_code == 200
    assert alice.get("/api/auth/status").json()["authenticated"] is False
    assert bob.get("/api/auth/status").json()["authenticated"] is True

    changed = bob.post(
        "/api/auth/change-password",
        json={"old_password": "bob-password-22", "new_password": "bob-password-33"},
    )
    assert changed.status_code == 200
    assert bob.get("/api/auth/status").json()["authenticated"] is False
    again = bob.post(
        "/api/auth/login",
        json={"username": "bob", "password": "bob-password-33"},
    )
    assert again.status_code == 200

    auth_api._fail_counter.clear()
    for _ in range(auth_api._MAX_FAILS):
        failed = alice.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
        assert failed.status_code == 401
    locked = alice.post(
        "/api/auth/login",
        json={"username": "alice", "password": "alice-password-1"},
    )
    assert locked.status_code == 429


def test_legacy_login_api_accepts_password_only_body() -> None:
    auth.set_password("legacy-secret")
    client = _api()
    response = client.post("/api/auth/login", json={"password": "legacy-secret"})
    assert response.status_code == 200
    assert response.json()["username"] == "admin"
    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_rate_limit_locks_username_across_ips_until_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 1_700_000_000.0}

    def _now() -> float:
        return now["t"]

    monkeypatch.setattr(auth_api.time, "time", _now)
    for _ in range(auth_api._MAX_FAILS):
        auth_api._record_login_fail("1.2.3.4", "alice")

    with pytest.raises(HTTPException) as locked_user:
        auth_api._check_login_rate_limit("5.6.7.8", "alice")
    assert locked_user.value.status_code == 429

    with pytest.raises(HTTPException):
        auth_api._check_login_rate_limit("1.2.3.4", "bob")

    auth_api._check_login_rate_limit("5.6.7.8", "bob")

    now["t"] += auth_api._LOCK_SECONDS + 1
    auth_api._check_login_rate_limit("5.6.7.8", "alice")
    auth_api._check_login_rate_limit("1.2.3.4", "bob")


def test_manage_users_script_prints_password_once(tmp_path: Path) -> None:
    script = _REPO / "scripts" / "manage_users.py"
    env = os.environ.copy()
    env["DATA_DIR"] = str(tmp_path)
    env["TICKFLOW_ENV_FILE"] = str(tmp_path / "missing.env")
    env.pop("AUTH_USERS", None)
    env["AUTH_PASSWORD"] = ""

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=_REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    added = run("add", "alice")
    assert added.returncode == 0, added.stderr
    password = _password_line(added.stdout)
    assert added.stdout.count(password) == 1
    stored = (tmp_path / "users.json").read_text(encoding="utf-8")
    assert password not in stored
    assert "$argon2id$" in stored

    listed = run("list")
    assert listed.returncode == 0
    assert listed.stdout.strip() == "alice"

    reset = run("reset", "alice")
    assert reset.returncode == 0, reset.stderr
    new_password = _password_line(reset.stdout)
    assert new_password != password
    assert new_password not in (tmp_path / "users.json").read_text(encoding="utf-8")

    removed = run("remove", "alice")
    assert removed.returncode == 0
    assert run("list").stdout.strip() == "（无用户）"

    env["AUTH_USERS"] = "[]"
    refused = run("add", "bob")
    assert refused.returncode == 2
    assert "密码:" not in refused.stdout


def _password_line(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("密码: "):
            return line.removeprefix("密码: ").strip()
    raise AssertionError(stdout)
