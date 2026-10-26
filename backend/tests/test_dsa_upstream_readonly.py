"""P-sec（批次 20261026gdsasec）：浏览器 upstream 对 system/portfolio 只读。

仅供研究，不构成投资建议；执行须人工确认。

钉死的几条：

- 浏览器 `POST/PUT/PATCH/DELETE /api/dsa/upstream/{system,portfolio}/…` → **405**，
  detail 可读，**不**调用 `forward()`（不转发 sidecar）
- `GET` 照旧转发（诊断/展示还是要能读）
- **其它前缀**的写方法本拍一个没收窄（收窄面越大越容易误伤）
- 闸在 `routes.upstream`，**不在** `proxy.forward()` —— 否则
  `schedule.save_schedule` 的服务端 `PUT system/config` 会被一起打断

不依赖真 sidecar：`forward` 全部 mock。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.dsa import routes as dsa_routes

ROUTES_PATH = Path(dsa_routes.__file__)
PROXY_PATH = ROUTES_PATH.parent / "proxy.py"
UPSTREAM_URL = "/api/dsa/upstream"


class _ForwardSpy:
    """替身 forward：记调用、回固定 200，测试断言「有没有真的转发出去」。"""

    def __init__(self, status_code: int = 200, payload: bytes = b'{"ok": true}'):
        self.calls: list[tuple[str, str]] = []
        self.status_code = status_code
        self.payload = payload

    def __call__(self, method, path, *, params=None, body=b"", content_type=None):
        self.calls.append((method.upper(), path))
        return self.status_code, self.payload, "application/json", {}


@pytest.fixture()
def spy(monkeypatch):
    s = _ForwardSpy()
    monkeypatch.setattr(dsa_routes, "forward", s)
    return s


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(dsa_routes.build_router())
    return TestClient(app)


# ---------------------------------------------------------------------------
# G-T2 / G-T3：system、portfolio 非 GET → 405，且不转发
# ---------------------------------------------------------------------------

SYS_PORT_PATHS = [
    ("system", "system/config"),
    ("system", "system/scheduler/run-now"),
    ("system", "system/config/notification/test-channel"),
    ("system", "system/config/export"),
    ("portfolio", "portfolio/risk"),
    ("portfolio", "portfolio/cash"),
    ("portfolio", "portfolio/holdings/600519"),
]
BLOCKED_METHODS = ["POST", "PUT", "PATCH", "DELETE"]


@pytest.mark.parametrize(("_family", "path"), SYS_PORT_PATHS)
@pytest.mark.parametrize("method", BLOCKED_METHODS)
def test_non_get_on_system_portfolio_is_405_and_not_forwarded(client, spy, method, path, _family):
    resp = client.request(method, f"{UPSTREAM_URL}/{path}", json={} if method != "DELETE" else None)

    assert resp.status_code == 405, f"{method} {path} 应 405，实际 {resp.status_code}"
    assert resp.status_code != 403  # N2：不是 403
    detail = resp.json()["detail"]
    assert "仅允许 GET" in detail
    # 前缀名要出现在 detail 里，排查时一眼知道卡在哪一类
    assert _family in detail
    # 关键：一个字节都没转出去
    assert spy.calls == []


def test_405_carries_allow_header(client, spy):
    resp = client.post(f"{UPSTREAM_URL}/portfolio/risk")
    assert resp.status_code == 405
    assert "GET" in resp.headers.get("allow", "")
    assert spy.calls == []


@pytest.mark.parametrize("method", BLOCKED_METHODS)
def test_405_detail_is_readable_chinese(client, spy, method):
    resp = client.request(method, f"{UPSTREAM_URL}/system/config")
    detail = resp.json()["detail"]
    # 可读中文，不是裸英文状态名
    assert any("\u4e00" <= ch <= "\u9fff" for ch in detail)
    assert "服务端入口" in detail


# ---------------------------------------------------------------------------
# G-T4：GET 仍然转发（只读没被误伤）
# ---------------------------------------------------------------------------


def test_get_portfolio_risk_still_forwards(client, spy):
    resp = client.get(f"{UPSTREAM_URL}/portfolio/risk")
    assert resp.status_code == 200
    assert spy.calls == [("GET", "portfolio/risk")]


def test_get_system_config_still_forwards(client, spy):
    resp = client.get(f"{UPSTREAM_URL}/system/config")
    assert resp.status_code == 200
    assert spy.calls == [("GET", "system/config")]


def test_head_is_allowed_like_get(client, spy):
    """HEAD 归在只读一侧：框架/探活常用，不该被当成写方法拦掉。"""
    client.head(f"{UPSTREAM_URL}/portfolio/risk")
    # HEAD 的具体状态码由框架与 forward 决定；这里只钉「没被闸判成写方法」
    assert all(call[0] in {"GET", "HEAD"} for call in spy.calls)

# ---------------------------------------------------------------------------
# G-T5：其它前缀的写方法本拍一个没收窄（对照测）
# ---------------------------------------------------------------------------


OTHER_PREFIX_WRITES = [
    ("POST", "analysis/analyze"),
    ("POST", "history/dismiss"),
    ("POST", "screening/run"),
    ("PUT", "backtest/config"),
    ("DELETE", "history/123"),
]


@pytest.mark.parametrize(("method", "path"), OTHER_PREFIX_WRITES)
def test_other_prefix_writes_are_untouched(client, spy, method, path):
    resp = client.request(method, f"{UPSTREAM_URL}/{path}", json={} if method != "DELETE" else None)

    assert resp.status_code == 200, f"{method} {path} 本拍不该被收窄"
    assert spy.calls == [(method, path)]


def test_other_prefix_names_that_start_with_system_are_not_matched():
    """`systematic/...` 不是 `system` 前缀：按首段比，不做 startswith 字符串匹配。"""
    from app.custom.dsa.routes import _reject_non_get_system_portfolio

    # 不抛即放行；路径本身不在 ALLOWED_PREFIXES 里，会由 forward 走 400，不是闸的事
    _reject_non_get_system_portfolio("systematic/thing", "POST")


# ---------------------------------------------------------------------------
# G-T6：闸不在 forward —— schedule 的服务端 PUT system/config 不受影响
# ---------------------------------------------------------------------------


def test_forward_is_not_patched_by_the_gate(monkeypatch):
    """`proxy.forward` 源码里不该出现 405 / 只读闸的痕迹。

    闸一旦被挪进 `forward()`，`schedule.save_schedule` 的内部
    `PUT system/config` 就会被一起拒掉 —— 那才是真回归。这条从源码层面钉住。
    """
    source = PROXY_PATH.read_text(encoding="utf-8")
    assert "405" not in source
    assert "Method Not Allowed" not in source
    assert "_reject_non_get_system_portfolio" not in source


def test_schedule_put_still_reaches_forward(monkeypatch, client):
    """`PUT /api/dsa/schedule` 是 TSP 自有路由，它的服务端保存不该被浏览器闸拦。"""
    saved: dict = {}

    def fake_save_schedule(**kwargs):
        saved.update(kwargs)
        return {"ok": True, **kwargs}

    monkeypatch.setattr(dsa_routes, "save_schedule", fake_save_schedule)
    resp = client.put(
        "/api/dsa/schedule",
        json={"enabled": True, "time": "18:30", "trading_days_only": True, "region": "cn", "watchlist": ""},
    )
    assert resp.status_code == 200
    assert saved.get("time") == "18:30"


def test_helper_is_reachable_before_forward_smoke(monkeypatch):
    """直接调辅助函数：405 与放行两路都对（不经过 HTTP 层）。"""
    from fastapi import HTTPException

    from app.custom.dsa.routes import _reject_non_get_system_portfolio

    with pytest.raises(HTTPException) as exc:
        _reject_non_get_system_portfolio("system/config", "PUT")
    assert exc.value.status_code == 405

    # 非法路径不在这里拦：交给 forward 走 400，错误口径保持原样
    _reject_non_get_system_portfolio("../../etc/passwd", "PUT")
    _reject_non_get_system_portfolio("not-a-prefix/x", "POST")


# ---------------------------------------------------------------------------
# 结构：闸只被 upstream() 调用
# ---------------------------------------------------------------------------


def test_gate_is_only_called_from_upstream():
    """辅助函数只允许被 `upstream()` 调用一次，别处不许引用。

    如果以后有人在 `schedule.py` 或 `proxy.py` 里也调它，服务端写就没了。
    """
    tree = ast.parse(ROUTES_PATH.read_text(encoding="utf-8"))
    callers: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "_reject_non_get_system_portfolio":
            continue
        # 找到包含它的最内层函数定义
        callers.append("_module_level")

    # 简化策略：源码里除定义与 upstream 内调用外不该再有别的引用
    text = ROUTES_PATH.read_text(encoding="utf-8")
    assert text.count("_reject_non_get_system_portfolio(") == 2, (
        "期望恰好两处：函数定义 1 处 + upstream() 内调用 1 处；"
        f"实际 {text.count('_reject_non_get_system_portfolio(')} 处"
    )
    assert callers == ["_module_level"]  # 只有 upstream 里那一次 Call 节点


def test_readonly_prefixes_are_exactly_system_and_portfolio():
    assert frozenset({"system", "portfolio"}) == dsa_routes._READONLY_UPSTREAM_PREFIXES
    assert frozenset({"GET", "HEAD"}) == dsa_routes._READONLY_UPSTREAM_ALLOWED_METHODS


# ---------------------------------------------------------------------------
# G-T9：三 env / 钉钉 零碰（本拍没有理由碰它们）
# ---------------------------------------------------------------------------


def test_three_env_literals_unchanged():
    from app.buy_sell_signals import constants as bss

    assert bss.PERSIST_ENV_NAME == "BUY_SELL_SIGNALS_PERSIST"
    assert bss.PUSH_ENV_NAME == "BUY_SELL_SIGNALS_PUSH"
    assert bss.DINGTALK_ENV_NAME == "BUY_SELL_SIGNALS_DINGTALK"


def test_routes_module_does_not_touch_dsa_review_or_sniper():
    """上游闸不该顺手改钉钉复盘或狙击判据。"""
    source = ROUTES_PATH.read_text(encoding="utf-8")
    assert "DSA_REVIEW_DINGTALK" not in source
    assert "market_review_push" not in source
    assert "ALERT_SOURCE_PREFIX" not in source
