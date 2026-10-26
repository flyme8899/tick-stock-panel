"""BSS **命中**钉钉外发（批次 20261027gbssding · P-ding-later）。

## 这个文件钉的是什么

不是一个"能不能发出去"的功能测，而是一张**拒发表**。命中外发最容易出的错不是
"发不出去"，是**发错了地方**：缺配时偷偷回落到资讯/定义态共群、把 candidate 当
已校准发出去、把 fixture 当生产命中发出去。这三种错都是"看起来在工作"的错，
所以每一条都单独钉。

## 矩阵（G-T1–G-T12 + V1–V12）

| # | 场景 | 期望 |
|---|---|---|
| G-T2 / V7 | 常量与配置键 | 命中键独立；三 env 字面量不变；example 有空占位 |
| G-T3 / V2 | 卡口唯一 | 发送目标 == 命中 URL，**不是**旧 URL |
| G-T4 / V1 | 缺配 | `hits_webhook_not_configured`；attempts=0；旧 URL 非空仍拒 |
| G-T5 / V4 | 可见性 | 只发 publishable ∧ calib_passed；candidate/pending/limit_reopen 不发 |
| G-T6 / V5 | fixture | 生产路径拒发；测例显式 `allow_fixture` |
| G-T7 | 正文边界 | 含 symbol + direction_label + 免责；无价格/数量/仓位 |
| G-T8 | 定义态零碰 | `_emit_dingtalk` / 三 env 门行为不变 |
| G-T9 / V8 | DSA_REVIEW 零碰 | 本派 diff 无复盘文件 |
| G-T10 / V6 | 空态 | `EMPTY_HITS_MESSAGE` 字节级不变 |
| G-T11 / V10 | 禁区 | 无 TRIGGERING 翻 True、无调度、无 Opt-A |
| G-T12 / V12 | 同 URL | 允许发送 + warning |
| V3 | 回落路径 | `hits_dingtalk` 无任何读 `dingtalk_webhook_url` 作发送目标 |
| V11 | 冷却 | 同键 30 分钟内二次 skip；过期可再发；不同 symbol/asof 不互挡 |
| V9 | 凭证 | 仓内无真实 token，只用 `example.invalid` |

全部 mock 发送（`opener` 注入），**不依赖**真实 webhook / secret / 钉钉群。

仅供研究，不构成投资建议；执行须人工确认。红涨绿跌。成本来回 18.2bp（旁注）。
"""
from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.buy_sell_signals import constants as C  # noqa: N812
from app.buy_sell_signals import disclaimer as D  # noqa: N812
from app.buy_sell_signals import hits_dingtalk as HD  # noqa: N812
from app.buy_sell_signals import hits_fixtures as fx
from app.buy_sell_signals.calib_store import CalibIndex
from app.buy_sell_signals.hits_dingtalk import (
    HITS_DINGTALK_DEDUPE_COOLDOWN_MIN,
    HITS_WEBHOOK_NOT_CONFIGURED,
    REASON_DEDUPE,
    REASON_FIXTURE,
    REASON_KEYS,
    REASON_NOT_VISIBLE,
    REASON_PUSHED,
    REASON_SEND_FAILED,
    HitPushOutcome,
    HitsDedupe,
    build_hit_dingtalk_text,
    dedupe_key,
    hits_dingtalk_status,
    hits_webhook_ready,
    maybe_try_push_hit,
    try_push_hit,
)
from app.buy_sell_signals.hits_visibility import VisibilityReject
from app.buy_sell_signals.store import InMemorySignalRegistryStore
from app.market_time import cn_now

SCOPE = frozenset({"000001", "600000"})
OLD_URL = "https://example.invalid/old"
HITS_URL = "https://example.invalid/hits"
HITS_SECRET = "example-not-a-real-secret"

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"


# ---------------------------------------------------------------- 辅助


class _Recorder:
    """假 opener。记录**每一个**被请求的 url，用来断言"旧 URL 没被碰过"。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request, timeout=None):
        self.calls.append(getattr(request, "full_url", str(request)))

        class _Resp:
            def read(self) -> bytes:
                return json.dumps({"errcode": 0}).encode()

            def close(self) -> None:
                return None

        return _Resp()

    @property
    def count(self) -> int:
        return len(self.calls)


class _Boom:
    """假 opener，永远抛。验 best-effort 不冒泡。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        raise OSError("network down")


