"""校准登记存储: 磁盘店 + 内存实现（测例注入）。

仅供研究，不构成投资建议；执行须人工确认。

## 与 `hits_store` / 定义注册表的三种不同态度

三份文件遇到"读不出来"时的处理**故意不一样**，各有各的理由：

| 文件 | 坏掉时 | 理由 |
|---|---|---|
| `registry.json` | 抛 `SignalRegistryCorruptError` | 唯一事实源、会被覆写，静默清空等于丢光生产记录 |
| `hits.json` | 退化为 0 条 + warning | 只读、可重建的派生数据，白屏比空态更糟 |
| `calib.json` | 退化为 0 条 + warning | 同 hits：读不出来时"没有任何东西被担保"是**正确**的结论 |

`calib.json` 的退化方向和命中层一致，但**含义更重**：退化成空不只是"少显示几条"，
而是"没有任何信号能出现在监控里"。这个方向是对的 —— 一份读不出来的登记，不能被
当成"大概没问题"。fail-closed 在这里的具体含义就是：读不出来 → 不可见。

## 为什么提供写方法，而 `hits_store` 一个都没有

`hits_store` 只读，是因为命中的写入属于评估器（另一拍），本拍连代码路径都不该有。
校准登记不同：它是**人/回测产物**登记进来的外部事实，没有写入口就等于永远只能手改
JSON —— 而手改 JSON 恰恰是本拍要防的那件事。

所以这里有 `upsert()`，但它**只写、不产结论**：
- 它不会替你填 `sample_n` / `cost_bp_roundtrip` / `interval`；
- 越过门槛的写入一律被 `CalibRecord` 的校验硬拒（抛错，不降级）；
- 它没有任何调度、没有自动触发、没有从命中反推登记的路径。

**生产路径上没有任何代码调用 `upsert()`。** 这一条有静态测例守着。

## 缺文件 = 无登记 = 全不可见

不读 `BUY_SELL_SIGNALS_PERSIST`，理由见 `constants.CALIB_FILENAME`：那个 env 管定义
注册表落不落盘，跟校准没有任何关系。把两件事绑在一起会造出"没开 PERSIST 所以
看不到校准"这种假耦合，而真实原因只是没人写这个文件。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Final, Protocol

from app.config import settings

from .calib_models import CalibRecord, CalibValidationError, calib_record_from_dict
from .constants import (
    CALIB_FILENAME,
    CALIB_SCHEMA_VERSION,
    CALIB_STATUS_PASSED,
    NAMESPACE,
    PERSIST_SUBDIR,
)

logger = logging.getLogger(__name__)

_SCHEMA_VERSION: Final = CALIB_SCHEMA_VERSION


class CalibStore(Protocol):
    """校准登记存储协议。

    只描述形状，不要求继承 —— 与 `SignalRegistryStore` / `HitStore` 同一风格。
    """

    def list_all(self) -> list[CalibRecord]: ...


class InMemoryCalibStore:
    """内存实现。**仅用于测例注入合成登记。**

    名字里带 `InMemory` 是为了让它在任何 traceback 里都显眼 —— 生产路径上看到它，
    基本说明有人接错了线。
    """

    def __init__(self, records: list[CalibRecord] | None = None) -> None:
        self._records: list[CalibRecord] = list(records or [])
        self._lock = threading.RLock()

    def list_all(self) -> list[CalibRecord]:
        with self._lock:
            return list(self._records)

    def upsert(self, record: CalibRecord) -> None:
        """按三键覆盖。测例用；生产路径不调用。"""
        if not isinstance(record, CalibRecord):
            raise CalibValidationError("upsert 需要 CalibRecord")
        with self._lock:
            self._records = [r for r in self._records if r.align_key != record.align_key]
            self._records.append(record)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class DiskCalibStore:
    """磁盘实现: 读/写 `<data_dir>/buy_sell_signals/calib.json`。

    ## 每次读都重新读文件

    与 `DiskHitStore` 同一个理由：文件由外部写入（回测产物登记），本进程只读时缓存
    会让"新登记要等重启才生效"。量级是几十到几百条，重读成本远低于让人以为系统卡住。

    ## `upsert()` 只写文件，不写结论

    见模块 docstring。写入前 `CalibRecord` 已经过完所有门槛校验（构造时就走），
    所以这里不再重复判定 —— 重复一遍只会让两处规则慢慢跑偏。
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        root = Path(str(data_dir)) if data_dir is not None else Path(settings.data_dir)
        self._dir = root / PERSIST_SUBDIR
        self._path = self._dir / CALIB_FILENAME
        self._write_lock = threading.RLock()

    @property
    def path(self) -> Path:
        """主文件路径 —— 便于日志与测试核对。"""
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    # ================================================================ 读

    def list_all(self) -> list[CalibRecord]:
        """读取全部登记。任何读不出来的情况都退化为空列表 + warning。

        **退化 = 全不可见**：可见性闸拿不到 `passed` 就一条都不放行。这是本模块
        fail-closed 的具体形态，不是"少显示几条"。
        """
        if not self._path.exists():
            return []
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("读取 %s 失败, 本轮按无登记处理(所有命中不可见): %s", self._path, exc)
            return []
        if not raw.strip():
            return []
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "%s 不是合法 JSON(%s), 本轮按无登记处理(所有命中不可见)", self._path, exc
            )
            return []
        if not isinstance(payload, dict):
            logger.warning("%s 顶层不是对象, 本轮按无登记处理", self._path)
            return []

        items = payload.get("entries")
        if not isinstance(items, list):
            logger.warning("%s 缺少 entries 数组, 本轮按无登记处理", self._path)
            return []

        out: list[CalibRecord] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                logger.warning("%s 第 %d 条登记不是对象, 已跳过", self._path, idx)
                continue
            try:
                out.append(calib_record_from_dict(item))
            except (CalibValidationError, TypeError, ValueError) as exc:
                # 单条脏数据只跳过这一条: 它对应的信号不可见, 其余照常。
                # 注意这里**不是**"降级成 pending" —— 是整条不存在。
                logger.warning("%s 第 %d 条登记反序列化失败, 已跳过: %s", self._path, idx, exc)
                continue
        return out

    # ================================================================ 写

    def upsert(self, record: CalibRecord) -> None:
        """按三键写入/覆盖一条登记。

        **生产路径无调用方**（静态测例守）。这里存在的目的是让"登记"这件事有一个
        唯一的代码入口，而不是让运维手改 JSON —— 手改文件既过不了门槛校验，也不会
        被任何人发现。

        每次整体重写（原子替换），不做增量追加：这份文件很小，而增量写一旦中途失败
        会留下半条记录。
        """
        if not isinstance(record, CalibRecord):
            raise CalibValidationError("upsert 需要 CalibRecord")

        from app.services.fs_utils import atomic_write_text

        with self._write_lock:
            existing = self.list_all()
            merged = [r for r in existing if r.align_key != record.align_key]
            merged.append(record)
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "namespace": NAMESPACE,
                "entries": [r.to_dict() for r in merged],
            }
            self._dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self._path,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )


