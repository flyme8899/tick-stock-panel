"""熔断状态的跨进程持久化。

回归的问题是: ``open_until`` 建在 ``time.monotonic()`` 上 —— 进程重启后从 0 重新
计数 —— 却被原样写盘、重启后原样读回。容器重启一次, 本来 10 分钟的冷却就变成
「重启前已运行了多久」, 实测可达数天, 这段时间该数据源等于被永久拉黑。

修法: 落盘改存「剩余冷却秒数」, 恢复时按当前时钟重算。
"""
from __future__ import annotations

from app.fund_flow.circuit import COOLDOWN_SECONDS, CircuitBreaker


def _tripped(source: str, now: float, *, cooldown_s: float = COOLDOWN_SECONDS) -> CircuitBreaker:
    breaker = CircuitBreaker(source, clock=lambda: now, cooldown_s=cooldown_s)
    for _ in range(breaker.failure_limit):
        breaker.failure()
    return breaker


def test_snapshot_records_remaining_cooldown() -> None:
    breaker = _tripped("akshare", 432000.0)
    snap = breaker.snapshot()
    assert snap["state"] == "open"
    assert snap["cooldown_remaining_s"] == COOLDOWN_SECONDS


def test_restore_after_restart_keeps_cooldown_not_elapsed_uptime() -> None:
    """运行 5 天后熔断, 重启 (monotonic 归零) 仍只等一个完整冷却期。"""
    snap = _tripped("akshare", 432000.0).snapshot()
    restarted = CircuitBreaker("akshare", clock=lambda: 5.0)
    restarted.restore(snap)
    assert restarted.allow() is False
    assert restarted.open_until == 5.0 + COOLDOWN_SECONDS
    # 关键: 不是 432600 - 5.0 那种「几天」量级
    assert restarted.open_until < 3600


def test_restore_accounts_for_elapsed_cooldown() -> None:
    """冷却已经过了一半再重启, 只剩一半。"""
    clock = {"now": 432000.0}
    breaker = CircuitBreaker("akshare", clock=lambda: clock["now"])
    for _ in range(breaker.failure_limit):
        breaker.failure()
    clock["now"] += 300.0
    restarted = CircuitBreaker("akshare", clock=lambda: 1.0)
    restarted.restore(breaker.snapshot())
    assert restarted.open_until == 1.0 + 300.0


def test_restore_when_cooldown_already_expired() -> None:
    clock = {"now": 1000.0}
    breaker = CircuitBreaker("akshare", clock=lambda: clock["now"])
    for _ in range(breaker.failure_limit):
        breaker.failure()
    clock["now"] += COOLDOWN_SECONDS + 1
    restarted = CircuitBreaker("akshare", clock=lambda: 1.0)
    restarted.restore(breaker.snapshot())
    assert restarted.allow() is True


def test_restore_keeps_failure_counter() -> None:
    """冷却期间累计的失败次数要带过去, 否则重启会重置计数。"""
    breaker = CircuitBreaker("akshare", clock=lambda: 10.0, failure_limit=8)
    breaker.failure()
    breaker.failure()
    snap = breaker.snapshot()
    assert snap["consecutive_failures"] == 2
    restarted = CircuitBreaker("akshare", clock=lambda: 1.0)
    restarted.restore(snap)
    assert restarted.consecutive_failures == 2


def test_legacy_snapshot_without_remaining_is_treated_as_expired() -> None:
    """旧状态文件只有 monotonic 基准的 open_until, 无法判断有效期。

    当成有效值会白等几天, 当成过期只是提前重试一次 (失败会重新熔断), 取后者。
    """
    restarted = CircuitBreaker("akshare", clock=lambda: 2.0)
    restarted.restore({"consecutive_failures": 3, "open_until": 432600.0, "state": "open"})
    assert restarted.allow() is True
    assert restarted.consecutive_failures == 3


def test_restore_tolerates_garbage() -> None:
    restarted = CircuitBreaker("akshare", clock=lambda: 2.0)
    restarted.restore({"consecutive_failures": "x", "cooldown_remaining_s": "y"})
    assert restarted.consecutive_failures == 0
    assert restarted.allow() is True


def test_roundtrip_through_real_clock_matches_behaviour() -> None:
    """用真实 monotonic 走一遍: 快照 → 新实例恢复 → 冷却仍然生效。"""
    import time

    breaker = CircuitBreaker("akshare")
    for _ in range(breaker.failure_limit):
        breaker.failure()
    snap = breaker.snapshot()
    assert snap["state"] == "open"

    fresh = CircuitBreaker("akshare")
    fresh.restore(snap)
    assert fresh.allow() is False
    # 剩余时间在 (0, cooldown] 之间, 不会因为跨实例被放大
    assert 0 < fresh.open_until - time.monotonic() <= COOLDOWN_SECONDS
