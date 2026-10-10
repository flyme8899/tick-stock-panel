"""在 DSA 进程内执行 ETF 轮动。

TSP 的 app 镜像不含 ``vendor/daily_stock_analysis``。``dsa_bootstrap`` 在
sidecar 导入 FastAPI 应用时挂上 ``POST /api/v1/tsp/etf-rotation``，由该进程
在自己的工作目录里执行固定命令。请求体不能改变参数；股票池和成本只读环境变量。

管理登录关闭时，这个入口如果跟着 DSA 端口一起暴露就会变成匿名触发。
因此每次都要带 ``X-TSP-Internal-Token``，并且和 ``DSA_INTERNAL_TOKEN`` 一致。
没配、太短或不相等时直接拒绝，不执行命令。
"""
from __future__ import annotations

import importlib.machinery
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("tsp.dsa.etf_job")

COMMAND = "python main.py --etf-rotation --no-notify"
ETF_UPSTREAM_PATH = "tsp/etf-rotation"
INTERNAL_TOKEN_HEADER = "X-TSP-Internal-Token"
TOKEN_ENV = "DSA_INTERNAL_TOKEN"
TOKEN_MISSING_DETAIL = (
    "未配置 DSA_INTERNAL_TOKEN。请在共享的 .env 里写成至少 16 位随机 ASCII，TSP 与 DSA 使用同一值。"
)
_UNAUTHORIZED = {"ok": False, "detail": "未授权", "command": COMMAND}
_RESULT_PREFIX = "TSP_ETF_RESULT "
_ARGV = ("main.py", "--etf-rotation", "--no-notify")
_TIMEOUT_SECONDS = 180
_TOKEN_MIN = 16
_TOKEN_MAX = 256
_LOCK = threading.Lock()


def configured_token() -> str:
    return os.getenv(TOKEN_ENV, "").strip()


def token_usable(token: str) -> bool:
    """共享密钥只接受可见 ASCII，避免换行把请求头拆开，也避免过短的占位值。"""
    if not isinstance(token, str) or not _TOKEN_MIN <= len(token) <= _TOKEN_MAX:
        return False
    return all(33 <= ord(char) <= 126 for char in token)


def presented_token_ok(presented: str) -> bool:
    expected = configured_token()
    if not token_usable(expected) or not isinstance(presented, str):
        return False
    candidate = presented.strip()
    if not token_usable(candidate):
        return False
    return secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


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


def split_rotation_output(output: str) -> tuple[str, dict | None]:
    """Pull the structured card off the CLI stdout. The line is not a log."""
    result = None
    kept: list[str] = []
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(_RESULT_PREFIX):
            try:
                parsed = json.loads(stripped[len(_RESULT_PREFIX):])
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if isinstance(parsed, dict):
                result = parsed
                continue
        kept.append(line)
    return "\n".join(kept).strip(), result


def _job_payload(ok: bool, output: str, code: int | None = None) -> dict:
    detail, result = split_rotation_output(output)
    body: dict[str, Any] = {"ok": ok, "detail": detail[-8000:], "command": COMMAND}
    if code is not None:
        body["code"] = code
    if result is not None:
        body["result"] = result
    return body


def execute_job() -> dict:
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
    return _job_payload(completed.returncode == 0, output, completed.returncode)


def run_job(request: Request) -> Any:
    """授权通过后才执行。未授权不占用正在运行的那把锁。"""
    presented = request.headers.get(INTERNAL_TOKEN_HEADER, "")
    if not presented_token_ok(presented):
        logger.info("ETF 轮动入口拒绝了未授权请求")
        return JSONResponse(status_code=401, content=dict(_UNAUTHORIZED))
    return execute_job()


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
