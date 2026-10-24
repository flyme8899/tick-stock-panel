"""信号注册表存储: 协议 + 内存实现 + (默认不可用的)磁盘空壳。

## 默认不落盘, 而且是"要落盘得先过两道门"

本批次是骨架, 骨架跑起来的第一件事往往就是"顺手接进日管道"。一旦接进去,
它就会开始往生产 `data/` 里写一些还没验证过、字段还会大改的记录 —— 而这些
记录之后没人清理。

所以 `DiskSignalRegistryStore` 做成:

- 构造时必须显式 `enable_persist=True`, 默认 False 直接抛 `PersistDisabledError`
- 本批次 `main.py` / 日管道里**没有任何**接线
- 写盘实现留了形状(以免以后重画), 但它的存在本身不构成"可以打开"的理由

`InMemorySignalRegistryStore` 是默认实现, 单测主路径走它, 进程退出即消失。

## 与 fs_utils.partition_writer 的关系

磁盘空壳**不调用** `partition_writer`。原因: 那个函数写的是分区 parquet 并做
跨进程互斥, 而注册表是"少量记录、频繁按 id 查", 形状根本不对。硬套上去会得到
一个既慢又难查的实现, 还会在 `data/` 下多出一批无人认领的分区。将来真要做落盘,
应当先单独设计(大概率是单文件 + 版本号, 或直接进 SQLite)。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import threading
from typing import Protocol

from .models import SignalRecord, SignalValidationError


class SignalRegistryError(RuntimeError):
    """存储层错误基类。"""


class PersistDisabledError(SignalRegistryError):
    """磁盘存储未显式启用。

    构造 `DiskSignalRegistryStore` 时没传 `enable_persist=True` 就到这里。
    """


class PublishPointerConflict(SignalRegistryError):
    """同一 signal_id 下出现了第二条 publish_pointer=True。"""


class SignalRegistryStore(Protocol):
    """注册表存储协议。

    对齐 `extensions/contracts.py` 的 `Protocol` 风格: 只描述形状, 不要求继承。
    """

    def get(self, signal_id: str, version: str | None = None) -> SignalRecord | None: ...

    def upsert(self, record: SignalRecord) -> SignalRecord: ...

    def list(self, *, status: str | None = None) -> list[SignalRecord]: ...

    def set_publish_pointer(self, signal_id: str, version: str) -> None: ...


class InMemorySignalRegistryStore:
    """线程安全的内存实现。

    主键是 `(signal_id, version)` 二元组: 同一信号的不同设计版本可以并存, 这也
    是为什么 `get(signal_id)` 不传 version 时要有一个确定的挑选规则 —— 见 `get`。
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], SignalRecord] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 读

    def get(self, signal_id: str, version: str | None = None) -> SignalRecord | None:
        """按 id 取记录。

        不传 `version` 时的规则: **优先返回当前持有 publish_pointer 的那一版**,
        否则返回 `updated_at` 最新的一版。

        这条规则是为了让"这条信号现在是什么状态"这个问题有唯一答案 —— 如果只是
        随便返回第一条, 调用方拿到的可能是三版之前的历史实现, 而且不会察觉。
        """
        with self._lock:
            if version is not None:
                return self._records.get((signal_id, version))
            candidates = [rec for (sid, _), rec in self._records.items() if sid == signal_id]
        if not candidates:
            return None
        for rec in candidates:
            if rec.publish_pointer:
                return rec
        return max(candidates, key=lambda rec: rec.updated_at)

    def list(self, *, status: str | None = None) -> list[SignalRecord]:
        """列出记录, 可按状态过滤。结果按 `(signal_id, version)` 排序, 稳定可复现。"""
        with self._lock:
            values = list(self._records.values())
        if status is not None:
            values = [rec for rec in values if rec.status == status]
        return sorted(values, key=lambda rec: (rec.signal_id, rec.version))

    def versions_of(self, signal_id: str) -> list[str]:
        """列出某个 signal_id 下已有的全部版本(排序后)。"""
        with self._lock:
            return sorted(ver for (sid, ver) in self._records if sid == signal_id)

    # ------------------------------------------------------------ 写

    def upsert(self, record: SignalRecord) -> SignalRecord:
        if not isinstance(record, SignalRecord):
            raise SignalValidationError("upsert 只接受 SignalRecord")

        with self._lock:
            # 指针互斥在这里也守一次: 如果有人手工构造了一条 publish_pointer=True 的
            # 记录塞进来, 而同一 id 的另一版已经持有指针, 这里必须拒绝 —— 否则
            # "同一 signal_id 至多一条指针"这条不变量会被绕过 set_publish_pointer。
            if record.publish_pointer:
                self._assert_pointer_free(record.signal_id, keep=(record.signal_id, record.version))
            self._records[(record.signal_id, record.version)] = record
        return record

    def set_publish_pointer(self, signal_id: str, version: str) -> None:
        """把发布指针指向某版本, 并把同 id 下其他版本的指针清掉。

        指针语义: "这个信号现在对外发布的实现是哪一版"。所以它是**互斥**的,
        不是"标记这一版可以发布"(那是 `publishable` 状态的事)。
        """
        with self._lock:
            key = (signal_id, version)
            target = self._records.get(key)
            if target is None:
                raise SignalRegistryError(f"信号 {signal_id!r} 没有版本 {version!r}")

            for (sid, _ver), rec in list(self._records.items()):
                if sid != signal_id or not rec.publish_pointer:
                    continue
                if (sid, rec.version) != key:
                    self._records[(sid, rec.version)] = rec.with_fields(publish_pointer=False)
            self._records[key] = self._records[key].with_fields(publish_pointer=True)

    def clear_publish_pointer(self, signal_id: str) -> None:
        """撤掉指针(例如整条信号退役时)。"""
        with self._lock:
            for (sid, ver), rec in list(self._records.items()):
                if sid == signal_id and rec.publish_pointer:
                    self._records[(sid, ver)] = rec.with_fields(publish_pointer=False)

    def _assert_pointer_free(self, signal_id: str, *, keep: tuple[str, str]) -> None:
        for (sid, ver), rec in self._records.items():
            if sid == signal_id and rec.publish_pointer and (sid, ver) != keep:
                raise PublishPointerConflict(
                    f"signal_id={signal_id!r} 已有版本 {ver!r} 持有 publish_pointer"
                )

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class DiskSignalRegistryStore:
    """磁盘存储空壳。**本批次不接线, 默认构造即拒绝。**

    存在的意义只有一个: 让"以后要落盘"这件事有个明确的落点, 而不是让人在
    内存实现上直接加 `json.dump`。所有写方法一律 `NotImplementedError` ——
    宁可炸, 也不要出现"看起来在写、其实写坏了"的半成品。
    """

    def __init__(self, data_dir: object, *, enable_persist: bool = False) -> None:
        if not enable_persist:
            raise PersistDisabledError(
                "DiskSignalRegistryStore 需要显式 enable_persist=True 才能构造; "
                "本批次(20261024g)不接线落盘, 也没有开启它的默认路径"
            )
        # 即便显式打开了, 本批次也没有实现。留着 data_dir 只为让签名稳定。
        self._data_dir = data_dir
        raise NotImplementedError(
            "磁盘实现本批次未实现(骨架批次禁止落盘); 若确需落盘, 请先在 "
            "docs/buy-sell-signals-skeleton.md 里补设计评审"
        )

    def get(self, signal_id: str, version: str | None = None) -> SignalRecord | None:
        raise NotImplementedError

    def upsert(self, record: SignalRecord) -> SignalRecord:
        raise NotImplementedError

    def list(self, *, status: str | None = None) -> list[SignalRecord]:
        raise NotImplementedError

    def set_publish_pointer(self, signal_id: str, version: str) -> None:
        raise NotImplementedError


__all__ = [
    "DiskSignalRegistryStore",
    "InMemorySignalRegistryStore",
    "PersistDisabledError",
    "PublishPointerConflict",
    "SignalRegistryError",
    "SignalRegistryStore",
]
