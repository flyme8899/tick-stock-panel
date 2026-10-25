"""批次 20261025gding: 钉钉支路的行为测试。

**仅供研究，不构成投资建议；执行须人工确认。**

与 `test_buy_sell_signal_push.py` 的分工: 那个文件守 #205 的 SSE/站内语义, 这个文件
只守 gding 新加的那一段 —— 钉钉什么时候发、什么时候静默跳过、失败后冷却还写不写。

三条设计取舍(读测试前先看):

1. **绝不打真实 webhook**。`app.news.dingtalk.send_markdown` 一律被 monkeypatch 成
   记录器, 断言的是"调了几次 + 传了什么"; `signed_url` / `post_payload` 一行都没跑。
2. **settings 用 monkeypatch.setattr 覆盖**, 不改环境变量。`app.config.settings` 是
   模块级单例, 改 env 不会重建它; 直接改字段才是这个单例实际被读到的那一份。
   `dingtalk_webhook_url` / `dingtalk_secret` 里的值全是**假的占位串**, 不可能是真凭证。
3. **`_emit_dingtalk` 在函数体内 import**, 所以 monkeypatch 必须打在
   `app.news.dingtalk.send_markdown` 上(模块属性), 而不是 `push` 模块的属性上 ——
   打后者不会生效, 而这种"测过了但没测到"的假绿最难发现。
"""
from __future__ import annotations

import ast
import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest

from app.buy_sell_signals import (
    DINGTALK_ENV_NAME,
    MANUAL_PUBLISH_CHECKLIST,
    PUSH_ENV_NAME,
    SignalRecord,
    dingtalk_env_enabled,
    manual_publish,
    transition,
    try_push,
)
from app.buy_sell_signals import push as push_module
from app.buy_sell_signals.constants import (
    DISCLAIMER_KEY,
    PUSH_DEDUPE_COOLDOWN_MIN,
)
from app.buy_sell_signals.disclaimer import DISCLAIMER_TEXT
from app.config import settings
from app.market_time import cn_now

PKG_DIR = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"

# 假凭证: 只为"日志里不许出现"这条断言提供可搜索的字符串。不是任何真实 webhook。
FAKE_WEBHOOK = "https://example.invalid/robot/send?access_token=FAKE-BSS-TOKEN"
FAKE_SECRET = "FAKE-BSS-SECRET"


# ================================================================ 夹具