def _docstring_ranges(tree: ast.AST) -> set[int]:
    """模块/类/函数的 docstring 所占行号集合。

    「扫描源码里有没有某个词」必须排除 docstring，否则**说明"这里不许这么做"的
    那句话本身会把扫描打红** —— 那样的扫描会逼人删掉解释，把注释写成暗号。
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            const = body[0].value
            if isinstance(const.value, str):
                start = const.lineno
                end = getattr(const, "end_lineno", start)
                lines.update(range(start, end + 1))
    return lines


def _code_lines(rel: str) -> list[tuple[int, str]]:
    """返回 `(行号, 内容)`，已剔除 docstring 行与纯注释行。"""
    src = (BACKEND / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    docs = _docstring_ranges(tree)
    out: list[tuple[int, str]] = []
    for idx, line in enumerate(src.splitlines(), start=1):
        if idx in docs:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((idx, line))
    return out


@pytest.fixture()
def send_side(monkeypatch):
    """把命中通道配好（URL + secret）并给一张干净的冷却表。"""
    fresh = HitsDedupe()
    monkeypatch.setattr(HD, "_DEDUPE", fresh)
    return fresh


def _registry(*records) -> InMemorySignalRegistryStore:
    reg = InMemorySignalRegistryStore()
    for rec in records:
        reg.upsert(rec)
    return reg


def _full_registry() -> InMemorySignalRegistryStore:
    return _registry(
        fx.publishable_definition(),
        fx.candidate_definition(),
        fx.observe_definition(),
        fx.limit_reopen_definition(),
        fx.daily_fundamental_definition(),
        fx.composite_definition(),
    )


def _passed_all() -> CalibIndex:
    return CalibIndex(
        [
            fx.passed_calib(signal_id="fx.pub.intraday_volume"),
            fx.passed_calib(signal_id="fx.pub.daily_quality_value"),
            fx.passed_calib(signal_id="fx.cand.intraday_volume"),
            fx.passed_calib(signal_id="fx.limit_reopen"),
        ]
    )


def _fresh_hit(**overrides):
    """夹具命中默认 `source.mode=fixture`，测外发要显式放行。"""
    return fx.calibrated_hit(**overrides)


def _push(hit, *, opener, dedupe=None, webhook=HITS_URL, secret=HITS_SECRET, **kwargs):
    return try_push_hit(
        hit,
        _full_registry(),
        calib=_passed_all(),
        scope_symbols=SCOPE,
        opener=opener,
        allow_fixture=True,
        webhook=webhook,
        secret=secret,
        dedupe=dedupe,
        **kwargs,
    )


# ======================================================= G-T2 / V7 · 常量与配置


def test_g_t2_hits_env_names_are_pinned():
    assert C.HITS_DINGTALK_WEBHOOK_ENV_NAME == "BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL"
    assert C.HITS_DINGTALK_SECRET_ENV_NAME == "BUY_SELL_SIGNALS_HITS_DINGTALK_SECRET"


def test_g_t2_reason_string_is_dedicated_not_shared_with_review():
    """拒发原因必须与复盘的 `review_webhook_not_configured` 区分开。

    两者混用会让运维照着日志去查错的键 —— 复盘查了 BSS 的 env，或者反过来。
    """
    assert HITS_WEBHOOK_NOT_CONFIGURED == "hits_webhook_not_configured"
    assert "review" not in HITS_WEBHOOK_NOT_CONFIGURED


def test_g_t2_three_env_literals_untouched():
    """V7: 三 env 的名字与语义**一个字都没动**。"""
    assert C.PERSIST_ENV_NAME == "BUY_SELL_SIGNALS_PERSIST"
    assert C.PUSH_ENV_NAME == "BUY_SELL_SIGNALS_PUSH"
    assert C.DINGTALK_ENV_NAME == "BUY_SELL_SIGNALS_DINGTALK"


def test_g_t2_config_has_two_new_fields_with_empty_defaults():
    from app.config import settings

    assert settings.buy_sell_signals_hits_dingtalk_webhook_url is not None
    assert settings.buy_sell_signals_hits_dingtalk_secret is not None
    # 旧字段仍在，且语义不变（共群，命中不读它作发送目标）。
    assert hasattr(settings, "dingtalk_webhook_url")


def test_g_t2_env_example_has_commented_placeholders():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL" in text
    assert "BUY_SELL_SIGNALS_HITS_DINGTALK_SECRET" in text
    # 必须是**注释掉的空占位**，不能有真值。
    for line in text.splitlines():
        if "BUY_SELL_SIGNALS_HITS_DINGTALK" in line:
            assert line.lstrip().startswith("#"), line
            assert line.rstrip().endswith("="), line


def test_v9_no_real_webhook_in_repo_files():
    """V9: example / 文档 / 本测例都只有 `example.invalid` 占位。"""
    for rel in (".env.example", "docs/buy-sell-signals-skeleton.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for marker in ("oapi.dingtalk.com", "access_token="):
            assert marker not in text, f"{rel} 出现疑似真实 webhook 片段: {marker}"


def test_v7_no_extra_enable_flag_is_introduced():
    """N3 写死：**不得**新增任何 enable 旗标（URL 非空即门，对标 #227）。

    这条守的是"多一个旗标就多一个配了但忘了开的位置"。旗标可以藏在三个地方：
    `Settings` 字段、env 常量名、模块级布尔常量 —— 三处都扫。
    """
    from app.config import Settings

    fields = set(Settings.model_fields)
    for name in fields:
        if "hits_dingtalk" in name:
            assert not ("enabled" in name or "enable" in name or name[-3:] == "_on"), (
                f"Settings 里出现额外 enable 旗标: {name}"
            )

    const_names = [n for n in C.__all__ if "HITS_DINGTALK" in n]
    for name in const_names:
        assert "ENABLED" not in name.upper(), f"constants 里出现 enable 旗标: {name}"

    for idx, line in _code_lines("app/buy_sell_signals/hits_dingtalk.py"):
        upper = line.upper()
        assert "ENABLED" not in upper or "AUCTION" in upper or "TRIGGERING" in upper, (
            f"命中模块第 {idx} 行引入 enable 语义: {line.strip()}"
        )


def test_v7_settings_fields_for_hits_are_exactly_two():
    """命中相关的 Settings 字段恰为两个（url + secret），不被顺手加第三个。"""
    from app.config import Settings

    hits_fields = sorted(n for n in Settings.model_fields if "hits_dingtalk" in n)
    assert hits_fields == [
        "buy_sell_signals_hits_dingtalk_secret",
        "buy_sell_signals_hits_dingtalk_webhook_url",
    ]


def test_g_t2_dedupe_cooldown_constant_is_30_and_independent():
    """N2: 30 分钟，且**不是**定义态那个常量。"""
    assert HITS_DINGTALK_DEDUPE_COOLDOWN_MIN == 30
    assert C.PUSH_DEDUPE_COOLDOWN_MIN == 30
    # 同值但两个名字：调一个不该影响另一个。
    assert "HITS_DINGTALK_DEDUPE_COOLDOWN_MIN" in C.__all__
    assert "PUSH_DEDUPE_COOLDOWN_MIN" in C.__all__


def test_dedupe_key_fields_include_symbol_and_asof():
    assert C.HITS_DINGTALK_DEDUPE_KEY_FIELDS == (
        "signal_id",
        "definition_version",
        "symbol",
        "asof",
        "freq_lock_version",
    )


# ======================================================= G-T3 / V2 · 卡口唯一


def test_g_t3_send_target_is_the_hits_webhook(send_side):
    """V2: 发送目标 == 命中 URL。"""
    rec = _Recorder()
    out = _push(_fresh_hit(), opener=rec, dedupe=send_side)
    assert out.reason == REASON_PUSHED
    assert out.attempts == 1
    assert rec.count == 1
    assert rec.calls[0].startswith(HITS_URL)


def test_g_t3_with_secret_signature_is_appended():
    """secret 传进去后，实际请求 url 带 timestamp/sign（加签生效）。"""
    rec = _Recorder()
    _push(_fresh_hit(), opener=rec, dedupe=HitsDedupe())
    assert "timestamp=" in rec.calls[0]
    assert "sign=" in rec.calls[0]


def test_g_t3_no_secret_means_plain_url():
    rec = _Recorder()
    out = _push(_fresh_hit(), opener=rec, secret="", dedupe=HitsDedupe())
    assert out.reason == REASON_PUSHED
    assert "sign=" not in rec.calls[0]


def test_v3_module_never_uses_common_webhook_as_send_target():
    """V3: 静态证明 — `hits_dingtalk` 里 `dingtalk_webhook_url` **只**出现在
    同群比对函数里，不可能成为 `send_markdown` 的目标。

    做法：找出所有引用该名字的地方，断言它们全在
    `shares_webhook_with_common_group` 的 AST 范围内。这个函数只回 bool，
    它的返回值从不传给 `send_markdown`（下面另有 AST 断言）。
    """
    src = (BACKEND / "app" / "buy_sell_signals" / "hits_dingtalk.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def _spans(node):
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        return range(start, end + 1)

    allowed: set[int] = set()
    for fn_name in ("_common_group_webhook_url", "shares_webhook_with_common_group"):
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        allowed |= set(_spans(fn))

    offending: list[int] = [
        node.lineno
        for node in ast.walk(tree)
        if (
            (isinstance(node, ast.Constant) and node.value == "dingtalk_webhook_url")
            or (isinstance(node, ast.Attribute) and node.attr == "dingtalk_webhook_url")
        )
        and node.lineno not in allowed
    ]
    assert offending == [], f"dingtalk_webhook_url 在 send 路径上出现: 行 {offending}"


def test_v3_send_markdown_never_receives_settings_common_url():
    """V3 续: `send_markdown(...)` 的第一个实参必须是 `url`，不是任何 settings 读取。"""
    src = (BACKEND / "app" / "buy_sell_signals" / "hits_dingtalk.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "send_markdown"
    ]
    assert len(calls) == 1, "本模块应当只有一处 send_markdown 调用"
    first = calls[0].args[0]
    assert isinstance(first, ast.Name) and first.id == "url"


# ======================================================= G-T4 / V1 · 缺配拒发


def test_g_t4_missing_hits_webhook_is_refused(send_side):
    rec = _Recorder()
    out = _push(_fresh_hit(), opener=rec, webhook="", dedupe=send_side)
    assert out.reason == HITS_WEBHOOK_NOT_CONFIGURED
    assert out.attempts == 0
    assert rec.count == 0


def test_g_t4_whitespace_only_webhook_is_refused(send_side):
    rec = _Recorder()
    out = _push(_fresh_hit(), opener=rec, webhook="   \n\t ", dedupe=send_side)
    assert out.reason == HITS_WEBHOOK_NOT_CONFIGURED
    assert out.attempts == 0
    assert rec.count == 0


def test_v1_old_url_non_empty_still_refuses(monkeypatch, send_side):
    """V1: 旧共群 URL **非空**，命中 URL 空 → 仍然拒发，且旧 URL 一次都没被请求。"""
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", OLD_URL)
    rec = _Recorder()
    out = _push(_fresh_hit(), opener=rec, webhook="", dedupe=send_side)
    assert out.reason == HITS_WEBHOOK_NOT_CONFIGURED
    assert rec.count == 0
    assert all(OLD_URL not in url for url in rec.calls)


def test_v1_settings_common_url_present_but_hits_absent(monkeypatch, send_side):
    """连 settings 里都有共群 URL 时也不回落（settings 是主要读取路径）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "dingtalk_webhook_url", OLD_URL, raising=False)
    monkeypatch.setattr(settings, "buy_sell_signals_hits_dingtalk_webhook_url", "", raising=False)
    monkeypatch.delenv(C.HITS_DINGTALK_WEBHOOK_ENV_NAME, raising=False)
    assert HD.hits_webhook_url() == ""
    assert hits_webhook_ready() is False

    rec = _Recorder()
    out = try_push_hit(
        _fresh_hit(),
        _full_registry(),
        calib=_passed_all(),
        scope_symbols=SCOPE,
        opener=rec,
        allow_fixture=True,
        dedupe=send_side,
    )
    assert out.reason == HITS_WEBHOOK_NOT_CONFIGURED
    assert rec.count == 0


