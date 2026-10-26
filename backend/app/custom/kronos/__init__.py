"""Kronos 接旧实装扩展（P6）。删掉本包即移除 Kronos 路由与门闩。

本包**不含** torch / Kronos 推理运行时：

- 推理只在离线批（``scripts/kronos_import_s3.py``）或独立 sidecar 里做；
- 主进程只读配置、探活、读门禁记录，然后把结果作为扩展表因子暴露；
- 未过门禁的预测因子不进买卖信号默认集（见 ``gates.py``）。

对外口径：本拍是「接旧骨架」。**没有**宣称信号可做、没有把预测接进 BSS、
没有抬升 S3「仅提醒 / 只降档」的结论上限。
"""
from __future__ import annotations

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "kronos.research"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from app.custom.kronos.routes import build_router

    registrar.include_router(build_router())
