"""Token 审计视图 + 安全配置开关。

审计只读: 它不改 `api_tokens.json`、不改校验链路、不动网关。所以这里的断言分两类:

1. **判据对不对** —— 年龄/空闲/从未使用的边界, 以及"从未使用"不能被当成 0 天。
2. **真的只读** —— 跑完审计, Token 文件逐字节不变。这一条是防"审计顺手改了
   `last_used_at`"这类隐蔽副作用; 一旦发生, 审计本身就会污染它自己的输入。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services import api_token_audit, api_tokens

# 带时区, 与生产一致: `api_tokens.cn_now()` 返回 Asia/Shanghai 的 aware datetime,
# 落盘的 created_at 也带 +08:00。用 naive 的 NOW 做减法是测试自己的 bug, 不是被测代码的。
CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=CN)


def _write_tokens(data_dir: Path, records: list[dict]) -> None:
    """直接写 api_tokens.json —— 绕过 create_token, 以便精确控制时间戳。"""
    path = data_dir / "user_data" / "api_tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(json.dumps({"tokens": records}, ensure_ascii=False, indent=2), encoding="utf-8")


def _record(
    token_id: str,
    *,
    age_days: int | None,
    idle_days: int | None,
    revoked: bool = False,
    scopes: list[str] | None = None,
) -> dict:
    created = (NOW - timedelta(days=age_days)).isoformat(timespec="seconds") if age_days is not None else None
    used = (NOW - timedelta(days=idle_days)).isoformat(timespec="seconds") if idle_days is not None else None
    return {
        "id": token_id,
        "name": f"token-{token_id}",
        "token_hash": hashlib.sha256(token_id.encode()).hexdigest(),
        "scopes": scopes or ["read"],
        "created_at": created,
        "last_used_at": used,
        "revoked": revoked,
    }


def _by_id(report: dict) -> dict[str, dict]:
    return {row["id"]: row for row in report["tokens"]}


# --------------------------------------------------------------------------- #
# 判据边界
# --------------------------------------------------------------------------- #

def test_fresh_active_token_has_no_flags(tmp_path: Path) -> None:
    _write_tokens(tmp_path, [_record("fresh", age_days=3, idle_days=0)])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    assert _by_id(report)["fresh"]["flags"] == []
    assert report["counts"]["active"] == 1
    assert "无需处理" in report["summary"]


def test_rotate_overdue_at_exactly_the_threshold(tmp_path: Path) -> None:
    """90 天整就该提醒 —— 边界取闭区间, 否则"第 90 天"永远差一天。"""
    _write_tokens(tmp_path, [_record("edge", age_days=90, idle_days=1)])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    assert "rotate_overdue" in _by_id(report)["edge"]["flags"]

    _write_tokens(tmp_path, [_record("edge", age_days=89, idle_days=1)])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    assert "rotate_overdue" not in _by_id(report)["edge"]["flags"]


def test_unused_stale_at_exactly_the_threshold(tmp_path: Path) -> None:
    _write_tokens(tmp_path, [_record("idle", age_days=40, idle_days=30)])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    assert "unused_stale" in _by_id(report)["idle"]["flags"]

    _write_tokens(tmp_path, [_record("idle", age_days=40, idle_days=29)])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    assert "unused_stale" not in _by_id(report)["idle"]["flags"]


def test_never_used_is_not_treated_as_zero_idle(tmp_path: Path) -> None:
    """从未用过的 Token 不该被算成"今天刚用过"而永远漏报。

    `idle_days` 对 null 的 `last_used_at` 是 None —— 如果有人图省事写成 0, 这些残留
    Token 就永远不会出现在任何提醒里, 而它们恰恰最该清理。
    """
    _write_tokens(tmp_path, [_record("never", age_days=120, idle_days=None)])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    row = _by_id(report)["never"]
    assert row["idle_days"] is None
    assert "never_used" in row["flags"]
    assert "unused_stale" not in row["flags"]
    assert report["counts"]["never_used"] == 1


def test_recently_created_unused_token_is_not_flagged(tmp_path: Path) -> None:
    """刚创建、还没接入的 Token 不该被报 —— 那是正常状态, 报它是噪音。"""
    _write_tokens(tmp_path, [_record("brand_new", age_days=2, idle_days=None)])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    assert _by_id(report)["brand_new"]["flags"] == []


def test_revoked_token_is_excluded_from_actionable_flags(tmp_path: Path) -> None:
    """已吊销的 Token 既有年龄又没使用, 但不该再报"该轮换" —— 已经没有意义了。"""
    _write_tokens(tmp_path, [_record("old_revoked", age_days=400, idle_days=None, revoked=True)])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    row = _by_id(report)["old_revoked"]
    assert row["revoked"] is True
    assert row["flags"] == ["revoked"]
    assert report["counts"]["active"] == 0
    assert report["counts"]["revoked"] == 1
    assert "没有生效中" in report["summary"]


def test_mixed_fleet_counts_and_summary(tmp_path: Path) -> None:
    _write_tokens(tmp_path, [
        _record("a", age_days=5, idle_days=1),          # 健康
        _record("b", age_days=100, idle_days=2),        # 该轮换
        _record("c", age_days=45, idle_days=60),        # 长期没用
        _record("d", age_days=200, idle_days=None),     # 从没用过
        _record("e", age_days=300, idle_days=1, revoked=True),
    ])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    counts = report["counts"]
    assert counts == {
        "total": 5,
        "active": 4,
        "revoked": 1,
        "rotate_due": 2,   # b + d
        "unused": 1,       # c
        "never_used": 1,   # d
    }
    summary = report["summary"]
    assert "4 个生效 Token" in summary
    assert "建议轮换" in summary and "建议吊销" in summary


def test_bad_or_missing_timestamps_do_not_crash(tmp_path: Path) -> None:
    """历史数据里可能有空/坏时间戳, 审计不能因此抛异常。"""
    _write_tokens(tmp_path, [
        {"id": "bad", "name": "bad", "scopes": [], "created_at": "不是时间",
         "last_used_at": None, "revoked": False},
        {"id": "none", "name": "none", "scopes": [], "created_at": None,
         "last_used_at": None, "revoked": False},
    ])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    for row in report["tokens"]:
        assert row["age_days"] is None
        assert row["idle_days"] is None
        assert "rotate_overdue" not in row["flags"]


def test_naive_timestamps_are_handled(tmp_path: Path) -> None:
    """不带时区的时间戳也要能算年龄。

    历史数据或手改过的 api_tokens.json 里可能是裸时间。拿带时区的 now 去减它会
    TypeError —— 审计报错会连带把整个设置页打挂, 这比"审计不准"更糟。
    """
    _write_tokens(tmp_path, [{
        "id": "naive", "name": "naive", "scopes": [],
        "created_at": "2026-01-01T00:00:00", "last_used_at": "2026-10-01T00:00:00",
        "revoked": False,
    }])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    row = _by_id(report)["naive"]
    assert row["age_days"] is not None and row["age_days"] > 200
    assert row["idle_days"] is not None and row["idle_days"] >= 20
    assert "rotate_overdue" in row["flags"]


def test_no_tokens_at_all(tmp_path: Path) -> None:
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    assert report["tokens"] == []
    assert report["counts"]["total"] == 0
    assert "没有生效中" in report["summary"]


# --------------------------------------------------------------------------- #
# 只读: 审计不能改它自己的输入
# --------------------------------------------------------------------------- #

def test_audit_does_not_modify_the_token_file(tmp_path: Path) -> None:
    """跑完审计, Token 文件逐字节不变。

    最容易踩的坑是审计时顺手 `_touch` 一下 `last_used_at` —— 那样每次看审计都会把
    "多久没用"重置成 0, 残留 Token 永远清不掉。
    """
    _write_tokens(tmp_path, [
        _record("a", age_days=100, idle_days=50),
        _record("b", age_days=1, idle_days=None),
    ])
    path = tmp_path / "user_data" / "api_tokens.json"
    before = path.read_bytes()
    api_token_audit.audit_tokens(tmp_path, now=NOW)
    api_token_audit.audit_tokens(tmp_path, now=NOW)
    assert path.read_bytes() == before


def test_audit_does_not_leak_the_hash(tmp_path: Path) -> None:
    """审计视图不能带出 token_hash —— 它挂在 UI 会话下, 但输出仍应当最小化。"""
    _write_tokens(tmp_path, [_record("a", age_days=1, idle_days=0)])
    report = api_token_audit.audit_tokens(tmp_path, now=NOW)
    text = repr(report)
    assert "token_hash" not in text
    assert _record("a", age_days=1, idle_days=0)["token_hash"] not in text


# --------------------------------------------------------------------------- #
# 端点接线
# --------------------------------------------------------------------------- #

def test_audit_endpoint_is_wired(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import settings as settings_api

    _write_tokens(tmp_path, [_record("a", age_days=100, idle_days=1)])
    app = FastAPI()
    app.include_router(settings_api.router)
    app.state.repo = type("R", (), {"store": type("S", (), {"data_dir": tmp_path})()})()
    with TestClient(app) as client:
        resp = client.get("/api/settings/api-tokens/audit")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["counts"]["rotate_due"] == 1
        assert body["tokens"][0]["id"] == "a"


def test_token_service_itself_is_untouched() -> None:
    """本轮承诺不改 api_tokens / api_gateway 的读写路径。

    这条不是形式主义: 审计模块是**新文件**, 只 import 现有的 `list_tokens` /
    `cn_now`。如果哪天有人把审计逻辑塞进 api_tokens, 那会顺带动到 Token 校验的
    热路径 —— 而失败表现只有 401, 分不清原因。
    """
    import inspect

    src = inspect.getsource(api_token_audit)
    for forbidden in ("verify_token(", "create_token(", "revoke_token(", "_save("):
        assert forbidden not in src, f"审计模块不该调用 {forbidden}"
