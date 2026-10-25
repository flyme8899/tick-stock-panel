"""Tier A 开放契约快照 — 开放面的任何变化必须是有意识的。

契约即承诺: /api/openapi.json?tier=a 列出的端点集合是二开方依赖的稳定面。
本测试把该集合冻结成常量 — 新增/移除/改方法都会失败, 失败者须同步更新
快照并在 PR 里说明契约变更 (对外破坏性变更需 major 版本)。

快照来源与 /api/openapi.json?tier=a 完全同源: app.openapi() 的 paths
逐 (method, path) 过 api_gateway.required_scope — 规则表是唯一源。

契约变更记录:
  - 2026-10 (P3): 移除全部 /api/paper* (20 条)。模拟盘读+写整体退出 Token 开放面,
    仅面板 UI 会话可用。持有历史 paper:trade scope 的 Token 亦不再获得访问权。
"""
from __future__ import annotations

import pytest

from app.services import api_gateway

# 冻结的开放面 (method + OpenAPI 路径模板), 按字母序。
# 变更此表 = 契约变更, 请在 commit message 与 changelog 中明示。
EXPECTED_OPEN_ENDPOINTS: set[str] = {
    "GET /api/alerts",
    "GET /api/backtest/candidates",
    "POST /api/backtest/factor/batch",
    "GET /api/backtest/factor/columns",
    "POST /api/backtest/factor/run",
    "POST /api/backtest/run",
    "GET /api/backtest/status",
    "POST /api/backtest/strategy/run",
    "GET /api/events",
    "POST /api/events/ticket",
    "GET /api/ext-data",
    "POST /api/ext-data/{config_id}/ingest",
    "GET /api/ext-data/{config_id}/rows",
    "GET /api/ext-data/{config_id}/values",
    "GET /api/ext-data/schema-all",
    "GET /api/ext-data/schema/{config_id}",
    "GET /api/ext-data/{config_id}/dimension-intraday",
    "GET /api/ext-data/{config_id}/dimension-members",
    "GET /api/fund-flow/board",
    "GET /api/fund-flow/etf-shares",
    "GET /api/fund-flow/health",
    "GET /api/fund-flow/intraday",
    "GET /api/fund-flow/margin",
    "GET /api/fund-flow/sectors",
    "GET /api/fund-flow/stock/{symbol}",
    "GET /api/index/daily",
    "GET /api/index/minute",
    "GET /api/intraday/indices",
    "GET /api/intraday/status",
    "GET /api/kline/daily",
    "GET /api/kline/daily/latest",
    "GET /api/kline/instruments/search",
    "GET /api/kline/minute",
    "GET /api/kline/minute-range",
    "GET /api/news/hot",
    "GET /api/news/messages",
    "GET /api/news/stocks/{symbol}",
    "GET /api/overview/market",
    "GET /api/regime/breadth",
    "GET /api/regime/breadth/latest",
    "GET /api/regime/caliber",
    "GET /api/regime/coverage",
    "GET /api/regime/history",
    "GET /api/regime/latest",
    "GET /api/regime/mainline",
    "GET /api/regime/phases",
    "GET /api/regime/states",
    "GET /api/screener/cached",
    "GET /api/screener/limit-ladder",
    "GET /api/screener/market-snapshot",
    "POST /api/screener/run",
    "POST /api/screener/run_all",
    "POST /api/screener/run_preset",
    "GET /api/screener/strategies",
    "GET /api/strategies",
    "GET /api/strategies/ai/status",
    "GET /api/strategies/{strategy_id}",
    "GET /api/strategies/{strategy_id}/source",
}


def _collect_open_endpoints() -> set[str]:
    """与 /api/openapi.json?tier=a 同源收集 (app.openapi paths 过 required_scope)。"""
    from app.main import app

    spec = app.openapi()
    found: set[str] = set()
    for path, ops in spec.get("paths", {}).items():
        for method in ops:
            if method in ("get", "post", "put", "delete", "patch") and api_gateway.required_scope(
                method.upper(), path,
            ):
                found.add(f"{method.upper()} {path}")
    return found


def test_open_contract_snapshot():
    found = _collect_open_endpoints()
    added = found - EXPECTED_OPEN_ENDPOINTS
    removed = EXPECTED_OPEN_ENDPOINTS - found
    if added or removed:
        pytest.fail(
            "Tier A 开放契约发生变化 — 这是对外承诺面, 须有意识地更新快照:\n"
            f"  新增: {sorted(added) or '无'}\n"
            f"  移除: {sorted(removed) or '无'}\n"
            "确认无误后, 同步更新本文件的 EXPECTED_OPEN_ENDPOINTS 并在 commit 中说明。",
        )


def test_open_contract_excludes_paper():
    """P3 收口: Tier A 开放面不得出现任何 /api/paper* 端点。

    与快照测试互补 — 快照整体比对失败时只报"新增/移除"集合, 这条把
    "模拟盘绝不能进开放面"固定成独立断言, 避免有人把 paper 端点和别的
    变更一起塞进快照后蒙过去。
    """
    from app.main import app

    spec = app.openapi()
    leaked = sorted(
        f"{m.upper()} {p}"
        for p, ops in spec.get("paths", {}).items()
        if p.startswith("/api/paper")
        for m in ops
        if m in ("get", "post", "put", "delete", "patch")
        and api_gateway.required_scope(m.upper(), p)
    )
    assert leaked == [], f"模拟盘端点不得对 Token 开放: {leaked}"
    # 反向自证: 该 path 集合在 OpenAPI 里确实存在 (不是因为没有路由才空)
    assert any(p.startswith("/api/paper") for p in spec.get("paths", {})), (
        "OpenAPI 里找不到 /api/paper* — 断言失去意义, 请检查路由是否改名"
    )


def test_paper_routes_exist_but_gateway_does_not_open_them():
    """双向断言: 路由在, 网关不放行。

    上一版只看"开放面里没有 paper" —— 如果 required_scope 整体失效 (返回 None),
    所有端点都会"不在开放面", 上一条断言照样绿而契约测试同时红。这条把两侧分开验:
      a) app.routes 里 /api/paper* 路由确实存在 (产品没被砍)
      b) 网关对每条 paper 路由的每个方法都返回 None (没开放)
    """
    from app.main import app

    paper_routes = [
        r for r in app.routes
        if getattr(r, "path", "").startswith("/api/paper")
    ]
    assert paper_routes, "模拟盘路由整体消失 — 产品边界变了, 需人工确认"

    opened = []
    for r in paper_routes:
        methods = getattr(r, "methods", None) or set()
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            if api_gateway.required_scope(m, r.path) is not None:
                opened.append(f"{m} {r.path}")
    assert opened == [], f"网关仍对模拟盘放行: {opened}"

    # 反面自证: required_scope 本身没坏死 (对照一条已知开放端点)
    assert api_gateway.required_scope("GET", "/api/kline/daily") == "read:market"