class FakeBus:
    """只记 `push_alerts` 调用, 用来数 SSE 扇出了几次。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def push_alerts(self, alerts: list[dict]) -> None:
        self.calls.append(alerts)

    @property
    def count(self) -> int:
        return len(self.calls)


class ExplodingBus:
    def push_alerts(self, alerts: list[dict]) -> None:
        raise RuntimeError("SSE 扇出炸了")


class DingTalkRecorder:
    """替身 `send_markdown`。`raise_on_call` 用来模拟外部通道抖动。"""

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.raise_on_call = raise_on_call

    def __call__(self, webhook: str, secret: str, title: str, text: str, *_a, **_kw) -> None:
        self.calls.append((webhook, secret, title, text))
        if self.raise_on_call:
            raise RuntimeError("钉钉返回 310000")

    @property
    def count(self) -> int:
        return len(self.calls)

    @property
    def last_text(self) -> str:
        assert self.calls, "一次都没发"
        return self.calls[-1][3]


@pytest.fixture()
def recorder(monkeypatch):
    """把真实 send_markdown 换掉, 并保证 settings 里的 webhook 是"已配置"。"""
    rec = DingTalkRecorder()
    monkeypatch.setattr("app.news.dingtalk.send_markdown", rec)
    monkeypatch.setattr(settings, "dingtalk_webhook_url", FAKE_WEBHOOK, raising=False)
    monkeypatch.setattr(settings, "dingtalk_secret", FAKE_SECRET, raising=False)
    return rec


@pytest.fixture()
def dingtalk_on(monkeypatch):
    monkeypatch.setenv(DINGTALK_ENV_NAME, "1")


@pytest.fixture()
def dingtalk_off(monkeypatch):
    monkeypatch.delenv(DINGTALK_ENV_NAME, raising=False)


@pytest.fixture()
def push_on(monkeypatch):
    monkeypatch.setenv(PUSH_ENV_NAME, "1")


@pytest.fixture()
def push_off(monkeypatch):
    monkeypatch.delenv(PUSH_ENV_NAME, raising=False)


def _full_checklist() -> dict[str, bool]:
    return {item: True for item in MANUAL_PUBLISH_CHECKLIST}


def _base_record(**overrides) -> SignalRecord:
    now = cn_now()
    kwargs = {
        "signal_id": "sig.dingtalk",
        "display_name": "钉钉样例",
        "family": "minute_event",
        "event_class": "auction",
        "spec_ref": "spec://gding",
        "created_at": now - timedelta(hours=1),
        "updated_at": now,
        "last_status_change_at": now,
    }
    kwargs.update(overrides)
    return SignalRecord(**kwargs)


def _published(**overrides) -> SignalRecord:
    """走真实状态机到 `publishable` + `push_gate=armed`。

    只用于 `event_class` 不是 `limit_reopen` 的记录 —— 炸板构造即 `observe`,
    状态机不给它任何通往 `publishable` 的边(这正是"炸板不可推"的机制保证)。
    """
    rec = transition(_base_record(**overrides), "observe")
    published = manual_publish(rec, publisher="qa", checklist=_full_checklist(), note="gding")
    assert published.status == "publishable"
    assert published.push_gate == "armed"
    return published


def _alerts_file(data_dir: Path) -> Path:
    return data_dir / "user_data" / "alerts.jsonl"


def _read_alerts(data_dir: Path) -> list[dict]:
    path = _alerts_file(data_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ================================================================ 门控


def test_dingtalk_env_name_and_truthy_forms(monkeypatch):
    """env 名写死; 真值集合与 persist / push 完全同形(大写、首尾空白都算)。"""
    assert DINGTALK_ENV_NAME == "BUY_SELL_SIGNALS_DINGTALK"

    monkeypatch.delenv(DINGTALK_ENV_NAME, raising=False)
    assert dingtalk_env_enabled() is False

    for raw in ("1", "true", "TRUE", " yes ", "on", "On"):
        monkeypatch.setenv(DINGTALK_ENV_NAME, raw)
        assert dingtalk_env_enabled() is True, raw

    for raw in ("", "0", "false", "off", "no", "y", "2"):
        monkeypatch.setenv(DINGTALK_ENV_NAME, raw)
        assert dingtalk_env_enabled() is False, raw


def test_dingtalk_truthy_matches_push_and_persist():
    """三个门真值同形 —— 但各自持有常量, 不是互相 import。"""
    from app.buy_sell_signals import store as store_module

    assert push_module._TRUTHY == store_module._TRUTHY
    tree = ast.parse((PKG_DIR / "push.py").read_text(encoding="utf-8"))
    # 查"import 绑定了哪些名字", 而不是扫全文 —— 注释里写"与 store._TRUTHY 同值"
    # 是合规说明, 不该被这条罚。
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            bound.update(alias.asname or alias.name for alias in node.names)
            if (node.module or "") == "store":
                for alias in node.names:
                    assert alias.name != "_TRUTHY", "push.py 不该从 store 借 _TRUTHY"
    assert "_TRUTHY" not in bound, "push.py 应从别处 import 了 _TRUTHY"
    assert "_TRUTHY" in {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }, "push.py 应自己定义 _TRUTHY"
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr == "_TRUTHY"
        and isinstance(node.value, ast.Name)
        for node in ast.walk(tree)
    ), "push.py 不该通过模块属性取别人的 _TRUTHY"


def test_dingtalk_skipped_without_env(push_on, dingtalk_off, recorder, tmp_path):
    """PUSH 开、DINGTALK 未设 → 钉钉静默跳过; SSE/站内/冷却与 #205 完全一致。"""
    bus = FakeBus()
    now = cn_now()

    result = try_push(_published(), quote_service=bus, data_dir=tmp_path, now=now)

    assert result.ok is True, result.reason
    assert result.sse_sent is True
    assert result.inbox_sent is True
    assert result.dingtalk_sent is False
    assert "dingtalk" not in result.channels
    assert recorder.count == 0, "门没开却发了钉钉"

    # 冷却与 #205 一致: 该写的一次也没少。
    assert result.record.last_pushed_at == now
    assert result.record.push_gate == "cooldown"
    assert result.record.push_dedupe_cooldown_until == now + timedelta(minutes=PUSH_DEDUPE_COOLDOWN_MIN)
    assert len(_read_alerts(tmp_path)) == 1
    assert bus.count == 1


