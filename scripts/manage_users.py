#!/usr/bin/env python3
"""管理面板登录账号。

账号写入 data/users.json（或 AUTH_USERS 指向的文件）。密码只存 Argon2id 哈希。
add / reset 会生成强密码，并且只在终端打印一次。

    cd backend && uv run python ../scripts/manage_users.py add alice
    cd backend && uv run python ../scripts/manage_users.py reset alice
    cd backend && uv run python ../scripts/manage_users.py remove alice
    cd backend && uv run python ../scripts/manage_users.py list
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "backend"))

from app.services.user_accounts import (  # noqa: E402
    UsersSourceError,
    add_user,
    generate_password,
    list_usernames,
    remove_user,
    set_user_password,
)


def _print_password(username: str, password: str) -> None:
    print(f"用户: {username}")
    print(f"密码: {password}")
    print("请立即保存。密码只显示这一次，文件里只写入哈希。", file=sys.stderr)


def _cmd_add(username: str) -> int:
    password = generate_password()
    try:
        record = add_user(username, password)
    except UsersSourceError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_password(record.username, password)
    return 0


def _cmd_reset(username: str) -> int:
    password = generate_password()
    try:
        record = set_user_password(username, password)
    except UsersSourceError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_password(record.username, password)
    return 0


def _cmd_remove(username: str) -> int:
    try:
        remove_user(username)
    except UsersSourceError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"已删除用户: {username.strip().casefold()}")
    return 0


def _cmd_list() -> int:
    names = list_usernames()
    if not names:
        print("（无用户）")
        return 0
    for name in names:
        print(name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理 TSP 面板登录账号")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="新增用户并打印一次生成的密码")
    add.add_argument("username")

    reset = sub.add_parser("reset", help="重置密码并打印一次生成的密码")
    reset.add_argument("username")

    remove = sub.add_parser("remove", help="删除用户")
    remove.add_argument("username")

    sub.add_parser("list", help="列出用户名")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "add":
        return _cmd_add(args.username)
    if args.cmd == "reset":
        return _cmd_reset(args.username)
    if args.cmd == "remove":
        return _cmd_remove(args.username)
    if args.cmd == "list":
        return _cmd_list()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
