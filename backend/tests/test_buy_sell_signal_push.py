"""批次 20261025gpush: 推送出口 `push.try_push` 的行为测试。

**仅供研究，不构成投资建议；执行须人工确认。**

设计取舍说明(读测试前先看这三条):

1. **不 patch 时钟**。`push_dedupe_cooldown_active` 内部读 `cn_now()`, 而我们传进去的
   `now` 是独立参数。绝大多数用例传 `now=cn_now()`, 两者一致; 只有"冷却期满"那条
   故意传未来的 `now`, 让"记录内部时钟"和"入口时钟"分离 —— 这正好也测出了
   `try_push` 必须先按入口时钟恢复 armed、否则会被 `push_eligible` 拦死这件事。
2. **假 bus 而不是真 QuoteService**。`try_push` 对 `quote_service` 只要求有
   `push_alerts(list[dict])`; 用记录调用的假对象, 能直接断言"调用次数 + 载荷"。
3. **站内写 `tmp_path`**。`alert_store.append_many` 会顺手往 `app.services.events.bus`
   发一条, 广播失败不影响落盘, 所以不需要清总线。
"""
from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.buy_sell_signals import (
    DISCLAIMER_TEXT,
    MANUAL_PUBLISH_CHECKLIST,
    PUSH_ENV_NAME,
    SignalRecord,
    manual_publish,
    push_env_enabled,
    transition,
    try_push,
)
from app.buy_sell_signals import push as push_module
from app.buy_sell_signals.constants import (
    MONITOR_EVENT_TYPE,
    PUSH_DEDUPE_COOLDOWN_MIN,
    PUSH_PAYLOAD_SCHEMA_VERSION,
)
from app.market_time import cn_now

PKG_DIR = Path(__file__).resolve().parents[1] / "app" / "buy_sell_signals"


# ================================================================ 夹具


