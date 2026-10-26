"""SCHEDULE_TIME 复盘：上下文生成不发通知，复用路径必须推钉钉并记下原因。

批次 20261026gdsading 起：复用路径的钉钉外发**只**走复盘专用 webhook
（``DSA_REVIEW_DINGTALK_WEBHOOK_URL``），缺则拒发 ``review_webhook_not_configured``；
每自然日成功外发 ≤1。所以下面的用例都显式给 ``_review_config(...)``，
不再依赖"notifier 可用就发"。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_VENDOR = _ROOT / "vendor" / "daily_stock_analysis"
sys.path.insert(0, str(_VENDOR))

from src.services.daily_market_context import (  # noqa: E402
    DEFERRED_MARKET_REVIEW_PUSH_REASON,
    DailyMarketContextService,
    market_context_history_query_id,
)
from src.services.market_review_push import notify_market_review_text  # noqa: E402

_REVIEW_URL = "https://example.invalid/review"
_OLD_URL = "https://example.invalid/old"


def _review_config(tmp_path, *, review_url=_REVIEW_URL, old_url=_OLD_URL, secret=None, **extra):
    """最小 config 替身：复盘键 + 旧键 + cap 落点（DB 同目录）。"""
    return SimpleNamespace(
        dsa_review_dingtalk_webhook_url=review_url,
        dsa_review_dingtalk_secret=secret,
        dingtalk_webhook_url=old_url,
        dingtalk_secret="old-secret",
        database_path=str(tmp_path / "data" / "stock_analysis.db"),
        **extra,
    )


class _HistoryDB:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def update_analysis_history_diagnostics(self, **kwargs):
        self.calls.append(kwargs)
        return 1


class _DingTalkNotifier:
    """Stand-in for NotificationService: report dispatch goes through DingTalk."""

    def __init__(self, *, available: bool = True, success: bool = True, status: str = "sent") -> None:
        self.available = available
        self.success = success
        self.status = status
        self.dingtalk_payloads: list[str] = []
        self.kwargs: list[dict] = []
        # 复盘卡口通过 override 注入 webhook；这里记下来，断言"用的是哪一条 URL"。
        self.dingtalk_webhook_overrides: list[str | None] = []

    def is_available(self) -> bool:
        return self.available

    def send_to_dingtalk(self, content: str) -> bool:
        self.dingtalk_payloads.append(content)
        return self.success

    def send_with_results(self, content: str, **kwargs):
        self.kwargs.append(kwargs)
        self.dingtalk_webhook_overrides.append(kwargs.get("dingtalk_webhook_override"))
        ok = self.send_to_dingtalk(content) if self.available else False
        return SimpleNamespace(
            dispatched=self.available,
            success=ok and self.success,
            status=self.status if self.available else "no_channel",
            message=None if ok else "dingtalk err",
            channel_results=[],
        )


def _generate_context(monkeypatch: pytest.MonkeyPatch) -> tuple[object, dict]:
    captured: dict = {}

    def fake_run_market_review(**kwargs):
        captured.update(kwargs)
        return "今日 A 股复盘正文"

    monkeypatch.setattr(
        "src.services.daily_market_context.run_market_review",
        fake_run_market_review,
    )
    monkeypatch.setattr(
        "src.services.daily_market_context.try_acquire_market_review_lock",
        lambda config: "lock",
    )
    monkeypatch.setattr(
        "src.services.daily_market_context.release_market_review_lock",
        lambda token: None,
    )
    service = DailyMarketContextService(db_manager=object(), today_fn=lambda: date(2026, 10, 12))
    context = service._run_market_review_context(
        region="cn",
        target_date=date(2026, 10, 12),
        config=SimpleNamespace(report_language="zh"),
        notifier=_DingTalkNotifier(),
        current_query_id="schedule-run",
    )
    return context, captured


def test_context_generation_defers_push_with_explicit_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    context, captured = _generate_context(monkeypatch)

    assert context is not None
    assert captured["send_notification"] is False
    assert captured["notification_skip_reason"] == DEFERRED_MARKET_REVIEW_PUSH_REASON
    assert captured["query_id"] == market_context_history_query_id("schedule-run", "cn")
    assert "今日 A 股复盘正文" in (context.full_report or "")


def test_reuse_path_sends_dingtalk_and_records_success(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    context, captured = _generate_context(monkeypatch)
    notifier = _DingTalkNotifier()
    db = _HistoryDB()
    history_query_id = captured["query_id"]

    outcome = notify_market_review_text(
        notifier,
        f"# 📈 大盘复盘\n\n{context.full_report}",
        history_query_id=history_query_id,
        notify=True,
        db=db,
        config=_review_config(tmp_path),
    )

    assert notifier.dingtalk_payloads
    assert "今日 A 股复盘正文" in notifier.dingtalk_payloads[0]
    assert notifier.kwargs[0]["route_type"] == "report"
    # 关键: 这次外发用的是复盘专用 webhook，不是旧共群
    assert notifier.dingtalk_webhook_overrides[0] == _REVIEW_URL
    assert outcome.status == "success"
    assert outcome.reason == "sent"
    assert db.calls[0]["query_id"] == "market_context_schedule-run_cn"
    assert db.calls[0]["code"] == "MARKET"
    run = db.calls[0]["notification_runs"][0]
    assert run["status"] == "success"
    assert run["success"] is True
    assert run["reason"] == "sent"


def test_reuse_path_records_skip_reason_without_sending() -> None:
    notifier = _DingTalkNotifier()
    db = _HistoryDB()

    outcome = notify_market_review_text(
        notifier,
        "# 📈 大盘复盘\n\n正文",
        history_query_id="market_context_schedule-run_cn",
        notify=False,
        db=db,
    )

    assert notifier.dingtalk_payloads == []
    assert outcome.status == "skipped"
    assert outcome.reason == "no_notify"
    run = db.calls[0]["notification_runs"][0]
    assert run["status"] == "skipped"
    assert run["reason"] == "no_notify"
    assert run["error_message_sanitized"] == "no_notify"


def test_reuse_path_records_not_configured_and_send_failure(tmp_path) -> None:
    db = _HistoryDB()
    missing = _DingTalkNotifier(available=False, status="no_channel")
    skipped = notify_market_review_text(
        missing,
        "正文",
        history_query_id="market_context_schedule-run_cn",
        notify=True,
        db=db,
        config=_review_config(tmp_path),
    )
    # notifier 自己不可用时仍是 not_configured（内部缺口），与"缺复盘 webhook"区分开
    assert missing.dingtalk_payloads == []
    assert skipped.status == "skipped"
    assert skipped.reason == "not_configured"
    assert db.calls[0]["notification_runs"][0]["reason"] == "not_configured"

    failed_notifier = _DingTalkNotifier(success=False, status="all_failed")
    failed = notify_market_review_text(
        failed_notifier,
        "正文",
        history_query_id="market_context_schedule-run_cn",
        notify=True,
        db=db,
        config=_review_config(tmp_path),
    )
    assert failed_notifier.dingtalk_payloads
    assert failed.status == "failed"
    assert failed.reason == "send_failed"
    assert db.calls[1]["notification_runs"][0]["status"] == "failed"
    assert db.calls[1]["notification_runs"][0]["reason"] == "send_failed"
    # 失败不占当日额度
    from src.core.market_review_daily_cap import is_review_daily_cap_reached

    assert is_review_daily_cap_reached(_review_config(tmp_path).database_path) is False


def test_schedule_reuse_branch_always_notifies_after_context_generation() -> None:
    """The old gate required is_available() and only logged, so a miss stayed skipped."""
    main_py = (_VENDOR / "main.py").read_text(encoding="utf-8")
    start = main_py.index("def run_full_analysis")
    end = main_py.index("\ndef ", start + 1)
    body = main_py[start:end]
    section = body[body.index("复盘上下文可复用"): body.index("review_result = None")]

    assert "notify_market_review_text" in section
    assert "market_context_generated_during_stock" in section
    assert "is_available()" not in section
    assert "deferred_pending_reuse_push" in section or "notification_runs" in section