def test_g_t4_hits_url_read_from_env_when_settings_empty(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "buy_sell_signals_hits_dingtalk_webhook_url", "", raising=False)
    monkeypatch.setenv(C.HITS_DINGTALK_WEBHOOK_ENV_NAME, HITS_URL)
    assert HD.hits_webhook_url() == HITS_URL
    assert hits_webhook_ready() is True


# ======================================================= G-T5 / V4 · 可见性


def test_g_t5_candidate_with_passed_registration_is_not_pushed(send_side):
    """V4: 登记里有 candidate 的 passed，但定义不是 publishable → 定义闸挡在前面。"""
    rec = _Recorder()
    out = _push(fx.candidate_calibrated_hit(), opener=rec, dedupe=send_side)
    assert out.reason == REASON_NOT_VISIBLE
    assert out.reject == VisibilityReject.NOT_PUBLISHABLE
    assert rec.count == 0


def test_g_t5_publishable_without_passed_is_not_pushed(send_side):
    rec = _Recorder()
    out = try_push_hit(
        _fresh_hit(),
        _full_registry(),
        calib=CalibIndex([]),  # 一条 passed 都没有
        scope_symbols=SCOPE,
        opener=rec,
        allow_fixture=True,
        webhook=HITS_URL,
        dedupe=send_side,
    )
    assert out.reason == REASON_NOT_VISIBLE
    assert out.reject == VisibilityReject.CALIB_NOT_PASSED
    assert rec.count == 0