def build_calib_store(data_dir: Path | str | None = None) -> CalibStore:
    """生产/启动路径的**唯一**入口: 恒返回磁盘店。

    刻意**不读** `BUY_SELL_SIGNALS_PERSIST`: 那个 env 管的是「定义注册表是否落盘」，
    与校准登记是两件事。
    """
    return DiskCalibStore(data_dir)


class CalibIndex:
    """按三键索引登记，供可见性闸查表。

    只认 `passed`：`pending` / `failed` 的条目**根本不存在于索引里**。这样闸那边
    只需要"查得到/查不到"，不需要在每个调用点重复写 `status == passed` 的比较 ——
    少一处比较就少一处写错的机会。

    ## 同一三键有多条登记时取哪一条

    取 `passed_at` 最新的那条（`passed_at` 都带时区，可直接比较）；都没有 `passed_at`
    时取最后写入的那条。不合并、不平均：一条登记就是一个结论，混起来会造出一个
    从来没有存在过的结论。
    """

    def __init__(self, records: list[CalibRecord]) -> None:
        self._by_key: dict[tuple[str, str, str], CalibRecord] = {}
        for record in records:
            if record.status != CALIB_STATUS_PASSED:
                continue
            key = record.align_key
            current = self._by_key.get(key)
            if current is None or self._is_newer(record, current):
                self._by_key[key] = record
        self._source_count = len(records)

    @staticmethod
    def _is_newer(candidate: CalibRecord, current: CalibRecord) -> bool:
        left = candidate.passed_at
        right = current.passed_at
        if left is None:
            return False
        if right is None:
            return True
        return left > right

    def __len__(self) -> int:
        """**passed** 条目数, 不是文件里的总条数。"""
        return len(self._by_key)

    @property
    def source_count(self) -> int:
        """读进来的原始条数（含 pending / failed / 重复键），供诊断字段用。"""
        return self._source_count

    def get(self, signal_id: str, definition_version: str, freq_lock_version: str) -> CalibRecord | None:
        """按三键查登记。三键任一为空/不符 → `None`（fail-closed）。"""
        if not signal_id or not definition_version or not freq_lock_version:
            return None
        return self._by_key.get((signal_id, definition_version, freq_lock_version))

    def passed_keys(self) -> frozenset[tuple[str, str, str]]:
        """所有 `passed` 的三键集合（只回键，不回内容）。"""
        return frozenset(self._by_key)


def build_index(store: CalibStore | None) -> CalibIndex:
    """从 store 建索引。`store is None` → 空索引（= 全不可见）。"""
    if store is None:
        return CalibIndex([])
    try:
        return CalibIndex(store.list_all())
    except Exception as exc:  # noqa: BLE001
        # store 自己已经吞掉 OSError / JSON 错误; 走到这里说明是意料外的形状问题。
        # 退化方向仍然是"全不可见", 不是"全都放行"。
        logger.warning("建校准索引失败, 本轮按无登记处理: %s", exc)
        return CalibIndex([])


__all__ = [
    "CalibIndex",
    "CalibStore",
    "DiskCalibStore",
    "InMemoryCalibStore",
    "build_calib_store",
    "build_index",
]
