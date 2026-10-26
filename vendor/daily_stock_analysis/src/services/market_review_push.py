"""Send a market review that was generated as context, and record the outcome.

Context generation keeps ``send_notification=False`` so stock-analysis preload
does not notify by itself. SCHEDULE_TIME then reuses that body. This module is
the follow-up: it always attempts delivery when notification is enabled, and
writes success, failure, or an explicit skip reason onto ``notification_runs``.

定时复盘钉钉独立通道（批次 20261026gdsading · P-ding）
-----------------------------------------------------
本模块是 DSA 定时复盘 / 日摘要的**唯一卡口**。三个硬性口径：

1. **只用** ``DSA_REVIEW_DINGTALK_WEBHOOK_URL``（+可选 ``DSA_REVIEW_DINGTALK_SECRET``）。
   缺失 / 空白 → ``MarketReviewPushOutcome(status="skipped", reason="review_webhook_not_configured",
   attempts=0)``，**不调用** notifier 的钉钉外发。
2. **禁止回落**旧 ``DINGTALK_WEBHOOK_URL`` —— 那是资讯 / BSS / 狙击共群。旧 URL
   非空也不能顶替；否则复盘会悄悄挤进买卖信号那条流，注意力竞争没人发现。
3. **日 cap = 1**（Asia/Shanghai 自然日），持久化在
   ``core.market_review_daily_cap`` 的小 JSON，跨进程重启仍生效。SCHEDULE 复用、
   merge 合并摘要、「立即跑一轮」凡经本卡口成功外发者**共用**同一额度。

注入方式: 给 ``send_with_results`` 传 ``dingtalk_webhook_override`` / ``dingtalk_secret_override``，
作用域限定在**本次调用**。刻意不改 ``DingtalkSender.__init__`` 的默认读键 ——
notifier 是单例，构造期改 webhook 会把复盘键泄给资讯 / 狙击。

复盘 URL 与旧 URL **字符串相同**时：允许发送 + warning（同群同机器人是用户显式选择，
但不代表注意力竞争已解除）。

不启用 / 不依赖 ``NOTIFICATION_DAILY_DIGEST_ENABLED``（上游 reserved，开了只 warning、
不发摘要）；本拍自管 cap，避免假绿。

仅供研究，不构成投资建议；执行须人工确认。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from src.core.market_review_daily_cap import (
    is_review_daily_cap_reached,
    record_review_success,
    review_daily_cap_path,
)
from src.services.run_diagnostics import sanitize_diagnostic_text

logger = logging.getLogger(__name__)

MARKET_REVIEW_HISTORY_CODE = "MARKET"

#: 缺复盘专用 webhook 时的拒发原因（可测、可诊断）
REVIEW_WEBHOOK_NOT_CONFIGURED = "review_webhook_not_configured"
#: 当日已成功外发过一条
REVIEW_DAILY_CAP_REACHED = "daily_cap_reached"


@dataclass(frozen=True)
class MarketReviewPushOutcome:
    status: str
    success: bool
    reason: str
    attempts: int = 1
    error_message: str | None = None

    def as_run(self, *, channel: str = "report") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel": channel,
            "status": self.status,
            "success": self.success,
            "attempts": self.attempts,
            "reason": self.reason,
            "created_at": datetime.now().isoformat(),
        }
        detail = self.error_message if self.error_message is not None else (
            None if self.success else self.reason
        )
        sanitized = sanitize_diagnostic_text(detail)
        if sanitized:
            payload["error_message_sanitized"] = sanitized
        return payload


def _channel_failure_detail(result: Any) -> str:
    parts = [str(getattr(result, "status", "") or "").strip()]
    message = getattr(result, "message", None)
    if message:
        parts.append(str(message))
    for item in getattr(result, "channel_results", None) or []:
        if getattr(item, "success", True):
            continue
        channel = getattr(item, "channel", None) or "unknown"
        error_code = getattr(item, "error_code", None) or "failed"
        parts.append(f"{channel}:{error_code}")
    return "; ".join(part for part in parts if part)


def _classify_dispatch(result: Any) -> MarketReviewPushOutcome:
    status_name = str(getattr(result, "status", "") or "")
    detail = _channel_failure_detail(result)
    if status_name == "noise_suppressed":
        return MarketReviewPushOutcome(
            status="skipped",
            success=False,
            reason="noise_suppressed",
            attempts=0,
            error_message=detail or "noise_suppressed",
        )
    if status_name == "no_channel" or (
        not getattr(result, "dispatched", True) and not getattr(result, "success", False)
    ):
        return MarketReviewPushOutcome(
            status="skipped",
            success=False,
            reason="not_configured",
            attempts=0,
            error_message=detail or "not_configured",
        )
    if getattr(result, "success", False):
        return MarketReviewPushOutcome(
            status="success",
            success=True,
            reason="sent",
            attempts=1,
        )
    return MarketReviewPushOutcome(
        status="failed",
        success=False,
        reason="send_failed",
        attempts=1,
        error_message=detail or "send_failed",
    )


def _dispatch_report(
    notifier: Any,
    content: str,
    *,
    review_webhook: str | None = None,
    review_secret: str | None = None,
) -> MarketReviewPushOutcome:
    if notifier is None or not callable(getattr(notifier, "is_available", None)):
        return MarketReviewPushOutcome(
            status="skipped",
            success=False,
            reason="not_configured",
            attempts=0,
            error_message="not_configured",
        )
    if not notifier.is_available():
        return MarketReviewPushOutcome(
            status="skipped",
            success=False,
            reason="not_configured",
            attempts=0,
            error_message="not_configured",
        )

    try:
        sender = getattr(notifier, "send_with_results", None)
        if callable(sender):
            return _classify_dispatch(
                sender(
                    content,
                    email_send_to_all=True,
                    route_type="report",
                    dingtalk_webhook_override=review_webhook,
                    dingtalk_secret_override=review_secret,
                )
            )
        send = getattr(notifier, "send", None)
        if not callable(send):
            return MarketReviewPushOutcome(
                status="skipped",
                success=False,
                reason="not_configured",
                attempts=0,
                error_message="not_configured",
            )
        if send(content, email_send_to_all=True, route_type="report"):
            return MarketReviewPushOutcome(
                status="success",
                success=True,
                reason="sent",
                attempts=1,
            )
        return MarketReviewPushOutcome(
            status="failed",
            success=False,
            reason="send_failed",
            attempts=1,
            error_message="send_failed",
        )
    except TypeError:
        # 老式 notifier / 测试替身不接受 override 关键字: 只有当本轮**没有** override
        # 时才退回无参调用。有 override 却不支持 → 宁可失败，也不能悄悄改用旧 webhook。
        if review_webhook is not None:
            logger.warning("复盘钉钉 webhook 注入失败: 发送器不支持 override 参数")
            return MarketReviewPushOutcome(
                status="failed",
                success=False,
                reason="review_webhook_override_unsupported",
                attempts=0,
                error_message="review_webhook_override_unsupported",
            )
        raise
    except Exception as exc:  # noqa: BLE001 - delivery must not abort the saved review
        logger.warning("复用大盘复盘推送异常: %s", sanitize_diagnostic_text(exc))
        return MarketReviewPushOutcome(
            status="failed",
            success=False,
            reason="send_failed",
            attempts=1,
            error_message=str(exc),
        )


def persist_market_review_notification_run(
    *,
    query_id: str | None,
    outcome: MarketReviewPushOutcome,
    db: Any = None,
    channel: str = "report",
) -> dict[str, Any]:
    """Append one notification_runs entry onto the market-review history row."""
    payload = outcome.as_run(channel=channel)
    if not query_id:
        return payload

    try:
        database = db
        if database is None:
            from src.storage import DatabaseManager

            database = DatabaseManager.get_instance()
        updated = database.update_analysis_history_diagnostics(
            query_id=query_id,
            code=MARKET_REVIEW_HISTORY_CODE,
            notification_runs=[payload],
        )
    except Exception as exc:  # noqa: BLE001 - a missed diagnostic row must not hide a sent review
        logger.warning(
            "大盘复盘通知结果写入失败: query_id=%s reason=%s err=%s",
            query_id,
            outcome.reason,
            exc,
        )
        return payload

    if not updated:
        logger.warning(
            "大盘复盘通知结果未落到历史记录: query_id=%s status=%s reason=%s",
            query_id,
            outcome.status,
            outcome.reason,
        )
    return payload


def resolve_review_dingtalk_settings(
    config: Any = None,
) -> tuple[str, str | None]:
    """读复盘专用 webhook / secret。返回 (webhook, secret)，webhook 已 strip。

    ``config`` 为空时取全局配置；取不到配置就当未配置（fail-closed），
    **不**回落到 ``config.dingtalk_webhook_url``。
    """
    if config is None:
        try:
            from src.config import get_config

            config = get_config()
        except Exception:  # noqa: BLE001 - 配置不可用时等同于未配置
            return "", None

    webhook = str(getattr(config, "dsa_review_dingtalk_webhook_url", "") or "").strip()
    secret = getattr(config, "dsa_review_dingtalk_secret", None)
    secret_text = str(secret).strip() if secret is not None else ""
    return webhook, (secret_text or None)


def _review_config_flag(config: Any, name: str) -> Any:
    return getattr(config, name, None) if config is not None else None


def _is_same_as_shared_webhook(config: Any, review_webhook: str) -> bool:
    """复盘 URL 与旧 ``DINGTALK_WEBHOOK_URL`` 是否同一串。"""
    shared = str(getattr(config, "dingtalk_webhook_url", "") or "").strip()
    return bool(shared) and shared == review_webhook


def notify_market_review_text(
    notifier: Any,
    content: str,
    *,
    history_query_id: str | None,
    notify: bool,
    db: Any = None,
    config: Any = None,
    today_fn: Callable[[], date] | None = None,
) -> MarketReviewPushOutcome:
    """Send one market-review body and persist success, failure, or skip reason.

    钉钉外发只走复盘专用 webhook（见模块 docstring 三个口径）。日 cap 只在
    ``success=True`` 后计额度；``notify=False`` / 正文为空 / 缺配置 / 超额
    都不计。
    """
    body = str(content or "").strip()
    review_webhook = ""
    if not notify:
        outcome = MarketReviewPushOutcome(
            status="skipped",
            success=False,
            reason="no_notify",
            attempts=0,
            error_message="no_notify",
        )
    elif not body:
        outcome = MarketReviewPushOutcome(
            status="skipped",
            success=False,
            reason="empty_report",
            attempts=0,
            error_message="empty_report",
        )
    else:
        review_webhook, review_secret = resolve_review_dingtalk_settings(config)
        cap_db_path = _review_config_flag(config, "database_path")
        if not review_webhook:
            # 硬门槛: 缺复盘 webhook 就拒发，绝不落到资讯 / 买卖 / 狙击共群。
            outcome = MarketReviewPushOutcome(
                status="skipped",
                success=False,
                reason=REVIEW_WEBHOOK_NOT_CONFIGURED,
                attempts=0,
                error_message=REVIEW_WEBHOOK_NOT_CONFIGURED,
            )
            logger.warning(
                "定时复盘钉钉未配置 DSA_REVIEW_DINGTALK_WEBHOOK_URL，已跳过外发"
                "（不会发到资讯/买卖共群）。cap_file=%s",
                review_daily_cap_path(cap_db_path),
            )
        elif is_review_daily_cap_reached(cap_db_path, today_fn=today_fn):
            outcome = MarketReviewPushOutcome(
                status="skipped",
                success=False,
                reason=REVIEW_DAILY_CAP_REACHED,
                attempts=0,
                error_message=REVIEW_DAILY_CAP_REACHED,
            )
            logger.info(
                "定时复盘钉钉今日已成功外发过一条，本次跳过（daily_cap_reached）: cap_file=%s",
                review_daily_cap_path(cap_db_path),
            )
        else:
            if _is_same_as_shared_webhook(config, review_webhook):
                logger.warning(
                    "DSA_REVIEW_DINGTALK_WEBHOOK_URL 与 DINGTALK_WEBHOOK_URL 相同"
                    "（资讯/BSS 共 webhook，注意力竞争未解除）；按用户显式选择继续发送。"
                )
            outcome = _dispatch_report(
                notifier,
                body,
                review_webhook=review_webhook,
                review_secret=review_secret,
            )
            if outcome.success:
                record_review_success(cap_db_path, today_fn=today_fn)
                logger.info("复用本轮大盘上下文推送大盘复盘成功（复盘独立 webhook）")
            elif outcome.status == "skipped":
                logger.info("复用本轮大盘上下文未推送: reason=%s", outcome.reason)
            else:
                logger.warning(
                    "复用本轮大盘上下文推送大盘复盘失败: reason=%s detail=%s",
                    outcome.reason,
                    sanitize_diagnostic_text(outcome.error_message),
                )

    persist_market_review_notification_run(
        query_id=history_query_id,
        outcome=outcome,
        db=db,
    )
    return outcome
