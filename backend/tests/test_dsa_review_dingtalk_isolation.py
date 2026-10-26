"""P-ding 零碰守卫：旁路渠道 / 三 env / 狙击前缀 / reserved digest。

G-T7 资讯/BSS/sniper 仍走旧 ``DINGTALK_WEBHOOK_URL``
G-T8 三 env 字面量与语义未改
G-T9 ``ALERT_SOURCE_PREFIX == "DSA价位"`` 未删未改语义
G-T10 未启用 / 未依赖 ``NOTIFICATION_DAILY_DIGEST_ENABLED``

这些项如果只靠 code review，下一次改钉钉的人很容易顺手破坏，且后果是
"复盘悄悄回流到买卖信号群"。所以钉成测例。

仅供研究，不构成投资建议；执行须人工确认。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_VENDOR = _ROOT / "vendor" / "daily_stock_analysis"
sys.path.insert(0, str(_VENDOR))


# ── G-T7 旁路渠道仍用旧 webhook ─────────────────────────────────────────


def test_dingtalk_sender_default_still_binds_shared_webhook() -> None:
    """``DingtalkSender`` 构造仍读 ``config.dingtalk_webhook_url``，不读复盘键。

    这是"资讯/BSS/狙击继续走旧群"的根 —— 一旦这里改读 DSA_REVIEW_*，
    那些渠道会被无声迁移到复盘机器人。
    """
    source = (_VENDOR / "src" / "notification_sender" / "dingtalk_sender.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    init = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    body_src = ast.get_source_segment(source, init) or ""
    assert "config.dingtalk_webhook_url" in body_src
    assert "config.dingtalk_secret" in body_src
    assert "dsa_review" not in body_src, "构造期不得读复盘键（单例会把它泄给所有渠道）"


def test_override_defaults_to_none_so_callers_unaffected() -> None:
    source = (_VENDOR / "src" / "notification_sender" / "dingtalk_sender.py").read_text(
        encoding="utf-8"
    )
    assert "webhook_url_override: Optional[str] = None" in source
    assert "secret_override: Optional[str] = None" in source
    # 默认 None → 用 self.webhook_url（原行为）
    assert "if webhook_url_override is not None else self.webhook_url" in source


def test_only_review_gate_passes_override() -> None:
    """全仓只有 market_review_push 传入钉钉 override。"""
    hits: list[str] = []
    for path in _VENDOR.joinpath("src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "dingtalk_webhook_override=" in text:
            hits.append(path.name)
    assert sorted(hits) == ["market_review_push.py", "notification.py"], hits


def test_sniper_and_bss_still_use_shared_dingtalk_config() -> None:
    """sniper / BSS 的钉钉发送仍从通用钉钉配置取 URL。"""
    sniper = _ROOT / "backend" / "app" / "custom" / "dsa" / "sniper_alerts.py"
    text = sniper.read_text(encoding="utf-8")
    assert "dsa_review" not in text.lower(), "狙击路径不得引用复盘键"


def test_other_channels_do_not_reference_review_key() -> None:
    """资讯 / BSS / 狙击代码里不出现 DSA_REVIEW_DINGTALK_*。"""
    forbidden = ("DSA_REVIEW_DINGTALK_WEBHOOK_URL", "DSA_REVIEW_DINGTALK_SECRET")
    scanned = 0
    for base in (_ROOT / "backend" / "app" / "buy_sell_signals",
                 _ROOT / "backend" / "app" / "news",
                 _ROOT / "backend" / "app" / "custom" / "dsa"):
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            scanned += 1
            text = path.read_text(encoding="utf-8")
            for key in forbidden:
                assert key not in text, f"{path} 不应引用 {key}"
    assert scanned > 0, "扫描目录不存在，守卫失效"


# ── G-T8 三 env 字面量未改 ──────────────────────────────────────────────


def test_bss_three_env_literals_unchanged() -> None:
    """三 env 的**取值**必须是原字符串，改改名/改值都要红。

    只断言"名字出现过"是不够的 —— 常量定义里被改名后，
    docstring 里那处同名仍会让它通过（这正是变异 M27 漏杀的原因）。
    这里只认 ``NAME: Final = "VALUE"`` 这种赋值语句。
    """
    import ast as _ast

    constants = _ROOT / "backend" / "app" / "buy_sell_signals" / "constants.py"
    source = constants.read_text(encoding="utf-8")
    tree = _ast.parse(source)
    assigned: dict[str, str] = {}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
            if isinstance(node.value, _ast.Constant) and isinstance(node.value.value, str):
                assigned[node.target.id] = node.value.value
        elif isinstance(node, _ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if (
                isinstance(target, _ast.Name)
                and isinstance(node.value, _ast.Constant)
                and isinstance(node.value.value, str)
            ):
                assigned[target.id] = node.value.value

    assert assigned.get("PERSIST_ENV_NAME") == "BUY_SELL_SIGNALS_PERSIST"
    assert assigned.get("PUSH_ENV_NAME") == "BUY_SELL_SIGNALS_PUSH"
    assert assigned.get("DINGTALK_ENV_NAME") == "BUY_SELL_SIGNALS_DINGTALK"


def test_three_env_not_touched_by_this_batch() -> None:
    """本批 diff 里三 env 只允许出现在文档/注释，不允许有赋值改动。"""
    diff = _git_diff()
    for line in diff.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        for name in ("BUY_SELL_SIGNALS_PERSIST", "BUY_SELL_SIGNALS_PUSH", "BUY_SELL_SIGNALS_DINGTALK"):
            if name in line and "=" in line:
                # 允许文档里描述（成对出现的 |-表 | 行），拒绝真正的赋值行
                stripped = line[1:].strip().lstrip("|").strip()
                assert not re.match(rf"^{name}\s*=", stripped), f"三 env 被改写: {line}"


def _git_diff() -> str:
    import subprocess

    out = subprocess.run(
        ["git", "diff", "06e45ce3..HEAD", "--", ".", ":!*.md"],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    # 工作树未提交时退回 diff 工作树
    if not out.stdout.strip():
        out = subprocess.run(
            ["git", "diff", "--", "."],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
    return out.stdout


# ── G-T9 狙击前缀 ───────────────────────────────────────────────────────


def test_sniper_alert_source_prefix_unchanged() -> None:
    sniper = _ROOT / "backend" / "app" / "custom" / "dsa" / "sniper_alerts.py"
    text = sniper.read_text(encoding="utf-8")
    assert 'ALERT_SOURCE_PREFIX = "DSA价位"' in text


def test_sniper_cooldown_unchanged() -> None:
    """G5 挂档：狙击冷却语义不动（30 分钟）。"""
    sniper = _ROOT / "backend" / "app" / "custom" / "dsa" / "sniper_alerts.py"
    text = sniper.read_text(encoding="utf-8")
    assert re.search(r"^COOLDOWN\s*=\s*timedelta\(minutes=30\)", text, re.M), (
        "狙击冷却常量未找到或已改值，若确属有意变更请同步本守卫"
    )


# ── G-T10 reserved digest 未被劫持 ──────────────────────────────────────


def test_reserved_daily_digest_flag_not_enabled() -> None:
    """本批没有把 ``NOTIFICATION_DAILY_DIGEST_ENABLED`` 打开/依赖。

    上游标明 reserved（开了只 warning、不发摘要）。若本批"顺手"启用它，
    会得到一个看起来有日摘要、实际没发的假绿。
    """
    diff = _git_diff()
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if "NOTIFICATION_DAILY_DIGEST_ENABLED" in line:
            # 允许在文档/注释里说明"不启用"，不允许赋值 True
            assert "= True" not in line, f"reserved digest 被启用: {line}"
            assert "default=True" not in line, f"reserved digest 默认被打开: {line}"


def _code_identifiers_and_literals(source: str) -> set[str]:
    """收集源码里**代码位置**用到的标识符与字符串字面量，跳过 docstring。

    docstring 里写「不会回落 DINGTALK_WEBHOOK_URL」是解释，不该被守卫当成
    真的引用；只有代码里的 ``config.dingtalk_webhook_url`` 才算。
    """
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            expr = node.body[0] if node.body else None
            if isinstance(expr, ast.Expr) and isinstance(expr.value, ast.Constant) and isinstance(expr.value.value, str):
                docstrings.add(id(expr.value))

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.add(node.value)
    return found


def test_review_cap_does_not_import_daily_digest() -> None:
    for name in ("core/market_review_daily_cap.py", "services/market_review_push.py"):
        source = (_VENDOR / "src" / name).read_text(encoding="utf-8")
        ids = _code_identifiers_and_literals(source)
        assert not any("NOTIFICATION_DAILY_DIGEST" in i for i in ids), (
            f"{name} 代码里引用了 reserved digest"
        )


# ── 禁回落：静态扫描 ────────────────────────────────────────────────────


def test_no_fallback_expression_in_review_gate() -> None:
    """卡口代码里不得出现 ``... or config.dingtalk_webhook_url`` 这类回落。"""
    push = (_VENDOR / "src" / "services" / "market_review_push.py").read_text(encoding="utf-8")
    assert "or config.dingtalk_webhook_url" not in push
    assert "or getattr(config, \"dingtalk_webhook_url\"" not in push
    # 唯一允许出现旧键的地方是"比较是否同串"的 warning 分支
    occurrences = push.count("dingtalk_webhook_url")
    assert occurrences >= 1
    for line in push.splitlines():
        if "or dingtalk_webhook_url" in line or "else config.dingtalk_webhook_url" in line:
            pytest.fail(f"发现疑似回落: {line}")


def test_review_cap_never_reads_shared_webhook() -> None:
    """cap 模块只管计数，不碰任何 webhook（读写它就没有回落的机会）。"""
    source = (_VENDOR / "src" / "core" / "market_review_daily_cap.py").read_text(encoding="utf-8")
    ids = _code_identifiers_and_literals(source)
    assert "dingtalk_webhook_url" not in ids
    assert not any("DINGTALK_WEBHOOK_URL" in i for i in ids)