def test_g_t5_pending_registration_is_not_pushed(send_side):
    rec = _Recorder()
    out = try_push_hit(
        _fresh_hit(),
        _full_registry(),
        calib=CalibIndex([fx.pending_calib()]),
        scope_symbols=SCOPE,
        opener=rec,
        allow_fixture=True,
        webhook=HITS_URL,
        dedupe=send_side,
    )
    assert out.reason == REASON_NOT_VISIBLE
    assert out.reject == VisibilityReject.CALIB_NOT_PASSED
    assert rec.count == 0


def test_g_t5_limit_reopen_is_not_pushed(send_side):
    rec = _Recorder()
    out = _push(fx.limit_reopen_calibrated_hit(), opener=rec, dedupe=send_side)
    assert out.reason == REASON_NOT_VISIBLE
    assert rec.count == 0


def test_g_t5_unlabelled_publishable_hit_is_not_pushed(send_side):
    """缺 `freq_lock_version` 的命中同样不可见（连口径都不知道）。"""
    rec = _Recorder()
    out = _push(fx.unlabelled_hit(), opener=rec, dedupe=send_side)
    assert out.reason == REASON_NOT_VISIBLE
    assert out.reject == VisibilityReject.CALIB_NOT_PASSED
    assert rec.count == 0


def test_g_t5_observable_hit_without_registration_is_not_pushed(send_side):
    rec = _Recorder()
    out = _push(fx.observe_hit(), opener=rec, dedupe=send_side)
    assert out.reason == REASON_NOT_VISIBLE
    assert rec.count == 0


