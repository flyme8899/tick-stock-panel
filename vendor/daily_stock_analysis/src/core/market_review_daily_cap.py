"""定时复盘钉钉的「每自然日最多成功外发一条」持久化门闩。

为什么单独成模块
----------------
日 cap 必须**跨进程重启**仍然生效。进程内 dict / 既有 ``_dedup_expires_at``
都随进程消失，重启后同一天能再发一条，等于没有 cap。

替代方案「查 ``notification_runs`` 当日已成功条数」也能算，但依赖历史行完整性:
诊断写入失败时（``persist_market_review_notification_run`` 只 warning 不抛）
那一行可能根本不存在，cap 就静默失效。所以这里选 ALIGN N1 推荐的小 JSON。

路径与 ``market_review.lock`` 同族（``Path(database_path).parent``）:
容器里 DB 挂 ``/app/data``，cap 文件跟着落同一卷，重启换容器也还在。

口径
----
- 维度: **Asia/Shanghai 自然日**（DSA 容器 ``TZ=Asia/Shanghai``）。
- 上限: 成功外发 **≤1**。只有 ``success=True`` 且实际命中复盘 webhook 才计 1。
- 失败 / skipped **不写**额度 —— 否则一次网络抖动就把当天额度烧掉。
- 文件损坏 / 读失败 → 视为 0（fail-open 到"允许发一条"）并打 warning。
  **绝不**因为 JSON 坏了就回落旧 ``DINGTALK_WEBHOOK_URL``。

保留最近 ``_MAX_RETAINED_DAYS`` 天键，避免文件无限胀。

仅供研究，不构成投资建议；执行须人工确认。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

REVIEW_TZ_NAME = "Asia/Shanghai"
REVIEW_TZ = ZoneInfo(REVIEW_TZ_NAME)

CAP_FILE_NAME = "market_review_dingtalk_daily_cap.json"
CAP_SCHEMA_VERSION = "1"
DAILY_SUCCESS_CAP = 1
_MAX_RETAINED_DAYS = 14

TodayFn = Callable[[], date]


def review_daily_cap_path(database_path: Any) -> Path:
    """``Path(database_path).parent / market_review_dingtalk_daily_cap.json``.

    与 ``core.market_review_lock.market_review_lock_path`` 同族，落在 DB 同目录。
    """
    raw = str(database_path or "./data/stock_analysis.db")
    return Path(raw).resolve().parent / CAP_FILE_NAME


def review_today(today_fn: TodayFn | None = None) -> date:
    """今日（Asia/Shanghai）。``today_fn`` 供测试注入，不依赖真实时钟。"""
    if today_fn is not None:
        return today_fn()
    return datetime.now(REVIEW_TZ).date()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """临时文件 + os.replace，避免读侧读到半截 JSON。

    临时文件必须唯一（``NamedTemporaryFile`` 自带随机名）: 固定叫 ``.tmp`` 时
    多线程会往同一个临时文件里写，一个线程 replace 搬走后另一个紧接着 replace
    已不存在的源就抛 FileNotFoundError。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def load_review_cap_state(path: Path) -> dict[str, Any]:
    """读 cap 状态。缺文件 → 空状态；损坏 → warning + 空状态（不抛）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("复盘钉钉日 cap 文件读取失败，按 0 处理: path=%s err=%s", path, exc)
        return {}

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("复盘钉钉日 cap 文件损坏，按 0 处理 (不会回落旧 webhook): path=%s err=%s", path, exc)
        return {}
    if not isinstance(payload, dict):
        logger.warning("复盘钉钉日 cap 文件结构异常，按 0 处理: path=%s", path)
        return {}
    days = payload.get("days")
    if days is not None and not isinstance(days, dict):
        logger.warning("复盘钉钉日 cap 的 days 字段结构异常，按 0 处理: path=%s", path)
        return {}
    return payload


def review_success_count(state: dict[str, Any], day_key: str) -> int:
    days = state.get("days")
    if not isinstance(days, dict):
        return 0
    entry = days.get(day_key)
    if not isinstance(entry, dict):
        return 0
    try:
        return max(0, int(entry.get("success_count") or 0))
    except (TypeError, ValueError):
        return 0


def is_review_daily_cap_reached(
    database_path: Any,
    *,
    today_fn: TodayFn | None = None,
) -> bool:
    """今日是否已达上限。缺文件/损坏 → False（允许发一条）。"""
    day_key = review_today(today_fn).isoformat()
    state = load_review_cap_state(review_daily_cap_path(database_path))
    return review_success_count(state, day_key) >= DAILY_SUCCESS_CAP


def _prune_days(days: dict[str, Any]) -> dict[str, Any]:
    """只留最近 N 天键，避免文件无限胀。键是 ISO 日期，字典序即时间序。"""
    if len(days) <= _MAX_RETAINED_DAYS:
        return days
    keep = sorted(days.keys())[-_MAX_RETAINED_DAYS:]
    return {key: days[key] for key in keep if key in days}


def record_review_success(
    database_path: Any,
    *,
    today_fn: TodayFn | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """记一次成功外发。**只在本轮钉钉真的 success=True 后调用**。

    计数夹紧到上限语义（已达上限就不会再发，所以 normal path 是 0→1）。
    """
    today = review_today(today_fn)
    day_key = today.isoformat()
    path = review_daily_cap_path(database_path)
    state = load_review_cap_state(path)

    days = state.get("days")
    if not isinstance(days, dict):
        days = {}
    entry = days.get(day_key)
    if not isinstance(entry, dict):
        entry = {}
    previous = review_success_count({"days": {day_key: entry}}, day_key)
    entry["success_count"] = min(previous + 1, DAILY_SUCCESS_CAP)
    stamp = (now_fn or (lambda: datetime.now(REVIEW_TZ)))()
    entry["last_success_at"] = stamp.isoformat()
    days[day_key] = entry

    payload = {
        "schema_version": CAP_SCHEMA_VERSION,
        "tz": REVIEW_TZ_NAME,
        "days": _prune_days(days),
    }
    try:
        _atomic_write_json(path, payload)
    except OSError as exc:
        # 写不进去不该让"已经发出去了"这件事变成失败上报 —— 但必须留痕，
        # 因为下一次同日调用不会被拦住。
        logger.warning("复盘钉钉日 cap 写入失败 (今日额度可能失效): path=%s err=%s", path, exc)
    return payload
