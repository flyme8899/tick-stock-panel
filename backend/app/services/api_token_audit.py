"""API Token 的审计视图 —— 只读, 不改存储格式, 不动网关。

## 为什么是"只读 + 小步"

Token 的创建/吊销/校验链路 (`api_tokens` / `api_gateway`) 是所有外部调用方的咽喉。
在它上面做"顺手重构"的风险远大于收益: 一次哈希算法或字段名的改动就能让全部已发的
Token 静默失效, 而失效表现是 401 —— 分不清是"Token 过期"还是"网关改坏了"。

所以这里只加**读**: 把 `api_tokens.json` 里已有的字段重新组织成一张"该轮换谁"的表,
不改写入路径、不改校验逻辑、不引入新字段。要改真实链路, 见
``docs/api-token-audit.md`` 里的分阶段计划。

## 判据从哪来

- 长期未使用: 有 `last_used_at` 但距今超过阈值。**没有** `last_used_at` 的 Token 不算 ——
  它可能是刚创建还没接入, 也可能是一直没被用; 无法区分时不该报警。
- 长期未轮换: 按 `created_at` 年龄。这一条不看使用情况: 高频使用的 Token 更需要轮换。
- 已吊销仍在册: 记录留下来是对的 (审计痕迹), 但要从"有效 Token"计数里排除。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.services import api_tokens

#: 超过这么多天没用过 → 建议吊销。30 天是"一个季度内至少被用过一次"的宽松口径。
STALE_UNUSED_DAYS = 30

#: 创建超过这么多天 → 建议轮换。90 天对齐常见合规要求里"密钥至少季度轮换"的档次。
STALE_ROTATE_DAYS = 90


def _parse_dt(raw: object) -> datetime | None:
    """解析存储里的时间戳。

    ``api_tokens`` 落盘的是 ``cn_now().isoformat()``, 带 ``+08:00``; 但历史数据或手改过
    的文件里也可能是不带时区的裸时间。两种都要能算年龄 —— 拿带时区的 now 去减不带
    时区的值会直接 ``TypeError``, 而审计报错会连带把整个 page 打挂。

    统一口径: 不带时区的按**本地时区**理解(落盘用的是北京时间), 再转成 aware 比较。
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def audit_tokens(data_dir: Path, *, now: datetime | None = None) -> dict:
    """返回 Token 审计表。纯读, 不改任何文件。

    ``now`` 可注入, 便于测试; 缺省用 ``cn_now()`` 与 `create_token` 同一时钟基准。
    """
    now = now or api_tokens.cn_now()
    rows: list[dict] = []
    for record in api_tokens.list_tokens(data_dir):
        created = _parse_dt(record.get("created_at"))
        last_used = _parse_dt(record.get("last_used_at"))
        age_days = (now - created).days if created else None
        idle_days = (now - last_used).days if last_used else None
        revoked = bool(record.get("revoked"))

        flags: list[str] = []
        if not revoked:
            if age_days is not None and age_days >= STALE_ROTATE_DAYS:
                flags.append("rotate_overdue")
            if idle_days is not None and idle_days >= STALE_UNUSED_DAYS:
                flags.append("unused_stale")
            if last_used is None and age_days is not None and age_days >= STALE_ROTATE_DAYS:
                # 从创建起就没用过, 且已经超过轮换周期 —— 多半是接完就忘的残留。
                flags.append("never_used")
        else:
            flags.append("revoked")

        rows.append({
            "id": record.get("id"),
            "name": record.get("name"),
            "scopes": record.get("scopes") or [],
            "created_at": record.get("created_at"),
            "last_used_at": record.get("last_used_at"),
            "revoked": revoked,
            "age_days": age_days,
            "idle_days": idle_days,
            "flags": flags,
        })

    active = [r for r in rows if not r["revoked"]]
    revoked_rows = [r for r in rows if r["revoked"]]
    return {
        "now": now.isoformat(timespec="seconds"),
        "thresholds": {
            "unused_days": STALE_UNUSED_DAYS,
            "rotate_days": STALE_ROTATE_DAYS,
        },
        "counts": {
            "total": len(rows),
            "active": len(active),
            "revoked": len(revoked_rows),
            "rotate_due": sum(1 for r in active if "rotate_overdue" in r["flags"]),
            "unused": sum(1 for r in active if "unused_stale" in r["flags"]),
            "never_used": sum(1 for r in active if "never_used" in r["flags"]),
        },
        "tokens": rows,
        # 一句话结论, 给日志/前端直接用。
        "summary": _summary(len(active), rows),
    }


def _summary(active: int, rows: list[dict]) -> str:
    due = sum(1 for r in rows if not r["revoked"] and "rotate_overdue" in r["flags"])
    unused = sum(1 for r in rows if not r["revoked"] and "unused_stale" in r["flags"])
    if active == 0:
        return "没有生效中的 API Token"
    if due == 0 and unused == 0:
        return f"{active} 个生效 Token, 无需处理"
    parts = []
    if due:
        parts.append(f"{due} 个超过 {STALE_ROTATE_DAYS} 天建议轮换")
    if unused:
        parts.append(f"{unused} 个超过 {STALE_UNUSED_DAYS} 天未使用建议吊销")
    return f"{active} 个生效 Token: " + "; ".join(parts)