def test_g_t5_gate_is_the_same_function_as_monitoring(monkeypatch, send_side):
    """外发闸 = 监控闸：把 `visible_hits` 换成"全放行"的桩，外发就会放行。

    这不是鼓励改闸，而是**证明**它真的走同一个函数（而不是另写一套软闸）。
    """
    import app.buy_sell_signals.hits_dingtalk as mod
    from app.buy_sell_signals.hits_visibility import FilteredHits

    def _fake(hits, registry, **kwargs):
        return FilteredHits(hits=list(hits), rejected={}, scope_applied=True, scope_size=2)

    monkeypatch.setattr(mod, "visible_hits", _fake)
    rec = _Recorder()
    out = _push(fx.candidate_calibrated_hit(), opener=rec, dedupe=send_side)
    assert out.reason == REASON_PUSHED, "闸被替换后应当放行 —— 说明确实复用了它"


# ======================================================= G-T6 / V5 · fixture


def test_v5_fixture_hit_is_not_pushed_in_production_path(send_side):
    """V5: 默认（生产）调用 → fixture 拒发。"""
    rec = _Recorder()
    out = try_push_hit(
        _fresh_hit(),
        _full_registry(),
        calib=_passed_all(),
        scope_symbols=SCOPE,
        opener=rec,
        webhook=HITS_URL,
        dedupe=send_side,
    )
    assert out.reason == REASON_FIXTURE
    assert out.attempts == 0
    assert rec.count == 0


def test_v5_fixture_check_runs_before_webhook_check(send_side):
    """顺序契约: fixture 门在缺配门之前。两者都命中时 reason 是 fixture。"""
    rec = _Recorder()
    out = try_push_hit(
        _fresh_hit(),
        _full_registry(),
        calib=_passed_all(),
        scope_symbols=SCOPE,
        opener=rec,
        webhook="",
        dedupe=send_side,
    )
    assert out.reason == REASON_FIXTURE
    assert rec.count == 0


def test_v5_shadow_mode_is_not_blocked_by_fixture_gate(send_side):
    """只有 `fixture` 被这道门拦；`shadow` 不在本门的拦截范围（它由可见性/来源另行判定）。"""
    from dataclasses import replace

    base = _fresh_hit()
    shadow = replace(base, source=replace(base.source, mode="shadow"))
    rec = _Recorder()
    out = _push(shadow, opener=rec, dedupe=send_side)
    assert out.reason == REASON_PUSHED


# ======================================================= G-T7 · 正文边界


def test_g_t7_text_contains_symbol_direction_and_disclaimer():
    title, text = build_hit_dingtalk_text(_fresh_hit())
    assert "000001" in title
    assert "000001" in text
    assert "research_watch" in text
    assert D.DISCLAIMER_TEXT in text


def test_g_t7_text_has_no_price_quantity_or_position():
    _, text = build_hit_dingtalk_text(_fresh_hit())
    for banned in ("price", "价格", "仓位", "数量", "订单", "买入价", "止损", "target", "position"):
        assert banned not in text, f"命中外发正文出现禁词: {banned}"


def test_g_t7_title_differs_from_definition_state_title():
    """标题前缀必须能与定义态「买卖信号 · {name}」区分。"""
    from app.buy_sell_signals.push import build_dingtalk_text

    hit_title, _ = build_hit_dingtalk_text(_fresh_hit())
    def_title, _ = build_dingtalk_text({"display_name": "放量突破"})
    assert hit_title.startswith("买卖信号命中")
    assert def_title.startswith("买卖信号 · ")
    assert hit_title != def_title


def test_g_t7_title_truncated_to_64():
    from dataclasses import replace

    long_hit = replace(_fresh_hit(), symbol="0" * 200)
    title, _ = build_hit_dingtalk_text(long_hit)
    assert len(title) <= 64


def test_g_t7_text_carries_audit_identifiers():
    hit = _fresh_hit()
    _, text = build_hit_dingtalk_text(hit)
    for value in (hit.signal_id, hit.definition_version, hit.asof, hit.family, hit.event_class, hit.hit_id):
        assert value in text


# ======================================================= V11 / N2 · 冷却


