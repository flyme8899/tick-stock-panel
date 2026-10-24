"""regime 计算口径(stamp)的登记、比对与落盘。

## 为什么需要 "口径版本"

`regime_history/part.parquet` 是一张**长期累积**的表: 每天的增量只算缺失/过期的那些天,
其余行原样留着。所以只要改动一处口径常量 —— 比如涨跌家数从「不剔除毛刺」改成
「剔除 `|日收益| > 25%`」—— 表里就会**同时存在两种口径的行**: 新行新口径, 老行老口径。
下游读的人看不出来: `up_count` 就是 `up_count`, 没有任何字段标注它是哪套算法算的。
20 日滚动窗口还会把两套口径平均在一起, `score` 序列在那里断一截。

`NOTES.md` §2.5 原来写的是「上线必做: 部署后手动 POST /api/regime/recompute 全量重算」
—— 靠人记住。这条纪律必然会漏, 而且漏了没有任何信号。

本模块把它变成机器守的: **口径常量 → 内容指纹 → stamp 文件**。指纹不一致时启动/增量
入口会拿到一个明确的结论, 而不是悄悄混算。

## 设计取舍

- **指纹是内容算出来的, 不是手写的版本号**: `REGIME_CALIBER_VERSION = "2"` 这种常量
  忘记 bump 就完全失效, 而"改了 `BREADTH_OUTLIER` 忘了改版本"恰恰是最容易发生的事。
  所以真正比对的是 `caliber_fingerprint()` —— 它把参与口径的常量值本身序列化后取
  哈希。改了任何一个值, 指纹必然变。`REGIME_CALIBER_VERSION` 仍然保留: 它是给人看的
  标签, 也是「只改了代码逻辑、常量没动」这种情形的唯一抓手。
- **不改任何口径数值**: `BREADTH_OUTLIER` / `BREADTH_THRESHOLDS` / 分档线都不动,
  本模块只读取它们。
- **stamp 文件与表同目录**: `regime_history/.regime_caliber.json`。跟着表走, 拷数据目录
  一起搬, 不会出现"表搬走了、口径记录没搬"。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 口径版本(人读标签)的权威定义在 `regime_builder.REGIME_CALIBER_VERSION` —— 它和
#: 其它口径常量放在一起, 改口径时不容易漏。这里只是转出口, 方便调用方一处 import。
#:
#: 真正生效的是 `caliber_fingerprint()` 算出来的**内容指纹**: 改了任何参与口径的常量,
#: 指纹必变, 不依赖谁记得 bump 版本号; 版本号只负责在日志/接口里给人一个说法。
#: 口径版本(人读标签)的权威定义在 `regime_builder.REGIME_CALIBER_VERSION` —— 它和
#: 其它口径常量放在一起, 改口径时不容易漏。这里通过模块级 `__getattr__` 转出口,
#: 不在导入期取快照(那会把「本模块先被导入」时的值钉死, 之后 monkeypatch 就失效)。
_LAZY_EXPORTS = frozenset({"REGIME_CALIBER_VERSION"})


def __getattr__(name: str) -> Any:
    """把 `REGIME_CALIBER_VERSION` 转发到 regime_builder, 保持"单一事实来源"。"""
    if name in _LAZY_EXPORTS:
        from app.services import regime_builder

        return getattr(regime_builder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

#: stamp 文件名。放在 `regime_history/` 目录里、与 `part.parquet` 同级。
CALIBER_STAMP_NAME = ".regime_caliber.json"

#: 参与口径指纹的常量名。**新增口径参数要加进这个元组**, 否则它变了指纹也不会变。
CALIBER_CONSTANTS: tuple[str, ...] = (
    "REGIME_CALIBER_VERSION",
    "BREADTH_WINDOW",
    "BREADTH_OUTLIER",
    "BREADTH_THRESHOLDS",
    "BREADTH_POS",
    "BREADTH_CONFIRM_DAYS",
    "STATE_STRONG",
    "STATE_LEAN_STRONG",
    "STATE_RANGE",
    "STATE_LEAN_WEAK",
)


def stamp_path(data_dir: Path) -> Path:
    """stamp 文件路径: ``<data_dir>/regime_history/.regime_caliber.json``。"""
    from app.services.regime_builder import REGIME_DIR

    return Path(data_dir) / REGIME_DIR / CALIBER_STAMP_NAME


def caliber_values() -> dict[str, Any]:
    """读出参与口径的常量当前值。

    缺失的常量直接 KeyError —— `CALIBER_CONSTANTS` 写错名字时应当立刻炸, 而不是
    悄悄少比一个参数。
    """
    from app.services import regime_builder

    values: dict[str, Any] = {}
    for name in CALIBER_CONSTANTS:
        if not hasattr(regime_builder, name):
            raise AttributeError(f"口径常量 {name} 在 regime_builder 里不存在")
        value = getattr(regime_builder, name)
        # tuple → list 便于 JSON / 哈希; 元组也有序, 不会丢信息。
        values[name] = list(value) if isinstance(value, tuple) else value
    return values


def caliber_fingerprint(values: dict[str, Any] | None = None) -> str:
    """口径内容指纹(sha256 前 16 位)。

    ``sort_keys`` 保证字段顺序不影响结果; 用 JSON 而不是 ``repr`` 是为了避免
    「同一个值在两种表示下哈希不同」这类噪音。
    """
    payload = caliber_values() if values is None else values
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def read_stamp(data_dir: Path) -> dict[str, Any] | None:
    """读 stamp。文件不存在/损坏都返回 None —— 调用方要能区分「没记录」和「不一致」。"""
    path = stamp_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def write_stamp(data_dir: Path, *, reason: str) -> dict[str, Any]:
    """把当前口径写进 stamp。全量重算成功之后调用。

    ``reason`` 只用于排查: 记录这次 stamp 是「recompute 全量重算」写的, 还是
    「启动时自动补算」写的。时间戳取本地时间, 只作记录, 不参与任何判定。
    """
    from app.services.fs_utils import atomic_write_text

    values = caliber_values()
    payload = {
        "version": values.get("REGIME_CALIBER_VERSION"),
        "fingerprint": caliber_fingerprint(values),
        "constants": values,
        "written_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
    }
    path = stamp_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子写: 半截 JSON 会让下一次读取拿到 None, 进而把"没记录"当成"不一致"反复重算。
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return payload


def check_caliber(data_dir: Path) -> dict[str, Any]:
    """比对盘上 stamp 与当前代码口径。

    返回::

        {
          "match": bool,             # 口径一致才为 True
          "stamped": dict | None,    # 盘上的 stamp(无则 None)
          "current": {"version", "fingerprint", "constants"},
          "changed": list[str],      # 值变了的常量名
          "reason": str,             # missing / changed / match
        }

    ``match=False`` 有明确的两种情形, 调用方必须区别对待:
    - ``reason="missing"``: 从没有 stamp(首次部署, 或这个特性上线前的历史数据)。
      没有证据说明表里是混口径的, 不该据此自动重算 —— 只提示。
    - ``reason="changed"``: 有 stamp 且指纹不同 —— 表里确实可能混着两种口径。
    """
    current_values = caliber_values()
    current = {
        "version": current_values.get("REGIME_CALIBER_VERSION"),
        "fingerprint": caliber_fingerprint(current_values),
        "constants": current_values,
    }
    stamped = read_stamp(data_dir)
    if stamped is None:
        return {"match": False, "stamped": None, "current": current, "changed": [], "reason": "missing"}

    stamped_constants = stamped.get("constants")
    changed: list[str] = []
    if isinstance(stamped_constants, dict):
        for name in CALIBER_CONSTANTS:
            if stamped_constants.get(name) != current_values.get(name):
                changed.append(name)
    stamp_fp = stamped.get("fingerprint")
    match = bool(stamp_fp) and stamp_fp == current["fingerprint"]
    if not match and not changed and isinstance(stamped_constants, dict):
        # 指纹不同但逐项比对没差 —— 说明 stamp 是旧版本写的、缺某些常量(新增了口径参数)。
        changed = [n for n in CALIBER_CONSTANTS if n not in stamped_constants]
    return {
        "match": match,
        "stamped": stamped,
        "current": current,
        "changed": changed,
        "reason": "match" if match else "changed",
    }


def describe_drift(report: dict[str, Any]) -> str:
    """把 check_caliber 的结论翻成一句人能读的话(日志/接口共用)。"""
    if report["match"]:
        return f"regime 口径一致 (v{report['current']['version']}, {report['current']['fingerprint']})"
    if report["reason"] == "missing":
        return "regime 口径未登记(无 stamp 文件), 历史行口径未知"
    changed = report.get("changed") or []
    stamped = report.get("stamped") or {}
    detail = ", ".join(
        f"{name}: {stamped.get('constants', {}).get(name)!r} → {report['current']['constants'].get(name)!r}"
        for name in changed
    )
    return f"regime 口径已变更(v{stamped.get('version')} → v{report['current']['version']}): {detail}"
