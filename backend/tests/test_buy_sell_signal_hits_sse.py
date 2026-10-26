"""SSE 分类字段补充(批次 20261026gbsshits · P-sse)。

改动范围**只有** `build_sse_alert` 的 keep 元组: 加上 `family` / `event_class`。
这里逐条锁住, 因为"顺带多留几个字段"和"顺带少留几个字段"都很容易发生:

- 现有的 keep 字段一个都不能丢(订阅端可能正在用);
- 新增的只有 `family` / `event_class`;
- 定义状态推送**仍然没有 symbol** —— 这条是本拍最容易被顺手破坏的边界。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import inspect

from app.buy_sell_signals.push import build_push_payload, build_sse_alert

#: 改动前的 keep 字段, 一个都不许掉。
PRE_EXISTING_KEEP = (
    "source",
    "type",
    "monitor_event_type",
    "severity",
    "message",
    "disclaimer",
    "signal_id",
    "version",
    "display_name",
    "status",
    "push_gate",
    "schema_version",
    "namespace",
)


def _record(**overrides):
    from app.buy_sell_signals.models import SignalRecord

    kwargs = {
        "signal_id": "sig.sse",
        "display_name": "SSE 夹具信号",
        "family": "minute_event",
        "event_class": "intraday_volume",
        "spec_ref": "fixture://spec",
    }
    kwargs.update(overrides)
    return SignalRecord(**kwargs)


def test_sse_alert_keeps_family_and_event_class():
    payload = build_push_payload(_record())
    alert = build_sse_alert(payload)
    assert alert["family"] == "minute_event"
    assert alert["event_class"] == "intraday_volume"


def test_sse_alert_keeps_every_pre_existing_field():
    """原有的 13 个字段一个都不能少 —— 补字段不该顺手丢字段。"""
    payload = build_push_payload(_record())
    alert = build_sse_alert(payload)
    missing = [k for k in PRE_EXISTING_KEEP if k not in alert]
    assert not missing, f"SSE keep 丢了既有字段: {missing}"


def test_sse_alert_drops_nothing_silently_because_payload_lost_a_key():
    """载荷缺某个 key 时静默跳过(既有行为), 但已有 key 必须被带出。"""
    alert = build_sse_alert({"source": "buy_sell_signals", "family": "daily_fundamental"})
    assert alert == {"source": "buy_sell_signals", "family": "daily_fundamental"}


def test_definition_status_push_still_has_no_symbol():
    """定义状态事件**不带** symbol。

    命中层才有 symbol; 定义状态是「这条信号上线/下线了」, 与具体个股无关。
    给这条推送补 symbol, 等于让订阅端以为"有票命中了"。
    """
    payload = build_push_payload(_record())
    assert "symbol" not in payload
    assert "symbol" not in build_sse_alert(payload)


def test_sse_keep_does_not_widen_beyond_the_two_new_fields():
    """keep 元组只该比改动前多 `family` / `event_class` 两项。

    用 AST 读源码而不是运行时取值: 元组长度是常量, 直接比集合最稳。
    """
    src = inspect.getsource(build_sse_alert)
    assert '"family"' in src
    assert '"event_class"' in src

    payload = build_push_payload(_record())
    kept = set(build_sse_alert(payload))
    expected = set(PRE_EXISTING_KEEP) | {"family", "event_class"}
    assert kept == expected, f"keep 集合发生变化: 多出 {kept - expected}, 少了 {expected - kept}"


def test_spec_ref_still_dropped_from_sse():
    """`spec_ref` 这类长字符串仍然不进 SSE —— 队列容量有限。"""
    alert = build_sse_alert(build_push_payload(_record()))
    assert "spec_ref" not in alert
