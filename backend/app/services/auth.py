"""访问认证 — 旧版共享密码 + 多用户账号。

设计:
  - 旧版共享密码仍用 PBKDF2-HMAC-SHA256（auth.json）。AUTH_PASSWORD 只做一次性初始化。
  - 多用户账号在 data/users.json（或 AUTH_USERS），密码只存 Argon2id / bcrypt 哈希。
  - 会话按账号签发。token 内存 + auth.json 双存，登出只作废当前 token。
  - 所有已登录账号权限相同，会话里不区分角色。

安全要点:
  - 设密码接口必须限制本机/内网(见 auth router), 防黑客抢占域名抢先设密码。
  - 登录限流: 同一 IP 或同一用户名错 5 次锁 5 分钟(见 auth router)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets as _secrets
import threading
import time
from pathlib import Path

from app.services import user_accounts
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# PBKDF2 参数(NIST 推荐, 单次校验 ~100ms, 兼顾安全与响应)
_PBKDF2_ITER = 200_000
_SALT_LEN = 16
_TOKEN_BYTES = 32

# 会话有效期: 30 天(自托管, 长一点减少重登频率)
SESSION_TTL = 30 * 24 * 3600
# 旧版共享密码登录使用的用户名。若 users 文件里另有 admin 账号，则该名字只属于那个账号。
LEGACY_USERNAME = "admin"

_lock = threading.Lock()
# 内存中的有效会话: { token: {username, kind, expire, iat, rev} }。进程重启后从磁盘恢复。
_sessions: dict[str, dict] = {}

# 「是否已设密码」缓存: 每个 /api/ 请求都要判定, auth_middleware 原先每次 read_text
# 磁盘 (阻塞事件循环)。此处懒加载缓存, set_password 后失效重算 (仍返回最新真值)。
_configured_cache: bool | None = None


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "auth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("auth.json malformed: %s", e)
    return {}


def _save(data: dict) -> None:
    p = _path()
    atomic_write_text(
        p, json.dumps(data, indent=2, ensure_ascii=False), mode=0o600,
    )


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。salt 为 None 时生成新 salt。"""
    if salt is None:
        salt = os.urandom(_SALT_LEN)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return salt.hex(), dk.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """恒定时间比较, 防时序攻击。"""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return _secrets.compare_digest(actual, expected)


# ================================================================
# 密码管理
# ================================================================

def _legacy_configured() -> bool:
    """是否已写入旧版共享密码。热路径命中缓存则不读盘。"""
    global _configured_cache
    if _configured_cache is None:
        d = _load()
        _configured_cache = bool(d.get("password_hash"))
    return bool(_configured_cache)


def is_configured() -> bool:
    """已有旧版共享密码，或至少有一个多用户账号。"""
    if _legacy_configured():
        return True
    return user_accounts.has_users()


def set_password(password: str) -> None:
    """设置/修改旧版共享密码。只作废 legacy 会话，不影响多用户账号的会话。"""
    global _configured_cache
    if len(password) < 6:
        raise ValueError("密码至少 6 位")
    salt_hex, hash_hex = _hash_password(password)
    with _lock:
        _drop_kind_locked("legacy")
        d = _load()
        d["password_hash"] = hash_hex
        d["password_salt"] = salt_hex
        d["updated_at"] = int(time.time())
        d["sessions"] = _dump_sessions()
        _save(d)
    _configured_cache = None  # 失效缓存, 下次 _legacy_configured 重读最新真值
    logger.info("access password set")


def bootstrap_from_env() -> bool:
    """首次初始化: 若环境变量 AUTH_PASSWORD 已配置且尚未设过密码, 则用它设密码。

    公网服务器部署场景: 避免每次都要 SSH 端口转发才能设首个密码。
    明文密码只在内存/配置中, 经 set_password() 哈希后写入 auth.json (chmod 0600)。
    一旦设置成功, 后续重启不再覆盖 (用户改密码走 UI, 不受环境变量影响)。

    Returns:
        True 表示本次用环境变量初始化了密码; False 表示无需初始化。
    """
    from app.config import _ENV_FILE, settings

    pwd = (settings.auth_password or "").strip()
    # Compose 会对 env_file 中未加单引号的 $VAR 做插值。Docker 部署时同时
    # 只读挂载原始 .env,首次初始化密码直接按 dotenv 语义读取,避免特殊字符被截断。
    if _ENV_FILE.is_file():
        from dotenv import dotenv_values

        raw_pwd = dotenv_values(_ENV_FILE, encoding="utf-8", interpolate=False).get("AUTH_PASSWORD")
        if isinstance(raw_pwd, str) and raw_pwd.strip():
            pwd = raw_pwd.strip()
    if not pwd:
        return False
    if _legacy_configured():
        # 已设过共享密码, 不覆盖 (避免环境变量反复重置用户在 UI 改的密码)。
        # 已有多用户账号时仍允许补上这份旧版密码。
        return False
    try:
        set_password(pwd)
        logger.info("access password bootstrapped from AUTH_PASSWORD env (one-time)")
        return True
    except ValueError as e:
        # 密码不合规 (< 6 位), 记日志但不阻断启动
        logger.warning("AUTH_PASSWORD bootstrap skipped: %s", e)
        return False


def _verify_legacy(password: str) -> bool:
    d = _load()
    if not d.get("password_hash"):
        return False
    return _verify_password(password, d.get("password_salt", ""), d["password_hash"])