def test_v11_second_push_within_cooldown_is_skipped(send_side):
    hit = _fresh_hit()
    rec = _Recorder()
    first = _push(hit, opener=rec, dedupe=send_side)
    second = _push(hit, opener=rec, dedupe=send_side)
    assert first.reason == REASON_PUSHED
    assert second.reason == REASON_DEDUPE
    assert second.attempts == 0
    assert rec.count == 1, "冷却窗内第二次不该产生请求"


def test_v11_one_minute_later_still_cooled(send_side):
    hit = _fresh_hit()
    rec = _Recorder()
    _push(hit, opener=rec, dedupe=send_side, now=cn_now())
    out = _push(hit, opener=rec, dedupe=send_side, now=cn_now() + timedelta(minutes=1))
    assert out.reason == REASON_DEDUPE
    assert rec.count == 1


def test_v11_after_cooldown_can_send_again(send_side):
    hit = _fresh_hit()
    rec = _Recorder()
    _push(hit, opener=rec, dedupe=send_side, now=cn_now())
    out = _push(
        hit,
        opener=rec,
        dedupe=send_side,
        now=cn_now() + timedelta(minutes=HITS_DINGTALK_DEDUPE_COOLDOWN_MIN + 1),
    )
    assert out.reason == REASON_PUSHED
    assert rec.count == 2


def test_v11_different_symbol_does_not_collide(send_side):
    rec = _Recorder()
    a = _push(_fresh_hit(), opener=rec, dedupe=send_side)
    b = _push(fx.calibrated_hit(symbol="600000"), opener=rec, dedupe=send_side)
    assert a.reason == REASON_PUSHED
    assert b.reason == REASON_PUSHED
    assert rec.count == 2


def test_v11_different_asof_does_not_collide(send_side):
    from dataclasses import replace

    base = _fresh_hit()
    today = fx.dated(0)
    yesterday = fx.dated(1)
    rec = _Recorder()
    _push(replace(base, asof=yesterday), opener=rec, dedupe=send_side)
    out = _push(replace(base, asof=today), opener=rec, dedupe=send_side)
    assert out.reason == REASON_PUSHED
    assert rec.count == 2


def test_v11_different_freq_lock_version_does_not_collide(send_side):
    """口径变了就是新信息，不该被旧冷却挡住。"""
    from dataclasses import replace

    base = _fresh_hit()
    rec = _Recorder()
    _push(base, opener=rec, dedupe=send_side)
    other = replace(base, freq_lock_version="freq_lock_v2")
    out = try_push_hit(
        other,
        _full_registry(),
        calib=_passed_all(),
        scope_symbols=SCOPE,
        opener=rec,
        allow_fixture=True,
        webhook=HITS_URL,
        dedupe=send_side,
    )
    # 三键里 freq_lock_version 变了 → 校准闸也认不出来（诚实：没有 v2 的 passed）。
    assert out.reason == REASON_NOT_VISIBLE


def test_v11_cooldown_not_written_on_missing_webhook(send_side):
    """缺配**不**写冷却：配好之后应当能立刻发。"""
    hit = _fresh_hit()
    rec = _Recorder()
    miss = _push(hit, opener=rec, webhook="", dedupe=send_side)
    assert miss.reason == HITS_WEBHOOK_NOT_CONFIGURED
    ok = _push(hit, opener=rec, dedupe=send_side)
    assert ok.reason == REASON_PUSHED


def test_v11_cooldown_not_written_on_send_failure(send_side):
    """发送失败**不**写冷却：抖动不该把这条命中静默 30 分钟。"""
    hit = _fresh_hit()
    boom = _Boom()
    failed = _push(hit, opener=boom, dedupe=send_side)
    assert failed.reason == REASON_SEND_FAILED
    assert failed.attempts == 1
    rec = _Recorder()
    ok = _push(hit, opener=rec, dedupe=send_side)
    assert ok.reason == REASON_PUSHED
    assert rec.count == 1


def test_v11_cooldown_not_written_when_not_visible(send_side):
    rec = _Recorder()
    out = _push(fx.candidate_calibrated_hit(), opener=rec, dedupe=send_side)
    assert out.reason == REASON_NOT_VISIBLE
    assert send_side.cooled_down(dedupe_key(fx.candidate_calibrated_hit()), now=cn_now()) is False


def test_v11_dedupe_key_has_five_parts():
    key = dedupe_key(_fresh_hit())
    assert len(key) == 5
    assert key[2] == "000001"


def test_v11_missing_freq_lock_version_becomes_empty_string():
    key = dedupe_key(fx.unlabelled_hit())
    assert key[4] == "", "缺失字段补空串，不抛（去重键计算不该比闸更严）"


def test_v11_inflight_claim_blocks_duplicate():
    table = HitsDedupe()
    key = ("a", "b", "c", "d", "e")
    assert table.claim(key) is True
    assert table.claim(key) is False
    table.release(key)
    assert table.claim(key) is True


# ======================================================= V12 · 同 URL


