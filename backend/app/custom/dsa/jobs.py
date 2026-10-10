"""Jobs that DSA exposes as CLI flags rather than its own HTTP routes.

The panel image does not contain ``vendor/daily_stock_analysis``. Docker
Compose profile ``dsa`` already runs that source inside the ``dsa`` service,
so the ETF rotation CLI is executed there. A local checkout that still has
the vendored tree and its interpreter keeps running the command in-process.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from app.custom.dsa.etf_job import (
    TOKEN_MISSING_DETAIL,
    configured_token,
    split_rotation_output,
    token_usable,
)
from app.custom.dsa.proxy import UpstreamError, enabled, forward

_REPO_ROOT = Path(__file__).resolve().parents[4]
_VENDOR = _REPO_ROOT / "vendor" / "daily_stock_analysis"
# 比 sidecar 内部的 180 秒多留一点，让超时以 JSON 形式回来，而不是连接被掐断。
_SIDECAR_TIMEOUT = 200.0


def vendor_root() -> Path:
    return _VENDOR


def sidecar_python() -> Path | None:
    configured = os.getenv("DSA_PYTHON", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    candidate = vendor_root() / ".venv" / "bin" / "python"
    return candidate if candidate.is_file() else None


def etf_rotation_command() -> str:
    return "python main.py --etf-rotation --no-notify"


def run_etf_rotation() -> dict:
    """Run the fixed ETF rotation command where the DSA source lives.

    Pool and cost settings come from the environment, not from the request.
    """
    if (vendor_root() / "main.py").is_file():
        return _run_local()
    return _run_in_sidecar()


def _run_local() -> dict:
    command = etf_rotation_command()
    python = sidecar_python()
    if python is None:
        return {
            "ok": False,
            "detail": "未找到 DSA 解释器。先运行 scripts/dsa.sh 安装 sidecar，或设置 DSA_PYTHON",
            "command": command,
        }
    env = os.environ.copy()
    env.setdefault("ENV_FILE", str(_REPO_ROOT / ".env"))
    try:
        completed = subprocess.run(
            [str(python), "main.py", "--etf-rotation", "--no-notify"],
            cwd=vendor_root(),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "ETF 轮动超时", "command": command}
    output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
    detail, result = split_rotation_output(output)
    payload = {
        "ok": completed.returncode == 0,
        "code": completed.returncode,
        "detail": detail[-8000:],
        "command": command,
    }
    if result is not None:
        payload["result"] = result
    return payload


def _run_in_sidecar() -> dict:
    """Ask the running DSA service to execute the CLI in its own tree."""
    command = etf_rotation_command()
    if not enabled():
        return {
            "ok": False,
            "detail": "未配置 DSA_BASE_URL。Docker 请设置为 http://dsa:8000，并用 docker compose --profile dsa 启动。",
            "command": command,
        }
    if not token_usable(configured_token()):
        return {"ok": False, "detail": TOKEN_MISSING_DETAIL, "command": command}
    try:
        status, payload, _media, _extra = forward(
            "POST",
            "tsp/etf-rotation",
            timeout=_SIDECAR_TIMEOUT,
        )
    except UpstreamError as exc:
        detail = str(exc)
        if "DSA_INTERNAL_TOKEN" not in detail:
            detail = f"{detail}。请用 docker compose --profile dsa 启动，并把 DSA_BASE_URL 设为 http://dsa:8000。"
        return {"ok": False, "detail": detail, "command": command}
    return _parse_sidecar_payload(status, payload, command)


def _parse_sidecar_payload(status: int, payload: bytes, command: str) -> dict:
    text = payload.decode("utf-8", errors="replace")
    try:
        body = json.loads(text) if text else {}
    except json.JSONDecodeError:
        body = {}
    if isinstance(body, dict) and "ok" in body:
        detail = str(body.get("detail") or "")
        parsed: dict = {
            "ok": bool(body.get("ok")),
            "detail": detail[-8000:],
            "command": command,
        }
        if "code" in body:
            parsed["code"] = body.get("code")
        if isinstance(body.get("result"), dict):
            parsed["result"] = body["result"]
        return parsed
    if status == 404:
        return {
            "ok": False,
            "detail": "决策服务没有 ETF 轮动入口。请用当前仓库的 dsa_bootstrap 重启：docker compose --profile dsa up --build。",
            "command": command,
        }
    snippet = text.strip()[-500:]
    return {"ok": False, "detail": snippet or f"HTTP {status}", "command": command}
