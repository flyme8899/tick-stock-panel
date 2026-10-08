"""A-share daily schedule written through DSA's own runtime scheduler.

The sidecar process owns the clock, the trading-day filter and the notification
channels. This module only checks the fields the decision page is allowed to
change, then persists them with DSA's system-config API.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.custom.dsa.proxy import UpstreamError, forward

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_LIST_SPLIT_RE = re.compile(r"[,，;；\s]+")
_REGION_TOKENS = frozenset({"cn", "hk", "us", "jp", "kr", "both"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class ScheduleSettingsError(Exception):
    """The submitted schedule cannot be stored. The message is safe to show."""


def normalize_watchlist(raw: str) -> str:
    codes = [part.strip() for part in _LIST_SPLIT_RE.split((raw or "").strip()) if part.strip()]
    if not codes:
        raise ScheduleSettingsError("自选列表不能为空。定时任务只分析这里的代码。")
    if len(codes) > 200:
        raise ScheduleSettingsError("自选列表最多 200 只。")
    invalid = next((code for code in codes if _CODE_RE.fullmatch(code) is None), None)
    if invalid is not None:
        raise ScheduleSettingsError(f"代码格式无效：{invalid}")
    unique: list[str] = []
    for code in codes:
        if code not in unique:
            unique.append(code)
    return ",".join(unique)


def normalize_region(raw: str) -> str:
    text = (raw or "cn").strip().lower() or "cn"
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        raise ScheduleSettingsError("请选择复盘市场。自托管部署默认只跑 A 股。")
    unknown = next((part for part in parts if part not in _REGION_TOKENS), None)
    if unknown is not None:
        raise ScheduleSettingsError("复盘市场只接受 cn、hk、us、jp、kr，或 both。")
    if "both" in parts and len(parts) > 1:
        raise ScheduleSettingsError("both 不能和其他市场写在一起。")
    ordered: list[str] = []
    for part in parts:
        if part not in ordered:
            ordered.append(part)
    return ",".join(ordered)


def normalize_clock(raw: str) -> str:
    text = (raw or "").strip()
    if _TIME_RE.fullmatch(text) is None:
        raise ScheduleSettingsError("时间要用 24 小时制 HH:MM，例如 18:00。时区是 Asia/Shanghai。")
    return text


def schedule_config_items(
    *,
    enabled: bool,
    time: str,
    trading_days_only: bool,
    region: str,
    watchlist: str,
) -> list[dict[str, str]]:
    """Env keys DSA reloads into its in-process scheduler."""
    clock = normalize_clock(time)
    return [
        {"key": "SCHEDULE_ENABLED", "value": _flag(enabled)},
        {"key": "SCHEDULE_TIME", "value": clock},
        {"key": "SCHEDULE_TIMES", "value": clock},
        {"key": "SCHEDULE_RUN_IMMEDIATELY", "value": "false"},
        {"key": "TRADING_DAY_CHECK_ENABLED", "value": _flag(trading_days_only)},
        {"key": "MARKET_REVIEW_ENABLED", "value": "true"},
        {"key": "MARKET_REVIEW_REGION", "value": normalize_region(region)},
        {"key": "STOCK_LIST", "value": normalize_watchlist(watchlist)},
    ]


def read_schedule_view(config: dict[str, Any], scheduler: dict[str, Any]) -> dict[str, Any]:
    items = _item_map(config.get("items"))
    extra = [part.strip() for part in items.get("SCHEDULE_TIMES", "").split(",") if part.strip()]
    return {
        "enabled": _as_bool(items.get("SCHEDULE_ENABLED", ""), default=False),
        "time": items.get("SCHEDULE_TIME", "").strip() or "18:00",
        "trading_days_only": _as_bool(items.get("TRADING_DAY_CHECK_ENABLED", ""), default=True),
        "region": items.get("MARKET_REVIEW_REGION", "").strip() or "cn",
        "watchlist": items.get("STOCK_LIST", "").strip(),
        "timezone": "Asia/Shanghai",
        "extra_times": extra,
        "scheduler": {
            "enabled": bool(scheduler.get("enabled")),
            "running": bool(scheduler.get("running")),
            "schedule_times": list(scheduler.get("schedule_times") or []),
            "next_run_at": scheduler.get("next_run_at"),
            "last_run_at": scheduler.get("last_run_at"),
            "last_success_at": scheduler.get("last_success_at"),
            "last_error": scheduler.get("last_error"),
            "last_skip_reason": scheduler.get("last_skip_reason"),
        },
    }


def load_schedule() -> dict[str, Any]:
    config = _json_call("GET", "system/config", params=[("include_schema", "false")])
    scheduler = _json_call("GET", "system/scheduler/status")
    return read_schedule_view(config, scheduler)


def save_schedule(
    *,
    enabled: bool,
    time: str,
    trading_days_only: bool,
    region: str,
    watchlist: str,
) -> dict[str, Any]:
    items = schedule_config_items(
        enabled=enabled,
        time=time,
        trading_days_only=trading_days_only,
        region=region,
        watchlist=watchlist,
    )
    current = _json_call("GET", "system/config", params=[("include_schema", "false")])
    version = current.get("config_version")
    if not isinstance(version, str) or not version:
        raise UpstreamError("决策服务没有返回配置版本，无法保存定时设置")
    body = json.dumps(
        {
            "config_version": version,
            "mask_token": "******",
            "reload_now": True,
            "items": items,
        },
        ensure_ascii=False,
    ).encode()
    saved = _json_call("PUT", "system/config", body=body, content_type="application/json")
    view = load_schedule()
    view["saved"] = True
    warnings = saved.get("warnings")
    view["warnings"] = warnings if isinstance(warnings, list) else []
    return view


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _as_bool(value: str, *, default: bool) -> bool:
    text = value.strip().lower()
    if not text:
        return default
    return text in _TRUE_VALUES


def _item_map(items: Any) -> dict[str, str]:
    if not isinstance(items, list):
        return {}
    mapped: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        raw = item.get("value")
        mapped[item["key"]] = "" if raw is None else str(raw)
    return mapped


def _json_call(
    method: str,
    path: str,
    *,
    params: list[tuple[str, str]] | None = None,
    body: bytes | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    status, payload, _media, _extra = forward(
        method,
        path,
        params=params,
        body=body,
        content_type=content_type,
    )
    data: Any = {}
    if payload:
        try:
            data = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            data = {}
    if status >= 400:
        code = status if 400 <= status < 500 else 502
        raise UpstreamError(_upstream_message(data, status), status_code=code)
    if not isinstance(data, dict):
        raise UpstreamError("决策服务返回了无法识别的配置")
    return data


def _upstream_message(data: Any, status: int) -> str:
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, dict):
        if detail.get("error") == "config_version_conflict":
            return "配置刚被改过，请重新打开定时推送后再保存。"
        issues = detail.get("issues")
        if isinstance(issues, list) and issues:
            first = issues[0]
            if isinstance(first, dict):
                key = str(first.get("key") or "配置")
                message = str(first.get("message") or "校验失败")
                return f"{key}：{message}"
        message = detail.get("message")
        if isinstance(message, str) and message.strip():
            return message
    if isinstance(detail, str) and detail.strip():
        return detail
    return f"保存定时设置失败（HTTP {status}）"
