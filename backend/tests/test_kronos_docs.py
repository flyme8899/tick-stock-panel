"""口径与零碰：文档说对话、不夸大；已合的 #218/#219/#220/#221 不受影响（G-T8 / G-T10）。

两类断言：

**G-T8 口径** —— ``docs/kronos-integration.md`` 必须写明 S2 不通过、S3 仅提醒 /
只降档、门禁四前置、权重纪律、卸载方式、脚注；并且**不得**出现「信号可做」
「已过门禁」「已上线进 BSS」这类没做到的说法。

**G-T10 零碰** —— 用静态扫描守住「本拍没动别人」：OpenBB 路径、DSA 升级 SOP、
lookahead 夹具、BSS 接线、三 env。这不是重复 CI，而是让「越界」在本地就红。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DOC = _ROOT / "docs" / "kronos-integration.md"
_BRIEF = _ROOT / "docs" / "agent-skills-brief.md"

# 必须写进文档的关键词（语义不可丢）。
_REQUIRED = (
    "enabled",
    "reachable",
    "gate_passed",
    "S2",
    "仅提醒",
    "降档",
    "费后",
    "样本外",
    "权重",
    "卸载",
    "仅供研究",
)

# 没做到就不许写的说法。
_FORBIDDEN_CLAIMS = (
    "信号可做",
    "已上线进 BSS",
    "预测可上线",
    "已过门禁",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_doc_exists() -> None:
    assert _DOC.is_file(), "缺少 docs/kronos-integration.md"


@pytest.mark.parametrize("keyword", _REQUIRED)
def test_doc_covers_keyword(keyword: str) -> None:
    assert keyword in _text(_DOC), f"文档缺关键词：{keyword}"


@pytest.mark.parametrize("phrase", _FORBIDDEN_CLAIMS)
def test_doc_does_not_claim_unfinished(phrase: str) -> None:
    """这些词只能以否定形式出现（「不能说信号可做」），不能当结论陈述。"""
    text = _text(_DOC)
    for match in re.finditer(re.escape(phrase), text):
        window = text[max(0, match.start() - 30) : match.end() + 10]
        negated = any(marker in window for marker in ("禁", "不", "未", "别", "勿"))
        assert negated, f"文档里把「{phrase}」当结论写了：{window!r}"


def test_doc_keeps_disclaimer_footer() -> None:
    tail = "\n".join(_text(_DOC).strip().splitlines()[-5:])

    assert "仅供研究" in tail
    assert "不构成投资建议" in tail
    assert "人工确认" in tail


def test_doc_states_gate_four_prerequisites() -> None:
    text = _text(_DOC)

    for item in ("费后", "样本外", "passed", "降档"):
        assert item in text


def test_doc_states_no_inference_this_batch() -> None:
    text = _text(_DOC)

    assert "本拍不新跑推理" in text or "不新跑推理" in text


def test_doc_states_uninstall_steps() -> None:
    text = _text(_DOC)

    assert "profiles" in text or "profile" in text
    assert "删除" in text or "删掉" in text


def test_doc_states_weights_not_in_repo() -> None:
    text = _text(_DOC)

    assert "不入仓" in text or "不进仓" in text or "不进 git" in text


def test_brief_marks_g6_closed_without_overclaim() -> None:
    text = _text(_BRIEF)

    assert "Kronos" in text
    assert "接旧骨架" in text
    # 收口句必须带门禁限制。
    assert "门禁" in text
    assert "信号可做" not in text or "禁" in text


# ── G-T10 零碰静态审 ─────────────────────────────────

def test_openbb_paths_untouched() -> None:
    """OpenBB 插件与 sidecar 容器定义仍在原位、未被改名。"""
    for relative in (
        "backend/app/plugins/openbb/client.py",
        "backend/app/plugins/openbb/plugin.yaml",
        "backend/app/custom/openbb/routes.py",
        "docker/openbb/Dockerfile",
    ):
        assert (_ROOT / relative).is_file(), f"OpenBB 路径缺失：{relative}"


def test_dsa_upgrade_sop_untouched() -> None:
    """#221 的 SOP、钉、hook 挂载语义都还在。"""
    sop = _text(_ROOT / "docs" / "dsa-upgrade.md")
    compose = _text(_ROOT / "docker-compose.yml")
    catalog = _text(_ROOT / "backend" / "app" / "custom" / "dsa" / "catalog.py")

    assert "四处钉" in sop
    assert "fund_flow_bridge.py:/opt/tsp/fund_flow_bridge.py:ro" in compose
    assert re.search(r'UPSTREAM_COMMIT = "[0-9a-f]{40}"', catalog)


def test_lookahead_fixture_untouched() -> None:
    """lookahead 夹具目录与测试仍在原位；本拍没改它。"""
    assert (_ROOT / "backend" / "app" / "backtest" / "lookahead_fixture").is_dir()
    assert (_ROOT / "backend" / "tests" / "backtest" / "test_lookahead_fixture.py").is_file()


def test_env_example_three_switches_unchanged() -> None:
    """三 env 在 .env.example 里的默认值没被本拍改写。"""
    text = _text(_ROOT / ".env.example")
    pattern = r"^(BUY_SELL_SIGNALS_(?:PERSIST|PUSH)|DINGTALK\w*)=(.*)$"
    found = dict(re.findall(pattern, text, re.MULTILINE))

    # 只要求「存在且没被赋成 1」——具体默认值由既有批次决定。
    for name, value in found.items():
        assert value.strip() in {"", "0", "false", "False", "off"}, f"{name} 默认值被改了：{value}"


def test_kronos_env_names_do_not_collide_with_bss() -> None:
    text = _text(_ROOT / ".env.example")
    kronos_lines = [line for line in text.splitlines() if "KRONOS" in line]

    assert kronos_lines, ".env.example 里没有 KRONOS 配置项"
    for line in kronos_lines:
        assert "BUY_SELL_SIGNALS" not in line
        assert "DINGTALK" not in line


def test_no_skipped_kronos_tests() -> None:
    """禁 skip/xfail 装绿。"""
    offenders: list[str] = []
    for path in (_ROOT / "backend" / "tests").glob("test_kronos*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            banned = ("pytest.skip", "pytest.xfail", "@pytest.mark.skip", "@pytest.mark.xfail")
            if stripped.startswith(banned):
                offenders.append(f"{path.name}:{lineno}")

    assert offenders == [], f"Kronos 测试里有 skip/xfail：{offenders}"
