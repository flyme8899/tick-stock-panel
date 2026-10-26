"""命中层存储: 只读磁盘店 + 内存实现(测例注入用)。

## 生产读路径是"只读"的, 这不是偷懒

本拍的评估器还没锁定阈值、`VOLUME_BREAKOUT_TRIGGERING_ENABLED` 仍是 `False`, 也**没有**
任何影子调度或启动扫描。这意味着: 今天生产环境上 `hits.json` 大概率不存在, 命中数为 0,
监控中心显示空态 —— 这是**正确且诚实**的状态, 不是缺陷。

所以这里只提供「读」。写方法一个都没有, 也就不存在"某段代码顺手把 live 命中写进去"的
可能性。夹具在测例里直接往 `tmp_path` 写文件, 或注入 `InMemoryHitStore`, 与生产路径
彻底分开。

## 缺文件 / 损坏 / 空数组, 三种情况的处理**不一样**

| 情况 | 处理 | 理由 |
|---|---|---|
| 文件不存在 | 0 条命中 | 全新部署就是没跑过评估器, 合法 |
| 空文件 / 空数组 | 0 条命中 | 等同于"还没写过" |
| 单条记录反序列化失败 | **跳过该条**, 其余照常返回 | 一条脏数据不该让整个 tab 打不开 |
| 顶层不是对象 / `hits` 不是数组 | 0 条命中 + warning | 这已经不是"数据有问题"而是"文件形状不对", 硬读会 500 |
| JSON 语法错误 | 0 条命中 + warning | 同上 |

注册表那边遇到损坏是**抛错**(`SignalRegistryCorruptError`), 因为它是唯一事实源、且会被
覆写, 静默清空等于丢光生产记录。命中层相反: 它是只读的、可重建的派生数据, 读不出来时
显示空态比让监控页白屏更合理。两者处理不同是有意的, 不是不一致。

不管哪种情况, 都**不会**因为文件读不出来就去读注册表凑数 —— 那会把定义当成命中。

仅供研究，不构成投资建议；执行须人工确认。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Final, Protocol

from app.config import settings

from .constants import (
    EMPTY_HITS_MESSAGE,
    HITS_FILENAME,
    HITS_SCHEMA_VERSION,
    NAMESPACE,
    PERSIST_SUBDIR,
)
from .hits_models import HitValidationError, SignalHit, signal_hit_from_dict

logger = logging.getLogger(__name__)

_SCHEMA_VERSION: Final = HITS_SCHEMA_VERSION


class HitStore(Protocol):
    """命中存储协议。**只读** —— 没有 upsert / write。

    对齐 `SignalRegistryStore` 的 Protocol 风格: 只描述形状, 不要求继承。
    """

    def list_all(self) -> list[SignalHit]: ...


class InMemoryHitStore:
    """内存实现。仅用于测例注入合成命中。

    名字里带 `InMemory` 是为了让它在任何 traceback 里都显眼 —— 生产路径上看到它,
    基本说明有人接错了线。
    """

    def __init__(self, hits: list[SignalHit] | None = None) -> None:
        self._hits: list[SignalHit] = list(hits or [])
        self._lock = threading.RLock()

    def list_all(self) -> list[SignalHit]:
        with self._lock:
            return list(self._hits)

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)


class DiskHitStore:
    """只读磁盘实现: 读 `<data_dir>/buy_sell_signals/hits.json`。

    每次 `list_all()` 都重新读文件, 不做缓存。理由: 命中文件由外部写入(将来是评估器),
    而本进程只读; 缓存会让"新写的命中要等重启才可见"。文件量级是"几百到几千条",
    每次读一次 JSON 的成本远低于让人以为系统卡住的代价。
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        root = Path(str(data_dir)) if data_dir is not None else Path(settings.data_dir)
        self._dir = root / PERSIST_SUBDIR
        self._path = self._dir / HITS_FILENAME

    @property
    def path(self) -> Path:
        """主文件路径 —— 便于日志与测试核对。没有写入后门。"""
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def list_all(self) -> list[SignalHit]:
        """读取全部命中。任何读不出来的情况都退化为空列表 + warning。"""
        if not self._path.exists():
            return []
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("读取 %s 失败, 本轮按 0 条命中处理: %s", self._path, exc)
            return []
        if not raw.strip():
            return []
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("%s 不是合法 JSON(%s), 本轮按 0 条命中处理", self._path, exc)
            return []
        if not isinstance(payload, dict):
            logger.warning("%s 顶层不是对象, 本轮按 0 条命中处理", self._path)
            return []

        items = payload.get("hits")
        if not isinstance(items, list):
            logger.warning("%s 缺少 hits 数组, 本轮按 0 条命中处理", self._path)
            return []

        out: list[SignalHit] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                logger.warning("%s 第 %d 条命中不是对象, 已跳过", self._path, idx)
                continue
            try:
                out.append(signal_hit_from_dict(item))
            except (HitValidationError, TypeError, ValueError) as exc:
                # 单条脏数据只跳过这一条: 让其余命中照常可见, 比整个 tab 打不开有用。
                logger.warning("%s 第 %d 条命中反序列化失败, 已跳过: %s", self._path, idx, exc)
                continue
        return out


def build_hit_store(data_dir: Path | str | None = None) -> HitStore:
    """生产/启动路径的**唯一**入口: 恒返回只读磁盘店。

    这里刻意**不读** `BUY_SELL_SIGNALS_PERSIST`: 那个 env 管的是「定义注册表是否落盘」,
    与命中层是两件事。把命中读取也挂在它下面, 会造出"没开 PERSIST 所以看不到命中"这种
    莫名其妙的耦合 —— 而命中看不看得见, 只该取决于文件在不在。
    """
    return DiskHitStore(data_dir)


def empty_payload() -> dict[str, Any]:
    """空态响应骨架(API 与测例共用, 避免文案在两处各写一遍)。"""
    return {
        "schema_version": _SCHEMA_VERSION,
        "namespace": NAMESPACE,
        "empty_message": EMPTY_HITS_MESSAGE,
        "hits": [],
        "count": 0,
    }


__all__ = [
    "DiskHitStore",
    "HitStore",
    "InMemoryHitStore",
    "build_hit_store",
    "empty_payload",
]
