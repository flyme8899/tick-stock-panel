"""DSA 上游会话：静态 cookie、缓存、401 单次重登、登录失败退避。"""
from __future__ import annotations

import threading
import time

import pytest

from app.custom.dsa.catalog import ENV_VARS
from app.custom.dsa.proxy import (
    _LOGIN_BACKOFF_BASE_SEC,
    _parse_session_set_cookie,
    _reset_session_cache,
    _session,
    forward,
)


class _Response:
    def __init__(self, status: int, body: bytes = b"{}", set_cookie: str = "") -> None:
        self.status_code = status
        self.content = body
        self.headers = {"content-type": "application/json"}
        self._set_cookie = [set_cookie] if set_cookie else []

    def get_list(self, name: str) -> list[str]:
        if name.lower() == "set-cookie":
            return list(self._set_cookie)
        return []


class _Headers(dict):
    def get_list(self, name: str) -> list[str]:
        if name.lower() != "set-cookie":
            return []
        raw = self.get("set-cookie", "")
        return [raw] if raw else []


def _response(status: int, body: bytes = b"{}", set_cookie: str = "") -> _Response:
    response = _Response(status, body, set_cookie)
    response.headers = _Headers(response.headers)
    if set_cookie:
        response.headers["set-cookie"] = set_cookie
    return response


class _Client:
    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def post(self, url: str, json: dict | None = None) -> _Response:
        CALLS.append(("POST", url, dict(json or {})))
        action = LOGIN_QUEUE.pop(0)
        if callable(action):
            return action()
        return action

    def request(self, method, url, params=None, content=None, headers=None) -> _Response:
        del params, content
        cookie = (headers or {}).get("cookie", "")
        CALLS.append((method, url, cookie))
        return DATA_QUEUE.pop(0)


CALLS: list = []
LOGIN_QUEUE: list = []
DATA_QUEUE: list = []


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    CALLS.clear()
    LOGIN_QUEUE.clear()
    DATA_QUEUE.clear()
    monkeypatch.delenv("DSA_PASSWORD", raising=False)
    monkeypatch.delenv("DSA_UPSTREAM_COOKIE", raising=False)
    monkeypatch.setenv("DSA_BASE_URL", "http://dsa.test")
    monkeypatch.setattr("app.custom.dsa.proxy.httpx.Client", _Client)
    _reset_session_cache()
    yield
    _reset_session_cache()


def _session_cookie(max_age: int = 3600) -> str:
    return (
        "dsa_session=nonce.1700000000.sig; HttpOnly; "
        f"Max-Age={max_age}; Path=/; SameSite=lax"
    )


def test_catalog_documents_password() -> None:
    item = next(row for row in ENV_VARS if row["name"] == "DSA_PASSWORD")
    assert item["required"] is False
    assert "Set-Cookie" in item["purpose"]


def test_no_password_uses_static_cookie_and_does_not_login() -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DSA_UPSTREAM_COOKIE", "dsa_session=static-token")
    DATA_QUEUE.append(_response(200, b'{"ok":true}'))
    try:
        status, payload, _media, extra = forward("GET", "history")
    finally:
        monkeypatch.undo()

    assert status == 200
    assert payload == b'{"ok":true}'
    assert CALLS == [("GET", "http://dsa.test/api/v1/history", "dsa_session=static-token")]
    assert "set-cookie" not in extra
    assert "static-token" not in extra.values()


def test_successful_login_is_cached_until_set_cookie_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    created = 1_700_000_000
    now = created + 100
    monkeypatch.setattr("app.custom.dsa.proxy.time.time", lambda: float(now))
    monkeypatch.setenv("DSA_PASSWORD", "test-password")
    monkeypatch.setenv("DSA_UPSTREAM_COOKIE", "dsa_session=static-token")
    LOGIN_QUEUE.append(_response(200, set_cookie=_session_cookie(3600)))
    DATA_QUEUE.extend([_response(200, b'{"ok":true}'), _response(200, b'{"ok":true}')])

    first = forward("GET", "usage/summary")
    second = forward("GET", "usage/summary")

    posts = [call for call in CALLS if call[0] == "POST"]
    gets = [call for call in CALLS if call[0] == "GET"]
    assert len(posts) == 1
    assert posts[0][2] == {"password": "test-password"}
    assert gets[0][2] == gets[1][2] == "dsa_session=nonce.1700000000.sig"
    assert first[0] == second[0] == 200
    assert b"test-password" not in first[1]
    assert "set-cookie" not in first[3]
    # 截止 = 签发 + Max-Age - 10 分钟，不是本地时钟 + 24 小时。
    assert _session["expire_at"] == created + 3600 - 600


