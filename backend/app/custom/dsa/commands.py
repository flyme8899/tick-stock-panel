"""Slash commands shared with the DSA chat bots, executed through the HTTP API."""
from __future__ import annotations

import json
import re
from typing import Any

from app.custom.dsa.catalog import BOT_COMMANDS
from app.custom.dsa.proxy import InvalidUpstreamPathError, UpstreamError, forward

_ALIASES = {
    alias: item["name"]
    for item in BOT_COMMANDS
    for alias in (item["name"], *item["aliases"])
}
_CODE = re.compile(
    r"^(?:\d{4,6}|[A-Za-z]{1,8}|hk\d{4,5}|sh\d{6}|sz\d{6}|\d{4,6}\.(?:T|KS|KQ|TW))$",
    re.IGNORECASE,
)


class CommandError(Exception):
    pass


def parse_command(text: str) -> tuple[str, list[str]]:
    raw = text.strip()
    if not raw:
        raise CommandError("请输入命令")
    if not raw.startswith("/"):
        return "chat", [raw]
    body = raw[1:].strip()
    if not body:
        raise CommandError("请输入命令")
    parts = body.split()
    name = _ALIASES.get(parts[0], parts[0].lower())
    known = {item["name"] for item in BOT_COMMANDS}
    if name not in known:
        raise CommandError(f"未知命令 /{parts[0]}，输入 /help 查看")
    return name, parts[1:]


def help_text(topic: str | None = None) -> str:
    if topic:
        key = _ALIASES.get(topic, topic.lower())
        for item in BOT_COMMANDS:
            if item["name"] == key:
                alias = "、".join(item["aliases"])
                return f"{item['usage']}\n{item['summary']}\n别名：{alias}"
        raise CommandError(f"没有 /{topic} 的说明")
    lines = ["可用命令："]
    lines.extend(f"{item['usage']}  {item['summary']}" for item in BOT_COMMANDS)
    return "\n".join(lines)


def _loads(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload.decode("utf-8", errors="replace")


def _call(method: str, path: str, body: dict | None = None, params: list[tuple[str, str]] | None = None) -> Any:
    raw = json.dumps(body).encode("utf-8") if body is not None else None
    content_type = "application/json" if body is not None else None
    status, payload, _media, _extra = forward(
        method,
        path,
        params=params,
        body=raw,
        content_type=content_type,
    )
    data = _loads(payload)
    if status >= 400:
        raise CommandError(_error_text(data, status))
    return data


def _error_text(data: Any, status: int) -> str:
    if isinstance(data, dict):
        detail = data.get("detail", data)
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error")
            if message:
                return str(message)
        if isinstance(detail, str):
            return detail
        message = data.get("message") or data.get("error")
        if message:
            return str(message)
    if isinstance(data, str) and data.strip():
        return data.strip()[:500]
    return f"上游返回 HTTP {status}"


def _pretty(data: Any) -> str:
    if isinstance(data, str):
        return data[:4000]
    return json.dumps(data, ensure_ascii=False, indent=2)[:4000]


def _is_code(token: str) -> bool:
    return bool(_CODE.match(token))


def execute(text: str) -> dict[str, Any]:
    name, args = parse_command(text)
    if name == "help":
        return {"ok": True, "command": name, "title": "帮助", "text": help_text(args[0] if args else None)}
    try:
        title, body = _dispatch(name, args)
    except UpstreamError as exc:
        raise CommandError(str(exc)) from exc
    except InvalidUpstreamPathError as exc:
        raise CommandError(str(exc)) from exc
    return {"ok": True, "command": name, "title": title, "text": body}


def _dispatch(name: str, args: list[str]) -> tuple[str, str]:
    if name == "analyze":
        if not args:
            raise CommandError("用法：/analyze <代码>")
        report = "full" if any(item.lower() == "full" for item in args[1:]) else "detailed"
        data = _call(
            "POST",
            "analysis/analyze",
            {
                "stock_code": args[0],
                "report_type": report,
                "async_mode": True,
                "notify": False,
                "original_query": args[0],
                "selection_source": "manual",
            },
        )
        return "分析已提交", _pretty(data)
    if name == "market":
        body: dict = {"send_notification": False}
        if args:
            body["region"] = args[0]
        data = _call("POST", "analysis/market-review", body)
        return "大盘复盘", _pretty(data)
    if name == "batch":
        codes = args or _watchlist_codes()
        if not codes:
            raise CommandError("没有可分析的代码。传入代码，或先配置 STOCK_LIST")
        data = _call(
            "POST",
            "analysis/analyze",
            {"stock_codes": codes[:20], "async_mode": True, "notify": False, "report_type": "detailed"},
        )
        return "批量分析", _pretty(data)
    if name == "ask":
        if not args:
            raise CommandError("用法：/ask <代码> [技能]")
        skill = args[1] if len(args) > 1 else None
        payload: dict = {"message": f"请分析 {args[0]}"}
        if skill:
            payload["skills"] = [skill]
        data = _call("POST", "agent/chat", payload)
        return "问股", _chat_text(data)
    if name == "chat":
        if not args:
            raise CommandError("用法：/chat <问题>")
        data = _call("POST", "agent/chat", {"message": " ".join(args)})
        return "对话", _chat_text(data)
    if name == "research":
        if not args:
            raise CommandError("用法：/research <代码或主题> [问题]")
        stock = args[0] if _is_code(args[0]) else None
        question = " ".join(args[1:] if stock else args)
        data = _call("POST", "agent/research", {"question": question or args[0], "stock_code": stock})
        return "深度研究", _chat_text(data)
    if name == "strategies":
        data = _call("GET", "agent/skills")
        return "策略技能", _pretty(data)
    if name == "history":
        data = _call("GET", "agent/chat/sessions")
        return "会话", _pretty(data)
    if name == "status":
        health = _call("GET", "health")
        try:
            schedule = _call("GET", "system/scheduler/status")
        except CommandError as exc:
            schedule = str(exc)
        return "状态", _pretty({"health": health, "scheduler": schedule})
    raise CommandError(f"未知命令 /{name}")


def _chat_text(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("reply", "content", "message", "text"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return _pretty(data)


def _watchlist_codes() -> list[str]:
    data = _call("GET", "stocks/watchlist")
    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        raw = data.get("items") or data.get("stocks") or data.get("codes") or []
        items = raw if isinstance(raw, list) else []
    else:
        items = []
    codes: list[str] = []
    for item in items:
        if isinstance(item, str):
            codes.append(item)
        elif isinstance(item, dict):
            code = item.get("stock_code") or item.get("code") or item.get("symbol")
            if code:
                codes.append(str(code))
    return codes
