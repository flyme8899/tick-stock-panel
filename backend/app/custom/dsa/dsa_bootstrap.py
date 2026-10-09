"""Start the DSA sidecar after installing the TSP quant-evidence hook.

Supported entrypoints (``scripts/dsa.sh``, ``scripts/dsa.ps1``, and the root
compose service) run this file instead of calling ``main.py`` directly.
Upstream skill text is assembled inside the sidecar, including scheduled runs,
and ``AnalyzeRequest`` has no free-text field. Patching the loader in-process
avoids editing ``vendor/daily_stock_analysis``.

Unset or ``off`` ``TSP_QUANT_EVIDENCE_FILE`` leaves skill loading unchanged.
A hook failure still starts DSA.
"""
from __future__ import annotations

import logging
import os
import runpy
import sys
from pathlib import Path

logger = logging.getLogger("tsp.dsa.bootstrap")

_OFF = {"", "off", "0", "false", "none"}


def _evidence_enabled() -> bool:
    raw = os.environ.get("TSP_QUANT_EVIDENCE_FILE")
    if raw is None:
        return False
    return raw.strip().lower() not in _OFF


def _load_renderer():
    import importlib.util

    path = Path(__file__).resolve().with_name("quant_evidence.py")
    spec = importlib.util.spec_from_file_location("tsp_quant_evidence", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ensure_dsa_import_path() -> None:
    root = Path.cwd()
    if (root / "main.py").is_file() and (root / "src").is_dir():
        entry = str(root)
        if entry not in sys.path:
            sys.path.insert(0, entry)


def _wrap(original, renderer):
    def wrapped(filepath):
        skill = original(filepath)
        if not _evidence_enabled():
            return skill
        try:
            note = renderer.render_skill_appendix(
                skill.name,
                Path(os.environ["TSP_QUANT_EVIDENCE_FILE"].strip()),
            )
        except Exception:  # noqa: BLE001 - 证据失败不能挡住技能加载
            logger.debug("量化证据未追加到 %s", getattr(skill, "name", ""), exc_info=True)
            return skill
        if note and "**量化回测参考**" not in (skill.instructions or ""):
            skill.instructions = f"{skill.instructions.rstrip()}\n\n{note}"
        return skill

    wrapped._tsp_quant_evidence = True  # type: ignore[attr-defined]
    return wrapped


def install() -> None:
    """Append quant notes while YAML / SKILL.md skills are loaded."""
    if not _evidence_enabled():
        return
    _ensure_dsa_import_path()
    import src.agent.skills.base as base

    renderer = _load_renderer()
    for name in ("load_skill_from_yaml", "load_skill_from_markdown"):
        current = getattr(base, name)
        if getattr(current, "_tsp_quant_evidence", False):
            continue
        wrapped = _wrap(current, renderer)
        setattr(base, name, wrapped)
        package = sys.modules.get("src.agent.skills")
        if package is not None and hasattr(package, name):
            setattr(package, name, wrapped)
    logger.info("已安装量化证据钩子：%s", os.environ.get("TSP_QUANT_EVIDENCE_FILE"))


def _install_news_bridge() -> None:
    """把 TSP 资讯接到 DSA 情报库。文件缺失或补丁失败都不挡住启动。"""
    import importlib.util

    path = Path(__file__).resolve().with_name("news_bridge.py")
    if not path.is_file():
        logger.info("未找到 news_bridge.py，DSA 不拉取 TSP 资讯")
        return
    spec = importlib.util.spec_from_file_location("tsp_news_bridge", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install()


def main() -> None:
    try:
        install()
    except Exception:
        logger.exception("量化证据钩子未安装，继续启动 DSA")
    try:
        _install_news_bridge()
    except Exception:
        logger.exception("TSP 资讯桥未安装，继续启动 DSA")
    if len(sys.argv) < 2:
        sys.argv = ["main.py", "--serve-only", "--host", "0.0.0.0", "--port", "8000"]
    else:
        sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
