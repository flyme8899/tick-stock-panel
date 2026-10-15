#!/usr/bin/env python3
r"""只读检查：盘中个股资金流快照的**实际间隔分布**（开发环境入口）。

**实现已移到** ``app/fund_flow/intraday_gap.py``，本脚本只做一层薄包装。

为什么搬家：``.dockerignore`` 排除了 ``scripts/``，所以这个脚本**进不了 Docker 镜像**，
在容器里没法用。而 ``Dockerfile`` 有 ``COPY backend/app ./app``，把实现放进 ``app/``
就能在镜像内直接调用：

    docker compose exec app python -m app.fund_flow.intraday_gap --days 3 --verbose

本文件为**开发环境**保留同名入口，参数与输出完全一致（直接转调，不重复实现）。

    PYTHONPATH=backend backend/.venv/bin/python scripts/check_stock_intraday_gap.py

Windows（``dev.ps1`` 同一套环境）:

    $env:PYTHONPATH = "backend"
    backend\.venv\Scripts\python scripts\check_stock_intraday_gap.py

详见 ``app/fund_flow/intraday_gap.py`` 的模块 docstring（读什么、怎么判读、退出码）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许直接 `python scripts/check_stock_intraday_gap.py` 而无需先设 PYTHONPATH：
# 把仓库的 backend/ 加进 sys.path，再从 app 包导入。
_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.fund_flow.intraday_gap import main

if __name__ == "__main__":
    raise SystemExit(main())
