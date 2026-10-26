"""定时复盘独立钉钉 webhook + 日摘要限流（批次 20261026gdsading · P-ding）。

覆盖 G-T2–G-T6：
- G-T2 配置键可读；.env.example 有注释空示例
- G-T3 卡口钉钉外发**只**用复盘 webhook
- G-T4 缺复盘 webhook → review_webhook_not_configured，且**未**调旧 URL
- G-T5 旧 URL 非空 + 复盘 URL 空 → 仍拒发
- G-T6 日 cap=1；跨重启（同 JSON）仍 cap；失败/skip 不计额度

全部用占位 URL（https://example.invalid/...）+ notifier 替身，不碰真钉钉网络。

仅供研究，不构成投资建议；执行须人工确认。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_VENDOR = _ROOT / "vendor" / "daily_stock_analysis"
sys.path.insert(0, str(_VENDOR))

from src.core.market_review_daily_cap import (  # noqa: E402
    CAP_FILE_NAME,
    DAILY_SUCCESS_CAP,
    is_review_daily_cap_reached,
    record_review_success,
    review_daily_cap_path,
)
from src.services.market_review_push import (  # noqa: E402
    REVIEW_DAILY_CAP_REACHED,
    REVIEW_WEBHOOK_NOT_CONFIGURED,
    notify_market_review_text,
    resolve_review_dingtalk_settings,
)

_REVIEW_URL = "https://example.invalid/review"
_OLD_URL = "https://example.invalid/old"
_TODAY = date(2026, 10, 26)


def _config(tmp_path, *, review_url=None, old_url=_OLD_URL, secret=None, **extra):
    base = Path(tmp_path) if tmp_path is not None else Path("/tmp/dsa-review-cap-nocap")
    return SimpleNamespace(
        dsa_review_dingtalk_webhook_url=review_url,
        dsa_review_dingtalk_secret=secret,
        dingtalk_webhook_url=old_url,
        dingtalk_secret="old-secret",
        database_path=str(base / "data" / "stock_analysis.db"),
        **extra,
    )


class _RecordingNotifier:
    """记下每次外发用了哪个 webhook —— 断言"是否回落旧群"的关键。"""

    def __init__(self, *, success: bool = True, available: bool = True) -> None:
        self.success = success
        self.available = available
        self.calls: list[dict] = []
        self.webhooks: list[str | None] = []
        self.secrets: list[str | None] = []

    def is_available(self) -> bool:
        return self.available

    def send_with_results(self, content: str, **kwargs):
        self.calls.append(kwargs)
        self.webhooks.append(kwargs.get("dingtalk_webhook_override"))
        self.secrets.append(kwargs.get("dingtalk_secret_override"))
        return SimpleNamespace(
            dispatched=True,
            success=self.success,
            status="sent" if self.success else "all_failed",
            message=None if self.success else "boom",
            channel_results=[],
        )


def _notify(notifier, tmp_path, *, body="# 📈 大盘复盘\n\n正文", config=None, today=_TODAY, db=None):
    return notify_market_review_text(
        notifier,
        body,
        history_query_id="market_context_run_cn",
        notify=True,
        db=db,
        config=config if config is not None else _config(tmp_path, review_url=_REVIEW_URL),
        today_fn=lambda: today,
    )


# ── G-T2 配置键 ──────────────────────────────────────────────────────────


def test_config_declares_review_fields_and_env_keys() -> None:
    """Config 声明两个新字段，且 env 读取行用的正是这两个键名。

    刻意**不**调 ``Config._load_from_env()``：那条路径会经
    ``_parse_stock_email_groups`` 拉起 ``data_provider`` 整条链（要
    ``fake_useragent`` 等抓取侧依赖），与"新键能不能读"无关，在只装了
    backend dev 依赖的环境里必然 ImportError。这里核的是声明与映射。
    """
    import dataclasses

    from src.config import Config

    fields = {f.name for f in dataclasses.fields(Config)}
    assert "dsa_review_dingtalk_webhook_url" in fields
    assert "dsa_review_dingtalk_secret" in fields

    source = (Path(_VENDOR) / "src" / "config.py").read_text(encoding="utf-8")
    assert "dsa_review_dingtalk_webhook_url=os.getenv('DSA_REVIEW_DINGTALK_WEBHOOK_URL')" in source
    assert "dsa_review_dingtalk_secret=os.getenv('DSA_REVIEW_DINGTALK_SECRET')" in source


def test_config_defaults_to_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """不设 env 时两个字段默认 None —— 即"未配置"，不是空串。"""
    from src.config import Config

    for key in ("DSA_REVIEW_DINGTALK_WEBHOOK_URL", "DSA_REVIEW_DINGTALK_SECRET"):
        monkeypatch.delenv(key, raising=False)
    cfg = Config()
    assert cfg.dsa_review_dingtalk_webhook_url is None
    assert cfg.dsa_review_dingtalk_secret is None


def test_env_example_has_commented_empty_placeholders() -> None:
    """.env.example 只放注释空占位，不放真实 URL。"""
    text = (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "# DSA_REVIEW_DINGTALK_WEBHOOK_URL=" in text
    assert "# DSA_REVIEW_DINGTALK_SECRET=" in text
    # 不许出现真实 webhook 令牌
    for line in text.splitlines():
        if "DSA_REVIEW_DINGTALK_WEBHOOK_URL" in line:
            assert line.strip().startswith("#")
            assert "access_token=" not in line


def test_registry_entries_declare_hard_gate() -> None:
    """registry 里两条新键可发现，且 help 写清"缺则拒发、不回落旧键"。"""
    from src.core.config_registry import get_field_definition

    for key in ("DSA_REVIEW_DINGTALK_WEBHOOK_URL", "DSA_REVIEW_DINGTALK_SECRET"):
        field = get_field_definition(key)
        assert field["key"] == key, f"registry 缺 {key}"
        assert field["is_sensitive"] is True
    desc = get_field_definition("DSA_REVIEW_DINGTALK_WEBHOOK_URL")["description"]
    assert REVIEW_WEBHOOK_NOT_CONFIGURED in desc
    assert "DINGTALK_WEBHOOK_URL" in desc


def test_resolve_reads_review_key_never_falls_back_to_old() -> None:
    assert resolve_review_dingtalk_settings(_config(None, review_url=_REVIEW_URL))[0] == _REVIEW_URL
    # 旧键非空、复盘键空 → 仍返回空（不回落）
    webhook, _ = resolve_review_dingtalk_settings(_config(None, review_url=None))
    assert webhook == ""
    # 纯空白也算未配置
    assert resolve_review_dingtalk_settings(_config(None, review_url="   "))[0] == ""


# ── G-T3 / G-T4 / G-T5 卡口 ──────────────────────────────────────────────


def test_review_send_uses_review_webhook_only(tmp_path) -> None:
    notifier = _RecordingNotifier()
    outcome = _notify(notifier, tmp_path)

    assert outcome.status == "success"
    assert outcome.reason == "sent"
    assert notifier.webhooks == [_REVIEW_URL]
    assert _OLD_URL not in notifier.webhooks


def test_review_secret_is_injected_alongside_webhook(tmp_path) -> None:
    notifier = _RecordingNotifier()
    _notify(notifier, tmp_path, config=_config(tmp_path, review_url=_REVIEW_URL, secret="review-secret"))
    assert notifier.secrets == ["review-secret"]


def test_missing_review_webhook_rejects_without_touching_old(tmp_path) -> None:
    """G-T4：缺复盘 webhook → 拒发；notifier 一次都没被调用（旧 URL 不可能被用）。"""
    notifier = _RecordingNotifier()
    outcome = _notify(notifier, tmp_path, config=_config(tmp_path, review_url=None))

    assert outcome.status == "skipped"
    assert outcome.success is False
    assert outcome.reason == REVIEW_WEBHOOK_NOT_CONFIGURED
    assert outcome.attempts == 0
    assert notifier.calls == [], "缺复盘 webhook 时不得调用发送器（否则就是回落旧群）"
    assert notifier.webhooks == []


def test_blank_review_webhook_counts_as_missing(tmp_path) -> None:
    notifier = _RecordingNotifier()
    outcome = _notify(notifier, tmp_path, config=_config(tmp_path, review_url="   \n  "))
    assert outcome.reason == REVIEW_WEBHOOK_NOT_CONFIGURED
    assert notifier.calls == []


def test_old_webhook_non_empty_does_not_substitute(tmp_path) -> None:
    """G-T5：旧 URL 非空 + 复盘 URL 空 → 仍拒发。"""
    notifier = _RecordingNotifier()
    config = _config(tmp_path, review_url=None, old_url=_OLD_URL)
    assert config.dingtalk_webhook_url == _OLD_URL

    outcome = _notify(notifier, tmp_path, config=config)

    assert outcome.reason == REVIEW_WEBHOOK_NOT_CONFIGURED
    assert notifier.calls == []
    assert _OLD_URL not in notifier.webhooks


def test_same_as_old_webhook_is_allowed_with_warning(tmp_path, caplog) -> None:
    """复盘 URL == 旧 URL：允许发送 + warning（不因此拒发）。"""
    notifier = _RecordingNotifier()
    config = _config(tmp_path, review_url=_OLD_URL, old_url=_OLD_URL)

    with caplog.at_level("WARNING"):
        outcome = _notify(notifier, tmp_path, config=config)

    assert outcome.status == "success"
    assert notifier.webhooks == [_OLD_URL]
    assert any("注意力竞争" in rec.message for rec in caplog.records)


# ── G-T6 日 cap ──────────────────────────────────────────────────────────


def test_second_success_same_day_is_capped(tmp_path) -> None:
    notifier = _RecordingNotifier()
    first = _notify(notifier, tmp_path)
    second = _notify(notifier, tmp_path)

    assert first.status == "success"
    assert second.status == "skipped"
    assert second.reason == REVIEW_DAILY_CAP_REACHED
    assert second.attempts == 0
    # 第二次不该再打 webhook
    assert notifier.webhooks == [_REVIEW_URL]


def test_cap_survives_process_restart(tmp_path) -> None:
    """跨重启：同 JSON 重新读一次，仍 cap（不依赖进程内状态）。"""
    _notify(_RecordingNotifier(), tmp_path)
    cap_path = review_daily_cap_path(_config(tmp_path).database_path)
    assert cap_path.exists()

    # 模拟重启：全新进程读同一文件
    code = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(_VENDOR)!r}); "
        "from src.core.market_review_daily_cap import is_review_daily_cap_reached, review_daily_cap_path; "
        f"db = {str(_config(tmp_path).database_path)!r}; "
        "print(review_daily_cap_path(db)); "
        "print(is_review_daily_cap_reached(db, today_fn=lambda: __import__('datetime').date(2026,10,26)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "True"

    # 同进程新建 notifier 再发也一样被拦
    assert _notify(_RecordingNotifier(), tmp_path).reason == REVIEW_DAILY_CAP_REACHED


def test_failed_send_does_not_consume_quota(tmp_path) -> None:
    failed = _RecordingNotifier(success=False)
    first = _notify(failed, tmp_path)
    assert first.status == "failed"
    assert is_review_daily_cap_reached(_config(tmp_path).database_path, today_fn=lambda: _TODAY) is False

    # 失败后当天仍可成功发一条
    ok = _notify(_RecordingNotifier(), tmp_path)
    assert ok.status == "success"


def test_skipped_paths_do_not_consume_quota(tmp_path) -> None:
    config = _config(tmp_path, review_url=None)
    _notify(_RecordingNotifier(), tmp_path, config=config)  # 缺 webhook skip
    _notify(_RecordingNotifier(), tmp_path, body="   ")      # 空正文 skip
    assert is_review_daily_cap_reached(_config(tmp_path).database_path, today_fn=lambda: _TODAY) is False


def test_cap_resets_on_next_shanghai_day(tmp_path) -> None:
    notifier = _RecordingNotifier()
    _notify(notifier, tmp_path, today=date(2026, 10, 26))
    capped = _notify(notifier, tmp_path, today=date(2026, 10, 26))
    assert capped.reason == REVIEW_DAILY_CAP_REACHED

    next_day = _notify(notifier, tmp_path, today=date(2026, 10, 27))
    assert next_day.status == "success"


def test_cap_file_schema_is_pinned(tmp_path) -> None:
    _notify(_RecordingNotifier(), tmp_path, today=_TODAY)
    cap_path = review_daily_cap_path(_config(tmp_path).database_path)
    assert cap_path.name == CAP_FILE_NAME
    assert cap_path.parent == (_config(tmp_path).database_path and Path(_config(tmp_path).database_path).parent)

    payload = json.loads(cap_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1"
    assert payload["tz"] == "Asia/Shanghai"
    # 日期键来自可注入的 today_fn，跟着 _TODAY 走
    entry = payload["days"][_TODAY.isoformat()]
    assert entry["success_count"] == DAILY_SUCCESS_CAP
    # last_success_at 取的是**真实时钟**（now_fn 未从 notify 这一层注入），
    # 所以只能断言"是个可解析的带时区时间戳"，不能钉死日期 ——
    # 钉死的话每天过零点都会红，而它跟这条用例真正要守的东西（schema）无关。
    stamp = datetime.fromisoformat(entry["last_success_at"])
    assert stamp.tzinfo is not None


def test_corrupted_cap_file_fails_open_without_old_fallback(tmp_path) -> None:
    """JSON 坏了 → 按 0 处理（允许发一条），但绝不回落旧 webhook。"""
    cap_path = review_daily_cap_path(_config(tmp_path).database_path)
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    cap_path.write_text("{ not json", encoding="utf-8")

    notifier = _RecordingNotifier()
    outcome = _notify(notifier, tmp_path)

    assert outcome.status == "success"
    assert notifier.webhooks == [_REVIEW_URL]


def test_record_success_returns_json_serialisable_state(tmp_path) -> None:
    cfg = _config(tmp_path)
    state = record_review_success(
        cfg.database_path,
        today_fn=lambda: _TODAY,
        now_fn=lambda: datetime(2026, 10, 26, 18, 5, tzinfo=None),
    )
    assert state["days"]["2026-10-26"]["success_count"] == 1
    json.dumps(state)  # 可序列化


# ── G-T3 入口覆盖：SCHEDULE 复用 / merge 都带 config ─────────────────────


def test_all_main_call_sites_pass_config() -> None:
    """``main.py`` 里三处 ``notify_market_review_text`` 调用都传了 config。

    不传 config 时 ``resolve_review_dingtalk_settings`` 会去取全局配置；
    虽然取得到，但显式传参才能保证读的是本轮那份配置（测试也才能注入）。
    更重要的是：**任何一处漏传**都可能让该入口走不到复盘键。
    """
    import ast as _ast

    main_src = (_VENDOR / "main.py").read_text(encoding="utf-8")
    tree = _ast.parse(main_src)
    calls = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name == "notify_market_review_text":
            calls.append(node)

    assert len(calls) == 3, f"预期 3 处调用，实际 {len(calls)}"
    for call in calls:
        keywords = {kw.arg for kw in call.keywords}
        assert "config" in keywords, f"main.py:{call.lineno} 的调用漏传 config"


def test_schedule_and_merge_share_same_cap(tmp_path) -> None:
    """SCHEDULE 复用成功一条后，同日 merge 摘要直接被 cap 拦住（共用额度）。"""
    notifier = _RecordingNotifier()
    schedule = _notify(notifier, tmp_path, body="# 📈 大盘复盘\n\nSCHEDULE 复用正文")
    merge = _notify(notifier, tmp_path, body="# 🚀 个股决策仪表盘\n\n---\n\n# 📈 大盘复盘")

    assert schedule.status == "success"
    assert merge.reason == REVIEW_DAILY_CAP_REACHED
    # 只有第一条真的发出去了
    assert len(notifier.webhooks) == 1


# ── DingtalkSender override 行为（真实类，绕过重依赖链）──────────────────


_SENDER_STUBBED_MODULES = ("src", "src.config", "src.formatters", "requests")


def _load_dingtalk_sender_class():
    """按文件加载 ``DingtalkSender``，把它自己的 import 全换成桩件。

    为什么不用真 import：
    * ``src.notification_sender.__init__`` 会把 ``data_provider`` 整条抓取链拉起来；
    * 连直接加载文件也不行 —— ``dingtalk_sender`` 顶层就 import
      ``src.formatters``，而后者 ``import markdown2``，这个包既不在
      ``backend/pyproject.toml`` 也不在 CI 环境里（CI 只装 backend 自己的依赖）。

    本用例只关心"发到哪个 URL / 用哪个 secret 加签"，所以把三条外部 import
    换成最小桩件即可，测的还是 ``dingtalk_sender.py`` 里真实的 override 逻辑。
    """
    import sys
    import types

    config_mod = types.ModuleType("src.config")

    class _Config:  # 仅作类型标注用，构造器不读它
        pass

    config_mod.Config = _Config

    formatters_mod = types.ModuleType("src.formatters")
    formatters_mod.chunk_content_by_max_bytes = lambda content, max_bytes, add_page_marker=False: [content]
    formatters_mod.strip_hidden_markdown_metadata = lambda text: text

    requests_mod = types.ModuleType("requests")
    requests_mod.post = None  # 由 _sender_with 装上 recorder

    src_pkg = types.ModuleType("src")
    src_pkg.__path__ = []  # type: ignore[attr-defined]

    saved = {name: sys.modules.get(name) for name in _SENDER_STUBBED_MODULES}
    sys.modules.update(
        {
            "src": src_pkg,
            "src.config": config_mod,
            "src.formatters": formatters_mod,
            "requests": requests_mod,
        }
    )
    try:
        import importlib.util

        path = _VENDOR / "src" / "notification_sender" / "dingtalk_sender.py"
        spec = importlib.util.spec_from_file_location("_p_ding_sender", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module.DingtalkSender


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"errcode": 0}


class _PostRecorder:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url, **_kwargs):
        self.urls.append(url)
        return _FakeResponse()


class _RequestsStub:
    def __init__(self, recorder: _PostRecorder) -> None:
        self.post = recorder


def _sender_with(monkeypatch, *, webhook, secret=None):
    cls = _load_dingtalk_sender_class()
    cfg = SimpleNamespace(dingtalk_webhook_url=webhook, dingtalk_secret=secret)
    sender = cls(cfg)
    recorder = _PostRecorder()
    # 模块已从 sys.modules 撤掉，只能顺着函数 __globals__ 找它引用的 requests 桩
    monkeypatch.setitem(sender.send_to_dingtalk.__globals__, "requests", _RequestsStub(recorder))
    return sender, recorder


def test_sender_override_switches_target_url(monkeypatch) -> None:
    """传 override 就打到 override，不传就打构造时那条（默认行为不变）。"""
    sender, recorder = _sender_with(monkeypatch, webhook=_OLD_URL)

    assert sender.send_to_dingtalk("正文") is True
    assert recorder.urls == [_OLD_URL]

    assert sender.send_to_dingtalk("正文", webhook_url_override=_REVIEW_URL) is True
    assert recorder.urls[-1] == _REVIEW_URL
    # 构造时绑定的那条没被改写
    assert sender.webhook_url == _OLD_URL


def test_sender_override_secret_signs_with_review_secret(monkeypatch) -> None:
    """加签必须用复盘 secret —— 只断言"有 sign=" 抓不到用错 secret。"""
    sender, recorder = _sender_with(monkeypatch, webhook=_OLD_URL, secret="old-secret")

    sender.send_to_dingtalk(
        "正文", webhook_url_override=_REVIEW_URL, secret_override="review-secret"
    )

    url = recorder.urls[-1]
    assert url.startswith(_REVIEW_URL)
    assert "sign=" in url

    # 复算一遍签名：拿复盘 secret 按钉钉规则算，应和 URL 里的一模一样
    import base64
    import hashlib
    import hmac
    import urllib.parse

    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    timestamp = query["timestamp"][0]
    got_sign = query["sign"][0]

    def _sign(secret: str) -> str:
        string_to_sign = f"{timestamp}\n{secret}".encode()
        digest = hmac.new(secret.encode(), string_to_sign, digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    # parse_qs already percent-decodes, so compare against the raw base64
    assert got_sign == _sign("review-secret"), "签名应使用复盘 secret"
    assert got_sign != _sign("old-secret"), "签名不应使用旧全局 secret"


def test_sender_empty_override_still_refuses(monkeypatch) -> None:
    """override 传空串 → 不发（不复用旧 URL）。"""
    sender, recorder = _sender_with(monkeypatch, webhook=_OLD_URL)
    assert sender.send_to_dingtalk("正文", webhook_url_override="") is False
    assert recorder.urls == []


# ── 时区边界：Asia/Shanghai 而非 UTC ────────────────────────────────────


def test_review_today_uses_shanghai_not_utc() -> None:
    """``review_today()`` 的日期必须按 Asia/Shanghai 算。

    在 UTC 16:00（= 上海次日 00:00）这个瞬间，UTC 还停在昨天、上海已是今天。
    若实现改用 ``datetime.utcnow().date()``，日 cap 会错位一整天。
    """
    from unittest.mock import patch

    import src.core.market_review_daily_cap as cap_mod

    real_datetime = datetime

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            instant = real_datetime(2026, 10, 26, 16, 30, tzinfo=UTC)
            if tz is None:
                # 模仿 naive now()：返回 UTC（正是要排除的写法）
                return instant.astimezone(UTC).replace(tzinfo=None)
            return instant.astimezone(tz)

    with patch.object(cap_mod, "datetime", _FrozenDatetime):
        assert cap_mod.review_today() == date(2026, 10, 27), "应按 Asia/Shanghai 落到次日"


def test_cap_boundary_across_shanghai_midnight(tmp_path) -> None:
    """上海跨日那一刻起，前一天的额度不再生效。"""
    notifier = _RecordingNotifier()
    config = _config(tmp_path, review_url=_REVIEW_URL)

    first = _notify(notifier, tmp_path, config=config, today=date(2026, 10, 26))
    same_day = _notify(notifier, tmp_path, config=config, today=date(2026, 10, 26))
    next_day = _notify(notifier, tmp_path, config=config, today=date(2026, 10, 27))

    assert first.status == "success"
    assert same_day.reason == REVIEW_DAILY_CAP_REACHED
    assert next_day.status == "success", "跨日后应重新开放额度"
    assert notifier.webhooks == [_REVIEW_URL, _REVIEW_URL]


# ── cap 文件体积：只留最近 N 天 ─────────────────────────────────────────


def test_cap_prunes_old_day_keys(tmp_path) -> None:
    """连续记 20 天，文件里只保留最近 14 天键 —— 否则永久运行会无限胀。"""
    from src.core.market_review_daily_cap import _MAX_RETAINED_DAYS

    cfg = _config(tmp_path, review_url=None)

    cap_path = review_daily_cap_path(cfg.database_path)
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    # 造 20 天历史（含一个很久以前的键，必须被裁掉）
    days = {
        f"2026-09-{d:02d}": {"success_count": 1, "last_success_at": f"2026-09-{d:02d}T18:00:00+08:00"}
        for d in range(1, 21)
    }
    cap_path.write_text(
        json.dumps({"schema_version": "1", "tz": "Asia/Shanghai", "days": days}),
        encoding="utf-8",
    )

    record_review_success(cfg.database_path, today_fn=lambda: date(2026, 9, 21))

    payload = json.loads(cap_path.read_text(encoding="utf-8"))
    assert len(payload["days"]) == _MAX_RETAINED_DAYS
    assert "2026-09-21" in payload["days"], "最新一天必须保留"
    assert "2026-09-01" not in payload["days"], "最旧的应被裁掉"


# ── override 一路透传到真实 sender（不只是 kwargs 断言）──────────────────


def test_override_survives_notification_layer(tmp_path, monkeypatch) -> None:
    """``NotificationService`` 必须把 override 一路交到钉钉发送调用上。

    不验这一层的话，卡口传了 override、notifier 内部却丢掉，用户看到的是
    "复盘已独立"，实际全打到旧群 —— 最难发现的一类假绿。

    用 AST 定位 ``_send_to_static_channel`` 里 DINGTALK 分支的调用节点，
    断言它真的把 override 作为关键字参数传下去了（而不是只声明了参数）。
    """
    import ast as _ast

    notification_path = _VENDOR / "src" / "notification.py"
    source = notification_path.read_text(encoding="utf-8")
    tree = _ast.parse(source)

    target = None
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "_send_to_static_channel":
            target = node
            break
    assert target is not None, "找不到 _send_to_static_channel"

    params = {a.arg for a in target.args.args} | {a.arg for a in target.args.kwonlyargs}
    assert {"dingtalk_webhook_override", "dingtalk_secret_override"} <= params

    # 找 DINGTALK 分支里的 send_to_dingtalk 调用
    dingtalk_calls = [
        node
        for node in _ast.walk(target)
        if isinstance(node, _ast.Call)
        and isinstance(node.func, _ast.Attribute)
        and node.func.attr == "send_to_dingtalk"
    ]
    assert len(dingtalk_calls) == 1, f"钉钉发送调用点数异常: {len(dingtalk_calls)}"
    kwargs = {kw.arg: _ast.unparse(kw.value) for kw in dingtalk_calls[0].keywords}
    assert kwargs.get("webhook_url_override") == "dingtalk_webhook_override"
    assert kwargs.get("secret_override") == "dingtalk_secret_override"

    # send_with_results 也要把它传给 _send_to_static_channel
    dispatch = next(
        node
        for node in _ast.walk(tree)
        if isinstance(node, _ast.FunctionDef) and node.name == "send_with_results"
    )
    dispatch_params = {a.arg for a in dispatch.args.args} | {a.arg for a in dispatch.args.kwonlyargs}
    assert {"dingtalk_webhook_override", "dingtalk_secret_override"} <= dispatch_params

    static_calls = [
        node
        for node in _ast.walk(dispatch)
        if isinstance(node, _ast.Call)
        and isinstance(node.func, _ast.Attribute)
        and node.func.attr == "_send_to_static_channel"
    ]
    assert static_calls, "send_with_results 里找不到 _send_to_static_channel 调用"
    passed = {kw.arg for kw in static_calls[0].keywords}
    assert {"dingtalk_webhook_override", "dingtalk_secret_override"} <= passed, (
        "override 在 send_with_results 处被丢弃"
    )