def test_v12_same_url_as_common_group_still_sends(monkeypatch, caplog, send_side):
    """V12: 命中 URL == 共群 URL → **允许**发送 + 打 warning（不拒发、不回落）。"""
    import app.buy_sell_signals.hits_dingtalk as mod

    monkeypatch.setattr(mod, "hits_webhook_url", lambda: HITS_URL)
    monkeypatch.setattr(mod, "_common_group_webhook_url", lambda: HITS_URL)
    assert HD.shares_webhook_with_common_group() is True
    rec = _Recorder()
    with caplog.at_level("WARNING"):
        out = _push(_fresh_hit(), opener=rec, webhook=HITS_URL, dedupe=send_side)
    assert out.reason == REASON_PUSHED
    assert rec.count == 1
    assert any("注意力竞争" in r.message for r in caplog.records)


def test_v12_different_url_has_no_warning(monkeypatch, send_side):
    import app.buy_sell_signals.hits_dingtalk as mod

    monkeypatch.setattr(mod, "hits_webhook_url", lambda: HITS_URL)
    monkeypatch.setattr(mod, "_common_group_webhook_url", lambda: OLD_URL)
    assert HD.shares_webhook_with_common_group() is False


def test_v12_status_does_not_leak_url_or_secret(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "buy_sell_signals_hits_dingtalk_webhook_url", HITS_URL, raising=False)
    monkeypatch.setattr(settings, "buy_sell_signals_hits_dingtalk_secret", HITS_SECRET, raising=False)
    status = hits_dingtalk_status()
    blob = json.dumps(status.__dict__ if hasattr(status, "__dict__") else {
        f: getattr(status, f) for f in ("configured", "shares_with_common_group", "cooldown_min", "env_name")
    }, ensure_ascii=False, default=str)
    assert HITS_URL not in blob
    assert HITS_SECRET not in blob


# ======================================================= 结果对象与不抛


def test_outcome_flags():
    assert HitPushOutcome(reason=REASON_PUSHED, attempts=1).ok is True
    assert HitPushOutcome(reason=REASON_DEDUPE).ok is False
    assert HitPushOutcome(reason=REASON_DEDUPE).attempts == 0


def test_reason_closed_set():
    assert frozenset(
        {
            REASON_PUSHED,
            HD.REASON_ALREADY_IN_FLIGHT,
            REASON_NOT_VISIBLE,
            REASON_FIXTURE,
            REASON_DEDUPE,
            REASON_SEND_FAILED,
            HITS_WEBHOOK_NOT_CONFIGURED,
        }
    ) == REASON_KEYS


def test_try_push_hit_never_raises_on_broken_registry(send_side):
    """registry 是 `None`（定义全缺失）→ 不可见，不抛。"""
    rec = _Recorder()
    out = try_push_hit(
        _fresh_hit(),
        None,
        calib=_passed_all(),
        scope_symbols=SCOPE,
        opener=rec,
        allow_fixture=True,
        webhook=HITS_URL,
        dedupe=send_side,
    )
    assert out.reason == REASON_NOT_VISIBLE
    assert out.reject == VisibilityReject.DEFINITION_MISSING


def test_maybe_try_push_hit_swallows_unexpected_error(monkeypatch, send_side):
    import app.buy_sell_signals.hits_dingtalk as mod

    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mod, "try_push_hit", _boom)
    out = maybe_try_push_hit(_fresh_hit(), _full_registry(), calib=_passed_all())
    assert out.reason == REASON_SEND_FAILED


# ======================================================= G-T8 · 定义态零碰


def test_g_t8_definition_push_module_untouched():
    """定义态 `try_push` 仍认 `DINGTALK_WEBHOOK_URL`，不认命中键。"""
    import inspect

    from app.buy_sell_signals import push

    src = inspect.getsource(push)
    assert "settings.dingtalk_webhook_url" in src
    assert "BUY_SELL_SIGNALS_HITS_DINGTALK" not in src


def test_g_t8_hits_module_does_not_touch_definition_gates():
    for idx, line in _code_lines("app/buy_sell_signals/hits_dingtalk.py"):
        for name in ("PUSH_ENV_NAME", "DINGTALK_ENV_NAME", "push_env_enabled", "dingtalk_env_enabled"):
            assert name not in line, f"命中模块第 {idx} 行碰了定义态门: {name}"


def test_g_t8_definition_dingtalk_still_uses_common_group(send_side, monkeypatch):
    """定义态的实际发送路径仍打共群 URL —— 本拍没有把它改到命中通道。"""
    from app.buy_sell_signals.push import _emit_dingtalk

    calls: list[tuple[str, str]] = []

    def _fake_send(url, secret, title, text, opener=None, **kw):
        calls.append((url, title))

    import app.news.dingtalk as ding

    monkeypatch.setattr(ding, "send_markdown", _fake_send)
    from app.config import settings

    monkeypatch.setattr(settings, "dingtalk_webhook_url", OLD_URL, raising=False)
    monkeypatch.setattr(settings, "buy_sell_signals_hits_dingtalk_webhook_url", HITS_URL, raising=False)
    _emit_dingtalk(None, {"display_name": "放量突破", "disclaimer": "x"})
    assert calls and calls[0][0] == OLD_URL