def authenticate(password: str, username: str | None = None) -> dict | None:
    """校验凭据，不签发会话。

    空用户名只匹配旧版共享密码。指定用户名时先查账号文件；
    仅当不存在同名账号且用户名是 admin 时，才回退到旧版密码。
    """
    name = (username or "").strip()
    if not name:
        if _verify_legacy(password):
            return {"username": LEGACY_USERNAME, "kind": "legacy", "rev": 0}
        # 只有多用户、没有旧版密码时，空用户名也走一次哈希，避免用响应时间判断用户名是否存在。
        if not _legacy_configured():
            user_accounts.verify_user("*", password)
        return None
    record = user_accounts.verify_user(name, password)
    if record is not None:
        return {"username": record.username, "kind": "user", "rev": record.rev}
    try:
        canonical = user_accounts.normalize_username(name)
    except ValueError:
        return None
    if (
        canonical == LEGACY_USERNAME
        and user_accounts.get_user(canonical) is None
        and _verify_legacy(password)
    ):
        return {"username": LEGACY_USERNAME, "kind": "legacy", "rev": 0}
    return None


def verify_and_create_session(password: str, username: str | None = None) -> str | None:
    """验证密码, 成功则创建该账号的会话并返回 token, 失败返回 None。"""
    identity = authenticate(password, username)
    if identity is None:
        return None
    return _issue_session(identity)


def _issue_session(identity: dict) -> str:
    token = _secrets.token_urlsafe(_TOKEN_BYTES)
    now = time.time()
    sess = {
        "username": identity["username"],
        "kind": identity["kind"],
        "expire": now + SESSION_TTL,
        "iat": now,
        "rev": int(identity.get("rev") or 0),
    }
    with _lock:
        _sessions[token] = sess
        _persist_sessions_locked()
    return token


def revoke_session(token: str) -> None:
    """注销会话(登出)。只删除这一个 token。"""
    with _lock:
        _sessions.pop(token, None)
        _persist_sessions_locked()


def revoke_user_sessions(username: str) -> None:
    """作废某个多用户账号的全部会话。"""
    try:
        canonical = user_accounts.normalize_username(username)
    except ValueError:
        return
    with _lock:
        dead = [
            token
            for token, sess in _sessions.items()
            if sess.get("kind") == "user" and sess.get("username") == canonical
        ]
        for token in dead:
            _sessions.pop(token, None)
        if dead:
            _persist_sessions_locked()


def session_info(token: str) -> dict | None:
    """有效会话的副本。无效 token 返回 None。"""
    if not token or not is_valid_session(token):
        return None
    with _lock:
        sess = _sessions.get(token)
        return dict(sess) if sess else None


def session_username(token: str) -> str | None:
    info = session_info(token)
    if info is None:
        return None
    return str(info.get("username") or "")


def is_valid_session(token: str) -> bool:
    """检查会话是否有效(存在、未过期、账号仍有效)。过期或已重置密码则清理。"""
    if not token:
        return False
    with _lock:
        sess = _sessions.get(token)
        if sess is None:
            return False
        if time.time() > float(sess["expire"]):
            _sessions.pop(token, None)
            _persist_sessions_locked()
            return False
        snapshot = dict(sess)
    if _session_authorized(snapshot):
        return True
    with _lock:
        current = _sessions.get(token)
        if current is not None and current.get("iat") == snapshot.get("iat"):
            _sessions.pop(token, None)
            _persist_sessions_locked()
    return False


def _session_authorized(sess: dict) -> bool:
    if sess.get("kind") == "user":
        user = user_accounts.get_user(str(sess.get("username") or ""))
        if user is None:
            return False
        return int(sess.get("rev") or 0) == user.rev
    return _legacy_configured()


def _drop_kind_locked(kind: str) -> None:
    for token, sess in list(_sessions.items()):
        if sess.get("kind") == kind:
            _sessions.pop(token, None)


def _dump_sessions() -> dict[str, dict]:
    return {token: dict(sess) for token, sess in _sessions.items()}


def _persist_sessions_locked() -> None:
    """把当前内存会话写回 auth.json(需持锁调用)。"""
    d = _load()
    d["sessions"] = _dump_sessions()
    _save(d)


def _coerce_session(raw: object) -> dict | None:
    """兼容旧格式 {token: expire_ts}。"""
    if isinstance(raw, (int, float)):
        return {
            "username": LEGACY_USERNAME,
            "kind": "legacy",
            "expire": float(raw),
            "iat": 0.0,
            "rev": 0,
        }
    if not isinstance(raw, dict):
        return None
    try:
        expire = float(raw["expire"])
    except (KeyError, TypeError, ValueError):
        return None
    kind = raw.get("kind")
    if kind not in ("user", "legacy"):
        kind = "legacy"
    username = str(raw.get("username") or (LEGACY_USERNAME if kind == "legacy" else ""))
    try:
        rev = int(raw.get("rev") or 0)
        iat = float(raw.get("iat") or 0)
    except (TypeError, ValueError):
        return None
    return {
        "username": username,
        "kind": kind,
        "expire": expire,
        "iat": iat,
        "rev": rev,
    }


def _restore_sessions() -> None:
    """启动时从 auth.json 恢复未过期会话(支持进程重启不丢登录态)。"""
    with _lock:
        d = _load()
        now = time.time()
        saved = d.get("sessions") or {}
        if not isinstance(saved, dict) or not saved:
            return
        migrated = False
        for token, raw in saved.items():
            if not isinstance(token, str):
                migrated = True
                continue
            if not isinstance(raw, dict):
                migrated = True
            sess = _coerce_session(raw)
            if sess and sess["expire"] > now:
                _sessions[token] = sess
            else:
                migrated = True
        if migrated:
            _persist_sessions_locked()


# 模块加载时恢复会话
try:
    _restore_sessions()
except Exception as e:  # noqa: BLE001
    logger.warning("restore sessions failed: %s", e)