def test_dingtalk_skipped_without_webhook(push_on, dingtalk_on, recorder, monkeypatch, tmp_path):
    """两门都开但 webhook 空 → 不调发送; SSE 照推; 冷却照写。"""
    monkeypatch.setattr(settings, "dingtalk_webhook_url", "   ", raising=False)
    assert push_module.dingtalk_webhook_ready() is False

    bus = FakeBus()
    result = try_push(_published(), quote_service=bus, data_dir=tmp_path)

    assert result.ok is True, result.reason
    assert result.sse_sent is True
    assert result.inbox_sent is True
    assert result.dingtalk_sent is False
    assert recorder.count == 0
    assert result.record.push_gate == "cooldown"
    assert len(_read_alerts(tmp_path)) == 1


def test_dingtalk_and_relation_with_push(push_off, dingtalk_on, recorder, tmp_path):
    """PUSH 关 + DINGTALK 开 → 整次 no-op, 连钉钉也不发(与关系, 不是并列)。"""
    bus = FakeBus()

    result = try_push(_published(), quote_service=bus, data_dir=tmp_path)

    assert result.ok is False
    assert PUSH_ENV_NAME in result.reason
    assert bus.count == 0
    assert _read_alerts(tmp_path) == []
    assert recorder.count == 0, "PUSH 关时不该有任何外部外发"
    assert result.record is None


# ================================================================ 正常路径


def test_dingtalk_sends_once_when_armed(push_on, dingtalk_on, recorder, tmp_path):
    """两门开 + webhook 齐 → `send_markdown` 恰好 1 次, 正文含合规脚注。"""
    bus = FakeBus()
    now = cn_now()

    result = try_push(_published(), quote_service=bus, data_dir=tmp_path, now=now)

    assert result.ok is True, result.reason
    assert result.dingtalk_sent is True
    assert set(result.channels) == {"sse", "inbox", "dingtalk"}
    assert recorder.count == 1

    webhook, secret, title, text = recorder.calls[0]
    assert webhook == FAKE_WEBHOOK
    assert secret == FAKE_SECRET
    assert "买卖信号" in title
    assert "钉钉样例" in title
    assert len(title) <= 64
    assert DISCLAIMER_TEXT in text
    assert "publishable" in text
    # 载荷里没有新闻正文位, 本包也不持有正文 —— 只断言信号标识确实带上了。
    assert "sig.dingtalk" in text


def test_dingtalk_payload_carries_disclaimer_key(push_on, dingtalk_on, recorder, tmp_path):
    """站内/SSE 载荷的 `disclaimer` 与钉钉正文用的是**同一句**脚注。"""
    result = try_push(_published(), quote_service=FakeBus(), data_dir=tmp_path)

    alerts = _read_alerts(tmp_path)
    # 载荷里键名写死 `disclaimer`; `DISCLAIMER_KEY` 是**记录**上的口径标识
    # (`research_only_v1`), 两件事不要混。
    assert alerts[0]["disclaimer"] == DISCLAIMER_TEXT
    assert DISCLAIMER_KEY != "disclaimer"
    assert DISCLAIMER_TEXT in recorder.last_text
    assert result.dingtalk_sent is True


