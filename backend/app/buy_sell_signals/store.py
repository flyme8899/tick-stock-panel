"""信号注册表存储: 协议 + 内存实现 + 可选落盘的磁盘实现。

## 落盘已开(可选) · 正式推送仍关

批次 20261025gp 把磁盘实现从空壳换成真落盘, 但**仍然是关的**, 只是多了一个
显式的开启方式:

- `DiskSignalRegistryStore` 构造时必须显式 `enable_persist=True`, 默认 False 直接抛
  `PersistDisabledError`;
- 生产/启动路径只有 `build_signal_registry_store()` 一个入口, 它读
  `BUY_SELL_SIGNALS_PERSIST`; 未设或假 → `InMemorySignalRegistryStore`, **不落盘**;
- 生产是否置 `BUY_SELL_SIGNALS_PERSIST=1` 另等部署口令, 不得默认开。

**落盘 ≠ 推送**。写进 `registry.json` 的记录不会因此进入推送链路: 没有 SSE、
没有飞书、没有 `news.push` 接线, 状态机也没有任何通往 `publishable` 的自动边。

`InMemorySignalRegistryStore` 仍是默认实现, 单测主路径走它, 进程退出即消失。

## 磁盘实现的形状

- 根目录由调用方传入(工厂传 `settings.data_dir / "buy_sell_signals"`), 主文件写死
  `registry.json`;
- 写入经 `fs_utils.atomic_write_text`(临时文件 + `os.replace`), 读侧不会读到半截 JSON;
- payload 先过 `json_safe(..., coerce=True)`(datetime → isoformat, 非有限浮点 → null)
  再 `json.dumps(..., allow_nan=False)`;
- 启动 reload 遇到损坏 JSON **显式抛错**, 绝不静默清空 —— 清空等于把生产记录丢光,
  而且下一次写入会把坏文件覆盖掉, 事后无从追查;
- 指针互斥与内存实现**同语义**: 同一 `signal_id` 至多一条 `publish_pointer=True`。

## 与 fs_utils.partition_writer 的关系

磁盘实现**不调用** `partition_writer`。那个函数写的是分区 parquet 并做跨进程互斥,
而注册表是"少量记录、频繁按 id 查", 形状根本不对。硬套上去会得到一个既慢又难查的
实现, 还会在 `data/` 下多出一批无人认领的分区。这里要的就是"单文件 + 全量读写"。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Final, Protocol

from app.config import settings
from app.services.fs_utils import atomic_write_text
from app.services.json_safe import json_safe

from .constants import (
    NAMESPACE,
    PERSIST_ENV_NAME,
    PERSIST_SUBDIR,
    REGISTRY_FILENAME,
    REGISTRY_SCHEMA_VERSION,
)
from .models import SignalRecord, SignalValidationError

logger = logging.getLogger(__name__)

_SCHEMA_VERSION: Final = REGISTRY_SCHEMA_VERSION


class SignalRegistryError(RuntimeError):
    """存储层错误基类。"""


class PersistDisabledError(SignalRegistryError):
    """磁盘存储未显式启用。

    构造 `DiskSignalRegistryStore` 时没传 `enable_persist=True` 就到这里。
    """


class SignalRegistryCorruptError(SignalRegistryError):
    """磁盘上的注册表文件无法解析。

    出现这个错误时**不要**自动重建空表: 那会把已有记录连同坏文件一起丢掉。人工
    看一眼 `registry.json`, 确认是截断/手改/编码问题之后再决定恢复或清理。
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
    """落盘实现: 单文件 JSON + 原子写 + 启动 reload。

    构造必须显式 `enable_persist=True`; 否则抛 `PersistDisabledError`。
    打开后行为与 `InMemorySignalRegistryStore` 一致 —— 包括指针互斥和
    `get` 不传 version 时的挑选规则, 这两条不是"内存实现的细节", 是注册表的语义,
    换个存储后端也必须保持。

    每次写操作都**全量**重写 `registry.json`。注册表记录量级是"几十到几百条",
    全量写换来的是"文件内容永远等于内存状态", 不会出现增量追加写坏一行导致整个
    文件不可解析的情况。
    """

    def __init__(self, data_dir: object, *, enable_persist: bool = False) -> None:
        if not enable_persist:
            raise PersistDisabledError(
                "DiskSignalRegistryStore 需要显式 enable_persist=True 才能构造; "
                f"生产路径请用 build_signal_registry_store() 并经 {PERSIST_ENV_NAME}=1 开启"
            )
        self._data_dir = Path(str(data_dir))
        self._path = self._data_dir / REGISTRY_FILENAME
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str], SignalRecord] = {}
        self._load()

    # ------------------------------------------------------------ 磁盘 IO

    @property
    def path(self) -> Path:
        """主文件路径 —— 便于日志与测试核对, 不提供写入后门。"""
        return self._path

    def _load(self) -> None:
        """启动时从磁盘恢复。文件不存在 / 空目录 → 空注册表(合法)。"""
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SignalRegistryError(f"读取 {self._path} 失败: {exc}") from exc
        if not raw.strip():
            # 空文件等同于"还没写过": 不是损坏, 但也不是有效内容
            return
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SignalRegistryCorruptError(
                f"{self._path} 不是合法 JSON({exc}); 不会自动清空, 请人工处理"
            ) from exc
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise SignalRegistryCorruptError(
                f"{self._path} 缺少 records 列表; 不会自动清空, 请人工处理"
            )

        loaded: dict[tuple[str, str], SignalRecord] = {}
        for item in records:
            if not isinstance(item, dict):
                raise SignalRegistryCorruptError(f"{self._path} 的 records 里出现非对象条目")
            try:
                record = SignalRecord.from_dict(item)
            except (SignalValidationError, ValueError, TypeError) as exc:
                raise SignalRegistryCorruptError(
                    f"{self._path} 中 signal_id={item.get('signal_id')!r} "
                    f"version={item.get('version')!r} 反序列化失败: {exc}"
                ) from exc
            loaded[(record.signal_id, record.version)] = record

        # 磁盘上若存在同一 signal_id 多条指针, 这是被外部改过的文件 —— 报错而不是
        # 猜哪一条是对的。
        pointers: dict[str, int] = {}
        for (sid, _ver), rec in loaded.items():
            if rec.publish_pointer:
                pointers[sid] = pointers.get(sid, 0) + 1
        for sid, count in pointers.items():
            if count > 1:
                raise SignalRegistryCorruptError(
                    f"{self._path} 中 signal_id={sid!r} 有 {count} 条 publish_pointer=True"
                )
        self._records = loaded

    def _flush(self) -> None:
        """把内存状态整体落盘。调用方必须已持有 `self._lock`。"""
        payload = {
            "namespace": NAMESPACE,
            "schema_version": _SCHEMA_VERSION,
            "records": [json_safe(rec.to_dict(), coerce=True) for rec in self._sorted_records()],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=False)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path, text)

    def _sorted_records(self) -> list[SignalRecord]:
        return sorted(self._records.values(), key=lambda rec: (rec.signal_id, rec.version))

    # ------------------------------------------------------------ 读

    def get(self, signal_id: str, version: str | None = None) -> SignalRecord | None:
        """按 id 取记录。规则与内存实现一致: 优先指针版, 否则 `updated_at` 最新。"""
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
        with self._lock:
            values = list(self._records.values())
        if status is not None:
            values = [rec for rec in values if rec.status == status]
        return sorted(values, key=lambda rec: (rec.signal_id, rec.version))

    def versions_of(self, signal_id: str) -> list[str]:
        with self._lock:
            return sorted(ver for (sid, ver) in self._records if sid == signal_id)

    # ------------------------------------------------------------ 写

    def upsert(self, record: SignalRecord) -> SignalRecord:
        if not isinstance(record, SignalRecord):
            raise SignalValidationError("upsert 只接受 SignalRecord")

        with self._lock:
            if record.publish_pointer:
                self._assert_pointer_free(record.signal_id, keep=(record.signal_id, record.version))
            self._records[(record.signal_id, record.version)] = record
            self._flush()
        return record

    def set_publish_pointer(self, signal_id: str, version: str) -> None:
        """把发布指针指向某版本。**这是注册表内部的指针, 不触发任何推送。**"""
        with self._lock:
            key = (signal_id, version)
            if key not in self._records:
                raise SignalRegistryError(f"信号 {signal_id!r} 没有版本 {version!r}")

            for (sid, _ver), rec in list(self._records.items()):
                if sid != signal_id or not rec.publish_pointer:
                    continue
                if (sid, rec.version) != key:
                    self._records[(sid, rec.version)] = rec.with_fields(publish_pointer=False)
            self._records[key] = self._records[key].with_fields(publish_pointer=True)
            self._flush()

    def clear_publish_pointer(self, signal_id: str) -> None:
        with self._lock:
            changed = False
            for (sid, ver), rec in list(self._records.items()):
                if sid == signal_id and rec.publish_pointer:
                    self._records[(sid, ver)] = rec.with_fields(publish_pointer=False)
                    changed = True
            if changed:
                self._flush()

    def _assert_pointer_free(self, signal_id: str, *, keep: tuple[str, str]) -> None:
        for (sid, ver), rec in self._records.items():
            if sid == signal_id and rec.publish_pointer and (sid, ver) != keep:
                raise PublishPointerConflict(
                    f"signal_id={signal_id!r} 已有版本 {ver!r} 持有 publish_pointer"
                )

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


