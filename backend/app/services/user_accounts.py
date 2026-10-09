"""多用户登录账号。

账号存在 data/users.json（可用 AUTH_USERS 改路径或内联 JSON）。
密码只接受 Argon2id / bcrypt 哈希，读到明文或未知格式时拒绝该账号。
所有账号权限相同，这里不记录角色。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import string
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# OWASP 推荐的 Argon2id 下限。登录校验大约几十毫秒。
_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)
_PASSWORD_MIN = 6
_PASSWORD_MAX = 128
_USERNAME_MAX = 32
_lock = threading.Lock()
_cache_key: object = None
_cache_users: dict[str, UserRecord] = {}
_dummy_hash: str | None = None
_dotenv_cache: tuple[str, int, str] | None = None


class UsersSourceError(RuntimeError):
    """AUTH_USERS 指向不可写的内联 JSON。"""


@dataclass(frozen=True)
class UserRecord:
    username: str
    password_hash: str
    created_at: int
    updated_at: int
    rev: int


def normalize_username(username: str) -> str:
    """大小写不敏感。返回用于存储和比较的用户名。"""
    name = (username or "").strip().casefold()
    if not name or len(name) > _USERNAME_MAX:
        raise ValueError("用户名须为 1–32 个字符")
    if any(ch.isspace() or ch in "/\\:" for ch in name):
        raise ValueError("用户名不能包含空格或路径分隔符")
    if not name[0].isalnum() or not all(ch.isalnum() or ch in "._-" for ch in name):
        raise ValueError("用户名只能包含字母、数字、点、下划线和连字符，且以字母或数字开头")
    return name


def generate_password() -> str:
    """生成一次性展示的强密码。不写入日志。"""
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(24))
        if (
            any(ch.islower() for ch in password)
            and any(ch.isupper() for ch in password)
            and any(ch.isdigit() for ch in password)
        ):
            return password


def hash_password(password: str) -> str:
    _check_password_length(password)
    return _HASHER.hash(password)


def is_password_hash(value: str) -> bool:
    return value.startswith("$argon2") or value.startswith(("$2a$", "$2b$", "$2y$"))


def verify_password_hash(password: str, encoded: str) -> bool:
    """只校验 Argon2id / bcrypt。未知格式（含明文）一律失败。"""
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    if encoded.startswith("$argon2"):
        try:
            return bool(_HASHER.verify(encoded, password))
        except (VerificationError, InvalidHashError):
            return False
    if encoded.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), encoded.encode("utf-8"))
        except (ValueError, TypeError):
            return False
    return False


def has_users() -> bool:
    return bool(load_users())


def get_user(username: str) -> UserRecord | None:
    try:
        canonical = normalize_username(username)
    except ValueError:
        return None
    return load_users().get(canonical)


def verify_user(username: str, password: str) -> UserRecord | None:
    """校验用户名和密码。用户不存在时仍做一次哈希比较，避免用耗时探测账号。"""
    try:
        canonical = normalize_username(username)
    except ValueError:
        verify_password_hash(password, _dummy())
        return None
    user = load_users().get(canonical)
    encoded = user.password_hash if user is not None else _dummy()
    if not verify_password_hash(password, encoded):
        return None
    return user


def list_usernames() -> list[str]:
    return sorted(load_users())


def add_user(username: str, password: str) -> UserRecord:
    canonical = normalize_username(username)
    password_hash = hash_password(password)
    with _lock:
        users = dict(_load_locked())
        if canonical in users:
            raise ValueError(f"用户已存在: {canonical}")
        now = int(time.time())
        record = UserRecord(
            username=canonical,
            password_hash=password_hash,
            created_at=now,
            updated_at=now,
            rev=1,
        )
        users[canonical] = record
        _save_locked(users)
        return record


def set_user_password(username: str, password: str) -> UserRecord:
    """重置密码并递增 rev，使该用户已签发的会话失效。"""
    canonical = normalize_username(username)
    password_hash = hash_password(password)
    with _lock:
        users = dict(_load_locked())
        current = users.get(canonical)
        if current is None:
            raise ValueError(f"用户不存在: {canonical}")
        record = UserRecord(
            username=canonical,
            password_hash=password_hash,
            created_at=current.created_at,
            updated_at=int(time.time()),
            rev=current.rev + 1,
        )
        users[canonical] = record
        _save_locked(users)
        return record


def remove_user(username: str) -> None:
    canonical = normalize_username(username)
    with _lock:
        users = dict(_load_locked())
        if canonical not in users:
            raise ValueError(f"用户不存在: {canonical}")
        del users[canonical]
        _save_locked(users)


def load_users() -> dict[str, UserRecord]:
    with _lock:
        return dict(_load_locked())


def users_file() -> Path:
    """当前账号文件路径。AUTH_USERS 为内联 JSON 时抛出 UsersSourceError。"""
    mode, locator = _resolve_source()
    if mode != "file":
        raise UsersSourceError(
            "AUTH_USERS 是内联 JSON，不能写入。请改为账号文件路径，或直接编辑环境变量中的哈希。"
        )
    return Path(locator)


def reset_cache() -> None:
    global _cache_key, _cache_users, _dotenv_cache
    with _lock:
        _cache_key = None
        _cache_users = {}
        _dotenv_cache = None


def _check_password_length(password: str) -> None:
    if not isinstance(password, str) or len(password) < _PASSWORD_MIN:
        raise ValueError(f"密码至少 {_PASSWORD_MIN} 位")
    if len(password) > _PASSWORD_MAX:
        raise ValueError(f"密码不能超过 {_PASSWORD_MAX} 位")


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _HASHER.hash("dummy-password-not-a-user")
    return _dummy_hash


def _default_users_path() -> Path:
    from app.config import settings

    return settings.data_dir / "users.json"


def _raw_auth_users() -> str:
    """优先读未插值的 .env，避免 Argon2 哈希里的 $ 被 Compose / dotenv 吃掉。"""
    from app.config import _ENV_FILE, settings

    file_val = _auth_users_from_dotenv(_ENV_FILE)
    if file_val:
        return file_val
    env_val = (os.environ.get("AUTH_USERS") or "").strip()
    if env_val:
        return env_val
    return (settings.auth_users or "").strip()


def _auth_users_from_dotenv(path: Path) -> str:
    global _dotenv_cache
    if not path.is_file():
        return ""
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = (str(path), stat.st_mtime_ns)
    if _dotenv_cache is not None and _dotenv_cache[0] == key[0] and _dotenv_cache[1] == key[1]:
        return _dotenv_cache[2]
    from dotenv import dotenv_values

    raw = dotenv_values(path, encoding="utf-8", interpolate=False).get("AUTH_USERS")
    value = raw.strip() if isinstance(raw, str) else ""
    _dotenv_cache = (key[0], key[1], value)
    return value


def _resolve_source() -> tuple[str, str]:
    raw = _raw_auth_users()
    if raw.startswith(("{", "[")):
        return ("inline", raw)
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            from app.config import _PROJECT_ROOT

            path = (_PROJECT_ROOT / path).resolve()
        return ("file", str(path))
    return ("file", str(_default_users_path()))


def _fingerprint() -> object:
    mode, locator = _resolve_source()
    if mode == "inline":
        return ("inline", locator)
    path = Path(locator)
    try:
        stat = path.stat()
    except OSError:
        return ("file", str(path), None)
    return ("file", str(path), stat.st_mtime_ns, stat.st_size)


def _load_locked() -> dict[str, UserRecord]:
    global _cache_key, _cache_users
    key = _fingerprint()
    if key == _cache_key:
        return _cache_users
    users = _read_source()
    _cache_key = key
    _cache_users = users
    return users


def _read_source() -> dict[str, UserRecord]:
    mode, locator = _resolve_source()
    if mode == "inline":
        try:
            document = json.loads(locator)
            return _users_from_doc(document)
        except json.JSONDecodeError as exc:
            logger.warning("AUTH_USERS 不是合法 JSON: %s", exc)
            return {}
        except ValueError as exc:
            logger.warning("AUTH_USERS 格式无效: %s", exc)
            return {}
    path = Path(locator)
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取用户文件失败: %s", exc)
        return {}
    try:
        return _users_from_doc(document)
    except ValueError as exc:
        logger.warning("用户文件格式无效: %s", exc)
        return {}


def _users_from_doc(document: object) -> dict[str, UserRecord]:
    entries = _entry_map(document)
    users: dict[str, UserRecord] = {}
    for username, item in entries.items():
        try:
            record = _record_from_item(username, item)
        except ValueError as exc:
            logger.warning("跳过无效账号 %s: %s", username, exc)
            continue
        if record.username in users:
            logger.warning("跳过重复账号: %s", record.username)
            continue
        users[record.username] = record
    return users


def _entry_map(document: object) -> dict[str, object]:
    if isinstance(document, list):
        mapped: dict[str, object] = {}
        for item in document:
            if isinstance(item, dict) and item.get("username"):
                mapped[str(item["username"])] = item
        return mapped
    if not isinstance(document, dict):
        raise ValueError("用户文件必须是 JSON 对象或数组")
    raw_users = document.get("users") if "users" in document else document
    if not isinstance(raw_users, dict):
        raise ValueError("users 字段必须是对象")
    return raw_users


def _record_from_item(username: str, item: object) -> UserRecord:
    canonical = normalize_username(str(username))
    if isinstance(item, str):
        password_hash = item
        created_at = 0
        updated_at = 0
        rev = 1
    elif isinstance(item, dict):
        password_hash = str(item.get("password_hash") or item.get("hash") or "")
        created_at = int(item.get("created_at") or 0)
        updated_at = int(item.get("updated_at") or created_at or 0)
        rev = int(item.get("rev") or 1)
    else:
        raise ValueError("账号记录格式无效")
    if not is_password_hash(password_hash):
        raise ValueError("密码必须是 Argon2id 或 bcrypt 哈希")
    return UserRecord(
        username=canonical,
        password_hash=password_hash,
        created_at=created_at,
        updated_at=updated_at,
        rev=rev,
    )


def _save_locked(users: dict[str, UserRecord]) -> None:
    global _cache_key, _cache_users
    mode, locator = _resolve_source()
    if mode != "file":
        raise UsersSourceError(
            "AUTH_USERS 是内联 JSON，不能写入。请改为账号文件路径，或直接编辑环境变量中的哈希。"
        )
    path = Path(locator)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "users": {
            name: {
                "password_hash": record.password_hash,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "rev": record.rev,
            }
            for name, record in sorted(users.items())
        },
    }
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )
    _cache_users = dict(users)
    _cache_key = _fingerprint()
