"""Kronos 三态：``enabled`` ≠ ``reachable`` ≠ ``gate_passed``。

三件事必须分开，混在一起就会变成「配了个 URL 就当能进信号」：

===================================  ==============================================
态                                    判据
===================================  ==============================================
``enabled``                           显式开关打开 **且** 配了 URL / 本地批路径之一
``reachable``                         sidecar ``/health`` 通 **或** 批成功标记新鲜
``gate_passed``                       存在**显式**门禁记录且四前置齐、未过期
===================================  ==============================================

设计取舍：

- **enabled 默认假**。与 DSA / OpenBB 的 ``enabled ≈ bool(URL)`` 不同——那两个是
  常驻 sidecar，配了 URL 就是配了。Kronos 默认**没有**部署物，用
  ``KRONOS_ENABLED=1`` 显式打开，避免「.env 里抄了一行模板就以为开了」。
- **reachable 走「或」**：常驻 sidecar 看 health；离线批只看「最近一次成功标记的
  时间戳是否在窗口内」。两者任一成立即可，因为本仓两种形态都允许。
- **gate_passed 只认真实记录**。读 ``data/kronos/gate.json``，必须
  ``passed=true`` + 四项前置齐全 + 未超过 ``expires_at``。文件不存在、JSON 坏、
  字段缺、过期 —— 全部按 false 处理，且在 ``detail`` 里说清原因。
  **不接受**「环境变量 KRONOS_GATE_PASSED=1」这种一拍脑门的置真：把门禁做成一行
  env，等于谁都能开。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# 批成功标记的默认新鲜窗口：两天。研究包按交易日产出，隔夜 + 一个周末仍算新鲜。
DEFAULT_FRESHNESS_HOURS = 48

# 门禁记录里的四项前置（顺序即文档顺序）。
GATE_PREREQUISITES = (
    "cost_after_tax",       # 费后检验口径一致（买 6.6bp / 卖 11.6bp 等）
    "out_of_sample",        # 样本外 / walkforward 记录可复核
    "explicit_passed",      # 显式 passed=true
    "risk_downgrade_only",  # 用途不超出「风险降档」
)

_TRUTHY = {"1", "true", "yes", "on"}
_OFF = {"", "0", "false", "no", "off", "none"}


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def switch_on() -> bool:
    """``KRONOS_ENABLED`` 是否为真值。默认（未设 / 0 / false / off）为假。"""
    raw = _env("KRONOS_ENABLED").lower()
    if raw in _OFF:
        return False
    return raw in _TRUTHY


def base_url() -> str:
    return _env("KRONOS_BASE_URL").rstrip("/")


def batch_dir() -> str:
    return _env("KRONOS_BATCH_DIR")


def data_root_override() -> bool:
    """是否用 ``KRONOS_DATA_DIR`` 覆盖了运行态目录。"""
    return bool(_env("KRONOS_DATA_DIR"))


def data_root() -> Path:
    """Kronos 自己的运行态目录（标记、门禁记录）。跟着 ``settings.data_dir`` 走。"""
    raw = _env("KRONOS_DATA_DIR")
    if raw:
        return Path(raw)
    from app.config import settings

    return Path(settings.data_dir) / "kronos"


def freshness_hours() -> float:
    raw = _env("KRONOS_FRESHNESS_HOURS")
    if not raw:
        return DEFAULT_FRESHNESS_HOURS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_FRESHNESS_HOURS
    return value if value > 0 else DEFAULT_FRESHNESS_HOURS


def enabled() -> bool:
    """显式开关打开，且至少配了 sidecar URL 或本地批目录之一。"""
    return switch_on() and bool(base_url() or batch_dir())


def enabled_detail() -> str:
    if not switch_on():
        return "未开启 KRONOS_ENABLED"
    if not base_url() and not batch_dir():
        return "未配置 KRONOS_BASE_URL 或 KRONOS_BATCH_DIR"
    return "已配置"


# --------------------------------------------------------------- reachable

def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def batch_marker() -> dict:
    """最近一次批成功标记 ``data/kronos/last_batch.json``。"""
    return _read_json(data_root() / "last_batch.json") or {}


def batch_fresh() -> tuple[bool, str]:
    marker = batch_marker()
    stamp = _parse_time(marker.get("finished_at"))
    if stamp is None:
        return False, "无批成功标记"
    age = datetime.now(UTC) - stamp
    limit = timedelta(hours=freshness_hours())
    if age > limit:
        hours = age.total_seconds() / 3600
        return False, f"批成功标记已过期（{hours:.1f}h > {freshness_hours():.0f}h）"
    return True, f"批成功标记新鲜（{age.total_seconds() / 3600:.1f}h 前）"


def sidecar_health() -> tuple[bool, str]:
    """sidecar ``/health`` 探活。只有固定这一条路径，没有任意上游转发。"""
    url = base_url()
    if not url:
        return False, "未配置 KRONOS_BASE_URL"
    try:
        import httpx

        response = httpx.get(f"{url}/health", timeout=5.0)
    except Exception as exc:
        logger.debug("Kronos sidecar 探活失败", exc_info=True)
        return False, f"sidecar 不可达：{type(exc).__name__}"
    if response.status_code >= 400:
        return False, f"sidecar 健康检查返回 HTTP {response.status_code}"
    payload = response.json() if response.content else {}
    if isinstance(payload, dict) and payload.get("status") not in (None, "ok", "healthy"):
        return False, f"sidecar 自报状态 {payload.get('status')!r}"
    return True, "sidecar 已连接"


def reachable() -> tuple[bool, str]:
    """sidecar health 或批成功标记，任一成立即可。"""
    if not enabled():
        return False, enabled_detail()
    reasons: list[str] = []
    if base_url():
        ok, detail = sidecar_health()
        if ok:
            return True, detail
        reasons.append(detail)
    if batch_dir():
        ok, detail = batch_fresh()
        if ok:
            return True, detail
        reasons.append(detail)
    return False, "；".join(reasons) or "未配置可达来源"


# -------------------------------------------------------------- gate_passed

def gate_record_path() -> Path:
    raw = _env("KRONOS_GATE_RECORD")
    return Path(raw) if raw else data_root() / "gate.json"


def gate_record() -> dict:
    """显式门禁记录。读不到按空记录处理（= 未过门禁）。"""
    return _read_json(gate_record_path()) or {}


def missing_prerequisites(record: dict | None = None) -> list[str]:
    """返回缺的前置项名字。空列表 = 四项齐。

    逐项判据写死，不给「有 passed 就够」留后门：

    - ``cost_after_tax`` / ``out_of_sample`` 必须是**非空字符串**（写明口径 / 记录
      位置），``true`` 这种布尔糊弄不算；
    - ``explicit_passed`` 必须**逐字** ``passed is True``（不是 1、不是 "true"）；
    - ``risk_downgrade_only`` 必须 ``True``，且 ``usage`` 若给了必须不是加码类。
    """
    record = gate_record() if record is None else record
    missing: list[str] = []
    cost = record.get("cost_after_tax")
    if not isinstance(cost, str) or not cost.strip():
        missing.append("cost_after_tax")
    sample = record.get("out_of_sample")
    if not isinstance(sample, str) or not sample.strip():
        missing.append("out_of_sample")
    if record.get("passed") is not True:
        missing.append("explicit_passed")
    if record.get("risk_downgrade_only") is not True:
        missing.append("risk_downgrade_only")
    usage = str(record.get("usage") or "").strip().lower()
    if usage in {"upgrade", "add_position", "increase", "buy"}:
        missing.append("risk_downgrade_only")
    return missing


def gate_expired(record: dict | None = None) -> bool:
    record = gate_record() if record is None else record
    raw = record.get("expires_at")
    if not isinstance(raw, str) or not raw.strip():
        return False
    stamp = _parse_time(raw)
    if stamp is None:
        # 写了过期时间但解析不出来 → 按过期处理（fail-closed）。
        return True
    return datetime.now(UTC) > stamp


def gate_passed() -> tuple[bool, str]:
    """门禁是否通过。任何一环不满足都是 false，且原因可读。"""
    record = gate_record()
    if not record:
        return False, f"未找到门禁记录（{gate_record_path().name} 不存在或不可解析）"
    missing = missing_prerequisites(record)
    if missing:
        return False, f"门禁前置未齐：{'、'.join(missing)}"
    if gate_expired(record):
        return False, "门禁记录已过期，需重新评估"
    return True, "门禁已通过（风险降档用途）"


def status() -> dict:
    """``/api/kronos/status`` 的载荷。三态各自独立求值，互不顶替。"""
    reachable_ok, reachable_detail = reachable()
    gate_ok, gate_detail = gate_passed()
    record = gate_record()
    from app.custom.kronos.schema import CONFIG_ID, SCHEMA_VERSION, source_refs

    return {
        "enabled": enabled(),
        "reachable": reachable_ok,
        "gate_passed": gate_ok,
        "detail": enabled_detail() if not enabled() else reachable_detail,
        "gate_detail": gate_detail,
        "schema_version": SCHEMA_VERSION,
        "ext_config_id": CONFIG_ID,
        "source_refs": source_refs(),
        "switch_on": switch_on(),
        "base_url": base_url(),
        "batch_dir": batch_dir(),
        "research_only": True,
        "purpose": "risk_downgrade_only",
        "gate": {
            "record_present": bool(record),
            "record_name": gate_record_path().name,
            "missing": missing_prerequisites(record) if record else list(GATE_PREREQUISITES),
            "expires_at": record.get("expires_at"),
        },
    }