# ---------------------------------------------------------------------------
# env 门 + 工厂
# ---------------------------------------------------------------------------

_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


def persist_env_enabled() -> bool:
    """`BUY_SELL_SIGNALS_PERSIST` 是否为真。

    真值写法对齐仓库既有风格(`1/true/yes/on`, 大小写与空白不敏感)。**只认这一个名字**,
    不要再引入第二个开关: 两个开关意味着"到底开没开"取决于谁后读。
    """
    return os.environ.get(PERSIST_ENV_NAME, "").strip().lower() in _TRUTHY


def build_signal_registry_store(data_dir: Path | str | None = None) -> SignalRegistryStore:
    """按 env 造 Store: **未设/假 → InMemory(不落盘)**; 真 → Disk。

    这是唯一的开启入口。调用方不需要知道磁盘实现的构造细节, 也就不容易
    "顺手 new 一个 Disk 并传 True"。
    """
    if not persist_env_enabled():
        return InMemorySignalRegistryStore()
    root = Path(data_dir) if data_dir is not None else Path(settings.data_dir)
    return DiskSignalRegistryStore(root / PERSIST_SUBDIR, enable_persist=True)


__all__ = [
    "DiskSignalRegistryStore",
    "InMemorySignalRegistryStore",
    "PersistDisabledError",
    "PublishPointerConflict",
    "SignalRegistryCorruptError",
    "SignalRegistryError",
    "SignalRegistryStore",
    "build_signal_registry_store",
    "persist_env_enabled",
]
