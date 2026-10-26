"""Kronos 推理 sidecar：只有 /health，没有推理接口。

为什么这么空：本拍（P6）是**接旧实装**——消费已经跑完的 S3 研究包，不新跑推理
（见 ``docs/kronos-integration.md``「本拍不新跑推理」）。推理接口属于 Opt-K1
（全市场日批 / GPU 调度），另等口令。

现在这个容器提供两件事：

1. ``/health`` —— 让主进程的 ``reachable`` 有个真实来源（没配就没人探）。
2. 把「权重在不在、能不能 import torch」如实报出来 —— 让人一眼看清 sidecar
   是否真的具备推理条件，而不是容器起来了就当能算。

**不**做：不跑模型、不写扩展表、不接买卖信号、不接受任意路径读文件。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

app = FastAPI(title="TSP Kronos sidecar", docs_url=None, redoc_url=None, openapi_url=None)

WEIGHTS_DIR = Path(os.environ.get("KRONOS_WEIGHTS_DIR", "/opt/kronos/weights"))


def _weights_present() -> list[str]:
    try:
        return sorted(p.name for p in WEIGHTS_DIR.iterdir() if p.is_dir())
    except OSError:
        return []


def _torch_version() -> str | None:
    try:
        import torch
    except Exception:  # noqa: BLE001 — 缺 torch 只影响推理，不影响探活
        return None
    return getattr(torch, "__version__", "unknown")


@app.get("/health")
def health() -> dict:
    weights = _weights_present()
    torch_version = _torch_version()
    ready = bool(weights) and torch_version is not None
    return {
        "status": "ok",
        "component": "kronos-sidecar",
        # 本侧车当前只做探活，推理在离线批；这一点写进 health 免得被误读。
        "inference": "offline_batch",
        "torch": torch_version,
        "weights": weights,
        "weights_ready": bool(weights),
        "ready_for_inference": ready,
        "research_only": True,
    }
