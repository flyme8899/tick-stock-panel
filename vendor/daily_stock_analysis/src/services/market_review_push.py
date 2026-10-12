"""Send a market review that was generated as context, and record the outcome.

Context generation keeps ``send_notification=False`` so stock-analysis preload
does not notify by itself. SCHEDULE_TIME then reuses that body. This module is
the follow-up: it always attempts delivery when notification is enabled, and
writes success, failure, or an explicit skip reason onto ``notification_runs``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.services.run_diagnostics import sanitize_diagnostic_text

logger = logging.getLogger(__name__)

MARKET_REVIEW_HISTORY_CODE = "MARKET"


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


def _dispatch_report(notifier: Any, content: str) -> MarketReviewPushOutcome:
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
                sender(content, email_send_to_all=True, route_type="report")
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


def notify_market_review_text(
    notifier: Any,
    content: str,
    *,
    history_query_id: str | None,
    notify: bool,
    db: Any = None,
) -> MarketReviewPushOutcome:
    """Send one market-review body and persist success, failure, or skip reason."""
    body = str(content or "").strip()
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
        outcome = _dispatch_report(notifier, body)
        if outcome.success:
            logger.info("复用本轮大盘上下文推送大盘复盘成功")
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
