"""Local jobs that DSA exposes as CLI flags rather than HTTP routes."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_VENDOR = _REPO_ROOT / "vendor" / "daily_stock_analysis"


def vendor_root() -> Path:
    return _VENDOR


def sidecar_python() -> Path | None:
    configured = os.getenv("DSA_PYTHON", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    candidate = _VENDOR / ".venv" / "bin" / "python"
    return candidate if candidate.is_file() else None


def etf_rotation_command() -> str:
    return "python main.py --etf-rotation --no-notify"


def run_etf_rotation() -> dict:
    """Run the vendored ETF rotation entry with a fixed argument list.

    The pool and cost settings come from the environment, not from the request,
    so this cannot be turned into a shell invocation.
    """
    if not (_VENDOR / "main.py").is_file():
        return {
            "ok": False,
            "detail": "仓库里没有 DSA 源码快照",
            "command": etf_rotation_command(),
        }
    python = sidecar_python()
    if python is None:
        return {
            "ok": False,
            "detail": "未找到 DSA 解释器。先运行 scripts/dsa.sh 安装 sidecar，或设置 DSA_PYTHON",
            "command": etf_rotation_command(),
        }
    env = os.environ.copy()
    env.setdefault("ENV_FILE", str(_REPO_ROOT / ".env"))
    try:
        completed = subprocess.run(
            [str(python), "main.py", "--etf-rotation", "--no-notify"],
            cwd=_VENDOR,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "ETF 轮动超时", "command": etf_rotation_command()}
    output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
    return {
        "ok": completed.returncode == 0,
        "code": completed.returncode,
        "detail": output[-8000:],
        "command": etf_rotation_command(),
    }