class FakeBus:
    """只记 `push_alerts` 调用。存在的意义是"能被数出来调用了几次"。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def push_alerts(self, alerts: list[dict]) -> None:
        self.calls.append(alerts)

    @property
    def count(self) -> int:
        return len(self.calls)

    @property
    def flat(self) -> list[dict]:
        return [item for batch in self.calls for item in batch]


class ExplodingBus:
    def push_alerts(self, alerts: list[dict]) -> None:
        raise RuntimeError("SSE 扇出炸了")


def _base_record(**overrides) -> SignalRecord:
    now = cn_now()
    kwargs = {
        "signal_id": "sig.push",
        "display_name": "推送样例",
        "family": "minute_event",
        "event_class": "auction",
        "spec_ref": "spec://push",
        "created_at": now - timedelta(hours=1),
        "updated_at": now,
        "last_status_change_at": now,
    }
    kwargs.update(overrides)
    return SignalRecord(**kwargs)


def _full_checklist() -> dict[str, bool]:
    """人工发布清单全勾。缺一项 `manual_publish` 就会拒 —— 这正是它该干的事。"""
    return {item: True for item in MANUAL_PUBLISH_CHECKLIST}


def _published(**overrides) -> SignalRecord:
    """走真实状态机到 publishable + push_gate=armed 的记录。"""
    rec = _base_record(**overrides)
    rec = transition(rec, "observe")
    published = manual_publish(rec, publisher="qa", checklist=_full_checklist(), note="批次测试")
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


@pytest.fixture()
def push_on(monkeypatch):
    monkeypatch.setenv(PUSH_ENV_NAME, "1")


@pytest.fixture()
def push_off(monkeypatch):
    monkeypatch.delenv(PUSH_ENV_NAME, raising=False)


# ================================================================ 门控


def test_push_disabled_without_env(push_off, tmp_path):
    """未设 env → 一行都不写: 不扇出、不落 alerts.jsonl、不写 last_pushed_at。"""
    bus = FakeBus()
    rec = _published()

    result = try_push(rec, quote_service=bus, data_dir=tmp_path)

    assert result.ok is False
    assert PUSH_ENV_NAME in result.reason
    assert bus.count == 0
    assert _read_alerts(tmp_path) == []
    assert result.record is None


def test_push_env_name_and_truthy_forms(monkeypatch):
    """env 名写死, 真值集合与 persist 完全一致(大写、空白都算)。"""
    assert PUSH_ENV_NAME == "BUY_SELL_SIGNALS_PUSH"
    monkeypatch.delenv(PUSH_ENV_NAME, raising=False)
    assert push_env_enabled() is False

    for raw in ("1", "true", "TRUE", " yes ", "on", "On"):
        monkeypatch.setenv(PUSH_ENV_NAME, raw)
        assert push_env_enabled() is True, raw

    for raw in ("", "0", "false", "off", "no", "y", "2"):
        monkeypatch.setenv(PUSH_ENV_NAME, raw)
        assert push_env_enabled() is False, raw


def test_push_truthy_matches_persist_truthy():
    """两个 env 的真值集合必须同形 —— 但它们各自持有常量, 不是互相 import。"""
    from app.buy_sell_signals import store as store_module

    assert push_module._TRUTHY == store_module._TRUTHY
    source = (PKG_DIR / "push.py").read_text(encoding="utf-8")
    assert "from .store import" in source
    assert "_TRUTHY" not in source.split("from .store import")[1].split("\n\n")[0], (
        "push.py 不应从 store 借用 _TRUTHY"
    )


# ================================================================ 正常路径


def test_push_once_when_eligible(push_on, tmp_path):
    """合格路径: SSE 恰好 1 次, jsonl 恰好 1 行, 冷却与 gate 已写回。"""
    bus = FakeBus()
    rec = _published()
    now = cn_now()

    result = try_push(rec, quote_service=bus, data_dir=tmp_path, now=now)

    assert result.ok is True, result.reason
    assert result.sse_sent is True
    assert result.inbox_sent is True
    assert bus.count == 1

    updated = result.record
    assert isinstance(updated, SignalRecord)
    assert updated.last_pushed_at == now
    assert updated.push_gate == "cooldown"
    assert updated.push_dedupe_cooldown_until == now + timedelta(minutes=PUSH_DEDUPE_COOLDOWN_MIN)
    assert PUSH_DEDUPE_COOLDOWN_MIN == 30

    lines = _read_alerts(tmp_path)
    assert len(lines) == 1
    assert lines[0]["signal_id"] == rec.signal_id
    assert lines[0]["status"] == "publishable"


def test_push_payload_has_disclaimer_and_event_type(push_on, tmp_path):
    """合规脚注与事件类型必须原样出现在两个通道的载荷里。"""
    bus = FakeBus()
    rec = _published()

    try_push(rec, quote_service=bus, data_dir=tmp_path)

    sse_payload = bus.flat[0]
    inbox_payload = _read_alerts(tmp_path)[0]
    for payload in (sse_payload, inbox_payload):
        assert payload["disclaimer"] == DISCLAIMER_TEXT
        assert payload["monitor_event_type"] == MONITOR_EVENT_TYPE
        assert payload["schema_version"] == PUSH_PAYLOAD_SCHEMA_VERSION
        assert payload["source"] == "buy_sell_signals"
        assert payload["signal_id"] == rec.signal_id
        assert rec.display_name in payload["message"]

    # 站内事件带 ts(毫秒) —— alert_store 既有约定
    assert isinstance(inbox_payload["ts"], int)
    assert inbox_payload["ts"] > 1_600_000_000_000


def test_push_without_quote_service_still_writes_inbox(push_on, tmp_path):
    """只给 data_dir 也允许推: 站内是真实通道, 不能因为缺 SSE 就整条不推。"""
    rec = _published()
    result = try_push(rec, quote_service=None, data_dir=tmp_path)
    assert result.ok is True
    assert result.inbox_sent is True
    assert result.sse_sent is False
    assert len(_read_alerts(tmp_path)) == 1


def test_push_writes_store_when_given(push_on, tmp_path):
    """传入 store 时, 推送后的记录会 upsert 进去(而不是只留在返回值里)。"""
    from app.buy_sell_signals import InMemorySignalRegistryStore

    store = InMemorySignalRegistryStore()
    rec = _published()
    store.upsert(rec)
    bus = FakeBus()

    result = try_push(rec, quote_service=bus, data_dir=tmp_path, store=store)

    assert result.ok is True
    stored = store.get(rec.signal_id, rec.version)
    assert stored is not None
    assert stored.push_gate == "cooldown"
    assert stored.last_pushed_at is not None


# ================================================================ 去重 / 冷却


def test_push_same_status_does_not_repeat(push_on, tmp_path):
    """同状态重算不推: 第二次调用必须被"状态未变化"拦下, 且不刷新冷却字段。"""
    bus = FakeBus()
    first = try_push(_published(), quote_service=bus, data_dir=tmp_path)
    assert first.ok is True
    after_first = first.record
    assert after_first is not None

    # 冷却过点 + 闸门拨回 armed, 唯一剩下的拒推理由就是"状态未变化"。
    later = after_first.last_pushed_at + timedelta(minutes=PUSH_DEDUPE_COOLDOWN_MIN + 1)
    revived = after_first.with_fields(push_gate="armed")
    second = try_push(revived, quote_service=bus, data_dir=tmp_path, now=later)

    assert second.ok is False
    assert "状态未变化" in second.reason
    assert bus.count == 1, "同状态不得再扇出"
    assert second.record is None


def test_push_blocked_during_cooldown(push_on, tmp_path):
    """冷却期内阻断: 即使把闸门拨回 armed 也不行 —— 派生 push_eligible 会拦。"""
    bus = FakeBus()
    first = try_push(_published(), quote_service=bus, data_dir=tmp_path)
    assert first.ok is True
    cooled = first.record
    assert cooled is not None

    # 制造一次新的状态变化 + 把闸门拨回 armed, 排除另外两条理由, 单看冷却拦不拦得住。
    changed = cooled.with_fields(last_status_change_at=cn_now(), push_gate="armed")
    during = try_push(changed, quote_service=bus, data_dir=tmp_path)

    assert during.ok is False
    assert "冷却中" in during.reason
    assert bus.count == 1


def test_push_cooldown_expired_alone_does_not_repush(push_on, tmp_path):
    """冷却期满 + 恢复 armed, 但状态没变 → 仍不推。**"同状态不推"优先。**"""
    bus = FakeBus()
    rec = _published()
    now = cn_now()

    first = try_push(rec, quote_service=bus, data_dir=tmp_path, now=now)
    assert first.ok is True
    cooled = first.record
    assert cooled is not None

    later = now + timedelta(minutes=PUSH_DEDUPE_COOLDOWN_MIN + 5)
    second = try_push(cooled, quote_service=bus, data_dir=tmp_path, now=later)

    assert second.ok is False
    assert "状态未变化" in second.reason
    assert "冷却" not in second.reason, "冷却已过, 不该再报冷却"
    assert bus.count == 1


def test_push_after_status_change_past_cooldown(push_on, tmp_path):
    """冷却已过 + 有新状态变化 → 入口先恢复 armed, 再推第二次。"""
    bus = FakeBus()
    now = cn_now()
    first = try_push(_published(), quote_service=bus, data_dir=tmp_path, now=now)
    assert first.ok is True
    cooled = first.record
    assert cooled is not None
    assert cooled.push_gate == "cooldown"

    later = now + timedelta(minutes=PUSH_DEDUPE_COOLDOWN_MIN + 5)
    # 真实场景里这次变化来自 transition(); 这里直接伪造时间戳, 避免绕一大圈状态。
    revived = cooled.with_fields(last_status_change_at=later, updated_at=later)

    second = try_push(revived, quote_service=bus, data_dir=tmp_path, now=later)

    assert second.ok is True, second.reason
    assert bus.count == 2
    assert second.record is not None
    assert second.record.last_pushed_at == later
    assert second.record.push_gate == "cooldown"
    assert len(_read_alerts(tmp_path)) == 2


def test_push_restores_armed_only_at_entry(push_on, tmp_path):
    """冷却期满的 armed 恢复只发生在 try_push 入口, 不改原记录(纯函数语义)。"""
    now = cn_now()
    cooled = _published().with_fields(
        push_gate="cooldown",
        last_pushed_at=now - timedelta(hours=2),
        push_dedupe_cooldown_until=now - timedelta(minutes=1),
        last_status_change_at=now,
    )
    assert cooled.push_gate == "cooldown"

    bus = FakeBus()
    result = try_push(cooled, quote_service=bus, data_dir=tmp_path, now=now)

    assert result.ok is True
    assert cooled.push_gate == "cooldown", "原记录是 frozen 的, 入口恢复不得就地修改"
    assert result.record is not None
    assert result.record.last_pushed_at == now


# ================================================================ 拒推


def test_push_rejects_limit_reopen_and_non_publishable(push_on, tmp_path):
    """炸板、观察中、闸门未 armed —— 三类都拒, 且都不产生任何副作用。"""
    bus = FakeBus()

    reopen = _base_record(signal_id="sig.reopen", event_class="limit_reopen")
    assert reopen.status == "observe"
    result = try_push(reopen, quote_service=bus, data_dir=tmp_path)
    assert result.ok is False
    assert "limit_reopen" in result.reason

    watching = transition(_base_record(signal_id="sig.w"), "observe")
    assert try_push(watching, quote_service=bus, data_dir=tmp_path).ok is False

    # publishable 必须有人签过字(models 会校验), 所以这里给一个"签过但闸门没拨"的记录。
    stuck = _published(signal_id="sig.p").with_fields(push_gate="manual_pending")
    result = try_push(stuck, quote_service=bus, data_dir=tmp_path)
    assert result.ok is False
    assert "armed" in result.reason

    assert bus.count == 0
    assert _read_alerts(tmp_path) == []


def test_evaluate_push_reasons_are_distinct(push_on):
    """`evaluate_push` 是纯函数: 每种不合格各有一条可区分的原因。"""
    reopen = _base_record(event_class="limit_reopen")
    assert "limit_reopen" in push_module.evaluate_push(reopen)

    watching = transition(_base_record(signal_id="sig.e1"), "observe")
    assert "publishable" in push_module.evaluate_push(watching)

    blocked = _published(signal_id="sig.e2").with_fields(push_gate="blocked")
    assert "armed" in push_module.evaluate_push(blocked)

    assert push_module.evaluate_push(_published(signal_id="sig.e3")) == ""


# ================================================================ 失败可见


def test_push_fanout_failure_does_not_write_cooldown(push_on, tmp_path):
    """扇出抛错 → 不写 last_pushed_at / 冷却。宁可下次重推, 也不要有"标记已推但没人收到"。"""
    result = try_push(_published(), quote_service=ExplodingBus(), data_dir=tmp_path)

    assert result.ok is False
    assert "扇出失败" in result.reason
    assert result.record is None
    assert result.inbox_sent is True, "站内先写, 已经写成功了"


def test_push_error_hierarchy():
    """异常类型的层次: 都归到 PushError 之下, 方便调用方一把抓。"""
    assert issubclass(push_module.PushDisabled, push_module.PushError)
    assert issubclass(push_module.PushRejected, push_module.PushError)
    assert push_module.PushRejected("x").reason == "x"


# ================================================================ 静态: 无飞书 / 无提醒层耦合


FORBIDDEN_SYMBOLS = (
    "send_feishu",
    "_maybe_send_webhook",
    "webhook_adapter",
    "T_COOLDOWN_MIN",
    "VWAP_BAND",
    "RANGE_BAND",
    "trade_marks",
    "news.push",
    "sse_starlette",
    "EventSourceResponse",
)


@pytest.mark.parametrize("symbol", FORBIDDEN_SYMBOLS)
def test_push_module_has_no_forbidden_symbol(symbol):
    """`push.py` 里连**标识符**都不许出现这些名字。

    注意这是按 AST 的 Name / Attribute / import 查, 不是全文匹配 —— docstring 里
    写"本模块不调 send_feishu"是合规说明, 不该被这条罚。
    """
    tree = ast.parse((PKG_DIR / "push.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attrs.add(node.func.attr)

    assert symbol not in names | attrs, f"push.py 出现禁用标识符 {symbol}"
    assert not any(symbol in module for module in modules), f"push.py import 了 {symbol}"


def test_push_module_only_imports_allowed_app_modules():
    """`push.py` 对 app 层的依赖只允许这几个: 明确白名单, 新依赖必须过评审。"""
    # 绝对导入的白名单。相对导入(`from .x import y`)单独处理: 它们只能指向本包,
    # 不构成跨包依赖。
    allowed = {
        "app.market_time",
        "app.services.alert_store",
        # `from app.services import alert_store` 会同时绑 app.services 这个包名,
        # 但它只能取到包内的 alert_store, 上一条白名单已经钉死了取谁。
        "app.services",
        # gding: 钉钉支路。只允许**发送函数**与**既有 settings**这两样:
        #   - `app.news.dingtalk` 是既有机器人实现, 本包只调 send_markdown
        #   - `app.config` 只为读 dingtalk_webhook_url / dingtalk_secret(既有字段)
        # `app.news.push` 仍禁 —— 那是资讯业务逻辑, 会顺带拖进 BAND / cooldown。
        "app.news.dingtalk",
        "app.config",
    }
    allowed_relative = {
        "",  # from . import disclaimer as _disclaimer
        "constants",
        "models",
        "state_machine",
        "store",
    }
    tree = ast.parse((PKG_DIR / "push.py").read_text(encoding="utf-8"))
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0:
                relative.add(node.module or "")
            elif node.module:
                absolute.add(node.module)
        elif isinstance(node, ast.Import):
            absolute.update(alias.name for alias in node.names)

    for module in absolute:
        assert not module.startswith("app.") or module in allowed, f"push.py 引入了未申报依赖 {module}"
    for module in relative:
        assert module in allowed_relative, f"push.py 引入了未申报的包内模块 {module!r}"


def test_push_uses_only_public_push_alerts():
    """对 quote_service 只调公开的 `push_alerts`, 不摸私有方法。"""
    source = (PKG_DIR / "push.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "push_alerts" in calls
    assert "_broadcast_alerts" not in calls