def test_dingtalk_same_status_does_not_resend(push_on, dingtalk_on, recorder, tmp_path):
    """成功后再调一次(状态没变) → 不扇出、不发钉钉; `last_pushed_at` 只前进一次。"""
    bus = FakeBus()
    rec = _published()
    first_now = cn_now()

    first = try_push(rec, quote_service=bus, data_dir=tmp_path, now=first_now)
    assert first.ok is True, first.reason
    assert first.dingtalk_sent is True
    assert recorder.count == 1

    # 冷却过了、闸门也恢复了, 但状态没变 —— 仍然不推。
    later = first_now + timedelta(minutes=PUSH_DEDUPE_COOLDOWN_MIN + 5)
    second = try_push(first.record, quote_service=bus, data_dir=tmp_path, now=later)

    assert second.ok is False
    assert "状态未变化" in second.reason
    assert recorder.count == 1, "同状态又发了一次钉钉"
    assert bus.count == 1
    assert len(_read_alerts(tmp_path)) == 1
    assert first.record.last_pushed_at == first_now


def test_dingtalk_sends_again_after_real_status_change(push_on, dingtalk_on, recorder, tmp_path):
    """先有新的状态变化、再等冷却过点 → 才会发第二次。"""
    bus = FakeBus()
    first_now = cn_now()

    first = try_push(_published(), quote_service=bus, data_dir=tmp_path, now=first_now)
    assert first.dingtalk_sent is True

    later = first_now + timedelta(minutes=PUSH_DEDUPE_COOLDOWN_MIN + 5)
    changed = first.record.with_fields(last_status_change_at=later, status="publishable")
    moved = push_module._restore_armed_if_cooldown_over(changed, later)
    assert moved.push_gate == "armed"

    second = try_push(moved, quote_service=bus, data_dir=tmp_path, now=later)

    assert second.ok is True, second.reason
    assert second.dingtalk_sent is True
    assert recorder.count == 2
    assert bus.count == 2


# ================================================================ 失败策略


def test_dingtalk_failure_still_writes_cooldown(push_on, dingtalk_on, monkeypatch, tmp_path):
    """钉钉抛错 → ok 仍 True、冷却仍只写一次、SSE 不二次扇出。"""
    exploding = DingTalkRecorder(raise_on_call=True)
    monkeypatch.setattr("app.news.dingtalk.send_markdown", exploding)
    monkeypatch.setattr(settings, "dingtalk_webhook_url", FAKE_WEBHOOK, raising=False)

    bus = FakeBus()
    now = cn_now()

    result = try_push(_published(), quote_service=bus, data_dir=tmp_path, now=now)

    assert exploding.count == 1, "应当只尝试一次, 不重试"
    assert result.ok is True, "钉钉失败不该把整次推送标成失败"
    assert result.dingtalk_sent is False
    assert set(result.channels) == {"sse", "inbox"}
    assert result.record.push_gate == "cooldown"
    assert result.record.last_pushed_at == now
    assert result.record.push_dedupe_cooldown_until == now + timedelta(minutes=PUSH_DEDUPE_COOLDOWN_MIN)
    assert bus.count == 1, "钉钉失败不该导致 SSE 再发一遍"
    assert len(_read_alerts(tmp_path)) == 1


def test_dingtalk_not_attempted_when_core_fanout_fails(push_on, dingtalk_on, recorder, tmp_path):
    """核心通道炸了 → 不写回、**也不发钉钉**(钉钉挂在核心成功之后)。"""
    result = try_push(_published(), quote_service=ExplodingBus(), data_dir=tmp_path)

    assert result.ok is False
    assert "扇出失败" in result.reason
    assert recorder.count == 0, "站内/SSE 都没成功, 不该往外部群里发"
    assert result.record is None