def test_unauthorized_triggers_single_refresh_and_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.custom.dsa.proxy.time.time", lambda: 1_700_000_100.0)
    monkeypatch.setenv("DSA_PASSWORD", "test-password")
    _session["cookie"] = "dsa_session=stale.1699990000.old"
    _session["expire_at"] = 1_700_000_100.0 + 500
    LOGIN_QUEUE.append(_response(200, set_cookie=_session_cookie()))
    DATA_QUEUE.extend([
        _response(401, b'{"detail":"unauthorized"}'),
        _response(200, b'{"ok":true}'),
    ])

    status, payload, _media, extra = forward("GET", "history")

    posts = [call for call in CALLS if call[0] == "POST"]
    gets = [call for call in CALLS if call[0] == "GET"]
    assert status == 200
    assert payload == b'{"ok":true}'
    assert len(posts) == 1
    assert gets[0][2] == "dsa_session=stale.1699990000.old"
    assert gets[1][2] == "dsa_session=nonce.1700000000.sig"
    assert "set-cookie" not in extra
    assert b"test-password" not in payload


def test_repeated_login_failures_back_off(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr("app.custom.dsa.proxy.time.time", lambda: clock["now"])
    monkeypatch.setenv("DSA_PASSWORD", "test-password")
    monkeypatch.setenv("DSA_UPSTREAM_COOKIE", "dsa_session=static-token")
    LOGIN_QUEUE.extend([_response(401), _response(401)])
    DATA_QUEUE.extend([_response(200, b"{}") for _ in range(4)])

    for _ in range(3):
        status, _payload, _media, _extra = forward("GET", "history")
        assert status == 200

    assert [call for call in CALLS if call[0] == "POST"] == [
        ("POST", "http://dsa.test/api/v1/auth/login", {"password": "test-password"}),
    ]
    assert all(call[2] == "dsa_session=static-token" for call in CALLS if call[0] == "GET")

    clock["now"] += _LOGIN_BACKOFF_BASE_SEC + 1
    forward("GET", "history")
    assert len([call for call in CALLS if call[0] == "POST"]) == 2


def test_expiry_without_max_age_uses_expires_and_ignores_hard_coded_day() -> None:
    now = 1_700_000_000.0
    parsed = _parse_session_set_cookie(
        "dsa_session=nonce.1700000000.sig; Expires=Thu, 01 Jan 2026 00:00:00 GMT",
        now,
    )
    assert parsed is not None
    _cookie, deadline = parsed
    assert deadline == 1_767_225_600 - 600
    assert _parse_session_set_cookie("dsa_session=nonce.1700000000.sig", now) is None
    assert _parse_session_set_cookie(
        "dsa_session=bad\r\nX: y; Max-Age=3600",
        now,
    ) is None


def test_concurrent_expiry_performs_one_login(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSA_PASSWORD", "test-password")
    started = threading.Event()

    def slow_login() -> _Response:
        started.set()
        threading.Event().wait(0.05)
        created = int(time.time())
        return _response(
            200,
            set_cookie=f"dsa_session=nonce.{created}.sig; HttpOnly; Max-Age=3600; Path=/",
        )

    LOGIN_QUEUE.append(slow_login)
    DATA_QUEUE.extend([_response(200, b"{}") for _ in range(8)])
    errors: list[BaseException] = []

    def one() -> None:
        try:
            forward("GET", "history")
        except BaseException as exc:  # noqa: BLE001 - 收集线程里的失败
            errors.append(exc)

    threads = [threading.Thread(target=one) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert started.is_set()
    assert len([call for call in CALLS if call[0] == "POST"]) == 1
