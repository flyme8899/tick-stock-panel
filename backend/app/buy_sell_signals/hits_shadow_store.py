"""影子生产写入 · 独立三键分桶(批次 20261028gp3b · P-bss-p3b · #9 R2-7②)。

## 这个模块做什么

把一条 `source.mode == "shadow"` 的命中, **以三键**写进一个**独立于**生产 live
`hits.json` 的分桶文件里, 并且能按同样三键把它读回来。

分桶路径(写死, 由三键确定性指认):

```
<data_dir>/buy_sell_signals/shadow/<signal_id>/<definition_version>/<freq_lock_version>/hits.json
```

三键 = `signal_id` + `definition_version` + `freq_lock_version`。首发对象是 #241 注册
的 `bss.daily_quality_value`(版本 `v1`, 口径 `freq_lock_v1`)。

## 这个模块**不**做什么

- **不**往生产 live `hits.json` 里写一个字节 —— 那是另一条写口, 不是本模块的事
- **不**给 `HitStore` Protocol / `DiskHitStore` 加 `upsert` / `write` / `save` / `append`
- **不**碰 `try_push_hit` / `visible_hits` / P3a 三闸的任何一行
- **不**翻 `TRIGGERING_ENABLED`
- **不**读 env、**不**新增开关
- **不**是 R2-3 观测口: 本模块给的 `list_shadow_hits` 只是**核验钩子**, 没有路由、
  没有前端页、没有"打开观测口"这个概念

## 大字: 三句必须分清

**路径已通 ≠ calib 已过 ≠ 可放量 ≠ TRIGGERING。**

写进分桶只说明「这套三键的样本路径在仓内存在且可指认」。它**不**说明:

| 不等于 | 为什么 |
|---|---|
| calib 已过 | calib 结论由 P2 评估器按 #242 锁面算, 与"文件写在哪"无关 |
| 可放量 | 放量要过可见性闸 + calib 登记 + 人工发布 |
| TRIGGERING 该开 | 两处 `*_TRIGGERING_ENABLED` 仍 `False`, 本模块一个字节都没碰 |
| 影子能进钉钉 | `try_push_hit` 对 shadow 一律 `shadow_not_pushable`(P3a 闸) |

**P3a 已合 ≠ 可放影子进钉钉。** #240 把"shadow 不可见、不可推"钉死了; 本模块只是
把 shadow **存下来**, 存的这些仍然被那道闸挡着。

**影子写入 ≠ 观测口 ≠ 放量。** 写入是"落到磁盘", 观测口是"给人在界面上看",
放量是"让它影响交易"。本拍只做第一件, 而且做在一条**独立于 live 的路径**上。

## 为什么必须独立分桶, 而不是在 live hits.json 里加个 mode 字段

如果 shadow 和 live 挤在同一个 `hits.json` 里, 那么:

1. **读口会被污染**。`DiskHitStore.list_all()` 返回的是全部命中, 下游 `visible_hits`
   要做一次过滤才能把 shadow 剔掉。过滤写错一次, 影子就进了可见流 —— 而"写错一次"
   这种事, 在只读路径上没人会发现。
2. **样本量会互相污染**。live 的条数会被 shadow 堆高, "样本够不够"变成两个来源的
   和, 谁也说不清到底够了没有。
3. **回滚会互相牵连**。要清影子样本, 就得动 live 文件。

分桶之后, 这三件事变成结构性不可能: 独立文件、独立条数、独立删除。代价是路径多三
层目录 —— 而这三层恰好就是三键, 顺便把"这批样本是哪套口径的"写进了路径本身。

## 幂等

追加策略是 **append-or-replace-by-id**: 读出现有桶 → 按 `hit_id` 替换或追加 →
原子写回(tmp + `os.replace`)。同一个 `hit_id` 重复写入不会让文件膨胀, 也不会留下
两条互相矛盾的记录。

不用"直接 append 一行 JSON"的理由: 那样同一个 id 写两次就有两条, 而下游按 id 去重
时取哪一条取决于顺序 —— 顺序又取决于谁先写的, 于是"最终状态"变得不可复现。

## 原子写

先写 `hits.json.<pid>.tmp`, 再 `os.replace` 到目标。`os.replace` 在同一文件系统内
是原子的, 所以读方要么看到旧的完整文件、要么看到新的完整文件, 不会看到半个 JSON。

## 合规

仅供研究, 不构成投资建议; 执行须人工确认。红涨绿跌。成本来回 18.2bp。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Final, Iterable

from ..config import settings

from .constants import (
    HITS_FILENAME,
    HITS_SCHEMA_VERSION,
    NAMESPACE,
    PERSIST_SUBDIR,
    SHADOW_SUBDIR,
)
from .hits_models import HitValidationError, SignalHit, signal_hit_from_dict

logger = logging.getLogger(__name__)

#: 影子写入唯一允许的 `source.mode`。
#:
#: 写死成字面量而不是"从 `HIT_SOURCE_MODES` 里挑一个", 是因为这里要挡的是「有人把
#: live 命中塞进影子桶」—— 如果取值来自一个宽集合,"哪天集合里多了个近义词"就会
#: 悄悄放行。集合本身在别处校验合法性, 这里校验的是**这一个具体值**。
SHADOW_MODE: Final = "shadow"

#: 三键字段名。顺序即路径层级顺序 —— 改顺序就改了路径, 已写出的样本会找不到。
SHADOW_BUCKET_KEYS: Final = ("signal_id", "definition_version", "freq_lock_version")

_SCHEMA_VERSION: Final = HITS_SCHEMA_VERSION


class ShadowWriteError(ValueError):
    """影子写入被拒。

    单独一个异常类型, 是为了让"写不进去"和"写进去但内容不对"在调用方那里可区分:
    前者是契约问题(调错了), 后者是数据问题。
    """


# ---------------------------------------------------------------- 路径


def _bucket_root(data_dir: Path | str | None) -> Path:
    """分桶根: `<data_dir>/buy_sell_signals/shadow`。"""
    root = Path(str(data_dir)) if data_dir is not None else Path(settings.data_dir)
    return root / PERSIST_SUBDIR / SHADOW_SUBDIR


def _require_key(name: str, value: Any) -> str:
    """三键的每一段都必须是**安全的单段目录名**。

    这里挡的不只是空串: `..` 和 `/` 会让路径逃出分桶根, 于是"三键指认"这件事
    从"可复现"变成"可被输入改写"。`signal_id` 里带点(`bss.daily_quality_value`)
    是合法的 —— 那是名字的一部分, 不是路径跳转。
    """
    if not isinstance(value, str) or not value.strip():
        raise ShadowWriteError(f"{name} 必须是非空字符串, 收到 {value!r}")
    text = value.strip()
    if text in (".", "..") or "/" in text or "\\" in text or "\x00" in text:
        raise ShadowWriteError(f"{name} 不能包含路径分隔符或指向上级: {value!r}")
    return text


def shadow_bucket_dir(
    data_dir: Path | str | None = None,
    *,
    signal_id: str,
    definition_version: str,
    freq_lock_version: str,
) -> Path:
    """返回影子分桶**目录**(不含文件名)。

    ```
    <data_dir>/buy_sell_signals/shadow/<signal_id>/<definition_version>/<freq_lock_version>/
    ```

    三键相同 → 路径相同(可复现); 任一个键不同 → 目录不同(跨桶不串)。
    """
    parts = (
        _require_key("signal_id", signal_id),
        _require_key("definition_version", definition_version),
        _require_key("freq_lock_version", freq_lock_version),
    )
    return _bucket_root(data_dir).joinpath(*parts)


def shadow_bucket_path(
    data_dir: Path | str | None = None,
    *,
    signal_id: str,
    definition_version: str,
    freq_lock_version: str,
) -> Path:
    """返回影子分桶 `hits.json` 的完整路径。

    桶内文件名仍叫 `hits.json`(与 live 同名) —— 这是刻意的: 两者**格式相同**,
    只是**位置不同**。同名让"用同一个反序列化器读两种文件"成为默认, 而位置不同
    让"读错文件"必须显式拼错路径才会发生。
    """
    return shadow_bucket_dir(
        data_dir,
        signal_id=signal_id,
        definition_version=definition_version,
        freq_lock_version=freq_lock_version,
    ) / HITS_FILENAME


def _live_hits_path(data_dir: Path | str | None) -> Path:
    """live `hits.json` 路径 —— **仅供比对断言用**, 本模块不往里写。

    存在的意义是让"我没有写它"这句话可测: 测例可以拿这个路径去 `exists()` /
    读内容, 证明影子写入没有顺手把 live 文件造出来。
    """
    root = Path(str(data_dir)) if data_dir is not None else Path(settings.data_dir)
    return root / PERSIST_SUBDIR / HITS_FILENAME


# ---------------------------------------------------------------- 读写


def _read_bucket(path: Path) -> list[SignalHit]:
    """读一个桶。缺失/空/坏 JSON 一律退化为空列表 + warning。

    与 `DiskHitStore.list_all` 同一种"读不出来的就跳过"策略: 一条脏数据不该让整个
    桶消失, 但也不该假装读到了。
    """
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("读取影子桶 %s 失败, 按 0 条处理: %s", path, exc)
        return []
    if not raw.strip():
        return []
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("影子桶 %s 不是合法 JSON(%s), 按 0 条处理", path, exc)
        return []
    if not isinstance(payload, dict):
        logger.warning("影子桶 %s 顶层不是对象, 按 0 条处理", path)
        return []
    items = payload.get("hits")
    if not isinstance(items, list):
        logger.warning("影子桶 %s 缺少 hits 数组, 按 0 条处理", path)
        return []

    out: list[SignalHit] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            logger.warning("影子桶 %s 第 %d 条不是对象, 跳过", path, idx)
            continue
        try:
            out.append(signal_hit_from_dict(item))
        except (HitValidationError, TypeError, ValueError) as exc:
            logger.warning("影子桶 %s 第 %d 条反序列化失败, 跳过: %s", path, idx, exc)
            continue
    return out


def _write_bucket(path: Path, hits: list[SignalHit]) -> None:
    """原子写一个桶。先写同目录 tmp, 再 `os.replace`。"""
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "namespace": NAMESPACE,
        "hits": [hit.to_dict() for hit in hits],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同目录的 tmp: `os.replace` 的原子性只在同一文件系统内成立。
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        # 写失败要清掉 tmp, 否则桶目录会堆一串半截文件 —— 而它们长得像数据。
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _require_shadow_hit(hit: SignalHit) -> None:
    """写入前的契约检查。三条, 少一条都不行。"""
    if not isinstance(hit, SignalHit):
        raise ShadowWriteError(f"只接受 SignalHit, 收到 {type(hit).__name__}")
    if hit.source.mode != SHADOW_MODE:
        # 这一条是模块的核心防线: 影子桶只装 shadow。live 命中走它自己的写口。
        raise ShadowWriteError(
            f"影子分桶只接受 source.mode={SHADOW_MODE!r}, 收到 {hit.source.mode!r}"
            " —— live 命中不属于这里"
        )
    if hit.namespace != NAMESPACE:
        raise ShadowWriteError(f"namespace 必须是 {NAMESPACE!r}, 收到 {hit.namespace!r}")


def upsert_shadow_hit(
    hit: SignalHit,
    *,
    data_dir: Path | str | None = None,
) -> Path:
    """把一条 shadow 命中写进它自己的三键分桶, 返回桶文件路径。

    策略 **append-or-replace-by-id**: 同 `hit_id` 已存在则替换那一条, 否则追加。
    重复写同一条不会让文件变长。

    硬约束:

    - `hit.source.mode` 必须 `== "shadow"`, 否则抛 `ShadowWriteError`
    - 绝不写 live `hits.json`(本函数根本不碰那个路径)
    - 原子写(tmp + `os.replace`)

    **不**做的事: 不推送、不写冷却、不改任何 `TRIGGERING`。写完这条命中仍然是
    "不可见、不可推"的 —— 闸在 `hits_visibility` / `hits_dingtalk`, 与本模块无关。
    """
    _require_shadow_hit(hit)
    if hit.freq_lock_version is None:
        # 三键里的第三键缺失时无法定位分桶。fail-closed: 拒写, 不猜一个默认口径。
        raise ShadowWriteError(
            "freq_lock_version 为空, 无法定位三键分桶 —— 缺口径标签的命中不许落盘"
        )

    path = shadow_bucket_path(
        data_dir,
        signal_id=hit.signal_id,
        definition_version=hit.definition_version,
        freq_lock_version=hit.freq_lock_version,
    )
    existing = _read_bucket(path)

    replaced = False
    merged: list[SignalHit] = []
    for item in existing:
        if item.hit_id == hit.hit_id:
            if not replaced:
                merged.append(hit)
                replaced = True
            # 同 id 的多余副本一并丢弃 —— 它们是历史脏数据, 不是"另一个版本"。
            continue
        merged.append(item)
    if not replaced:
        merged.append(hit)

    _write_bucket(path, merged)
    return path


def write_shadow_hits(
    hits: Iterable[SignalHit],
    *,
    data_dir: Path | str | None = None,
) -> dict[Path, int]:
    """批量写入, 返回 `{桶路径: 桶内总条数}`。

    按桶分组后每个桶只读一次、写一次 —— 逐条写会在 N 条上做 N 次读+写, 而每次写都是
    一次 `os.replace`; 中间态虽然不会坏, 但会慢得多。

    **任一**条不合法则整体拒写(先全部校验, 再落盘)。理由: 半批写进去之后报错, 调用方
    拿到的状态既不是"之前"也不是"之后", 而是一个没人设计过的中间态。
    """
    batch = list(hits)
    for hit in batch:
        _require_shadow_hit(hit)
        if hit.freq_lock_version is None:
            raise ShadowWriteError(
                f"hit {hit.hit_id!r} 的 freq_lock_version 为空, 无法定位三键分桶"
            )

    grouped: dict[Path, list[SignalHit]] = {}
    for hit in batch:
        path = shadow_bucket_path(
            data_dir,
            signal_id=hit.signal_id,
            definition_version=hit.definition_version,
            freq_lock_version=hit.freq_lock_version,
        )
        grouped.setdefault(path, []).append(hit)

    result: dict[Path, int] = {}
    for path, incoming in grouped.items():
        existing = _read_bucket(path)
        by_id: dict[str, SignalHit] = {item.hit_id: item for item in existing}
        order: list[str] = [item.hit_id for item in existing]
        for hit in incoming:
            if hit.hit_id not in by_id:
                order.append(hit.hit_id)
            by_id[hit.hit_id] = hit
        merged = [by_id[hid] for hid in order]
        _write_bucket(path, merged)
        result[path] = len(merged)
    return result


def list_shadow_hits(
    *,
    data_dir: Path | str | None = None,
    signal_id: str,
    definition_version: str,
    freq_lock_version: str,
) -> list[SignalHit]:
    """只读列出一个三键分桶。缺文件 → `[]`。

    **这是核验钩子, 不是 R2-3 观测口。** 它没有分页、没有过期判定、没有权限模型、
    没有路由 —— 它存在的唯一目的是让"写进去了没有"这件事在测例里可断言。

    它**不**参与生产读路径: `DiskHitStore.list_all()` 仍然只读 live `hits.json`,
    不会因为这里存在影子桶就多看它们一眼。
    """
    path = shadow_bucket_path(
        data_dir,
        signal_id=signal_id,
        definition_version=definition_version,
        freq_lock_version=freq_lock_version,
    )
    return _read_bucket(path)


def count_shadow_hits(
    *,
    data_dir: Path | str | None = None,
    signal_id: str,
    definition_version: str,
    freq_lock_version: str,
) -> int:
    """某个三键桶的条数。

    存在的意义: "样本够不够"这个问题要能问得出来。**注意**: 条数只是条数, 不是
    "样本已达标" —— 达标由 P2 评估器按 #242 锁面判, 不由这里判。
    """
    return len(
        list_shadow_hits(
            data_dir=data_dir,
            signal_id=signal_id,
            definition_version=definition_version,
            freq_lock_version=freq_lock_version,
        )
    )


__all__ = [
    "SHADOW_BUCKET_KEYS",
    "SHADOW_MODE",
    "ShadowWriteError",
    "count_shadow_hits",
    "list_shadow_hits",
    "shadow_bucket_dir",
    "shadow_bucket_path",
    "upsert_shadow_hit",
    "write_shadow_hits",
]