def test_dingtalk_no_secret_in_logs(push_on, dingtalk_on, monkeypatch, caplog, tmp_path):
    """失败路径的日志里不得出现 webhook URL / secret / token 子串。"""
    monkeypatch.setattr(
        "app.news.dingtalk.send_markdown", DingTalkRecorder(raise_on_call=True)
    )
    monkeypatch.setattr(settings, "dingtalk_webhook_url", FAKE_WEBHOOK, raising=False)
    monkeypatch.setattr(settings, "dingtalk_secret", FAKE_SECRET, raising=False)

    with caplog.at_level(logging.DEBUG, logger="app.buy_sell_signals.push"):
        try_push(_published(), quote_service=FakeBus(), data_dir=tmp_path)

    text = caplog.text
    assert text, "失败路径应当留下 warning 日志"
    for needle in (FAKE_WEBHOOK, FAKE_SECRET, "FAKE-BSS-TOKEN"):
        assert needle not in text, f"日志里出现了明文 {needle!r}"


# ================================================================ 拒推场景(与 #205 一致)


def test_dingtalk_rejects_limit_reopen_like_push(push_on, dingtalk_on, recorder, tmp_path):
    """炸板(event_class=limit_reopen) → 拒推, 连钉钉都不发。"""
    bus = FakeBus()
    reopen = _base_record(signal_id="sig.reopen", event_class="limit_reopen")
    assert reopen.status == "observe", "炸板构造即 observe"

    result = try_push(reopen, quote_service=bus, data_dir=tmp_path)

    assert result.ok is False
    assert "limit_reopen" in result.reason
    assert bus.count == 0
    assert recorder.count == 0


def test_dingtalk_rejects_non_publishable(push_on, dingtalk_on, recorder, tmp_path):
    """非 publishable(observe) → 拒推, 不发钉钉。"""
    now = cn_now()
    rec = transition(
        SignalRecord(
            signal_id="sig.observe",
            display_name="观察中",
            family="minute_event",
            event_class="auction",
            spec_ref="spec://gding",
            created_at=now - timedelta(hours=1),
            updated_at=now,
            last_status_change_at=now,
        ),
        "observe",
    )

    result = try_push(rec, quote_service=FakeBus(), data_dir=tmp_path)

    assert result.ok is False
    assert "publishable" in result.reason
    assert recorder.count == 0


def test_dingtalk_blocked_during_cooldown(push_on, dingtalk_on, recorder, tmp_path):
    """冷却期内再调 → 拒推, 不发钉钉。"""
    now = cn_now()
    first = try_push(_published(), quote_service=FakeBus(), data_dir=tmp_path, now=now)
    assert recorder.count == 1

    again = try_push(
        first.record, quote_service=FakeBus(), data_dir=tmp_path, now=now + timedelta(minutes=5)
    )

    assert again.ok is False
    assert "冷却中" in again.reason
    assert recorder.count == 1


# ================================================================ 静态: 无飞书 / 依赖白名单


def test_dingtalk_reuses_existing_sender_only():
    """`push.py` 只调既有 `send_markdown`, 不复制加签、不新建机器人。"""
    tree = ast.parse((PKG_DIR / "push.py").read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)

    assert "app.news.dingtalk" in modules, "钉钉发送必须走既有 send_markdown"
    # 自己实现加签 = 平行机器人, 硬禁。
    for banned in ("hmac", "hashlib", "base64", "urllib.request"):
        assert not any(m == banned or m.startswith(f"{banned}.") for m in modules), banned


def test_dingtalk_env_read_from_settings_not_new_config():
    """凭证只读既有 settings 字段, 不新增配置项、不另起 env 名。"""
    source = (PKG_DIR / "push.py").read_text(encoding="utf-8")
    assert 'getattr(settings, "dingtalk_webhook_url", "")' in source
    assert 'getattr(settings, "dingtalk_secret", "")' in source
    assert "DINGTALK_BSS" not in source

    # 不应自己再读一遍原始 env 名: 只看**字符串字面量**, 不看 docstring 说明文字。
    tree = ast.parse(source)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "DINGTALK_WEBHOOK_URL" not in literals
    assert "DINGTALK_SECRET" not in literals