# ======================================================= G-T9 / V8 · DSA_REVIEW 零碰


def test_v8_hits_module_does_not_reference_review_channel():
    for idx, line in _code_lines("app/buy_sell_signals/hits_dingtalk.py"):
        for name in ("DSA_REVIEW", "market_review_push", "review_webhook_not_configured"):
            assert name not in line, f"命中模块第 {idx} 行引用了复盘通道: {name}"


def test_v8_no_review_files_in_this_batch():
    """本派新增/改动文件里**没有**复盘相关文件（用 git 不可用时的替代：后缀扫描）。"""
    batch_files = {
        "backend/app/buy_sell_signals/hits_dingtalk.py",
        "backend/app/buy_sell_signals/constants.py",
        "backend/app/config.py",
        "backend/tests/test_buy_sell_signal_hits_dingtalk.py",
        "docs/buy-sell-signals-skeleton.md",
        ".env.example",
    }
    for path in batch_files:
        assert "market_review" not in path
        assert "dsa_review" not in path


# ======================================================= G-T10 / V6 · 空态


def test_g_t10_empty_message_byte_identical():
    assert C.EMPTY_HITS_MESSAGE == "暂无已校准的买卖信号"
    assert len(C.EMPTY_HITS_MESSAGE) == 10


def test_g_t10_hits_module_does_not_touch_empty_message():
    for idx, line in _code_lines("app/buy_sell_signals/hits_dingtalk.py"):
        assert "EMPTY_HITS_MESSAGE" not in line, f"第 {idx} 行不该碰空态常量"
        assert "暂无已校准" not in line, f"第 {idx} 行把空态文案写进了命中通道"


# ======================================================= G-T11 / V10 · 禁区


def test_v10_triggering_still_false():
    from app.buy_sell_signals.auction_abnormal_stub import AUCTION_ABNORMAL_TRIGGERING_ENABLED
    from app.buy_sell_signals.volume_breakout_stub import VOLUME_BREAKOUT_TRIGGERING_ENABLED

    assert VOLUME_BREAKOUT_TRIGGERING_ENABLED is False
    assert AUCTION_ABNORMAL_TRIGGERING_ENABLED is False


def test_g_t11_no_scheduler_or_lifespan_wiring():
    """命中模块里不出现调度器 / crontab / 扫全表的写法。"""
    for idx, line in _code_lines("app/buy_sell_signals/hits_dingtalk.py"):
        for banned in ("APScheduler", "BackgroundScheduler", "crontab", "list_all(", "add_job"):
            assert banned not in line, f"命中模块第 {idx} 行出现调度/扫表痕迹: {banned}"


def test_g_t11_no_mcp_or_order_path():
    for idx, line in _code_lines("app/buy_sell_signals/hits_dingtalk.py"):
        for banned in ("paper:trade", "place_order", "mcp", "submit_order"):
            assert banned not in line, f"命中模块第 {idx} 行出现下单痕迹: {banned}"


def test_g_t11_hits_store_protocol_still_read_only():
    from app.buy_sell_signals.hits_store import HitStore

    methods = {n for n in dir(HitStore) if not n.startswith("_")}
    assert methods == {"list_all"}, f"HitStore 仍须只读, 实际多出: {methods - {'list_all'}}"


# ======================================================= G-T12 · 文档


def test_g_t12_docs_has_p_ding_later_section():
    text = (REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md").read_text(encoding="utf-8")
    assert "P-ding-later" in text
    assert "BUY_SELL_SIGNALS_HITS_DINGTALK_WEBHOOK_URL" in text
    assert "hits_webhook_not_configured" in text


def test_g_t12_docs_state_three_channels_boundary():
    text = (REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md").read_text(encoding="utf-8")
    for token in ("定义态", "命中", "复盘"):
        assert token in text
    assert "不回落到" in text or "不回落" in text


def test_g_t12_docs_pending_hook_is_checked_off():
    """「明确不做」里那条命中钉钉挂档句应已被勾掉（删除线或标注已交付）。"""
    text = (REPO_ROOT / "docs" / "buy-sell-signals-skeleton.md").read_text(encoding="utf-8")
    markers = ("~~P-ding-later", "~~命中钉钉", "P-ding-later 已交付", "本拍即 P-ding-later")
    assert any(m in text for m in markers), "文档未勾掉 P-ding-later 挂档句"


def test_g_t12_disclaimer_module_text_unchanged():
    assert D.DISCLAIMER_TEXT == "仅供研究，不构成投资建议；执行须人工确认。"
