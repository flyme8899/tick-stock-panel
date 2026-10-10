"""在 DSA 进程内执行 ETF 轮动。

TSP 的 app 镜像不含 ``vendor/daily_stock_analysis``。``dsa_bootstrap`` 在
sidecar 导入 FastAPI 应用时挂上 ``POST /api/v1/tsp/etf-rotation``，由该进程
在自己的工作目录里执行固定命令。请求体不能改变参数；股票池和成本只读环境变量。
"""
from __future__ import annotations

import importlib.machinery
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("tsp.dsa.etf_job")

COMMAND = "python main.py --etf-rotation --no-notify"
_ARGV = ("main.py", "--etf-rotation", "--no-notify")
_TIMEOUT_SECONDS = 180
_LOCK = threading.Lock()


def _root_candidates() -> tuple[Path, ...]:
    return (Path.cwd(), Path("/app"))


def dsa_root() -> Path | None:
    """DSA 源码所在目录。优先当前工作目录，其次镜像里的 /app。"""
    seen: set[Path] = set()
    for candidate in _root_candidates():
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "main.py").is_file() and (resolved / "src").is_dir():
            return resolved
    return None


def run_job() -> dict:
    """执行固定的 ETF 轮动命令。重叠调用直接失败，不另起一套进程。"""
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "detail": "ETF 轮动正在运行", "command": COMMAND}
    try:
        return _run_locked()
    except Exception as exc:  # 接口要返回原因，不能变成未捕获的 500
        logger.exception("ETF 轮动失败")
        return {"ok": False, "detail": str(exc) or "ETF 轮动失败", "command": COMMAND}
    finally:
        _LOCK.release()


def _run_locked() -> dict:
    root = dsa_root()
    if root is None:
        return {"ok": False, "detail": "DSA 工作目录里没有 main.py", "command": COMMAND}
    argv = [sys.executable, *_ARGV]
    try:
        completed = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "ETF 轮动超时", "command": COMMAND}
    output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
    logger.info("ETF 轮动结束，退出码 %s", completed.returncode)
    return {
        "ok": completed.returncode == 0,
        "code": completed.returncode,
        "detail": output[-8000:],
        "command": COMMAND,
    }


def install(app: Any) -> None:
    """把轮动入口挂到已经创建好的 FastAPI 应用上。重复调用无效果。"""
    if getattr(app.state, "tsp_etf_rotation", False):
        return
    app.add_api_route(
        "/api/v1/tsp/etf-rotation",
        run_job,
        methods=["POST"],
        include_in_schema=False,
    )
    app.state.tsp_etf_rotation = True
    logger.info("已注册 ETF 轮动入口 POST /api/v1/tsp/etf-rotation")


class _WrappingLoader:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create_module(self, spec: Any) -> Any:
        create = getattr(self._inner, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module: Any) -> None:
        self._inner.exec_module(module)
        app = getattr(module, "app", None)
        if app is None:
            return
        try:
            install(app)
        except Exception:
            logger.exception("ETF 轮动入口未注册")


class _EtfAppFinder:
    """在 DSA 导入应用之后挂上轮动入口。此时 .env 已经由 main.py 加载。"""

    _tsp_etf_job = True

    def __init__(self, fullname: str = "api.app") -> None:
        self.fullname = fullname
        self._busy = False

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if fullname != self.fullname or self._busy:
            return None
        self._busy = True
        try:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        finally:
            self._busy = False
        if spec is None or spec.loader is None:
            return None
        if not isinstance(spec.loader, _WrappingLoader):
            spec.loader = _WrappingLoader(spec.loader)
        return spec


def install_import_hook(fullname: str = "api.app") -> None:
    """DSA 稍后导入 ``api.app`` 时注册轮动入口。已经导入过则立刻注册。"""
    existing = sys.modules.get(fullname)
    if existing is not None and getattr(existing, "app", None) is not None:
        install(existing.app)
        return
    for item in sys.meta_path:
        if getattr(item, "_tsp_etf_job", False) and getattr(item, "fullname", "") == fullname:
            return
    sys.meta_path.insert(0, _EtfAppFinder(fullname))
