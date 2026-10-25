"""P3 收口: Token 通道只读默开, 交易类 scope 全线拒绝 (签发 + 网关双侧)。

本文件对应任务书 G-T1~G-T9 中"跨模块"的语义, 与既有 test_api_gateway.py
(网关 401/403/429 矩阵) 和 test_openapi_contract.py (Tier A 契约) 分工:

  G-T1  签发侧: create_token 拒绝 paper:trade 及 trade:*/order:* 前缀
  G-T3  网关侧: 只读 Token 打 /api/paper/orders → 403
  G-T4  存量兼容: 手工塞进文件的 paper:trade 历史记录, Bearer 仍 403 (fail-closed)
  G-T6  UI 会话路径 (无 Bearer) 不被 Token 桶影响, 模拟盘读写照常
  G-T9  文档口径: 不得出现"Token 可模拟下单 / 信号可做"这类表述

设计说明: 这里刻意用**手工写 api_tokens.json** 的方式造历史记录, 而不是调
create_token — 因为 create_token 已经签不出 paper:trade 了, 只有伪造存量才能
证明"拒绝来自网关规则剥离, 而不是仅仅来自签发门"。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.services import api_gateway, api_tokens

# 推荐的只读默认组合 (G-T2 / R5)
READONLY_SCOPES = ["read:market", "read:analysis", "read:ext"]


# ── G-T1: 签发侧拒绝交易类 scope ──────────────────────────
@pytest.mark.parametrize("scope", [
    "paper:trade",
    "paper:read",
    "trade:market",
    "trade:submit",
    "order:create",
])
def test_create_token_rejects_trading_scopes(tmp_path: Path, scope: str):
    """任何交易类 scope 都不得被签发 — 精确名与前缀两种命中方式都要兜住。"""
    with pytest.raises(ValueError, match="不可用于 Token 通道"):
        api_tokens.create_token(tmp_path, "越权", [scope])
    # 拒绝时不得留下任何记录 (不能"先写盘再报错")
    assert api_tokens.list_tokens(tmp_path) == []


def test_create_token_rejects_mixed_list_atomically(tmp_path: Path):
    """合法 + 非法混选时整体拒绝, 不静默丢弃非法项。"""
    with pytest.raises(ValueError, match="不可用于 Token 通道"):
        api_tokens.create_token(tmp_path, "混选", ["read:market", "paper:trade"])
    assert api_tokens.list_tokens(tmp_path) == []


def test_scopes_catalog_has_no_trading_scope():
    """SCOPES 是签发白名单 — 里面不能出现任何交易类 scope。"""
    assert "paper:trade" not in api_tokens.SCOPES
    leaked = [s for s in api_tokens.SCOPES if s in api_tokens.FORBIDDEN_SCOPES
              or any(s.startswith(p) for p in api_tokens.FORBIDDEN_SCOPE_PREFIXES)]
    assert leaked == []
    # 只读三档必须在册 (默开可验)
    assert set(READONLY_SCOPES) <= set(api_tokens.SCOPES)
    # write:ext 保持可签发 (非默认), 见 R6
    assert "write:ext" in api_tokens.SCOPES


def test_write_ext_still_issuable(tmp_path: Path):
    """R6: write:ext 不在默认组合里, 但仍可显式签发。"""
    record, _ = api_tokens.create_token(tmp_path, "数据写入", ["write:ext"])
    assert record["scopes"] == ["write:ext"]


def test_forbidden_scope_lists_are_consistent():
    """双层禁用集合必须自洽 — 精确名与前缀不能互相遮蔽。

    变异史: 只清空 FORBIDDEN_SCOPES 时全部测试仍绿, 因为 "paper:" 前缀已经覆盖了
    "paper:trade"。这不是测试缺口而是等价变异, 但"两层里有一层是死代码"本身值得
    锁一条 — 将来若有人删掉前缀层, 精确名这层必须真的顶住。
    """
    # 精确名单项必须命中 (不依赖前缀)
    for name in api_tokens.FORBIDDEN_SCOPES:
        assert name in api_tokens._forbidden([name]), f"{name} 未被精确名集合拦下"
    # 前缀层必须至少有一个真实前缀, 且都能命中
    assert api_tokens.FORBIDDEN_SCOPE_PREFIXES, "前缀集合不应为空"
    for prefix in api_tokens.FORBIDDEN_SCOPE_PREFIXES:
        probe = prefix + "probe"
        assert probe in api_tokens._forbidden([probe]), f"前缀 {prefix} 未生效"
    # 精确名集合里的项必须落在某个前缀内 (否则说明规范漂移)
    for name in api_tokens.FORBIDDEN_SCOPES:
        assert any(name.startswith(p) for p in api_tokens.FORBIDDEN_SCOPE_PREFIXES), (
            f"{name} 不在任何前缀下 — 请同步 FORBIDDEN_SCOPE_PREFIXES"
        )
    # 合法 scope 一个都不能被误伤
    for ok in api_tokens.SCOPES:
        assert api_tokens._forbidden([ok]) == [], f"合法 scope {ok} 被误判为禁用"


# ── 网关夹具: 复刻 auth_middleware 的 Bearer 分支 ──────────
@pytest.fixture
def gw(tmp_path: Path) -> tuple[TestClient, Path]:
    app = FastAPI()

    @app.get("/api/kline/daily")
    def market():
        return {"ok": True}

    @app.get("/api/paper/overview")
    def overview():
        return {"ok": True}

    @app.get("/api/paper/positions")
    def positions():
        return {"ok": True}

    @app.post("/api/paper/orders")
    def submit():
        return {"ok": True}

    @app.delete("/api/paper/orders/{order_id}")
    def cancel(order_id: str):
        return {"ok": True}

    @app.post("/api/ext-data/x/ingest")
    def ingest():
        return {"status": "ok"}

    @app.post("/api/paperback")  # 前缀混淆诱饵: 路由存在, 但不是 /api/paper*
    def paperback():
        return {"ok": True}

    from app.config import Settings
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    object.__setattr__(settings, "data_dir", tmp_path)

    @app.middleware("http")
    async def token_gate(request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        authz = request.headers.get("authorization", "")
        if authz.startswith("Bearer "):
            verdict = api_gateway.evaluate(
                settings.data_dir, request.method, path,
                authz[len("Bearer "):].strip(),
            )
            if verdict["status"] is not None:
                return JSONResponse(
                    status_code=verdict["status"],
                    content={"detail": verdict["detail"]},
                    headers=verdict["headers"],
                )
            resp = await call_next(request)
            for k, v in verdict["headers"].items():
                resp.headers[k] = v
            return resp
        return await call_next(request)

    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app), tmp_path


PAPER_CALLS = [
    ("GET", "/api/paper/overview"),
    ("GET", "/api/paper/positions"),
    ("POST", "/api/paper/orders"),
    ("DELETE", "/api/paper/orders/o_1"),
]


def _seed_legacy_paper_token(tmp_path: Path, scopes: list[str] | None = None) -> str:
    """手工写入一条带 paper:trade 的"历史"Token 记录, 返回明文。

    绕开 create_token — 现在它签不出来, 只有伪造存量才能验证网关的 fail-closed。
    """
    plaintext = "tsp_" + "a" * 64
    p = tmp_path / "user_data" / "api_tokens.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": "tok_legacy",
        "name": "存量的模拟盘 Token",
        "token_hash": api_tokens._hash(plaintext),
        "scopes": scopes if scopes is not None else ["read:market", "paper:trade"],
        "created_at": "2026-10-01T09:00:00",
        "last_used_at": None,
        "revoked": False,
    }
    p.write_text(json.dumps({"tokens": [record]}, ensure_ascii=False), encoding="utf-8")
    return plaintext


# ── G-T4: 存量记录 fail-closed ────────────────────────────
def test_legacy_paper_token_can_still_read_market(gw):
    """存量 Token 的只读 scope 继续有效 — 拒绝只针对模拟盘。"""
    client, tmp_path = gw
    plaintext = _seed_legacy_paper_token(tmp_path)
    hdr = {"Authorization": f"Bearer {plaintext}"}
    assert client.get("/api/kline/daily", headers=hdr).status_code == 200


@pytest.mark.parametrize("method,path", PAPER_CALLS)
def test_legacy_paper_token_denied_on_paper(gw, method: str, path: str):
    """G-T4 核心: 存量记录即使仍带 paper:trade, Bearer 打模拟盘也一律 403。

    拒绝必须来自网关规则表剥离 (required_scope → None), 不是靠吊销或签发门。
    """
    client, tmp_path = gw
    plaintext = _seed_legacy_paper_token(tmp_path)
    r = client.request(method, path, headers={"Authorization": f"Bearer {plaintext}"})
    assert r.status_code == 403
    assert "未对外开放" in r.json()["detail"]
    # 不是"缺 scope"式的 403 — 那说明规则还在, 只是 scope 对不上
    assert "缺少所需 scope" not in r.json()["detail"]
    # 记录本身仍有效 (未被误吊销)
    assert api_tokens.verify_token(tmp_path, plaintext) is not None


def test_paper_prefix_collision_not_denied(gw):
    """/api/paperback 与 /api/paper 无关 — 剥离规则不能误伤同前缀路由。"""
    client, tmp_path = gw
    plaintext = _seed_legacy_paper_token(tmp_path)
    r = client.post("/api/paperback", headers={"Authorization": f"Bearer {plaintext}"})
    # 该路由本就不在开放面, 403 是"未对外开放"; 关键是 required_scope 不把它当 paper
    assert api_gateway.required_scope("POST", "/api/paperback") is None
    assert api_gateway.required_scope("GET", "/api/paperback") is None
    assert r.status_code == 403


# ── G-T2 / G-T3: 只读默开可验 ─────────────────────────────
def test_readonly_token_reads_market_with_ratelimit_header(gw):
    """G-T2: 推荐只读组合 → 行情 200 且带限流头。"""
    client, tmp_path = gw
    _, tok = api_tokens.create_token(tmp_path, "只读默认", READONLY_SCOPES)
    r = client.get("/api/kline/daily", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.headers.get("X-RateLimit-Limit") == "120"
    assert int(r.headers["X-RateLimit-Remaining"]) < 120


@pytest.mark.parametrize("method,path", PAPER_CALLS)
def test_readonly_token_denied_on_paper(gw, method: str, path: str):
    """G-T3: 只有只读 scope 的 Token 打模拟盘 → 403。"""
    client, tmp_path = gw
    _, tok = api_tokens.create_token(tmp_path, "只读默认", READONLY_SCOPES)
    r = client.request(method, path, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


# ── G-T6: UI 会话路径不受影响 ─────────────────────────────
@pytest.mark.parametrize("method,path", PAPER_CALLS)
def test_ui_session_path_unaffected_by_token_bucket(gw, method: str, path: str,
                                                   monkeypatch: pytest.MonkeyPatch):
    """G-T6: 无 Bearer 的 UI 会话完全不进网关 — 模拟盘读写照常, 也不吃 Token 桶。

    先造一个额度用尽的 Token 桶, 证明 UI 路径不受牵连。
    """
    client, tmp_path = gw
    monkeypatch.setattr(api_gateway, "rate_limit_per_min", lambda: 1)
    _, tok = api_tokens.create_token(tmp_path, "限流", ["read:market"])
    hdr = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/kline/daily", headers=hdr).status_code == 200
    assert client.get("/api/kline/daily", headers=hdr).status_code == 429  # 桶已耗尽

    r = client.request(method, path)  # 无 Authorization 头 = UI 会话
    assert r.status_code == 200
    assert "X-RateLimit-Limit" not in r.headers


# ── G-T9: 文档口径 (静态) ─────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]

_DOC_FILES = [
    REPO_ROOT / "docs" / "agent-skills-brief.md",
    REPO_ROOT / "docs" / "open-platform-plan.md",
    REPO_ROOT / "skills" / "README.md",
    REPO_ROOT / "examples" / "open-api" / "README.md",
    REPO_ROOT / "mcp-server" / "README.md",
]

# 这些组合是在承诺"Token 能下单 / 信号能直接成交", 一律禁止
_FORBIDDEN_PHRASES = (
    "Token 可模拟下单",
    "Token 可以模拟下单",
    "Token 可交易",
    "Token 可实盘",
    "信号可做",
    "可以直接下单",
)


@pytest.mark.parametrize("path", _DOC_FILES, ids=lambda p: p.name)
def test_docs_have_no_trading_promise(path: Path):
    assert path.exists(), f"文档缺失: {path}"
    text = path.read_text(encoding="utf-8")
    hit = [ph for ph in _FORBIDDEN_PHRASES if ph in text]
    assert hit == [], f"{path.name} 出现越界表述: {hit}"


def test_research_only_footnote_present():
    """脚注必须在 BRIEF 里保留 — 它是公开文档的免责底线。"""
    brief = (REPO_ROOT / "docs" / "agent-skills-brief.md").read_text(encoding="utf-8")
    assert "仅供研究" in brief and "不构成投资建议" in brief
    assert "人工确认" in brief
