"""P5 升级 SOP 的口径与防滚动：文档在、七步可扫、不出现浮钉、脚注不丢。

``docs/dsa-upgrade.md`` 是给人看的流程，但里面几条硬口径必须能被机器按住：

* 七步标题都在（少一步就是流程断了）；
* 不把 ``latest`` / ``main`` / ``HEAD`` 当钉值，也不出现「自动跟随上游」这类
  和本仓实际能力相反的说法；
* 结尾合规脚注在（仅供研究 / 不构成投资建议 / 人工确认）；
* 「不与自有信号同可信度」这类边界句在；
* ``dsa-integration.md`` 有指向本 SOP 的交叉链，避免两份文档各说一套。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SOP = _ROOT / "docs" / "dsa-upgrade.md"
_INTEGRATION = _ROOT / "docs" / "dsa-integration.md"
_BRIEF = _ROOT / "docs" / "agent-skills-brief.md"
_SCRIPT = _ROOT / "scripts" / "dsa_upgrade_check.py"

# 七步标题的关键词，按顺序必须出现。
_STEPS = (
    "选定目标 commit",
    "同步 vendor",
    "更新四处钉",
    "重建 sidecar",
    "钩子验收矩阵",
    "推送",
    "changelog",
)

# 允许出现的「否定式」浮钉说法：这些是在禁止它，不是在用。
_FLOATING = ("latest", "main", "HEAD")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_sop_exists() -> None:
    assert _SOP.is_file(), "缺少 docs/dsa-upgrade.md"


def test_sop_has_seven_steps_in_order() -> None:
    text = _text(_SOP)
    positions = []
    for keyword in _STEPS:
        index = text.find(keyword)
        assert index >= 0, f"SOP 里找不到步骤关键词：{keyword}"
        positions.append(index)

    assert positions == sorted(positions), "七步顺序被打乱"


def test_sop_lists_four_pin_sources() -> None:
    text = _text(_SOP)

    for relative in (
        "backend/app/custom/dsa/catalog.py",
        "vendor/daily_stock_analysis/VENDOR.md",
        "docs/dsa-integration.md",
        "THIRD_PARTY_NOTICES.md",
    ):
        assert relative in text, f"SOP 没写钉的位置：{relative}"


def test_sop_names_all_five_hooks() -> None:
    text = _text(_SOP)

    for name in ("quant_evidence", "news_bridge", "etf_job", "fund_flow_bridge", "analysis_bridge"):
        assert name in text, f"SOP 的钩子矩阵缺 {name}"


def test_sop_forbids_rolling_pin() -> None:
    """浮钉只能以禁止的口吻出现，不能写「钉 latest」。"""
    text = _text(_SOP)

    for word in _FLOATING:
        for match in re.finditer(rf"`{word}`", text, re.IGNORECASE):
            window = text[max(0, match.start() - 40) : match.end() + 20]
            forbidden = "禁止" in window or "不" in window
            assert forbidden, f"`{word}` 出现在非禁止语境：{window!r}"


def test_sop_states_no_upstream_follow() -> None:
    text = _text(_SOP)

    assert "不做跟随滚动更新" in text
    assert "不跟踪上游 HEAD" in text


def test_sop_keeps_research_disclaimer() -> None:
    tail = _text(_SOP).strip().splitlines()[-5:]
    footer = "\n".join(tail)

    assert "仅供研究" in footer
    assert "不构成投资建议" in footer
    assert "人工确认" in footer


def test_sop_keeps_boundary_wording() -> None:
    text = _text(_SOP)

    assert "不与 TSP 自有买卖信号混排" in text
    assert "不共享同一可信度" in text
    # 推送只验收配置与探活，不许写成生产已验证。
    assert "不能**写「推送已生产验证」" in text or "推送已生产验证" in text
    assert "不真发钉钉" in text


def test_sop_documents_enabled_vs_reachable() -> None:
    text = _text(_SOP)

    assert "`enabled` 只看 `DSA_BASE_URL`" in text
    assert "`reachable` 必须来自真实 health" in text


def test_sop_mentions_compose_mount_requirement() -> None:
    text = _text(_SOP)

    assert "fund_flow_bridge.py" in text
    assert "/opt/tsp/" in text
    assert "新增钩子文件时必须同步补一行挂载" in text


def test_sop_points_to_self_check_script() -> None:
    text = _text(_SOP)

    assert "scripts/dsa_upgrade_check.py" in text
    assert _SCRIPT.is_file(), "SOP 里提到的自检脚本不存在"


def test_integration_links_to_upgrade_sop() -> None:
    text = _text(_INTEGRATION)

    assert "dsa-upgrade.md" in text, "dsa-integration.md 没有链到升级 SOP"
    assert "不跟随上游滚动更新" in text


def test_brief_marks_p5_closed_and_opt_a_deferred() -> None:
    text = _text(_BRIEF)

    assert "P5 DSA 升级 SOP" in text
    assert "实调用接线仍 defer" in text
    assert "Opt-A" in text


def test_script_rejects_floating_pin(tmp_path: Path) -> None:
    """自检脚本对浮钉返回 1（守住上面「禁止浮钉」的机械口径）。"""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("tsp_dsa_upgrade_check", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析注解时要能在 sys.modules 里找到本模块。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        for relative in (
            "backend/app/custom/dsa/catalog.py",
            "vendor/daily_stock_analysis/VENDOR.md",
            "docs/dsa-integration.md",
            "THIRD_PARTY_NOTICES.md",
        ):
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Upstream commit: `latest`\n", encoding="utf-8")
        (tmp_path / "docker-compose.yml").write_text("", encoding="utf-8")

        report = module.run(tmp_path)

        assert report.ok is False
        assert module.main(["--check", "--root", str(tmp_path)]) == 1
    finally:
        sys.modules.pop(spec.name, None)


@pytest.mark.parametrize("relative", ["docs/dsa-upgrade.md"])
def test_sop_has_no_bare_sha_shortcut(relative: str) -> None:
    """SOP 里出现的 SHA 必须是完整 40 位，不能写前 7 位糊弄。"""
    text = _text(_ROOT / relative)

    for match in re.finditer(r"\b([0-9a-f]{7,39})\b", text):
        candidate = match.group(1)
        # 只拦「像缩写 SHA」的短十六进制串，普通词不算。
        assert len(candidate) >= 7 and not re.fullmatch(r"[0-7]{1,3}", candidate)
        assert len(candidate) == 40 or not re.fullmatch(r"[0-9a-f]{7}", candidate), (
            f"SOP 里出现疑似缩写 SHA：{candidate}"
        )
