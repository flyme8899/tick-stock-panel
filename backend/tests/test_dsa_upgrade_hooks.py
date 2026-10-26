"""H1–H5 钩子矩阵：文件在 / 文件缺 两态都不许把 DSA 拖住。

``backend/app/custom/dsa/dsa_bootstrap.py::main()`` 依次装五个钩子：

===========  ==========================  ==================================
编号          钩子                        装载点
===========  ==========================  ==================================
H1            quant_evidence.py           包住 skill 加载器，追加量化回测段
H2            news_bridge.py              ``install()``
H3            etf_job.py                  ``install_import_hook()``
H4            fund_flow_bridge.py         ``install()``
H5            analysis_bridge.py          ``install()``
===========  ==========================  ==================================

契约只有两条，这里逐条按住：

1. 钩子文件缺失 -> 打 ``未找到 …`` 的 info 日志并跳过，不抛、不假装装上。
2. 钩子内部报错 -> ``logger.exception`` 后继续装下一个，``main()`` 仍然往下走到
   DSA 入口。**没有任何「升级成功」式的假绿**：日志里不许出现成功字样。

测试只 monkeypatch ``__file__`` 指向临时目录和 ``runpy.run_path``（免得真的起
DSA），不改任何产品代码。
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.custom.dsa import dsa_bootstrap

_HOOKS = {
    "H1": "quant_evidence.py",
    "H2": "news_bridge.py",
    "H3": "etf_job.py",
    "H4": "fund_flow_bridge.py",
    "H5": "analysis_bridge.py",
}

# 文件缺失时的原文日志，逐字对齐产品代码。
_MISSING_LOG = {
    "H2": "未找到 news_bridge.py，DSA 不拉取 TSP 资讯",
    "H3": "未找到 etf_job.py，DSA 不提供 ETF 轮动入口",
    "H4": "未找到 fund_flow_bridge.py，DSA 不读取资金进出",
    "H5": "未找到 analysis_bridge.py，DSA 不注入个股上下文",
}

# 装完一个钩子后 install 里打的成功日志（H1 在 install() 末尾）。
_INSTALLED_LOG = {
    "H1": "已安装量化证据钩子",
    "H2": "已安装 TSP 资讯桥",
    "H3": "已注册 ETF 轮动入口",
    "H4": "已安装资金进出桥",
    "H5": "已安装个股上下文与价位登记",
}

_BOOTSTRAP_MODULE = "tsp_hook_probe"


def _isolate(monkeypatch, directory: Path) -> None:
    """把 bootstrap 的 ``__file__`` 挪到空目录，钩子文件由用例自己铺。"""
    monkeypatch.setattr(dsa_bootstrap, "__file__", str(directory / "dsa_bootstrap.py"))


@pytest.fixture
def started(monkeypatch):
    """截住 ``runpy.run_path``，记录 main() 是否走到了 DSA 入口。"""
    calls: list[str] = []

    def fake_run_path(path, run_name=None):  # noqa: ANN001, ANN202
        calls.append(path)
        return {}

    monkeypatch.setattr(dsa_bootstrap.runpy, "run_path", fake_run_path)
    monkeypatch.setattr(dsa_bootstrap.sys, "argv", ["main.py"])
    return calls


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch):
    """默认不带令牌、不带量化证据文件，避免真去连外网。"""
    for name in ("NEWS_DSA_FEED_TOKEN", "TSP_NEWS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TSP_QUANT_EVIDENCE_FILE", "off")


def test_every_hook_file_exists_in_repo() -> None:
    hook_dir = Path(dsa_bootstrap.__file__).resolve().parent

    missing = [name for name in _HOOKS.values() if not (hook_dir / name).is_file()]

    assert missing == [], f"钩子文件缺失：{missing}"


def test_main_installs_all_five_hooks_in_order(monkeypatch, started) -> None:
    """五个钩子按 H1→H5 顺序装，且每次都是「先文件存在、再调用」。"""
    order: list[str] = []
    for code, name in _HOOKS.items():
        original = getattr(dsa_bootstrap, f"_install_{_suffix(code)}", None) or getattr(
            dsa_bootstrap, "install"
        )
        assert original is not None, name

    def record(code: str):
        def _spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            order.append(code)

        return _spy

    monkeypatch.setattr(dsa_bootstrap, "install", record("H1"))
    monkeypatch.setattr(dsa_bootstrap, "_install_news_bridge", record("H2"))
    monkeypatch.setattr(dsa_bootstrap, "_install_etf_job", record("H3"))
    monkeypatch.setattr(dsa_bootstrap, "_install_fund_flow_bridge", record("H4"))
    monkeypatch.setattr(dsa_bootstrap, "_install_analysis_bridge", record("H5"))

    dsa_bootstrap.main()

    assert order == ["H1", "H2", "H3", "H4", "H5"]
    assert started == ["main.py"]


def _suffix(code: str) -> str:
    return {
        "H1": "quant_evidence",
        "H2": "news_bridge",
        "H3": "etf_job",
        "H4": "fund_flow_bridge",
        "H5": "analysis_bridge",
    }[code]


@pytest.mark.parametrize("code", ["H2", "H3", "H4", "H5"])
def test_missing_hook_file_logs_and_skips(monkeypatch, caplog, code, tmp_path: Path) -> None:
    """文件缺：打指定 info，不抛，不出现「已安装」假绿。"""
    _isolate(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO, logger="tsp.dsa.bootstrap"):
        getattr(dsa_bootstrap, f"_install_{_suffix(code)}")()

    messages = [record.getMessage() for record in caplog.records]
    assert _MISSING_LOG[code] in messages
    assert not any(_INSTALLED_LOG[code] in message for message in messages)


@pytest.mark.parametrize("code", ["H2", "H3", "H4", "H5"])
def test_present_hook_file_reaches_install(monkeypatch, caplog, code, tmp_path: Path) -> None:
    """文件在：装载到 ``install()`` / ``install_import_hook()``，并打成功日志。

    钩子用 ``spec_from_file_location`` 装、不写进 ``sys.modules``，所以探针把
    调用记在进程环境变量里，用例再读回来。
    """
    name = _HOOKS[code]
    entry = "install_import_hook" if code == "H3" else "install"
    marker = f"TSP_PROBE_{code}"
    monkeypatch.delenv(marker, raising=False)
    (tmp_path / name).write_text(
        "import os\n\n\n"
        f"def {entry}(*args, **kwargs):\n"
        f"    os.environ['{marker}'] = 'called'\n",
        encoding="utf-8",
    )
    _isolate(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO, logger="tsp.dsa.bootstrap"):
        getattr(dsa_bootstrap, f"_install_{_suffix(code)}")()

    assert dsa_bootstrap.os.environ.get(marker) == "called", f"{name} 的 {entry}() 没被调用"
    messages = [record.getMessage() for record in caplog.records]
    assert _MISSING_LOG[code] not in messages


@pytest.mark.parametrize("code", ["H2", "H3", "H4", "H5"])
def test_broken_hook_does_not_block_startup(monkeypatch, caplog, code, started, tmp_path: Path) -> None:
    """钩子内部炸：exception 日志 + 继续，DSA 入口照常被调用。"""
    (tmp_path / _HOOKS[code]).write_text("raise RuntimeError('探针故意失败')\n", encoding="utf-8")
    _isolate(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO, logger="tsp.dsa.bootstrap"):
        dsa_bootstrap.main()

    assert started == ["main.py"], "钩子失败不该挡住 DSA 启动"
    failures = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert any(record.exc_info for record in failures), "失败必须带异常栈，不能只吞掉"


@pytest.mark.parametrize("code", ["H2", "H3", "H4", "H5"])
def test_missing_files_never_pretend_success(monkeypatch, caplog, started, tmp_path: Path, code) -> None:
    """五个文件全缺：四条 info + H1 的 off 日志，日志里没有任何成功字样。"""
    _isolate(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO, logger="tsp.dsa.bootstrap"):
        dsa_bootstrap.main()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    for expected in _MISSING_LOG.values():
        assert expected in messages
    assert _INSTALLED_LOG["H2"] not in messages
    assert _INSTALLED_LOG["H4"] not in messages
    assert "升级成功" not in messages
    assert started == ["main.py"]


@pytest.mark.parametrize("value", ["", "off", "OFF", "0", "false", "none", "  "])
def test_h1_disabled_values_do_not_wrap(monkeypatch, value) -> None:
    """H1 env 关闭态：一个加载器都不包。"""
    monkeypatch.setenv("TSP_QUANT_EVIDENCE_FILE", value)
    base = SimpleNamespace(load_skill_from_yaml=lambda p: p, load_skill_from_markdown=lambda p: p)
    monkeypatch.setitem(dsa_bootstrap.sys.modules, "src.agent.skills.base", base)

    dsa_bootstrap.install()

    assert not getattr(base.load_skill_from_yaml, "_tsp_quant_evidence", False)
    assert not getattr(base.load_skill_from_markdown, "_tsp_quant_evidence", False)


@pytest.mark.parametrize("value", ["/opt/tsp/quant_evidence.yaml", " evidence.yaml "])
def test_h1_enabled_value_wraps_both_loaders(monkeypatch, caplog, value, tmp_path: Path) -> None:
    """H1 env 打开态：两个加载器都被包，且重复调用不重复包。"""
    monkeypatch.setenv("TSP_QUANT_EVIDENCE_FILE", value)
    (tmp_path / "quant_evidence.py").write_text(
        "def render_skill_appendix(skill_name, path=None):\n    return '**量化回测参考** 探针'\n",
        encoding="utf-8",
    )
    _isolate(monkeypatch, tmp_path)

    def loader(filepath):  # noqa: ANN001, ANN202
        return SimpleNamespace(name="probe", instructions="原说明")

    base = SimpleNamespace(load_skill_from_yaml=loader, load_skill_from_markdown=loader)
    # install() 里是 `import src.agent.skills.base as base`，只塞 sys.modules 不够。
    monkeypatch.setitem(dsa_bootstrap.sys.modules, "src.agent.skills.base", base)
    fake_src = ModuleType("src")
    fake_src.__path__ = []  # type: ignore[attr-defined]
    fake_agent = ModuleType("src.agent")
    fake_agent.__path__ = []  # type: ignore[attr-defined]
    fake_skills = ModuleType("src.agent.skills")
    fake_skills.base = base  # type: ignore[attr-defined]
    for name, module in (("src", fake_src), ("src.agent", fake_agent), ("src.agent.skills", fake_skills)):
        monkeypatch.setitem(dsa_bootstrap.sys.modules, name, module)

    with caplog.at_level(logging.INFO, logger="tsp.dsa.bootstrap"):
        dsa_bootstrap.install()
        once = base.load_skill_from_yaml
        dsa_bootstrap.install()

    assert getattr(base.load_skill_from_yaml, "_tsp_quant_evidence", False)
    assert getattr(base.load_skill_from_markdown, "_tsp_quant_evidence", False)
    assert base.load_skill_from_yaml is once, "重复调用不该再包一层"
    assert base.load_skill_from_yaml("skill.yaml").instructions.endswith("**量化回测参考** 探针")
    assert any("已安装量化证据钩子" in record.getMessage() for record in caplog.records)
